from datetime import datetime,timezone
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from .models import Hive,Visit,VisitBatch,VisitConflict
from .yard_service import require,audit
from .visit_time import (CURRENT,LATE,DEFERRED,CONFLICT,CONFLICT_LOST,SUPERSEDED,PARTICIPATING,
                         READING_FIELDS,effective,current_visit,open_conflict,conflict_at,
                         active_care,reading,snapshot,record_conflict,add_to_conflict,quarantine,isolate,apply_current,serialize,lock_hive,lock_hives,
                         promote_pending)

OUTCOMES=('accepted','late','deferred','conflict','duplicate','rejected')
LABELS={'accepted':'已接纳为当前箱况','late':'晚到已保留，不改当前箱况','deferred':'处置中，待闭环后参与筛查',
        'conflict':'同采集时刻读数冲突，已隔离待裁决','duplicate':'重试读数已在库，未重复入库','rejected':'被拒绝',
        'kept':'裁决保持原当前读数'}


def utc(value):
    if value is None:return None
    if value.tzinfo is not None:return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def same_time(db,hive_id,collected_at,exclude_id=None):
    q=select(Visit).where(Visit.hive_id==hive_id,Visit.collected_at==collected_at,Visit.intake_state!=CONFLICT_LOST)
    if exclude_id:q=q.where(Visit.id!=exclude_id)
    return db.scalars(q.order_by(Visit.id)).all()


def integrate(db,hive,v,user):
    """接纳一条带采集时间的读数，返回 (outcome, 冲突对象或撞重读数)。"""
    if active_care(db,hive.id) or open_conflict(db,hive.id):
        v.intake_state=DEFERRED;v.status='deferred'
        reason='处置期间' if active_care(db,hive.id) else '冲突待裁决期间'
        audit(db,user,f'补传读数 {v.code} 在{reason}暂缓，裁决/闭幕后参与筛查')
        return DEFERRED,None
    current=current_visit(db,hive.id,True)
    if current is None:
        apply_current(db,hive,v);v.status='recorded'
        audit(db,user,f'补传读数 {v.code} 成为当前箱况')
        return 'accepted',None
    incoming=effective(v);cur=effective(current)
    if incoming<cur:
        v.intake_state=LATE;v.status='stale'
        audit(db,user,f'补传晚到读数 {v.code}，保留原值但不改当前箱况')
        return LATE,None
    if incoming==cur:
        if reading(v)==reading(current):return 'duplicate',current
        isolate(db,hive.id,v,user.email)
        audit(db,user,f'补传读数 {v.code} 与同采集时刻读数不一致，隔离待裁决')
        return CONFLICT,open_conflict(db,hive.id)
    apply_current(db,hive,v);v.status='recorded'
    audit(db,user,f'补传读数 {v.code} 成为当前箱况')
    return 'accepted',None


def canonical(body):
    rows=[r.model_dump(mode='json') for r in body.records]
    return sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def batch_view(batch,duplicate=False):
    return {'batch_id':batch.client_batch_id,'stored_id':batch.id,'duplicate':duplicate,'state':batch.state,
            'submitted_by':batch.submitted_by,'submitted_at':batch.submitted_at,
            'results':batch.results or [],'summary':batch.summary or {}}


def sync(db,body,user):
    with serialize(db):
        return _sync(db,body,user)


def _sync(db,body,user):
    digest=canonical(body)
    existing=db.scalar(select(VisitBatch).where(VisitBatch.client_batch_id==body.client_batch_id))
    if existing:
        if existing.content_hash!=digest:raise HTTPException(409,'该补传批次号已用于不同内容，请换批次号后重传')
        return batch_view(existing,True)
    batch=VisitBatch(client_batch_id=body.client_batch_id,content_hash=digest,submitted_by=user.email,state='applied',results=[])
    db.add(batch)
    try:db.flush()
    except IntegrityError:
        db.rollback()
        existing=db.scalar(select(VisitBatch).where(VisitBatch.client_batch_id==body.client_batch_id))
        if not existing or existing.content_hash!=digest:raise HTTPException(409,'该补传批次号已用于不同内容，请换批次号后重传')
        return batch_view(existing,True)
    hives={}
    # 先解析蜂箱再按编号升序加锁，先于读取当前读数，和建处置/筛查/裁决串行化。
    for item in body.records:resolve_hive(db,item,hives)
    lock_hives(db,[hive.id for hive in hives.values()])
    batch.hive_ids=sorted(hives)
    results=[ingest(db,batch,item,user,hives) for item in body.records]
    summary={key:sum(1 for r in results if r['outcome']==key) for key in OUTCOMES}
    batch.results=results;batch.summary=summary
    audit(db,user,f'离线补传批次 {batch.client_batch_id}：当前{summary["accepted"]} / 晚到{summary["late"]} / 待接纳{summary["deferred"]} / 冲突{summary["conflict"]}')
    db.commit();return batch_view(batch)


def resolve_hive(db,item,hives):
    if item.hive_id is not None:
        if item.hive_id not in hives:
            row=db.scalar(select(Hive).where(Hive.id==item.hive_id))
            if row:hives[row.id]=row
        return hives.get(item.hive_id)
    if item.hive_code:
        row=db.scalar(select(Hive).where(Hive.code==item.hive_code.strip()))
        if row:hives[row.id]=row
        return row
    return None


def ingest(db,batch,item,user,hives):
    result={'client_record_id':item.client_record_id,'code':item.code,'outcome':'rejected','visit_id':None,'conflict_id':None,'intake_state':None,'message':''}
    hive=resolve_hive(db,item,hives)
    if not hive:
        result['message']='蜂箱不存在或编号无效';return result
    values=item.model_dump(include=set(READING_FIELDS))
    collected_at=utc(item.collected_at)
    if collected_at is None:
        result['message']='离线补传必须填写现场采集时间';return result
    existing=db.scalar(select(Visit).where(Visit.code==item.code))
    if existing:
        if existing.hive_id==hive.id and (existing.collected_at or existing.recorded_at)==collected_at and reading(existing)==reading(values):
            result.update(visit_id=existing.id,intake_state=existing.intake_state,outcome='duplicate',message=LABELS['duplicate'])
        else:
            result.update(visit_id=existing.id,intake_state=existing.intake_state,message='巡检编号已被其他读数占用，未入库')
        return result
    values.update(code=item.code,hive_id=hive.id,collected_at=collected_at,source_batch_id=batch.id)
    for other in same_time(db,hive.id,collected_at):
        if reading(other)==reading(values):
            result.update(visit_id=other.id,intake_state=other.intake_state,outcome='duplicate',message=LABELS['duplicate'])
            return result
    # 同刻已有其他读数且未在处置中：直接并入冲突组隔离；处置中则下一步暂缓，闭环后再判定。
    if not active_care(db,hive.id) and same_time(db,hive.id,collected_at):
        v=Visit(**values,intake_state=CONFLICT,status='conflict');db.add(v);db.flush()
        conflict=open_conflict_at(db,hive,v,user.email)
        result.update(visit_id=v.id,conflict_id=conflict.id,intake_state=CONFLICT,outcome='conflict',message=LABELS['conflict'])
        return result
    v=Visit(**values,intake_state='pending');db.add(v);db.flush()
    outcome,ref=integrate(db,hive,v,user)
    if outcome=='duplicate':
        db.delete(v);db.flush()
        result.update(visit_id=ref.id,intake_state=ref.intake_state,outcome='duplicate',message=LABELS['duplicate'])
        return result
    result.update(visit_id=v.id,conflict_id=ref.id if isinstance(ref,VisitConflict) else None,
                  intake_state=v.intake_state,outcome=outcome,message=LABELS.get(outcome,outcome))
    return result


PROTECTED=('caring','followup','needs_requeen','closed')


def open_conflict_at(db,hive,v,actor):
    """同采集时刻出现不同读数：参与当前箱况的旧读数一并隔离，其他读数原样并入冲突组。"""
    conflict=record_conflict(db,hive.id,effective(v),v,actor)
    for other in same_time(db,hive.id,effective(v),exclude_id=v.id):
        if other.status in PROTECTED:
            add_to_conflict(db,conflict,other);continue
        if other.intake_state!=CONFLICT:quarantine(other)
    return conflict


def resolve(db,conflict_id,body,user):
    with serialize(db):
        return _resolve(db,conflict_id,body,user)


def _resolve(db,conflict_id,body,user):
    conflict=require(db,VisitConflict,conflict_id)
    lock_hive(db,conflict.hive_id)
    conflict=require(db,VisitConflict,conflict_id,True);hive=require(db,Hive,conflict.hive_id,True)
    if conflict.state!='open':raise HTTPException(409,'该冲突已完成裁决，不能重复选定')
    chosen=require(db,Visit,body.visit_id,True)
    if chosen.hive_id!=hive.id or chosen.collected_at!=conflict.collected_at or chosen.intake_state not in (CONFLICT,'current','accepted',None):
        raise HTTPException(409,'只能选定本次冲突中同箱同采集时刻的隔离读数')
    if active_care(db,hive.id):
        chosen.intake_state=DEFERRED;chosen.status='deferred';outcome=DEFERRED
    else:
        current=current_visit(db,hive.id,True)
        if current and current.id==chosen.id:
            chosen.intake_state=current.intake_state;chosen.status=chosen.prior_status or 'recorded';outcome='kept'
        elif current is None or effective(chosen)>=effective(current):
            apply_current(db,hive,chosen);chosen.status=chosen.prior_status or 'recorded';outcome='accepted'
        else:
            chosen.intake_state=LATE;chosen.status='stale';outcome=LATE
    chosen.prior_status=None
    for sibling in db.scalars(select(Visit).where(Visit.hive_id==hive.id,Visit.collected_at==conflict.collected_at,Visit.id!=chosen.id,Visit.intake_state==CONFLICT)).all():
        sibling.intake_state=CONFLICT_LOST;sibling.status='stale'
    conflict.state='resolved';conflict.resolved_visit_id=chosen.id;conflict.decided_by=user.email
    conflict.decided_at=datetime.utcnow();conflict.note=body.note.strip()
    conflict.readings=[snapshot(chosen) if int(row.get('visit_id') or 0)==chosen.id else {**row,'intake_state':CONFLICT_LOST} for row in (conflict.readings or [])]
    audit(db,user,f'主管裁决采集冲突 {conflict.id}，选定 {chosen.code}：{LABELS.get(outcome,outcome)}')
    # 裁决期间暂缓的补传读数重新按采集时间接纳；仍有活跃处置时继续暂缓到闭环。
    if not active_care(db,hive.id):
        for pending,result in promote_pending(db,hive,user.email,reason='冲突裁决'):
            audit(db,user,f'裁决后接纳暂缓读数 {pending.code}：{result}')
    db.commit();return {'conflict':conflict,'chosen':chosen,'outcome':outcome,'message':LABELS.get(outcome,outcome)}


def trace(db,visit):
    batch=None;record=None
    if visit.source_batch_id:
        stored=db.get(VisitBatch,visit.source_batch_id)
        if stored:
            batch=batch_view(stored)
            record=next((row for row in batch['results'] if row.get('visit_id')==visit.id),None)
    conflict=None
    if visit.collected_at:
        conflict=db.scalar(select(VisitConflict).where(VisitConflict.hive_id==visit.hive_id,VisitConflict.collected_at==visit.collected_at).order_by(VisitConflict.id.desc()))
    return {'visit':visit,'batch':batch,'record':record,'conflict':conflict,
            'label':(record.get('message') or LABELS.get(record['outcome'],record['outcome'])) if record else (('历史手记 / 在线巡检') if visit.collected_at is None else '现场手记')}
