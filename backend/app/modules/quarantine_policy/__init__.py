"""隔离原因码策略机（只追加版本 + 策略证明）。

落库形态（择一，本仓取第一种）：

1. **策略表按原因码 + 版本**：`quarantine_policies(reason_code, version)` 只追加，
   一个原因码的「当前策略」= 该码的最大版本；改策略只 INSERT 新版本，绝不 UPDATE。
2. **策略证明**：`quarantine_policy_proofs` 记录每一次隔离决策
   （原因码 / 策略版本 / 动作 / 隔离键），只追加、可回溯、只读查询。

决策路径（择一，本仓取第一种）：

- **决策行钉死策略版本，再配只追加新决策不改旧行**。隔离行在入隔离时钉死
  `policy_at_quarantine + policy_version`，此后任何策略变更都不改写该行；
  每轮复扫是一次**新决策**，按当前（最新）策略版本执行，并追加一条策略证明。
  旧决策行与已处置行的处置字段不可变。

因此：策略版本一处变更不会驱动历史隔离处置行改写，只影响变更后新决策。
"""
from __future__ import annotations

import sqlite3
import uuid

# 三个互斥策略：一个原因码只能绑定其中一个。
POLICY_AUTO = "自动可再配"
POLICY_CONFIRM = "需确认后可再配"
POLICY_PERMANENT = "永久拒绝"
ALL_POLICIES = (POLICY_AUTO, POLICY_CONFIRM, POLICY_PERMANENT)

# 策略证明的动作目录（只追加，不改写）。
ACTION_QUARANTINE = "隔离"
ACTION_REBALANCE = "再配"
ACTION_BLOCK_PERMANENT = "永久拒绝拦下"
ACTION_BLOCK_CONFIRM = "待确认拦下"
ACTION_RECLASSIFY = "原因码重判"
ACTION_RECHECK_FAILED = "复扫未通过"
ACTION_CONFIRM = "确认登记"
ALL_ACTIONS = (
    ACTION_QUARANTINE,
    ACTION_REBALANCE,
    ACTION_BLOCK_PERMANENT,
    ACTION_BLOCK_CONFIRM,
    ACTION_RECLASSIFY,
    ACTION_RECHECK_FAILED,
    ACTION_CONFIRM,
)

# 有限原因码目录。原因码是枚举，不允许运行时新增；策略可在设置页改。
REASON_CODES = (
    {
        "code": "NO_ORIGINAL",
        "label": "无原单",
        "description": "冲正找不到可关联的原正向流水；原单可能后补，允许下轮自动再配。",
        "default_policy": POLICY_AUTO,
        "sort_order": 10,
        "reason_text": "冲正无原单",
    },
    {
        "code": "ACCOUNT_MISMATCH",
        "label": "账户不一致",
        "description": "冲正落账账户与原单账户不同；须人工登记确认后才允许参与再配。",
        "default_policy": POLICY_CONFIRM,
        "sort_order": 20,
        "reason_text": "冲正账户与原单不一致",
    },
    {
        "code": "OVER_EXCESS",
        "label": "超额",
        "description": "冲正金额超过原单尚未冲销的余额；永久拒绝，不得自动改余额。",
        "default_policy": POLICY_PERMANENT,
        "sort_order": 30,
        "reason_text": "冲正超额",
    },
    {
        "code": "AMOUNT_INVALID",
        "label": "金额非法",
        "description": "冲正金额为零或负数，属于无效流水；永久拒绝。",
        "default_policy": POLICY_PERMANENT,
        "sort_order": 40,
        "reason_text": "冲正金额非法",
    },
    {
        "code": "UNCLASSIFIED",
        "label": "未识别原因",
        "description": "旧库自由文本无法映射到有限原因码时的兜底；永久拒绝，须人工改用明确原因码。",
        "default_policy": POLICY_PERMANENT,
        "sort_order": 90,
        "reason_text": "未识别隔离原因",
    },
)

REASON_BY_CODE = {d["code"]: d for d in REASON_CODES}
REASON_TEXT = {d["code"]: d["reason_text"] for d in REASON_CODES}

# 旧库只有自由文本，迁移时按此字典回填原因码。
LEGACY_REASON_TO_CODE = {
    d["reason_text"]: d["code"] for d in REASON_CODES
}


def label(code: str) -> str:
    meta = REASON_BY_CODE.get(code)
    return meta["label"] if meta else code


def reason_text(code: str) -> str:
    return REASON_TEXT.get(code, label(code))


def is_valid_policy(policy: str) -> bool:
    return policy in ALL_POLICIES


def legacy_code(reason: str) -> str | None:
    return LEGACY_REASON_TO_CODE.get(reason)


# —— 策略表（原因码 + 版本，只追加） ——


def seed_policies(conn: sqlite3.Connection, now: str) -> None:
    """补种缺失原因码的**首个版本**；已有版本的码一律不动。"""
    for d in REASON_CODES:
        exists = conn.execute(
            "SELECT 1 FROM quarantine_policies WHERE reason_code = ? LIMIT 1",
            (d["code"],),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO quarantine_policies(
              reason_code, version, label, description, policy, sort_order,
              updated_by, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, '系统', ?)
            """,
            (
                d["code"],
                d["label"],
                d["description"],
                d["default_policy"],
                d["sort_order"],
                now,
            ),
        )


_CURRENT_SELECT = """
SELECT p.reason_code, p.version, p.label, p.description, p.policy,
       p.sort_order, p.updated_by, p.updated_at
FROM quarantine_policies p
JOIN (
  SELECT reason_code, MAX(version) AS version
  FROM quarantine_policies
  GROUP BY reason_code
) m ON m.reason_code = p.reason_code AND m.version = p.version
"""


def list_policies(conn: sqlite3.Connection) -> list[dict]:
    """每个原因码的当前版本（最大版本）。"""
    rows = conn.execute(
        _CURRENT_SELECT + " ORDER BY p.sort_order, p.reason_code"
    ).fetchall()
    return [dict(r) for r in rows]


def policy_map(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["reason_code"]: r for r in list_policies(conn)}


def list_policy_versions(conn: sqlite3.Connection, code: str | None = None) -> list[dict]:
    """只读版本台账：全部版本按原因码 + 版本倒序。"""
    if code:
        rows = conn.execute(
            """
            SELECT * FROM quarantine_policies
            WHERE reason_code = ?
            ORDER BY version DESC
            """,
            (code,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM quarantine_policies
            ORDER BY sort_order, reason_code, version DESC
            """
        ).fetchall()
    current = {
        r["reason_code"]: r["version"] for r in conn.execute(
            "SELECT reason_code, MAX(version) AS version FROM quarantine_policies GROUP BY reason_code"
        ).fetchall()
    }
    out = []
    for r in rows:
        d = dict(r)
        d["is_current"] = current.get(d["reason_code"]) == d["version"]
        out.append(d)
    return out


def update_policy(
    conn: sqlite3.Connection,
    code: str,
    policy: str,
    updated_by: str,
    now: str,
) -> dict:
    """改策略 = 追加一个新版本；不触碰任何隔离行/确认行（禁止回溯改史）。"""
    if not is_valid_policy(policy):
        raise ValueError(f"未知策略：{policy}")
    row = conn.execute(
        """
        SELECT * FROM quarantine_policies
        WHERE reason_code = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    if not row:
        raise LookupError(f"原因码不存在：{code}")
    if row["policy"] == policy:
        # 策略未变：不制造空版本，直接返回当前行。
        out = dict(row)
        out["appended"] = False
        return out

    conn.execute(
        """
        INSERT INTO quarantine_policies(
          reason_code, version, label, description, policy, sort_order,
          updated_by, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            row["version"] + 1,
            row["label"],
            row["description"],
            policy,
            row["sort_order"],
            updated_by or "未署名",
            now,
        ),
    )
    updated = conn.execute(
        """
        SELECT * FROM quarantine_policies
        WHERE reason_code = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    out = dict(updated)
    out["appended"] = True
    return out


# —— 策略证明（只追加，只读查询） ——


def append_proof(
    conn: sqlite3.Connection,
    *,
    quarantine_id: int,
    quarantine_key: str,
    reason_code: str,
    policy_version: int,
    policy: str,
    action: str,
    settle_run_id: int | None,
    now: str,
    detail: str = "",
) -> None:
    """追加一条策略证明。永远不会 UPDATE/DELETE 既有证明。"""
    conn.execute(
        """
        INSERT INTO quarantine_policy_proofs(
          proof_key, quarantine_id, quarantine_key, reason_code,
          policy_version, policy, action, settle_run_id, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"QPP-{uuid.uuid4().hex[:12].upper()}",
            quarantine_id,
            quarantine_key,
            reason_code,
            policy_version,
            policy,
            action,
            settle_run_id,
            detail,
            now,
        ),
    )


def list_proofs(
    conn: sqlite3.Connection,
    *,
    quarantine_id: int | None = None,
    policy_version: int | None = None,
    reason_code: str | None = None,
    quarantine_key: str | None = None,
    action: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """只读：按隔离 id / 策略版本 / 原因码 / 隔离键 / 动作查询策略证明。"""
    clauses: list[str] = []
    params: list[object] = []
    if quarantine_id is not None:
        clauses.append("quarantine_id = ?")
        params.append(quarantine_id)
    if policy_version is not None:
        clauses.append("policy_version = ?")
        params.append(policy_version)
    if reason_code:
        clauses.append("reason_code = ?")
        params.append(reason_code)
    if quarantine_key:
        clauses.append("quarantine_key = ?")
        params.append(quarantine_key)
    if action:
        clauses.append("action = ?")
        params.append(action)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(int(limit), 1000))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT * FROM quarantine_policy_proofs
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def get_proof(conn: sqlite3.Connection, proof_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM quarantine_policy_proofs WHERE id = ?", (proof_id,)
    ).fetchone()
    return dict(row) if row else None
