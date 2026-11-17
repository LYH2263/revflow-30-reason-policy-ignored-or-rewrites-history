"""隔离原因码策略机。

落地形态：**原因码策略表**（`quarantine_policies`），每个有限原因码恰好一行、
一个互斥策略；不采用「配置行不可变快照 + 版本号」形态。策略在设置页可维护，
归并扫描一律以当前策略表为准。
"""
from __future__ import annotations

import sqlite3

# 三个互斥策略：一个原因码只能绑定其中一个。
POLICY_AUTO = "自动可再配"
POLICY_CONFIRM = "需确认后可再配"
POLICY_PERMANENT = "永久拒绝"
ALL_POLICIES = (POLICY_AUTO, POLICY_CONFIRM, POLICY_PERMANENT)

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


def seed_policies(conn: sqlite3.Connection, now: str) -> None:
    """补种缺失原因码；不覆盖设置页已维护的策略。"""
    for d in REASON_CODES:
        conn.execute(
            """
            INSERT OR IGNORE INTO quarantine_policies(
              reason_code, label, description, policy, sort_order, updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, '系统', ?)
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


def list_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT reason_code, label, description, policy, sort_order, updated_by, updated_at
        FROM quarantine_policies
        ORDER BY sort_order, reason_code
        """
    ).fetchall()
    return [dict(r) for r in rows]


def policy_map(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["reason_code"]: r for r in list_policies(conn)}


def update_policy(
    conn: sqlite3.Connection,
    code: str,
    policy: str,
    updated_by: str,
    now: str,
) -> dict:
    if not is_valid_policy(policy):
        raise ValueError(f"未知策略：{policy}")
    row = conn.execute(
        "SELECT * FROM quarantine_policies WHERE reason_code = ?", (code,)
    ).fetchone()
    if not row:
        raise LookupError(f"原因码不存在：{code}")
    conn.execute(
        """
        UPDATE quarantine_policies
        SET policy = ?, updated_by = ?, updated_at = ?
        WHERE reason_code = ?
        """,
        (policy, updated_by or "未署名", now, code),
    )
    conn.execute(
        """
        UPDATE quarantine_items
        SET policy_at_quarantine = ?
        WHERE reason_code = ?
        """,
        (policy, code),
    )
    conn.execute(
        """
        UPDATE quarantine_confirmations
        SET policy_at_confirm = ?
        WHERE quarantine_id IN (
          SELECT id FROM quarantine_items WHERE reason_code = ?
        )
        """,
        (policy, code),
    )
    updated = conn.execute(
        "SELECT * FROM quarantine_policies WHERE reason_code = ?", (code,)
    ).fetchone()
    return dict(updated)
