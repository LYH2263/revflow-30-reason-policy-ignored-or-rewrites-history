from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules import quarantine_policy


POLICY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS quarantine_policies (
  id INTEGER PRIMARY KEY,
  reason_code TEXT NOT NULL,
  version INTEGER NOT NULL,
  label TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  policy TEXT NOT NULL CHECK (policy IN ('自动可再配', '需确认后可再配', '永久拒绝')),
  sort_order INTEGER NOT NULL DEFAULT 0,
  updated_by TEXT NOT NULL DEFAULT '系统',
  updated_at TEXT NOT NULL,
  UNIQUE (reason_code, version)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER
);
""" + POLICY_TABLE_DDL + """
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  reason TEXT NOT NULL,
  policy_at_quarantine TEXT NOT NULL,
  policy_version INTEGER NOT NULL DEFAULT 0,
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '未平',
  retry_count INTEGER NOT NULL DEFAULT 0,
  settle_run_id INTEGER NOT NULL,
  last_checked_run_id INTEGER,
  last_checked_policy_version INTEGER,
  resolved_run_id INTEGER,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_confirmations (
  id INTEGER PRIMARY KEY,
  quarantine_id INTEGER NOT NULL UNIQUE REFERENCES quarantine_items(id),
  policy_at_confirm TEXT NOT NULL,
  policy_version INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  confirmed_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- 策略证明：只追加，不可改写；记录每次隔离决策的原因码 / 策略版本 / 动作 / 隔离键。
CREATE TABLE IF NOT EXISTS quarantine_policy_proofs (
  id INTEGER PRIMARY KEY,
  proof_key TEXT NOT NULL UNIQUE,
  quarantine_id INTEGER NOT NULL REFERENCES quarantine_items(id),
  quarantine_key TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  policy TEXT NOT NULL,
  action TEXT NOT NULL,
  settle_run_id INTEGER,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  reason_buckets_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  reason_buckets_json TEXT NOT NULL DEFAULT '{}',
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 只追加 / 不可回溯改史的数据库级护栏。
# 任何绕过接口层直接 UPDATE/DELETE 的尝试都会 ABORT，而不是静默改史。
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_policies_no_update
BEFORE UPDATE ON quarantine_policies
BEGIN
  SELECT RAISE(ABORT, '策略表只追加：禁止改写历史策略版本');
END;
CREATE TRIGGER IF NOT EXISTS trg_policies_no_delete
BEFORE DELETE ON quarantine_policies
BEGIN
  SELECT RAISE(ABORT, '策略表只追加：禁止删除历史策略版本');
END;
CREATE TRIGGER IF NOT EXISTS trg_proofs_no_update
BEFORE UPDATE ON quarantine_policy_proofs
BEGIN
  SELECT RAISE(ABORT, '策略证明只追加：禁止改写');
END;
CREATE TRIGGER IF NOT EXISTS trg_proofs_no_delete
BEFORE DELETE ON quarantine_policy_proofs
BEGIN
  SELECT RAISE(ABORT, '策略证明只追加：禁止删除');
END;
CREATE TRIGGER IF NOT EXISTS trg_quarantine_resolved_frozen
BEFORE UPDATE OF status, resolved_run_id, resolved_at, policy_at_quarantine,
                 policy_version, reason_code, reason, amount_cents, entry_key
ON quarantine_items
WHEN OLD.status = '已再配'
BEGIN
  SELECT RAISE(ABORT, '已再配隔离行的处置结果不可改写');
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_policy_table(conn) -> None:
    """旧库的「一码一行」策略表升级为「原因码 + 版本」只追加表。

    旧行的策略值成为该码的版本 1；历史不改写，只让后续变更追加新版本。
    """
    if "version" in _columns(conn, "quarantine_policies"):
        return
    conn.execute("DROP TRIGGER IF EXISTS trg_policies_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_policies_no_delete")
    conn.execute("ALTER TABLE quarantine_policies RENAME TO quarantine_policies_legacy")
    conn.executescript(POLICY_TABLE_DDL)
    conn.execute(
        """
        INSERT INTO quarantine_policies(
          reason_code, version, label, description, policy, sort_order,
          updated_by, updated_at
        )
        SELECT reason_code, 1, label, description, policy, sort_order,
               updated_by, updated_at
        FROM quarantine_policies_legacy
        """
    )
    conn.execute("DROP TABLE quarantine_policies_legacy")


def _migrate(conn) -> None:
    """旧库补列并回填原因码；原因码是枚举，旧自由文本按字典映射。"""
    _migrate_policy_table(conn)

    qcols = _columns(conn, "quarantine_items")
    additions = {
        "reason_code": "TEXT NOT NULL DEFAULT ''",
        "policy_at_quarantine": "TEXT NOT NULL DEFAULT ''",
        "policy_version": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT '未平'",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "last_checked_run_id": "INTEGER",
        "last_checked_policy_version": "INTEGER",
        "resolved_run_id": "INTEGER",
        "resolved_at": "TEXT",
    }
    for col, ddl in additions.items():
        if col not in qcols:
            conn.execute(f"ALTER TABLE quarantine_items ADD COLUMN {col} {ddl}")

    ccols = _columns(conn, "quarantine_confirmations")
    if "policy_version" not in ccols:
        conn.execute(
            "ALTER TABLE quarantine_confirmations ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 0"
        )

    for run_table in ("settle_runs", "verify_reports"):
        if "reason_buckets_json" not in _columns(conn, run_table):
            conn.execute(
                f"ALTER TABLE {run_table} ADD COLUMN reason_buckets_json TEXT NOT NULL DEFAULT '{{}}'"
            )
    if "error_text" not in _columns(conn, "settle_runs"):
        conn.execute(
            "ALTER TABLE settle_runs ADD COLUMN error_text TEXT NOT NULL DEFAULT ''"
        )
    # 旧行无原因码：按旧自由文本回填；无法映射的落 UNCLASSIFIED，fail-closed。
    # 快照钉的是该码在迁移后表里的首个版本（即历史当时生效的策略），而非硬编码默认。
    stale = conn.execute(
        "SELECT id, reason FROM quarantine_items WHERE reason_code = '' AND status != '已再配'"
    ).fetchall()
    for row in stale:
        code = quarantine_policy.legacy_code(row["reason"]) or "UNCLASSIFIED"
        ver = conn.execute(
            """
            SELECT policy, version FROM quarantine_policies
            WHERE reason_code = ? ORDER BY version ASC LIMIT 1
            """,
            (code,),
        ).fetchone()
        conn.execute(
            """
            UPDATE quarantine_items
            SET reason_code = ?, policy_at_quarantine = ?, policy_version = ?
            WHERE id = ?
            """,
            (
                code,
                (ver["policy"] if ver else quarantine_policy.REASON_BY_CODE[code]["default_policy"]),
                (ver["version"] if ver else 0),
                row["id"],
            ),
        )
    # 补列后旧行的策略快照可能为空：回填为该码当时的首个版本，保证审计列有值。
    # 已再配行的处置字段钉死，绝不回溯改写，故只补未平行。
    conn.execute(
        """
        UPDATE quarantine_items
        SET policy_at_quarantine = COALESCE(
              (SELECT p.policy FROM quarantine_policies p
               WHERE p.reason_code = quarantine_items.reason_code
               ORDER BY p.version ASC LIMIT 1),
              '需确认后可再配'
            ),
            policy_version = COALESCE(
              (SELECT MIN(p.version) FROM quarantine_policies p
               WHERE p.reason_code = quarantine_items.reason_code),
              0
            )
        WHERE policy_at_quarantine = '' AND reason_code != '' AND status != '已再配'
        """
    )
    # 旧行若已有策略快照但缺版本号：按快照值匹配出当时版本；匹配不到则取首个版本。
    conn.execute(
        """
        UPDATE quarantine_items
        SET policy_version = COALESCE(
              (SELECT MAX(p.version) FROM quarantine_policies p
               WHERE p.reason_code = quarantine_items.reason_code
                 AND p.policy = quarantine_items.policy_at_quarantine),
              (SELECT MIN(p.version) FROM quarantine_policies p
               WHERE p.reason_code = quarantine_items.reason_code),
              0
            )
        WHERE policy_version = 0 AND reason_code != '' AND status != '已再配'
        """
    )
    # 索引在建列之后创建（旧库 migrate 前没有这些列）。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quarantine_entry ON quarantine_items(entry_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quarantine_status ON quarantine_items(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proofs_quarantine ON quarantine_policy_proofs(quarantine_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proofs_version ON quarantine_policy_proofs(policy_version)"
    )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 先把旧「一码一行」表升级为版本表并补种各码首个版本，再回填旧隔离行，
        # 保证迁移时旧行能钉到真实存在过的策略版本。
        _migrate_policy_table(conn)
        quarantine_policy.seed_policies(conn, _now())
        _migrate(conn)
        conn.executescript(TRIGGERS)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            conn.commit()
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()
