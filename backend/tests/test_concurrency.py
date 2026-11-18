"""并发场景：同批/异批同时补传、补传与建处置同时发生。

SQLite 用文件库并开启 WAL，线程并发走真实事务；PostgreSQL 下 FOR UPDATE 串行化更强。
"""
import os,threading,tempfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker

from src.database import Base,Session
from src.models import Hive,Visit,VisitConflict,User
from src.auth import hash_password
from src import app as app_module


@pytest.fixture()
def client(tmp_path,monkeypatch):
    url='sqlite+pysqlite:///'+str(tmp_path/'concurrent.db')
    engine=create_engine(url,connect_args={'check_same_thread':False,'timeout':30})
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA journal_mode=WAL')
    Base.metadata.create_all(engine)
    ThreadSession=sessionmaker(engine,expire_on_commit=False)
    with ThreadSession() as db:
        for role in ['admin','beekeeper']:
            db.add(User(email=role+'@shanjing.test',password=hash_password('Hive2026!'),role=role))
        for i in range(3):
            db.add(Hive(code=f'H-{200+i}',site='山荆东坡',queen_present=True,colony_frames=6,care_state='observe'))
        db.commit()
    calls={'n':0};lock=threading.Lock()

    def session_override():
        with ThreadSession() as db:yield db

    app_module.app.dependency_overrides[app_module.session]=session_override
    class Q:
        def ping(self):return True
        def set(self,*a,**k):pass
        def get(self,k):return None
        def rpush(self,*a,**k):pass
    monkeypatch.setattr(app_module,'queue',Q())
    with TestClient(app_module.app) as c:
        c.ThreadSession=ThreadSession
        c.engine=engine
        yield c
    app_module.app.dependency_overrides.clear()


def headers(client,role='beekeeper'):
    r=client.post('/auth/login',json={'email':role+'@shanjing.test','password':'Hive2026!'})
    return {'Authorization':'Bearer '+r.json()['token']}


def rec(code,hive,when,weight=22.0):
    return {'client_record_id':code,'code':code,'hive_code':hive,'collected_at':when.isoformat(),
            'weight_kg':weight,'temperature':31.0,'activity':40.0,'feed_left':1.0,'queen_seen':True,'colony_frames':6}


def run_parallel(calls):
    results=[None]*len(calls);errors=[]
    def worker(i,fn):
        try:results[i]=fn()
        except Exception as error:errors.append(error)
    threads=[threading.Thread(target=worker,args=(i,fn)) for i,fn in enumerate(calls)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert not errors,errors
    return results


def test_concurrent_identical_batches_ingest_once(client):
    h=headers(client)
    payload={'client_batch_id':'SAME','records':[rec('V-1','H-200',datetime(2026,11,17,9,0))]}
    results=run_parallel([lambda:client.post('/visits/sync-batches',headers=h,json=payload) for _ in range(6)])
    assert all(r.status_code==200 for r in results),[r.text for r in results]
    assert sum(1 for r in results if r.json()['duplicate'] is False)==1
    assert sum(1 for r in results if r.json()['duplicate'] is True)==5
    with client.ThreadSession() as db:
        assert db.scalar(select(__import__('sqlalchemy').func.count()).select_from(Visit))==1


def test_concurrent_distinct_batches_newest_wins(client):
    h=headers(client)
    stamps=[datetime(2026,11,17,8+i,0) for i in range(5)]
    results=run_parallel([lambda i=i,when=when:client.post('/visits/sync-batches',headers=h,json={'client_batch_id':f'B{i}','records':[rec(f'V-{i}','H-201',when,weight=20+i)]}) for i,when in enumerate(stamps)])
    assert all(r.status_code==200 for r in results),[r.text for r in results]
    with client.ThreadSession() as db:
        visits=db.scalars(select(Visit).where(Visit.hive_id==2).order_by(Visit.id)).all()
        assert len(visits)==5
        current=[v for v in visits if v.intake_state=='current']
        assert len(current)==1,current
        assert current[0].collected_at==stamps[-1]
        hive=db.get(Hive,2)
        assert hive.colony_frames==6


def test_concurrent_same_moment_conflict_yields_single_open_conflict(client):
    h=headers(client)
    calls=[lambda i=i:client.post('/visits/sync-batches',headers=h,json={'client_batch_id':f'C{i}','records':[rec(f'W-{i}','H-202',datetime(2026,11,17,9,0),weight=21+i)]}) for i in range(4)]
    results=run_parallel(calls)
    assert all(r.status_code==200 for r in results),[r.text for r in results]
    with client.ThreadSession() as db:
        conflicts=db.scalars(select(VisitConflict).where(VisitConflict.state=='open')).all()
        assert len(conflicts)==1
        isolated=db.scalars(select(Visit).where(Visit.hive_id==3)).all()
        assert len(isolated)==4
        assert all(v.intake_state=='conflict' for v in isolated)


def test_backfill_and_care_plan_race_is_serialized(client):
    h=headers(client);adm=headers(client,'admin')
    client.post('/visits/sync-batches',headers=h,json={'client_batch_id':'BASE','records':[rec('V-BASE','H-200',datetime(2026,11,17,7,0))]})
    with client.ThreadSession() as db:
        v=db.scalar(select(Visit).where(Visit.code=='V-BASE'));v.status='screened';db.commit();base=v.id
    def backfill():return client.post('/visits/sync-batches',headers=h,json={'client_batch_id':'NEW','records':[rec('V-NEW','H-200',datetime(2026,11,17,10,0),weight=26.0)]})
    def plan():return client.post('/care',headers=h,json={'visit_id':base,'kind':'requeen','feed_kg':0})
    sync_r,plan_r=run_parallel([backfill,plan])
    assert sync_r.status_code==200,sync_r.text
    outcome=sync_r.json()['results'][0]['outcome']
    with client.ThreadSession() as db:
        visits=db.scalars(select(Visit).where(Visit.hive_id==1)).all()
        current=[v for v in visits if v.intake_state=='current']
        assert len(current)==1
        if plan_r.status_code==200:
            # 建处置成功则补传必须暂缓，不能悄悄替换处置依据
            assert outcome=='deferred',outcome
            assert current[0].code=='V-BASE'
        else:
            assert plan_r.status_code==409
            assert outcome=='accepted',outcome
            assert current[0].code=='V-NEW'
