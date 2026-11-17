"""原因码策略回归：策略必须被执行；改策略不得回溯改写历史处置。

覆盖基线所无的策略证明实体（原因码 / 策略版本 / 动作 / 隔离键）及其只读查询。
"""
from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test_proof")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

db_path = Path(os.environ["DATA_DIR"]) / "app.db"
if db_path.exists():
    db_path.unlink()

from app.main import app  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _by_key(rows, key):
    return next(r for r in rows if r["entry_key"] == key)


def _balances(client):
    return {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}


def _policies(client):
    return {
        r["reason_code"]: r
        for r in client.get("/api/quarantine/policies").json()["策略"]
    }


def test_auto_rebalances_only_after_original_arrives(client):
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"
    orphan = _by_key(client.get("/api/quarantine").json(), "REV-20261116-004")
    assert orphan["reason_code"] == "NO_ORIGINAL"
    assert orphan["policy"] == "自动可再配"
    assert orphan["status"] == "未平"

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
    assert _by_key(client.get("/api/quarantine").json(), "REV-20261116-004")["status"] == "已再配"


def test_permanent_never_touches_balance(client):
    """永久拒绝项永远不能改正式余额——哪怕跑很多轮、哪怕有确认尝试。"""
    over = _by_key(client.get("/api/quarantine").json(), "REV-20261116-006")
    assert over["reason_code"] == "OVER_EXCESS"
    assert over["policy"] == "永久拒绝"

    # 永久拒绝项不接受确认标记。
    r = client.post(
        f"/api/quarantine/{over['id']}/confirmation",
        json={"confirmed_by": "账务-甲"},
    )
    assert r.status_code == 409

    before = _balances(client)
    gaps = []
    for _ in range(3):
        run = client.post("/api/settle-runs").json()
        gaps.append(run["永久拒绝拦下"])
        gap = _balances(client)
        assert gap == before, "永久拒绝项不得改正式余额"
    assert all(g >= 1 for g in gaps)
    assert _by_key(client.get("/api/quarantine").json(), "REV-20261116-006")["status"] == "未平"


def test_unconfirmed_confirm_policy_never_touches_balance(client):
    """需确认项无确认标记：无论如何复扫都不得改正式余额。"""
    item = _by_key(client.get("/api/quarantine").json(), "REV-20261116-005")
    assert item["reason_code"] == "ACCOUNT_MISMATCH"
    assert item["policy"] == "需确认后可再配"
    assert item["confirmation"] is None

    before = _balances(client)
    for _ in range(3):
        run = client.post("/api/settle-runs").json()
        assert run["待确认拦下"] >= 1
        assert _balances(client) == before
    assert _by_key(client.get("/api/quarantine").json(), "REV-20261116-005")["status"] == "未平"


def test_confirm_then_rebalance_is_recorded(client):
    item = _by_key(client.get("/api/quarantine").json(), "REV-20261116-005")
    r = client.post(
        f"/api/quarantine/{item['id']}/confirmation",
        json={"confirmed_by": "账务-甲", "note": "原单账户错录"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["policy_at_confirm"] == "需确认后可再配"
    assert r.json()["policy_version"] >= 1

    run = client.post("/api/settle-runs").json()
    assert run["本次再配"] >= 1
    resolved = _by_key(client.get("/api/quarantine").json(), "REV-20261116-005")
    assert resolved["status"] == "已再配"
    assert resolved["confirmation"]["confirmed_by"] == "账务-甲"


def test_policy_change_appends_version_not_rewrite_history(client):
    """改策略只追加新版本；既有隔离行的处置快照与确认快照一字不动。"""
    q_before = {
        r["id"]: {
            "reason_code": r["reason_code"],
            "policy_at_quarantine": r["policy_at_quarantine"],
            "snapshot_version": r["snapshot_version"],
            "status": r["status"],
            "resolved_run_id": r["resolved_run_id"],
            "resolved_at": r["resolved_at"],
        }
        for r in client.get("/api/quarantine").json()
    }
    versions_before = client.get("/api/quarantine/policies/versions").json()["版本"]
    proof_count_before = len(
        client.get("/api/quarantine/proofs", params={"limit": 1000}).json()["证明"]
    )

    # 把所有已知原因码都改成一个不同策略，尽量触发「若有改史逻辑就会暴露」。
    for code, row in _policies(client).items():
        target = "自动可再配" if row["policy"] != "自动可再配" else "永久拒绝"
        r = client.put(
            f"/api/quarantine/policies/{code}",
            json={"policy": target, "updated_by": "账务-乙"},
        )
        assert r.status_code == 200, r.text

    q_after = {
        r["id"]: {
            "reason_code": r["reason_code"],
            "policy_at_quarantine": r["policy_at_quarantine"],
            "snapshot_version": r["snapshot_version"],
            "status": r["status"],
            "resolved_run_id": r["resolved_run_id"],
            "resolved_at": r["resolved_at"],
        }
        for r in client.get("/api/quarantine").json()
    }
    assert q_after == q_before, "改策略不得回溯改写历史隔离处置字段"

    # 版本台账只增不减，且每个码版本号递增。
    versions_after = client.get("/api/quarantine/policies/versions").json()["版本"]
    assert len(versions_after) > len(versions_before)
    for code in _policies(client):
        rows = [v for v in versions_after if v["reason_code"] == code]
        nums = sorted(v["version"] for v in rows)
        assert nums == list(range(1, len(nums) + 1)), "版本必须连续只追加"
        assert sum(1 for v in rows if v["is_current"]) == 1

    # 改策略本身不生产证明（证明只记录隔离决策），历史证明条数不变。
    proof_count_after = len(
        client.get("/api/quarantine/proofs", params={"limit": 1000}).json()["证明"]
    )
    assert proof_count_after == proof_count_before


def test_policy_change_only_affects_later_decisions(client):
    """改策略后：新决策按新策略执行；旧处置结果不被回写。"""
    code = "AMOUNT_INVALID"
    item = _by_key(client.get("/api/quarantine").json(), "REV-20261116-007")
    snapshot_before = (
        item["policy_at_quarantine"],
        item["snapshot_version"],
        item["status"],
    )

    # 改为永久拒绝（若当前不是），确保新决策是「拦下」。
    client.put(
        f"/api/quarantine/policies/{code}",
        json={"policy": "永久拒绝", "updated_by": "账务-丙"},
    )
    before_bal = _balances(client)
    run = client.post("/api/settle-runs").json()
    assert run["永久拒绝拦下"] >= 1
    assert _balances(client) == before_bal

    after = _by_key(client.get("/api/quarantine").json(), "REV-20261116-007")
    assert (after["policy_at_quarantine"], after["snapshot_version"], after["status"]) == snapshot_before
    assert after["policy"] == "永久拒绝"
    assert after["policy_version"] >= 2

    # 新决策落入策略证明，且钉的是新版本。
    proofs = client.get(
        "/api/quarantine/proofs", params={"quarantine_id": after["id"], "limit": 1000}
    ).json()["证明"]
    assert any(
        p["action"] == "永久拒绝拦下" and p["policy_version"] == after["policy_version"]
        for p in proofs
    )

    # 改回原策略，避免影响后续用例的语义预期（不核对，仅复位）。
    client.put(
        f"/api/quarantine/policies/{code}",
        json={"policy": snapshot_before[0], "updated_by": "账务-丙"},
    )


def test_policy_proofs_are_queryable_by_id_and_version(client):
    q = client.get("/api/quarantine").json()
    item = q[0]

    by_id = client.get(f"/api/quarantine/{item['id']}/proofs")
    assert by_id.status_code == 200
    proofs = by_id.json()["证明"]
    assert proofs, "每个隔离决策都应有策略证明"
    for p in proofs:
        assert p["quarantine_id"] == item["id"]
        assert p["quarantine_key"] == item["entry_key"]
        assert p["reason_code"]
        assert p["policy_version"] >= 1
        assert p["policy"] in ("自动可再配", "需确认后可再配", "永久拒绝")
        assert p["action"]
        assert p["proof_key"]

    version = proofs[0]["policy_version"]
    by_version = client.get(
        "/api/quarantine/proofs", params={"policy_version": version, "limit": 1000}
    ).json()
    assert by_version["证明"]
    assert all(p["policy_version"] == version for p in by_version["证明"])

    # 按隔离键查询。
    by_key = client.get(
        "/api/quarantine/proofs", params={"quarantine_key": item["entry_key"]}
    ).json()["证明"]
    assert by_key and all(p["quarantine_key"] == item["entry_key"] for p in by_key)


def test_proof_and_version_tables_reject_mutation(client):
    """数据库级护栏：证明与历史版本不可被 UPDATE / DELETE 改写。"""
    import sqlite3

    with connect() as conn:
        proof = conn.execute(
            "SELECT id FROM quarantine_policy_proofs ORDER BY id LIMIT 1"
        ).fetchone()
        ver = conn.execute(
            "SELECT id FROM quarantine_policies ORDER BY id LIMIT 1"
        ).fetchone()
        assert proof and ver
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE quarantine_policy_proofs SET action = '篡改' WHERE id = ?",
                (proof["id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "DELETE FROM quarantine_policy_proofs WHERE id = ?", (proof["id"],)
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE quarantine_policies SET policy = '永久拒绝' WHERE id = ?",
                (ver["id"],),
            )
        conn.rollback()


def test_resolved_rows_are_frozen(client):
    """已再配行的处置字段不可被直接 UPDATE（防绕过接口层改史）。"""
    import sqlite3

    resolved = [
        r for r in client.get("/api/quarantine").json() if r["status"] == "已再配"
    ]
    assert resolved, "前置用例应已产生已再配行"
    target = resolved[0]
    with connect() as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE quarantine_items SET status = '未平' WHERE id = ?",
                (target["id"],),
            )
        conn.rollback()


def test_verify_reports_consistent_and_share_buckets(client):
    report = client.post("/api/verify-reports")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["consistent"] == 1
    assert body["ledger_net_cents"] == body["balance_sum_cents"]
    buckets = {b["reason_code"]: b for b in body["原因分桶"]}
    policies = _policies(client)
    assert set(buckets) == set(policies)
    for code, b in buckets.items():
        assert b["policy"] == policies[code]["policy"]
        assert b["policy_version"] == policies[code]["version"]


def test_settle_violation_rolls_back_whole_run(client):
    """再配违例须整笔回滚：余额、隔离行、策略证明都不得留下半截状态。"""
    before_bal = _balances(client)
    before_q = client.get("/api/quarantine").json()
    before_proofs = client.get(
        "/api/quarantine/proofs", params={"limit": 1000}
    ).json()["证明"]
    assert any(r["status"] == "未平" for r in before_q), "需要未平项才能触发复扫决策"

    # 注入：本轮任何策略决策证明写入失败，归并必须整体回滚。
    with connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER tmp_fail_proof BEFORE INSERT ON quarantine_policy_proofs
            BEGIN SELECT RAISE(ABORT, '注入失败：证明写入违约'); END
            """
        )
        conn.commit()
    try:
        resp = client.post("/api/settle-runs")
        assert resp.status_code == 500
        assert "已整体回滚" in resp.text
    finally:
        with connect() as conn:
            conn.execute("DROP TRIGGER IF EXISTS tmp_fail_proof")
            conn.commit()

    assert _balances(client) == before_bal, "失败归并不得留下半截余额"
    after_q = client.get("/api/quarantine").json()
    assert [
        (r["id"], r["status"], r["policy_at_quarantine"], r["snapshot_version"],
         r["last_checked_run_id"], r["retry_count"])
        for r in after_q
    ] == [
        (r["id"], r["status"], r["policy_at_quarantine"], r["snapshot_version"],
         r["last_checked_run_id"], r["retry_count"])
        for r in before_q
    ], "失败归并不得留下半截隔离处置"
    assert (
        client.get("/api/quarantine/proofs", params={"limit": 1000}).json()["证明"]
        == before_proofs
    ), "失败归并不得留下半截策略证明"
    runs = client.get("/api/settle-runs").json()
    assert runs[0]["status"] == "失败"
