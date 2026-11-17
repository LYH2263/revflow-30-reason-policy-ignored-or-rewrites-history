from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test_policy")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

db_path = Path(os.environ["DATA_DIR"]) / "app.db"
if db_path.exists():
    db_path.unlink()

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _quarantine(client):
    return client.get("/api/quarantine").json()


def _by_key(rows, key):
    return next(r for r in rows if r["entry_key"] == key)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["项目"] == "冲流台"


def test_policy_table_seeded_with_mutually_exclusive_policies(client):
    body = client.get("/api/quarantine/policies").json()
    rows = {r["reason_code"]: r for r in body["策略"]}
    assert set(rows) == {
        "NO_ORIGINAL",
        "ACCOUNT_MISMATCH",
        "OVER_EXCESS",
        "AMOUNT_INVALID",
        "UNCLASSIFIED",
    }
    assert {r["policy"] for r in rows.values()} <= set(body["可选策略"])
    assert body["可选策略"] == ["自动可再配", "需确认后可再配", "永久拒绝"]
    assert rows["NO_ORIGINAL"]["policy"] == "自动可再配"
    assert rows["ACCOUNT_MISMATCH"]["policy"] == "需确认后可再配"
    assert rows["OVER_EXCESS"]["policy"] == "永久拒绝"


def test_settle_assigns_reason_codes(client):
    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "完成"

    q = _quarantine(client)
    reasons = {r["entry_key"]: r["reason_code"] for r in q}
    assert reasons["REV-20261116-004"] == "NO_ORIGINAL"
    assert reasons["REV-20261116-005"] == "ACCOUNT_MISMATCH"
    assert reasons["REV-20261116-006"] == "OVER_EXCESS"
    assert reasons["REV-20261116-007"] == "AMOUNT_INVALID"
    for r in q:
        assert r["reason_code"]
        assert r["policy"] in ("自动可再配", "需确认后可再配", "永久拒绝")


def test_permanent_and_confirm_policies_hold_back_balance(client):
    before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    run = client.post("/api/settle-runs").json()

    q = _quarantine(client)
    over = _by_key(q, "REV-20261116-006")
    mismatch = _by_key(q, "REV-20261116-005")
    assert over["status"] == "未平"
    assert over["policy"] == "永久拒绝"
    assert mismatch["status"] == "未平" and mismatch["confirmation"] is None
    assert run["永久拒绝拦下"] >= 1
    assert run["待确认拦下"] >= 1

    after = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    assert after == before, "永久拒绝 / 待确认项不得改动余额"


def test_confirmation_is_auditable_and_enables_rebalance(client):
    q = _quarantine(client)
    mismatch = _by_key(q, "REV-20261116-005")
    assert mismatch["can_confirm"] is True

    # 需确认项无确认标记不得配平——先跑一轮仍是未平。
    client.post("/api/settle-runs")
    assert _by_key(_quarantine(client), "REV-20261116-005")["status"] == "未平"

    r = client.post(
        f"/api/quarantine/{mismatch['id']}/confirmation",
        json={"confirmed_by": "账务-甲", "note": "原单账户错录，确认按原单冲销"},
    )
    assert r.status_code == 200, r.text
    conf = r.json()
    assert conf["confirmed_by"] == "账务-甲"
    assert conf["policy_at_confirm"] == "需确认后可再配"

    # 重复确认被拒（不可覆盖审计）。
    again = client.post(
        f"/api/quarantine/{mismatch['id']}/confirmation",
        json={"confirmed_by": "账务-乙"},
    )
    assert again.status_code == 409

    run = client.post("/api/settle-runs").json()
    assert run["本次再配"] >= 1
    resolved = _by_key(_quarantine(client), "REV-20261116-005")
    assert resolved["status"] == "已再配"
    assert resolved["confirmation"]["confirmed_by"] == "账务-甲"


def test_permanent_rejects_never_rebalance_even_after_confirm_attempt(client):
    q = _quarantine(client)
    over = _by_key(q, "REV-20261116-006")
    r = client.post(
        f"/api/quarantine/{over['id']}/confirmation",
        json={"confirmed_by": "账务-甲"},
    )
    assert r.status_code == 409, "永久拒绝项不接受确认标记"
    before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    client.post("/api/settle-runs")
    after = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    assert after == before


def test_auto_policy_rebalances_when_original_arrives(client):
    q = _quarantine(client)
    orphan = _by_key(q, "REV-20261116-004")
    assert orphan["policy"] == "自动可再配"

    # 后补原单：直接落库模拟装载新正向。
    from app.db import connect

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status, note
            ) VALUES ('FWD-MISSING', 1, '正向', 12000, 'ORD-9X99', NULL,
                      '2026-11-17T09:00:00Z', '待归并', '后补原单')
            """
        )
        conn.commit()

    run = client.post("/api/settle-runs").json()
    assert run["本次再配"] >= 1
    resolved = _by_key(_quarantine(client), "REV-20261116-004")
    assert resolved["status"] == "已再配"


def test_policy_change_takes_effect_next_scan(client):
    r = client.put(
        "/api/quarantine/policies/AMOUNT_INVALID",
        json={"policy": "自动可再配", "updated_by": "账务-甲"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["policy"] == "自动可再配"

    bad = client.put(
        "/api/quarantine/policies/AMOUNT_INVALID",
        json={"policy": "随便放行"},
    )
    assert bad.status_code == 400
    unknown = client.put(
        "/api/quarantine/policies/NOT_A_CODE",
        json={"policy": "永久拒绝"},
    )
    assert unknown.status_code == 404

    q = _by_key(_quarantine(client), "REV-20261116-007")
    assert q["policy"] == "自动可再配"
    # 策略允许复扫，但金额非法仍然判失败：策略不能把非法流水配平。
    before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    client.post("/api/settle-runs")
    after = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    assert after == before
    assert _by_key(_quarantine(client), "REV-20261116-007")["status"] == "未平"


def test_report_buckets_match_quarantine_and_policies(client):
    report = client.post("/api/verify-reports")
    assert report.status_code == 200, report.text
    rj = report.json()
    assert rj["consistent"] == 1
    assert rj["ledger_net_cents"] == rj["balance_sum_cents"]

    buckets = {b["reason_code"]: b for b in rj["原因分桶"]}
    q = _quarantine(client)
    policies = {
        r["reason_code"]: r["policy"]
        for r in client.get("/api/quarantine/policies").json()["策略"]
    }
    for code, b in buckets.items():
        open_rows = [x for x in q if x["reason_code"] == code and x["status"] == "未平"]
        assert b["open_count"] == len(open_rows)
        assert b["open_amount_cents"] == sum(x["amount_cents"] for x in open_rows)
        assert b["policy"] == policies[code]

    # 永久拒绝金额与未平隔离同源，且余额互证一致。
    permanent = sum(
        b["open_amount_cents"] for b in buckets.values() if b["policy"] == "永久拒绝"
    )
    assert rj["detail"]["永久拒绝隔离金额分"] == permanent

    # 归并运行也带同一套分桶。
    run = client.get(f"/api/settle-runs/{rj['settle_run_id']}").json()
    assert {b["reason_code"] for b in run["原因分桶"]} == set(buckets)


def test_settle_is_fail_closed_transaction(client):
    """策略执行与配平同一失败闭：中途失败不得留下半截余额或隔离。"""
    from app.db import connect

    before_bal = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    before_q = len(_quarantine(client))

    with connect() as conn:
        conn.execute("DROP TABLE quarantine_confirmations")
        conn.commit()
    try:
        r = client.post("/api/settle-runs")
        assert r.status_code == 500
        assert "已整体回滚" in r.text
    finally:
        from app.seed import init_db

        with connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quarantine_confirmations (
                  id INTEGER PRIMARY KEY,
                  quarantine_id INTEGER NOT NULL UNIQUE REFERENCES quarantine_items(id),
                  policy_at_confirm TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  confirmed_by TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
        init_db()

    after_bal = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    assert after_bal == before_bal
    assert len(_quarantine(client)) == before_q
    runs = client.get("/api/settle-runs").json()
    assert runs[0]["status"] == "失败"
    assert runs[0]["error_text"]
