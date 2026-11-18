"""采集时间口径、当前箱况与冲突隔离的共享读模型。

- 有现场采集时间的手记按 collected_at 排序；升级前的旧手记退回 recorded_at。
- 只有 intake_state=current 的读数会改写蜂箱王况/框数，并进入每箱最新筛查。
- late/deferred/conflict/conflict_lost/superseded 均保留原值，但不改当前箱况、不参与筛查。
"""
from sqlalchemy import func,or_,select,text
from .models import Hive,Visit,CareAction,VisitConflict,Audit

import threading
from contextlib import contextmanager

# SQLite 测试/单机场景没有行级锁，用进程内锁在事务级串行。
_local_lock=threading.RLock()

CURRENT='current'
LATE='late'
DEFERRED='deferred'
CONFLICT='conflict'
CONFLICT_LOST='conflict_lost'
SUPERSEDED='superseded'

READING_FIELDS=('weight_kg','temperature','activity','feed_left','queen_seen','colony_frames')
# accepted 仅用于升级前的历史手记：保留可用，但新写入一律使用 current 等明确状态。
PARTICIPATING=('current','accepted')
ACTIVE_CARE=('planned','executed','rechecked','needs_requeen')


def lock_hive(db,hive_id):
    """单箱串行化，供建处置、裁决、筛查、在线登记使用。"""
    lock_hives(db,[hive_id])


def lock_hives(db,hive_ids):
    """按编号升序加箱级事务咨询锁，补传、建处置、筛查共用，避免死锁。

    PostgreSQL 用 pg_advisory_xact_lock 串行化；其它库（SQLite 测试）没有行锁，
    由 serialize() 在事务级串行覆盖。
    """
    if dialect(db)!='postgresql':return
    for hive_id in sorted({int(h) for h in hive_ids if h}):
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':hive_id})


def dialect(db):
    return db.bind.dialect.name if db.bind is not None else ''


def serialize(db):
    """SQLite 没有行级锁：用进程内串行覆盖读取—写入—提交整个事务。"""
    return _serialized() if dialect(db)!='postgresql' else _nop()


@contextmanager
def _serialized():
    _local_lock.acquire()
    try:yield
    finally:_local_lock.release()


@contextmanager
def _nop():yield


def effective(value):return value.collected_at or value.recorded_at
def order_columns():return func.coalesce(Visit.collected_at,Visit.recorded_at).desc(),Visit.id.desc()
def participating_clause():return or_(Visit.intake_state.in_(PARTICIPATING),Visit.intake_state.is_(None))


def current_visit(db,hive_id,lock=False):
    q=select(Visit).where(Visit.hive_id==hive_id,participating_clause()).order_by(*order_columns()).limit(1)
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    return db.scalar(q)


def current_id(db,hive_id):
    row=current_visit(db,hive_id)
    return row.id if row else None


def open_conflict(db,hive_id,lock=False):
    q=select(VisitConflict).where(VisitConflict.hive_id==hive_id,VisitConflict.state=='open').order_by(VisitConflict.id.desc())
    if lock:q=q.with_for_update()
    return db.scalar(q)


def conflict_at(db,hive_id,collected_at,lock=False):
    q=select(VisitConflict).where(VisitConflict.hive_id==hive_id,VisitConflict.collected_at==collected_at,VisitConflict.state=='open')
    if lock:q=q.with_for_update()
    return db.scalar(q)


def active_care(db,hive_id):
    return db.scalar(select(CareAction.id).join(Visit,CareAction.visit_id==Visit.id).where(Visit.hive_id==hive_id,CareAction.status.in_(ACTIVE_CARE)))


def reading(value):
    if isinstance(value,dict):return tuple(value.get(k) for k in READING_FIELDS)
    return tuple(getattr(value,k) for k in READING_FIELDS)


def snapshot(v):
    stamp=effective(v)
    row={'visit_id':v.id,'code':v.code,'hive_id':v.hive_id,'intake_state':v.intake_state,'collected_at':stamp.isoformat() if stamp else None}
    row.update({k:getattr(v,k) for k in READING_FIELDS})
    return row


def apply_current(db,hive,v,touch_care_state=True):
    """让一份读数成为当前箱况；旧当前读数保留，但退出筛查范围。

    touch_care_state=False 用于处置刚闭环时：更新王况/框数，但越冬状态等后续筛查结论。
    """
    prior=current_visit(db,hive.id,True)
    if prior and prior.id!=v.id and prior.status!='closed':
        prior.intake_state=SUPERSEDED
        if prior.status in ('recorded','screened','blocked'):prior.status='stale'
    v.intake_state=CURRENT
    hive.queen_present=v.queen_seen
    hive.colony_frames=v.colony_frames
    if touch_care_state and hive.care_state not in ('spring_rescue','spring_ready'):
        hive.care_state='observe' if v.queen_seen else 'needs_care'


def record_conflict(db,hive_id,collected_at,v,actor='system'):
    """把同箱同采集时刻的不同读数并入同一冲突组，等待主管选定。"""
    conflict=conflict_at(db,hive_id,collected_at,True)
    if not conflict:
        conflict=VisitConflict(hive_id=hive_id,collected_at=collected_at,state='open',readings=[])
        db.add(conflict);db.flush()
    rows=[row for row in (conflict.readings or []) if int(row.get('visit_id') or 0)!=v.id]
    rows.append(snapshot(v));conflict.readings=rows
    db.add(Audit(actor=actor,action=f'采集读数冲突 蜂箱{hive_id} / {collected_at.isoformat()}，读数隔离待裁决'))
    return conflict


def quarantine(v):
    """标记冲突隔离，并记住原状态，裁决时恢复。"""
    if v.prior_status is None and v.status!='conflict':v.prior_status=v.status
    v.intake_state=CONFLICT;v.status='conflict'


def isolate(db,hive_id,v,actor='system'):
    """将一条读数标记为冲突隔离，并并入冲突组。"""
    quarantine(v)
    return record_conflict(db,hive_id,effective(v),v,actor)


def add_to_conflict(db,conflict,v):
    rows=[row for row in (conflict.readings or []) if int(row.get('visit_id') or 0)!=v.id]
    rows.append(snapshot(v));conflict.readings=rows
    return conflict


def promote_pending(db,hive,actor='system',touch_care_state=True,reason='处置闭环'):
    """把暂缓的补传读数按采集时间重新接纳，返回 (读数, 结局) 列表。"""
    pending=db.scalars(select(Visit).where(Visit.hive_id==hive.id,Visit.intake_state==DEFERRED).order_by(Visit.collected_at.asc(),Visit.id.asc())).all()
    outcomes=[]
    for v in pending:
        current=current_visit(db,hive.id,True)
        if current is None or effective(v)>effective(current):
            apply_current(db,hive,v,touch_care_state=touch_care_state);v.status='recorded';outcome=CURRENT
        elif effective(v)==effective(current):
            if reading(v)==reading(current):
                v.intake_state=CONFLICT_LOST;v.status='stale';outcome=CONFLICT_LOST
            else:
                isolate(db,hive.id,v,actor);outcome=CONFLICT
        else:
            v.intake_state=LATE;v.status='stale';outcome=LATE
        outcomes.append((v,outcome))
    if outcomes:
        db.add(Audit(actor=actor,action=f'{hive.code} {reason}，{len(outcomes)}条暂缓补传读数重新接纳'))
    return outcomes


def promote_deferred(db,hive,actor='system'):
    """处置闭幕后，把期间补传的读数按采集时间重新接纳；不直接推翻越冬状态。"""
    return promote_pending(db,hive,actor,touch_care_state=False,reason='处置闭环')
