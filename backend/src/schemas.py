from datetime import datetime
from pydantic import BaseModel,Field,ConfigDict,field_validator
from .constants import CareKind


class Input(BaseModel):
    model_config=ConfigDict(allow_inf_nan=False)


class Login(BaseModel):
    email:str
    password:str


class HiveInput(BaseModel):
    code:str=Field(min_length=1,max_length=30)
    site:str=Field(min_length=1,max_length=50)
    queen_present:bool
    colony_frames:int=Field(ge=0,le=20)


class VisitInput(BaseModel):
    code:str=Field(min_length=1,max_length=50)
    hive_id:int
    weight_kg:float=Field(gt=0,le=100)
    temperature:float=Field(ge=-10,le=50)
    activity:float=Field(ge=0,le=500)
    feed_left:float=Field(ge=0,le=20)
    queen_seen:bool
    colony_frames:int=Field(ge=0,le=20)


class OfflineVisit(Input):
    '''离线手记：必须带现场采集时间，可只给蜂箱编号。'''
    client_record_id:str=Field(min_length=1,max_length=80)
    code:str=Field(min_length=1,max_length=50)
    hive_id:int|None=None
    hive_code:str|None=Field(default=None,max_length=30)
    collected_at:datetime
    weight_kg:float=Field(gt=0,le=100)
    temperature:float=Field(ge=-10,le=50)
    activity:float=Field(ge=0,le=500)
    feed_left:float=Field(ge=0,le=20)
    queen_seen:bool
    colony_frames:int=Field(ge=0,le=20)

    @field_validator('code','client_record_id')
    @classmethod
    def required(cls,v):
        if not v.strip():raise ValueError('不能为空')
        return v.strip()

    @field_validator('hive_code')
    @classmethod
    def hive_ref(cls,v):
        if v is None:return None
        if not v.strip():raise ValueError('蜂箱编号不能为空')
        return v.strip()


class BatchInput(Input):
    client_batch_id:str=Field(min_length=1,max_length=80)
    records:list[OfflineVisit]=Field(min_length=1,max_length=200)

    @field_validator('client_batch_id')
    @classmethod
    def batch_required(cls,v):
        if not v.strip():raise ValueError('批次号不能为空')
        return v.strip()

    @field_validator('records')
    @classmethod
    def unique_records(cls,rows):
        ids=[r.client_record_id for r in rows]
        if len(ids)!=len(set(ids)):raise ValueError('同一批次内手记编号不能重复')
        codes=[r.code for r in rows]
        if len(codes)!=len(set(codes)):raise ValueError('同一批次内巡检编号不能重复')
        return rows


class ResolveInput(Input):
    visit_id:int
    note:str=Field(min_length=2,max_length=300)

    @field_validator('note')
    @classmethod
    def note_required(cls,v):
        if len(v.strip())<2:raise ValueError('请填写裁决依据')
        return v.strip()


class CareInput(BaseModel):
    visit_id:int
    kind:CareKind
    feed_kg:float=Field(ge=0,le=10)


class ExecutionInput(BaseModel):
    queen_installed:bool=False


class FollowupInput(BaseModel):
    followup_weight:float=Field(gt=0,le=100)
    followup_queen:bool
    followup_frames:int=Field(ge=0,le=20)
