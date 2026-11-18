"""补传/冲突规则补充：旧手记可用、闭环后接纳、卡片式种子数据口径。"""
import os,tempfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ.setdefault('DATABASE_URL','sqlite+pysqlite:///'+str(Path(tempfile.mkdtemp(prefix='apiary-sup-'))/'t.db'))
os.environ['SEED_DEMO']='false'

from src.database import Base,engine,Session
from src.models import Hive,Visit,User,VisitConflict,VisitBatch,CareAction
from src.auth import hash_password
from src import app as app_module
from src.visit_time import effective


class Q:
    def ping(self):return True
    def set(self,*a,**k):pass
    def get(self,k):return None
    def rpush(self,*a,**k):pass


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    with Session() as db:
        for role in ['admin','beekeeper','viewer']:
            db.add(User(email=role+'@shanjing.test',password=hash_password('Hive2026!'),role=role))
        db.add(Hive(code='H-300',site='山荆东坡',queen_present=True,colony_frames=6,care_state='winter_ready'))
        db.add(Hive(code='H-301',site='山荆林缘',queen_present=True,colony_frames=6,care_state='winter_ready'))
        db.commit()
    monkeypatch.setattr(app_module,'queue',Q())
    with TestClient(app_module.app) as c:yield c


def headers(client,role='beekeeper'):
    return {'Authorization':'Bearer '+client.post('/auth/login',json={'email':role+'@shanjing.test','password':'Hive2026!'}).json()['token']}


def rec(code,hive='H-300',when='2026-11-17T09:00:00',weight=22.0,queen=True,frames=6):
    return {'client_record_id':code,'code':code,'hive_code':hive,'collected_at':when,'weight_kg':weight,'temperature':31.0,'activity':40.0,'feed_left':1.0,'queen_seen':queen,'colony_frames':frames}


def sync(client,headers,batch,records):
    return client.post('/visits/sync-batches',headers=headers,json={'client_batch_id':batch,'records':records})


def visit(client,headers,code):
    return next(v for v in client.get('/visits',headers=headers).json() if v['code']==code)


def test_close_promotes_deferred_without_resetting_winter_ready(client):
    h=headers(client);adm=headers(client,'admin')
    sync(client,h,'B0',[rec('V-BASE',when='2026-11-17T08:00:00')])
    with Session() as db:
        v=db.scalar(select(Visit).where(Visit.code=='V-BASE'));v.status='screened';db.commit();base=v.id
    plan=client.post('/care',headers=h,json={'visit_id':base,'kind':'feed','feed_kg':1.0})
    assert plan.status_code==200,plan.text
    care=plan.json()['id']
    lot=client.post('/feed-lots',headers=h,json={'code':'FD-X','received_kg':5,'expires_on':'2027-11-30'}).json()
    assert client.post('/feed-issues',headers=h,json={'care_id':care,'lot_id':lot['id']}).status_code==200
    r=sync(client,h,'B1',[rec('V-DURING',when='2026-11-18T09:00:00',queen=False,frames=4)])
    assert r.json()['summary']['deferred']==1
    assert client.post(f'/care/{care}/execute',headers=h,json={'queen_installed':False}).status_code==200
    assert client.post(f'/care/{care}/followup',headers=h,json={'followup_weight':25,'followup_queen':True,'followup_frames':8}).status_code==200
    assert client.post(f'/care/{care}/close',headers=adm).status_code==200
    hive=client.get('/hives',headers=h).json()[0]
    assert visit(client,h,'V-DURING')['intake_state']=='current'
    assert hive['colony_frames']==4 and hive['queen_present'] is False
    # 闭环接纳不直接推翻越冬状态，仍交给后续筛查判断
    assert hive['care_state']=='winter_ready'
    assert visit(client,h,'V-DURING')['status']=='recorded'


def test_deferred_conflict_resolved_after_close_creates_quarantine(client):
    h=headers(client);adm=headers(client,'admin')
    sync(client,h,'B0',[rec('V-BASE',when='2026-11-17T08:00:00')])
    with Session() as db:
        v=db.scalar(select(Visit).where(Visit.code=='V-BASE'));v.status='screened';db.commit();base=v.id
    plan=client.post('/care',headers=h,json={'visit_id':base,'kind':'requeen','feed_kg':0}).json()
    # 两条同采集时刻、读数不同，都在处置期间补传
    first=sync(client,h,'B1',[rec('V-D1',when='2026-11-18T09:00:00',weight=21.0)])
    second=sync(client,h,'B2',[rec('V-D2',when='2026-11-18T09:00:00',weight=27.0)])
    assert first.json()['summary']['deferred']==1
    assert second.json()['summary']['deferred']==1
    # 执行换王 + 观察 + 复查 + 关闭前置条件
    assert client.post(f"/care/{plan['id']}/execute",headers=h,json={'queen_installed':True}).status_code==200
    obs=client.post('/queen-observations',headers=h,json={'care_id':plan['id'],'queen_code':'QN-T1','installed_on':datetime.utcnow().date().isoformat(),'eggs_seen':True,'accepted':True,'note':'王笼已咬开，巢脾见新卵'})
    assert obs.status_code==200,obs.text
    assert client.post(f"/queen-observations/{obs.json()['id']}/confirm",headers=adm).status_code==200
    assert client.post(f"/care/{plan['id']}/followup",headers=h,json={'followup_weight':26,'followup_queen':True,'followup_frames':7}).status_code==200
    r=client.post(f"/care/{plan['id']}/close",headers=adm)
    assert r.status_code==200,r.text
    # 闭环后同刻不同读数被隔离成冲突
    assert client.get('/visit-conflicts',headers=h).json(), 'expected a conflict after promotion'
    states={v['code']:v['intake_state'] for v in client.get('/visits',headers=h).json()}
    assert states['V-D1'] in ('current','conflict')
    assert 'conflict' in states.values()


def test_closed_care_keeps_original_basis(client):
    h=headers(client);adm=headers(client,'admin')
    sync(client,h,'B0',[rec('V-BASE',when='2026-11-17T08:00:00')])
    with Session() as db:
        v=db.scalar(select(Visit).where(Visit.code=='V-BASE'));v.status='screened';db.commit();base=v.id
    care=client.post('/care',headers=h,json={'visit_id':base,'kind':'feed','feed_kg':1.0}).json()['id']
    lot=client.post('/feed-lots',headers=h,json={'code':'FD-Y','received_kg':5,'expires_on':'2027-11-30'}).json()
    client.post('/feed-issues',headers=h,json={'care_id':care,'lot_id':lot['id']})
    client.post(f'/care/{care}/execute',headers=h,json={'queen_installed':False})
    client.post(f'/care/{care}/followup',headers=h,json={'followup_weight':24,'followup_queen':True,'followup_frames':7})
    assert client.post(f'/care/{care}/close',headers=adm).status_code==200
    sync(client,h,'B1',[rec('V-AFTER',when='2026-11-19T09:00:00')])
    row=next(c for c in client.get('/care',headers=h).json() if c['id']==care)
    assert row['visit_id']==base and row['status']=='closed'
    assert visit(client,h,'V-AFTER')['intake_state']=='current'
    assert visit(client,h,'V-BASE')['status']=='closed'


def test_late_record_stays_out_of_screen_and_care_basis(client):
    h=headers(client)
    sync(client,h,'B0',[rec('V-NEW',when='2026-11-17T12:00:00')])
    sync(client,h,'B1',[rec('V-OLD',when='2026-11-17T09:00:00',queen=False,frames=3)])
    assert visit(client,h,'V-OLD')['intake_state']=='late'
    # 晚到记录不能作为建处置依据，也不能单独筛查
    assert client.post('/care',headers=h,json={'visit_id':visit(client,h,'V-OLD')['id'],'kind':'feed','feed_kg':1}).status_code==409
    assert client.post(f"/visits/{visit(client,h,'V-OLD')['id']}/screen",headers=h).status_code==409
    # 历史仍在，只是不改箱况
    assert visit(client,h,'V-OLD')['weight_kg']==22.0
    hive=client.get('/hives',headers=h).json()[0]
    assert hive['colony_frames']==6 and hive['queen_present'] is True


def test_viewer_cannot_backfill_or_resolve(client):
    v=headers(client,'viewer');h=headers(client)
    assert sync(client,v,'B0',[rec('V-A')]).status_code==403
    assert client.post('/visits/sync-batches',headers=v,json={'client_batch_id':'X','records':[rec('V-A')]}).status_code==403


def test_open_conflict_pauses_only_that_hive(client):
    h=headers(client)
    sync(client,h,'B0',[rec('V-A','H-300',when='2026-11-17T09:00:00',weight=22)])
    r=sync(client,h,'B1',[rec('V-B','H-300',when='2026-11-17T09:00:00',weight=27)])
    assert r.json()['summary']['conflict']==1
    # 冲突期间该箱的新补传暂缓，不改箱况，也不新建第二个冲突
    late=sync(client,h,'B2',[rec('V-C','H-300',when='2026-11-17T11:00:00',weight=25)])
    assert late.json()['summary']['deferred']==1
    assert visit(client,h,'V-C')['intake_state']=='deferred'
    # 其他箱照常补传与筛查
    other=sync(client,h,'B3',[rec('V-D','H-301',when='2026-11-17T11:00:00')])
    assert other.json()['summary']['accepted']==1
    # 只有冲突箱被排除在新筛查之外（H-301 一条进入筛查）
    screen=client.post('/screening',headers=h)
    assert screen.status_code==200,screen.text
    assert screen.json()['count']==1
    assert len(client.get('/visit-conflicts',headers=h).json())==1


def test_resolve_rejects_foreign_visit(client):
    h=headers(client);adm=headers(client,'admin')
    sync(client,h,'B0',[rec('V-A','H-300',when='2026-11-17T09:00:00',weight=22)])
    sync(client,h,'B1',[rec('V-B','H-300',when='2026-11-17T09:00:00',weight=27)])
    sync(client,h,'B2',[rec('V-OTHER','H-301',when='2026-11-17T09:00:00')])
    cid=client.get('/visit-conflicts',headers=h).json()[0]['id']
    foreign=visit(client,h,'V-OTHER')['id']
    r=client.post(f'/visit-conflicts/{cid}/resolve',headers=adm,json={'visit_id':foreign,'note':'跨箱选定'})
    assert r.status_code==409


def test_schema_upgrade_backfills_legacy_visits(tmp_path):
    from sqlalchemy import create_engine
    from src.schema_upgrade import upgrade_schema
    url='sqlite+pysqlite:///'+str(tmp_path/'legacy.db')
    engine=create_engine(url)
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE visits (id INTEGER PRIMARY KEY, code VARCHAR(50), hive_id INTEGER, status VARCHAR(30), recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
        c.exec_driver_sql("INSERT INTO visits(code,hive_id,status,recorded_at) VALUES ('V-LEGACY',1,'screened','2026-11-01 08:00:00')")
    upgrade_schema(engine)
    with engine.connect() as c:
        row=c.exec_driver_sql('SELECT collected_at,intake_state,prior_status,source_batch_id FROM visits').fetchone()
    assert row[0] is not None and row[1]=='accepted' and row[3] is None
    # 幂等：重复执行不报错，也不覆盖已有采集时间
    upgrade_schema(engine)
