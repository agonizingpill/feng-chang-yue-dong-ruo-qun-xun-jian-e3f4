"""轻量在线升级：无生产迁移工具，只为已有库补新列并回填历史手记。"""
from sqlalchemy import inspect,text


def upgrade_schema(bind):
    '''补 Visit 的采集时刻/接纳状态列；旧手记沿用入库时间并视为已接纳。'''
    tables=set(inspect(bind).get_table_names())
    if 'visits' not in tables:return
    columns={c['name'] for c in inspect(bind).get_columns('visits')}
    with bind.begin() as conn:
        if 'collected_at' not in columns:
            conn.execute(text('ALTER TABLE visits ADD COLUMN collected_at TIMESTAMP'))
        if 'intake_state' not in columns:
            conn.execute(text("ALTER TABLE visits ADD COLUMN intake_state VARCHAR(24) DEFAULT 'accepted'"))
        if 'prior_status' not in columns:
            conn.execute(text('ALTER TABLE visits ADD COLUMN prior_status VARCHAR(30)'))
        if 'source_batch_id' not in columns:
            conn.execute(text('ALTER TABLE visits ADD COLUMN source_batch_id INTEGER'))
        # 旧手记没有采集时间，沿用入库时间，按原逻辑继续可查、可筛查、可建处置。
        conn.execute(text("UPDATE visits SET collected_at=recorded_at WHERE collected_at IS NULL AND intake_state='accepted'"))
