from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules import quarantine_policy


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
CREATE TABLE IF NOT EXISTS quarantine_policies (
  reason_code TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  policy TEXT NOT NULL CHECK (policy IN ('自动可再配', '需确认后可再配', '永久拒绝')),
  sort_order INTEGER NOT NULL DEFAULT 0,
  updated_by TEXT NOT NULL DEFAULT '系统',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  reason TEXT NOT NULL,
  policy_at_quarantine TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '未平',
  retry_count INTEGER NOT NULL DEFAULT 0,
  settle_run_id INTEGER NOT NULL,
  last_checked_run_id INTEGER,
  resolved_run_id INTEGER,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_confirmations (
  id INTEGER PRIMARY KEY,
  quarantine_id INTEGER NOT NULL UNIQUE REFERENCES quarantine_items(id),
  policy_at_confirm TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  confirmed_by TEXT NOT NULL,
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


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn) -> None:
    """旧库补列并回填原因码；原因码是枚举，旧自由文本按字典映射。"""
    qcols = _columns(conn, "quarantine_items")
    additions = {
        "reason_code": "TEXT NOT NULL DEFAULT ''",
        "policy_at_quarantine": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT '未平'",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "last_checked_run_id": "INTEGER",
        "resolved_run_id": "INTEGER",
        "resolved_at": "TEXT",
    }
    for col, ddl in additions.items():
        if col not in qcols:
            conn.execute(f"ALTER TABLE quarantine_items ADD COLUMN {col} {ddl}")
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
    stale = conn.execute(
        "SELECT id, reason FROM quarantine_items WHERE reason_code = ''"
    ).fetchall()
    for row in stale:
        code = quarantine_policy.legacy_code(row["reason"]) or "UNCLASSIFIED"
        conn.execute(
            """
            UPDATE quarantine_items
            SET reason_code = ?, policy_at_quarantine = ?
            WHERE id = ?
            """,
            (code, quarantine_policy.REASON_BY_CODE[code]["default_policy"], row["id"]),
        )
    # 补列后旧行的策略快照可能为空：按当前策略表回填，保证审计列有值。
    conn.execute(
        """
        UPDATE quarantine_items
        SET policy_at_quarantine = COALESCE(
          (SELECT p.policy FROM quarantine_policies p WHERE p.reason_code = quarantine_items.reason_code),
          '需确认后可再配'
        )
        WHERE policy_at_quarantine = '' AND reason_code != ''
        """
    )
    # 索引在建列之后创建（旧库 migrate 前没有这些列）。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quarantine_entry ON quarantine_items(entry_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quarantine_status ON quarantine_items(status)"
    )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        quarantine_policy.seed_policies(conn, _now())
        _migrate(conn)
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
