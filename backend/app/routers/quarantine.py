from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import settle as svc

router = APIRouter(tags=["未平隔离"])


class PolicyUpdate(BaseModel):
    policy: str = Field(..., description="自动可再配 | 需确认后可再配 | 永久拒绝")
    updated_by: str = Field("未署名", max_length=64)


class ConfirmationCreate(BaseModel):
    confirmed_by: str = Field(..., min_length=1, max_length=64)
    note: str = Field("", max_length=500)


@router.get("/quarantine")
def get_quarantine():
    return svc.list_quarantine()


@router.get("/quarantine/policies")
def get_policies():
    return svc.list_policies()


@router.put("/quarantine/policies/{reason_code}")
def put_policy(reason_code: str, body: PolicyUpdate):
    return svc.update_policy(reason_code, body.policy, body.updated_by)


@router.post("/quarantine/{quarantine_id}/confirmation")
def post_confirmation(quarantine_id: int, body: ConfirmationCreate):
    return svc.confirm_quarantine(quarantine_id, body.confirmed_by, body.note)
