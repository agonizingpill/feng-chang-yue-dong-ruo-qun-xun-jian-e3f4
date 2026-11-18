"""后端行为测试：离线补传、晚到保留、冲突裁决、处置期间暂缓与并发。

运行：cd backend && python -m pytest tests -q
使用临时 SQLite，不依赖 Docker/PostgreSQL；同时在 PostgreSQL 上以 FOR UPDATE 串行化。
"""
import os,tempfile
from datetime import datetime,timedelta
from pathlib import Path

tmp=tempfile.mkdtemp(prefix='apiary-test-')
os.environ.setdefault('DATABASE_URL','sqlite+pysqlite:///'+str(Path(tmp)/'test.db'))
os.environ['SEED_DEMO']='false'

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.database import Base,engine,Session
from src.models import Hive,Visit,VisitConflict,VisitBatch,CareAction
from src.auth import hash_password
from src.models import User
from src import app as app_module


class MemoryQueue:
    '''测试替身：不依赖 Redis，保留筛查入队与结果读取接口。'''
    def __init__(self):self.store={};self.jobs=[]
    def ping(self):return True
    def set(self,key,value,ex=None):self.store[key]=value
    def get(self,key):return self.store.get(key)
    def rpush(self,key,value):self.jobs.append(value)


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    with Session() as db:
        for role in ['admin','beekeeper','viewer']:
            db.add(User(email=role+'@shanjing.test',password=hash_password('Hive2026!'),role=role))
        db.add(Hive(code='H-100',site='山荆东坡',queen_present=True,colony_frames=6,care_state='observe'))
        db.add(Hive(code='H-101',site='山荆林缘',queen_present=True,colony_frames=6,care_state='observe'))
        db.commit()
    monkeypatch.setattr(app_module,'queue',MemoryQueue())
    with TestClient(app_module.app) as c:
        yield c


def token(client,role='beekeeper'):
    r=client.post('/auth/login',json={'email':role+'@shanjing.test','password':'Hive2026!'})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}


def record(code,hive_code='H-100',when=None,weight=22.0,queen=True,frames=6,record_id=None):
    return {'client_record_id':record_id or 'r-'+code,'code':code,'hive_code':hive_code,
            'collected_at':(when or datetime(2026,11,17,8,30)).isoformat(),
            'weight_kg':weight,'temperature':31.0,'activity':40.0,'feed_left':1.2,
            'queen_seen':queen,'colony_frames':frames}


def sync(client,headers,batch_id,records):
    return client.post('/visits/sync-batches',headers=headers,json={'client_batch_id':batch_id,'records':records})


def only_visit(client,headers,code):
    rows=client.get('/visits',headers=headers).json()
    return next(r for r in rows if r['code']==code)


def test_newer_collection_becomes_current_even_when_uploaded_out_of_order(client):
    h=token(client)
    first=record('V-A',when=datetime(2026,11,17,10,0),weight=24.0)
    r=sync(client,h,'B-1',[first]);assert r.status_code==200,r.text
    assert r.json()['summary']['accepted']==1
    # 晚到：采集时间更早，但后入库
    late=record('V-B',when=datetime(2026,11,17,9,0),weight=23.0,queen=False,frames=4)
    r=sync(client,h,'B-2',[late]);assert r.status_code==200
    assert r.json()['summary']['late']==1
    assert only_visit(client,h,'V-B')['intake_state']=='late'
    assert only_visit(client,h,'V-A')['intake_state']=='current'
    hive=client.get('/hives',headers=h).json()[0]
    assert hive['queen_present'] is True and hive['colony_frames']==6


def test_newer_late_arrival_supersedes_and_lowers_current(client):
    h=token(client)
    sync(client,h,'B-1',[record('V-A',when=datetime(2026,11,17,8,0),weight=22.0)])
    r=sync(client,h,'B-2',[record('V-B',when=datetime(2026,11,17,12,0),weight=25.0,queen=False,frames=4)])
    assert r.json()['summary']['accepted']==1
    assert only_visit(client,h,'V-A')['intake_state']=='superseded'
    assert only_visit(client,h,'V-B')['intake_state']=='current'
    hive=client.get('/hives',headers=h).json()[0]
    assert hive['queen_present'] is False and hive['colony_frames']==4
    # 晚到旧记录仍在，可回查
    assert only_visit(client,h,'V-A')['weight_kg']==22.0


def test_retry_same_batch_is_idempotent_and_reports_per_record(client):
    h=token(client)
    records=[record('V-A',hive_code='H-100'),record('V-B',hive_code='H-101',weight=21.0)]
    first=sync(client,h,'B-1',records);assert first.status_code==200
    count=len(client.get('/visits',headers=h).json())
    again=sync(client,h,'B-1',records)
    assert again.status_code==200 and again.json()['duplicate'] is True
    assert again.json()['results']==first.json()['results']
    assert len(client.get('/visits',headers=h).json())==count
    # 换批次号重传同一批：逐条识别为重复，不重复入库
    other=sync(client,h,'B-2',records).json()
    assert other['summary']['duplicate']==2
    assert len(client.get('/visits',headers=h).json())==count


def test_same_batch_id_different_content_rejected(client):
    h=token(client)
    sync(client,h,'B-1',[record('V-A')])
    r=sync(client,h,'B-1',[record('V-A',weight=30.0)])
    assert r.status_code==409


def test_same_moment_different_reading_isolated_and_pauses_hive(client):
    a=token(client);adm=token(client,'admin')
    r=sync(client,a,'B-1',[record('V-A',when=datetime(2026,11,17,9,0),weight=22.0)])
    assert r.json()['summary']['accepted']==1
    r=sync(client,a,'B-2',[record('V-B',when=datetime(2026,11,17,9,0),weight=26.0,queen=False,frames=4)])
    body=r.json();assert body['summary']['conflict']==1,body
    conflict_id=body['results'][0]['conflict_id']
    # 原当前读数被隔离，箱况保持裁决前不变
    assert only_visit(client,a,'V-A')['intake_state']=='conflict'
    assert only_visit(client,a,'V-B')['intake_state']=='conflict'
    hive=client.get('/hives',headers=a).json()[0]
    assert hive['queen_present'] is True
    # 未裁决：暂停该箱筛查与建处置
    assert client.post('/screening',headers=a).status_code==409
    assert client.post('/visits',headers=a,json={'code':'V-X','hive_id':1,'weight_kg':22,'temperature':31,'activity':40,'feed_left':1,'queen_seen':True,'colony_frames':6}).status_code==409
    # 其他箱照常
    other=sync(client,a,'B-3',[record('V-C',hive_code='H-101')])
    assert other.json()['summary']['accepted']==1
    # 主管裁决
    c=client.get('/visit-conflicts',headers=a).json()[0]
    assert c['id']==conflict_id and c['state']=='open'
    chosen=only_visit(client,a,'V-B')['id']
    r=client.post(f'/visit-conflicts/{conflict_id}/resolve',headers=adm,json={'visit_id':chosen,'note':'以V-B现场复读为准'})
    assert r.status_code==200,r.text
    assert r.json()['outcome']=='accepted'
    assert only_visit(client,a,'V-B')['intake_state']=='current'
    assert only_visit(client,a,'V-A')['intake_state']=='conflict_lost'
    # 裁决后该箱可建处置
    assert client.get('/visit-conflicts',headers=a).json()[0]['state']=='resolved'
    assert client.post('/screening',headers=a).status_code==200


def test_only_supervisor_resolves_and_repeat_rejected(client):
    a=token(client);b=token(client,'beekeeper')
    sync(client,a,'B-1',[record('V-A',when=datetime(2026,11,17,9,0),weight=22.0)])
    sync(client,a,'B-2',[record('V-B',when=datetime(2026,11,17,9,0),weight=26.0)])
    cid=client.get('/visit-conflicts',headers=a).json()[0]['id']
    chosen=only_visit(client,a,'V-A')['id']
    assert client.post(f'/visit-conflicts/{cid}/resolve',headers=b,json={'visit_id':chosen,'note':'蜂师尝试裁决'}).status_code==403
    adm=token(client,'admin')
    assert client.post(f'/visit-conflicts/{cid}/resolve',headers=adm,json={'visit_id':chosen,'note':'以原读数为准'}).status_code==200
    assert client.post(f'/visit-conflicts/{cid}/resolve',headers=adm,json={'visit_id':chosen,'note':'重复裁决'}).status_code==409


def test_identical_retry_at_same_moment_is_not_a_conflict(client):
    a=token(client)
    sync(client,a,'B-1',[record('V-A',when=datetime(2026,11,17,9,0),weight=22.0)])
    r=sync(client,a,'B-2',[record('V-B',when=datetime(2026,11,17,9,0),weight=22.0)])
    assert r.json()['summary']['duplicate']==1
    assert client.get('/visit-conflicts',headers=a).json()==[]

def test_backfill_during_care_is_deferred_until_close(client):
    a=token(client);adm=token(client,'admin')
    sync(client,a,'B-1',[record('V-A',when=datetime(2026,11,17,8,0),weight=22.0)])
    job=client.post('/screening',headers=a).json()
    assert job['count']>=1
    # 测试不启动 worker 时，手工把 Visit 置为已筛查以便建处置
    with Session() as db:
        v=db.scalar(select(Visit).where(Visit.code=='V-A'));v.status='screened';db.commit()
    plan=client.post('/care',headers=a,json={'visit_id':only_visit(client,a,'V-A')['id'],'kind':'feed','feed_kg':1.5})
    assert plan.status_code==200,plan.text
    care_id=plan.json()['id']
    lot=client.post('/feed-lots',headers=a,json={'code':'FD-TEST','received_kg':10,'expires_on':'2027-11-30'})
    assert lot.status_code==200,lot.text
    assert client.post('/feed-issues',headers=a,json={'care_id':care_id,'lot_id':lot.json()['id']}).status_code==200
    # 执行中补传：暂缓，不改依据、不进筛查
    r=sync(client,a,'B-2',[record('V-B',when=datetime(2026,11,18,9,0),weight=20.0,queen=False,frames=3)])
    assert r.json()['summary']['deferred']==1
    assert only_visit(client,a,'V-B')['intake_state']=='deferred'
    hive=client.get('/hives',headers=a).json()[0]
    assert hive['colony_frames']==6
    # 已建处置保留原依据
    care=next(c for c in client.get('/care',headers=a).json() if c['id']==care_id)
    assert care['visit_id']==only_visit(client,a,'V-A')['id']
    # 闭环后补传读数才参与筛查
    assert client.post(f'/care/{care_id}/execute',headers=a,json={'queen_installed':False}).status_code==200
    assert client.post(f'/care/{care_id}/followup',headers=a,json={'followup_weight':24.0,'followup_queen':True,'followup_frames':6}).status_code==200
    assert client.post(f'/care/{care_id}/close',headers=adm).status_code==200
    assert only_visit(client,a,'V-B')['intake_state']=='current'
    assert client.get('/hives',headers=a).json()[0]['colony_frames']==3


def test_old_visit_without_collection_time_remains_usable(client):
    a=token(client)
    with Session() as db:
        hive=db.scalar(select(Hive).where(Hive.code=='H-100'))
        db.add(Visit(code='V-OLD',hive_id=hive.id,weight_kg=21.0,temperature=30.0,activity=35.0,feed_left=1.0,queen_seen=True,colony_frames=6,status='recorded',intake_state='accepted'))
        db.commit()
    # 未在采集时刻之前的补传仍受旧手记约束
    r=sync(client,a,'B-1',[record('V-NEW',when=datetime(2020,1,1,8,0),weight=25.0)])
    assert r.json()['summary']['late']==1
    # 旧手记仍可作为建处置依据（经筛查）
    with Session() as db:
        v=db.scalar(select(Visit).where(Visit.code=='V-OLD'));v.status='screened';db.commit()
    r=client.post('/care',headers=a,json={'visit_id':only_visit(client,a,'V-OLD')['id'],'kind':'feed','feed_kg':1.0})
    assert r.status_code==200,r.text


def test_trace_returns_batch_and_conflict_for_each_record(client):
    a=token(client);adm=token(client,'admin')
    sync(client,a,'B-1',[record('V-A',when=datetime(2026,11,17,9,0),weight=22.0)])
    r=sync(client,a,'B-2',[record('V-B',when=datetime(2026,11,17,9,0),weight=26.0)])
    vid=only_visit(client,a,'V-B')['id']
    t=client.get(f'/visits/{vid}/trace',headers=a).json()
    assert t['batch']['batch_id']=='B-2'
    assert t['record']['outcome']=='conflict'
    assert t['conflict']['state']=='open'
    s=only_visit(client,a,'V-A')['id']
    assert client.get(f'/visits/{s}/trace',headers=a).json()['batch']['batch_id']=='B-1'
    # 裁决后回查去向
    cid=t['conflict']['id']
    client.post(f'/visit-conflicts/{cid}/resolve',headers=adm,json={'visit_id':vid,'note':'以复读为准'})
    t2=client.get(f'/visits/{vid}/trace',headers=a).json()
    assert t2['conflict']['state']=='resolved' and t2['conflict']['decided_by']=='admin@shanjing.test'


def test_batch_input_validation(client):
    a=token(client)
    r=client.post('/visits/sync-batches',headers=a,json={'client_batch_id':'B-1','records':[]})
    assert r.status_code==422
    dup=[record('V-A'),record('V-A',record_id='other')]
    assert sync(client,a,'B-1',dup).status_code==422
    r=client.post('/visits/sync-batches',headers=a,json={'client_batch_id':'B-1','records':[{**record('V-A'),'collected_at':'not-a-time'}]})
    assert r.status_code==422
