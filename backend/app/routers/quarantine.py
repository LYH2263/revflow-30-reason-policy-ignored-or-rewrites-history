from fastapi import APIRouter, Query
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


@router.get("/quarantine/policies/versions")
def get_policy_versions(
    reason_code: str | None = Query(None, description="按原因码过滤"),
):
    """只读：策略表按原因码 + 版本的全部历史（只追加，不可改史）。"""
    return svc.list_policy_versions(reason_code)


@router.get("/quarantine/proofs")
def get_policy_proofs(
    quarantine_id: int | None = Query(None, description="按隔离 id 查询"),
    policy_version: int | None = Query(None, description="按策略版本查询"),
    reason_code: str | None = Query(None),
    quarantine_key: str | None = Query(None, description="按隔离键查询"),
    action: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """只读：策略证明（原因码 / 策略版本 / 动作 / 隔离键）。"""
    return svc.list_policy_proofs(
        quarantine_id=quarantine_id,
        policy_version=policy_version,
        reason_code=reason_code,
        quarantine_key=quarantine_key,
        action=action,
        limit=limit,
    )


@router.put("/quarantine/policies/{reason_code}")
def put_policy(reason_code: str, body: PolicyUpdate):
    return svc.update_policy(reason_code, body.policy, body.updated_by)


@router.get("/quarantine/{quarantine_id}/proofs")
def get_policy_proofs_by_id(quarantine_id: int):
    """只读：该隔离项的完整决策证明链（只追加）。"""
    return svc.list_policy_proofs(quarantine_id=quarantine_id, limit=1000)


@router.post("/quarantine/{quarantine_id}/confirmation")
def post_confirmation(quarantine_id: int, body: ConfirmationCreate):
    return svc.confirm_quarantine(quarantine_id, body.confirmed_by, body.note)
