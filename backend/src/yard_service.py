import uuid,json
from datetime import datetime
from sqlalchemy import select
from fastapi import HTTPException
from .models import Hive,Visit,CareAction,Audit,VisitConflict
from .visit_time import current_visit,current_id,open_conflict,apply_current,serialize,lock_hive,lock_hives

def require(db,model,key,lock=False):
    q=select(model).where(model.id==key)
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    row=db.scalar(q)
    if not row:raise HTTPException(404,'蜂场记录不存在')
    return row

def audit(db,user,text):db.add(Audit(actor=user.email,action=text))

def latest(db,hive_id):return current_id(db,hive_id)

def save_hive(db,body,user,key=None):
    with serialize(db):
        return _save_hive(db,body,user,key)


def _save_hive(db,body,user,key=None):
    hive=require(db,Hive,key,True) if key else Hive()
    if key:
        from .season_models import ApiaryMove
        if db.scalar(select(ApiaryMove.id).where(ApiaryMove.hive_id==hive.id,ApiaryMove.state.in_(['requested','approved']))):raise HTTPException(409,'在途转场期间蜂箱档案冻结，可先撤销申请')
    if key and hive.care_state in ['caring','needs_requeen']:raise HTTPException(409,'该蜂群有进行中的处置，王况和框数请在复查簿记录')
    if key and open_conflict(db,hive.id):raise HTTPException(409,'该箱读数冲突待主管裁决，期间先不改箱况档案')
    other=db.scalar(select(Hive).where(Hive.code==body.code,Hive.id!=key)) if key else db.scalar(select(Hive).where(Hive.code==body.code))
    if other:raise HTTPException(409,'蜂箱编号已存在')
    for k,v in body.model_dump().items():setattr(hive,k,v)
    db.add(hive);audit(db,user,f'维护蜂箱 {body.code}');db.commit();return hive

def record_visit(db,body,user):
    with serialize(db):
        return _record_visit(db,body,user)


def _record_visit(db,body,user):
    lock_hive(db,body.hive_id)
    hive=require(db,Hive,body.hive_id,True)
    if open_conflict(db,hive.id):raise HTTPException(409,'该箱存在同采集时刻读数冲突，主管裁决前暂停登记与新建处置')
    if hive.care_state in ['caring','needs_requeen']:raise HTTPException(409,'该箱处置未结束，请在处置簿登记复查，不能覆盖原巡检')
    if db.scalar(select(Visit).where(Visit.code==body.code)):raise HTTPException(409,'巡检编号已存在')
    visit=Visit(**body.model_dump(),collected_at=datetime.utcnow(),intake_state='current',status='recorded')
    db.add(visit);db.flush();apply_current(db,hive,visit)
    audit(db,user,f'巡箱 {hive.code} / {body.code} {body.weight_kg}kg');db.commit();return visit

def enqueue_screen(db,user,queue,key=None):
    with serialize(db):
        return _enqueue_screen(db,user,queue,key)


def _enqueue_screen(db,user,queue,key=None):
    if key:
        v=require(db,Visit,key);lock_hive(db,v.hive_id)
        v=require(db,Visit,key,True)
        if v.intake_state not in (None,'current'):raise HTTPException(409,'该条为晚到、待接纳或冲突隔离读数，不参与当前箱况筛查')
        if latest(db,v.hive_id)!=v.id:raise HTTPException(409,'该箱已有更新采集时间的巡检，请筛查最新记录')
        if open_conflict(db,v.hive_id):raise HTTPException(409,'该箱读数冲突待主管裁决，暂不能筛查')
        candidates=[v]
    else:
        candidates=[]
        lock_hives(db,db.scalars(select(Hive.id).order_by(Hive.id)).all())
        for hive in db.scalars(select(Hive).order_by(Hive.id).with_for_update()).all():
            if open_conflict(db,hive.id):continue
            v=current_visit(db,hive.id,True)
            if v and v.status in ['recorded','screened','blocked']:candidates.append(v)
        candidates.sort(key=lambda v:v.id)
    if not candidates:raise HTTPException(409,'没有可筛查的最新巡检，请先登记读数')
    for v in candidates:
        if v.status not in ['recorded','screened','blocked']:raise HTTPException(409,'已进入处置的巡检不能重新筛查或覆盖原值')
        v.status='screening'
    jobid=uuid.uuid4().hex;ids=[v.id for v in candidates]
    audit(db,user,f'弱群筛查 {len(ids)} 箱最新巡检');db.commit()
    queue.set('apiary:screen:'+jobid,json.dumps({'status':'queued'}),ex=86400);queue.rpush('apiary:screening',json.dumps({'job_id':jobid,'visit_ids':ids}));return {'job_id':jobid,'count':len(ids)}
