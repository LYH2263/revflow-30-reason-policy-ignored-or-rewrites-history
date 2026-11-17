from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules import quarantine_policy as qp


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# —— 策略门禁 ——
#
# 决策路径：隔离行在「入隔离」时钉死策略版本（policy_at_quarantine +
# policy_version），此后策略变更**不改写该行**；每轮复扫是一次新决策，按当前
# （各码最大版本）策略执行，并追加一条策略证明。旧决策行与已再配行永不被回溯。


def _policy_row(policy_map: dict[str, dict], code: str) -> dict:
    """当前策略行；查不到时 fail-closed 视为「需确认」，绝不放行。"""
    row = policy_map.get(code)
    if not row or not qp.is_valid_policy(row["policy"]):
        return {
            "reason_code": code,
            "policy": qp.POLICY_CONFIRM,
            "version": 0,
        }
    return row


def _current_policy(policy_map: dict[str, dict], code: str) -> str:
    return _policy_row(policy_map, code)["policy"]


def _has_confirmation(conn, quarantine_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM quarantine_confirmations WHERE quarantine_id = ?",
        (quarantine_id,),
    ).fetchone()
    return row is not None


def _gate(conn, policy_map: dict[str, dict], item: dict) -> tuple[dict, bool]:
    """返回 (当前策略行, 是否允许本轮尝试配平)。

    - 自动可再配：放行。
    - 需确认后可再配：登记确认后才放行；无确认一律拦住。
    - 永久拒绝：永不放行，永不触达余额。
    - 未知 / 非法策略：fail-closed，按需确认处理。
    """
    row = _policy_row(policy_map, item["reason_code"])
    policy = row["policy"]
    if policy == qp.POLICY_AUTO:
        return row, True
    if policy == qp.POLICY_PERMANENT:
        return row, False
    # 需确认（含 fail-closed 兜底）必须有人工确认登记。
    return row, _has_confirmation(conn, item["id"])


# —— 原因分桶（隔离 / 策略同源） ——


def reason_buckets(conn, settle_run_id: int | None = None) -> list[dict]:
    """按原因码分桶：全部由 quarantine_items 与 quarantine_policies 现算。"""
    policies = qp.policy_map(conn)
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT
              q.reason_code AS code,
              COUNT(*) AS total,
              SUM(CASE WHEN q.status = '未平' THEN 1 ELSE 0 END) AS open_count,
              SUM(CASE WHEN q.status = '未平' THEN q.amount_cents ELSE 0 END) AS open_amount_cents,
              SUM(CASE WHEN q.status = '已再配' THEN 1 ELSE 0 END) AS resolved_count,
              SUM(CASE WHEN q.settle_run_id = ? THEN 1 ELSE 0 END) AS new_count,
              SUM(CASE WHEN q.resolved_run_id = ? THEN 1 ELSE 0 END) AS resolved_this_run,
              SUM(
                CASE WHEN q.status = '未平'
                  AND EXISTS (
                    SELECT 1 FROM quarantine_confirmations c
                    WHERE c.quarantine_id = q.id
                  )
                THEN 1 ELSE 0 END
              ) AS confirmed_open
            FROM quarantine_items q
            GROUP BY q.reason_code
            """,
            (settle_run_id or -1, settle_run_id or -1),
        ).fetchall()
    )
    by_code = {r["code"]: r for r in rows}
    out = []
    for code, meta in sorted(qp.REASON_BY_CODE.items(), key=lambda kv: kv[1]["sort_order"]):
        r = by_code.pop(code, {})
        row = _policy_row(policies, code)
        out.append(
            {
                "reason_code": code,
                "label": meta["label"],
                "policy": row["policy"],
                "policy_version": row["version"],
                "total": r.get("total", 0) or 0,
                "open_count": r.get("open_count", 0) or 0,
                "open_amount_cents": r.get("open_amount_cents", 0) or 0,
                "resolved_count": r.get("resolved_count", 0) or 0,
                "new_count": r.get("new_count", 0) or 0,
                "resolved_this_run": r.get("resolved_this_run", 0) or 0,
                "confirmed_open": r.get("confirmed_open", 0) or 0,
            }
        )
    # 未知原因码（理论上不应出现）也如实分桶，避免静默吞数。
    for code, r in by_code.items():
        r = dict(r)
        row = _policy_row(policies, code)
        r["reason_code"] = code
        r["label"] = code
        r["policy"] = row["policy"]
        r["policy_version"] = row["version"]
        out.append(r)
    return out


def _buckets_json(conn, settle_run_id: int | None = None) -> str:
    return json.dumps(reason_buckets(conn, settle_run_id), ensure_ascii=False)


# —— 查询 ——


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE status = '未平'"
        ).fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "原因分桶": reason_buckets(conn),
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine() -> list[dict]:
    """每条隔离带原因码、当前策略、钉死快照与确认标记。

    展示的 `policy` / `policy_version` 是策略表**当前**值，用于「下一轮会怎么判」；
    `policy_at_quarantine` / `snapshot_version` 是入隔离时钉死的快照，二者分列，
    便于核对「改策略不改史」。
    """
    with connect() as conn:
        policies = qp.policy_map(conn)
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT q.*, a.code AS account_code, a.name AS account_name
                FROM quarantine_items q
                JOIN accounts a ON a.id = q.account_id
                ORDER BY q.id DESC
                """
            ).fetchall()
        )
        for r in rows:
            row = _policy_row(policies, r["reason_code"])
            r["reason_label"] = qp.label(r["reason_code"])
            # 先留入隔离时钉死的快照，再给出「当前」策略（用于下一轮会怎么判）。
            r["snapshot_version"] = r["policy_version"]
            r["policy"] = row["policy"]
            r["policy_version"] = row["version"]
            conf = conn.execute(
                """
                SELECT * FROM quarantine_confirmations
                WHERE quarantine_id = ?
                """,
                (r["id"],),
            ).fetchone()
            r["confirmation"] = dict(conf) if conf else None
            r["can_confirm"] = (
                r["policy"] == qp.POLICY_CONFIRM
                and r["status"] == "未平"
                and conf is None
            )
        return rows


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )
        for r in rows:
            r["原因分桶"] = json.loads(r.pop("reason_buckets_json") or "[]")
            r["detail"] = json.loads(r.get("detail_json") or "{}")
        return rows


# —— 策略、版本与证明（只读） ——


def list_policies() -> dict:
    with connect() as conn:
        rows = qp.list_policies(conn)
        buckets = {b["reason_code"]: b for b in reason_buckets(conn)}
        for r in rows:
            b = buckets.get(r["reason_code"], {})
            r["open_count"] = b.get("open_count", 0)
            r["open_amount_cents"] = b.get("open_amount_cents", 0)
        return {"策略": rows, "可选策略": list(qp.ALL_POLICIES)}


def list_policy_versions(code: str | None = None) -> dict:
    """只读版本台账：策略表按原因码 + 版本的全部历史。"""
    with connect() as conn:
        if code and code not in qp.REASON_BY_CODE:
            raise HTTPException(404, f"原因码不存在：{code}")
        return {"版本": qp.list_policy_versions(conn, code)}


def list_policy_proofs(
    quarantine_id: int | None = None,
    policy_version: int | None = None,
    reason_code: str | None = None,
    quarantine_key: str | None = None,
    action: str | None = None,
    limit: int = 200,
) -> dict:
    """只读策略证明：可按隔离 id / 策略版本（及原因码/隔离键/动作）查询。"""
    with connect() as conn:
        rows = qp.list_proofs(
            conn,
            quarantine_id=quarantine_id,
            policy_version=policy_version,
            reason_code=reason_code,
            quarantine_key=quarantine_key,
            action=action,
            limit=limit,
        )
        return {"证明": rows, "条数": len(rows)}


def update_policy(code: str, policy: str, updated_by: str) -> dict:
    """改策略 = 追加新版本，只影响变更后新决策；不触碰任何隔离/确认历史行。"""
    with connect() as conn:
        try:
            row = qp.update_policy(conn, code, policy, updated_by, _now())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        conn.commit()
        return row


def confirm_quarantine(quarantine_id: int, confirmed_by: str, note: str = "") -> dict:
    with connect() as conn:
        item = conn.execute(
            "SELECT * FROM quarantine_items WHERE id = ?", (quarantine_id,)
        ).fetchone()
        if not item:
            raise HTTPException(404, "隔离项不存在")
        if item["status"] != "未平":
            raise HTTPException(409, f"该隔离项已处理（{item['status']}），无需确认")
        row = _policy_row(qp.policy_map(conn), item["reason_code"])
        policy = row["policy"]
        if policy != qp.POLICY_CONFIRM:
            raise HTTPException(
                409, f"该原因码当前策略为「{policy}」，不接受确认标记"
            )
        exists = _has_confirmation(conn, quarantine_id)
        if exists:
            raise HTTPException(409, "该隔离项已有确认，不可重复确认")
        now = _now()
        conn.execute(
            """
            INSERT INTO quarantine_confirmations(
              quarantine_id, policy_at_confirm, policy_version, note, confirmed_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                quarantine_id,
                policy,
                row["version"],
                note or "",
                confirmed_by or "未署名",
                now,
            ),
        )
        qp.append_proof(
            conn,
            quarantine_id=quarantine_id,
            quarantine_key=item["entry_key"],
            reason_code=item["reason_code"],
            policy_version=row["version"],
            policy=policy,
            action=qp.ACTION_CONFIRM,
            settle_run_id=None,
            now=now,
            detail=f"确认人 {confirmed_by or '未署名'}",
        )
        conn.commit()
        row_out = conn.execute(
            """
            SELECT c.*, q.reason_code, q.reason, q.amount_cents, q.status
            FROM quarantine_confirmations c
            JOIN quarantine_items q ON q.id = c.quarantine_id
            WHERE c.quarantine_id = ?
            """,
            (quarantine_id,),
        ).fetchone()
        return dict(row_out)


# —— 归并 ——


def _insert_quarantine(
    conn,
    *,
    entry_key: str,
    account_id: int,
    code: str,
    amount_cents: int,
    run_id: int,
    now: str,
    policy_map: dict[str, dict],
) -> int:
    """落一条隔离，并钉死当轮策略快照 + 追加「隔离」证明。"""
    row = _policy_row(policy_map, code)
    cur = conn.execute(
        """
        INSERT INTO quarantine_items(
          entry_key, account_id, reason_code, reason, policy_at_quarantine,
          policy_version, amount_cents, status, retry_count, settle_run_id,
          last_checked_run_id, last_checked_policy_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '未平', 0, ?, ?, ?, ?)
        """,
        (
            entry_key,
            account_id,
            code,
            qp.reason_text(code),
            row["policy"],
            row["version"],
            amount_cents,
            run_id,
            run_id,
            row["version"],
            now,
        ),
    )
    qid = cur.lastrowid
    qp.append_proof(
        conn,
        quarantine_id=qid,
        quarantine_key=entry_key,
        reason_code=code,
        policy_version=row["version"],
        policy=row["policy"],
        action=qp.ACTION_QUARANTINE,
        settle_run_id=run_id,
        now=now,
        detail=f"入隔离，金额 {amount_cents} 分",
    )
    return qid


def _judge(
    conn,
    reversal: dict,
    fwd_by_key: dict[str, dict],
    used: dict[str, int],
    allow_account_mismatch: bool = False,
) -> tuple[str | None, dict | None]:
    """判定一条冲正：返回 (失败原因码 | None, 命中的原单)。

    账户不一致仅在人工确认后才放行；放行后仍按「配对」处理——冲销额记在
    原单账户，避免跨账户凭空搬运。
    """
    if reversal["amount_cents"] <= 0:
        return "AMOUNT_INVALID", None
    target = reversal.get("target_entry_key")
    if not target or target not in fwd_by_key:
        return "NO_ORIGINAL", None
    fwd = fwd_by_key[target]
    if fwd["account_id"] != reversal["account_id"] and not allow_account_mismatch:
        return "ACCOUNT_MISMATCH", fwd
    remain = fwd["amount_cents"] - used.get(target, 0)
    if reversal["amount_cents"] > remain:
        return "OVER_EXCESS", fwd
    return None, fwd


def _record_failed_run(conn, error: str, now: str) -> None:
    conn.execute(
        """
        INSERT INTO settle_runs(status, started_at, finished_at, error_text)
        VALUES ('失败', ?, ?, ?)
        """,
        (now, now, error[:1000]),
    )


def run_settle() -> dict:
    """一轮归并：已归并冲正额度 → 隔离项按原因码策略门禁 → 配平并落余额。

    策略门禁与配平同处一个事务：任一失败整体回滚，余额、隔离、证明、运行记录
    都不会出现「策略已执行但余额未配」的半截状态。

    决策路径：每轮复扫都按**当前**策略版本作新决策，旧隔离行只在仍是「未平」
    时更新本轮检查信息；一旦「已再配」，其处置字段被触发器钉死，永不再改。
    """
    conn = connect()
    now = _now()
    run_id: int | None = None
    try:
        policy_map = qp.policy_map(conn)
        if not policy_map:
            raise RuntimeError("原因码策略表为空，拒绝归并")

        cur = conn.execute(
            "INSERT INTO settle_runs(status, started_at) VALUES ('进行中', ?)", (now,)
        )
        run_id = cur.lastrowid

        forwards = rows_to_dicts(
            conn.execute("SELECT * FROM ledger_entries WHERE kind = '正向' ORDER BY id").fetchall()
        )
        pending_forwards = [f for f in forwards if f["status"] == "待归并"]
        fwd_by_key = {f["entry_key"]: f for f in forwards}

        # 历史已归并冲正占用额度，必须跨轮累计，否则再配会重复冲销。
        used: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT target_entry_key AS k, SUM(amount_cents) AS s
            FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND target_entry_key IS NOT NULL
            GROUP BY target_entry_key
            """
        ).fetchall():
            used[row["k"]] = row["s"] or 0

        pending_reversals = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status IN ('待归并', '已隔离') ORDER BY id"
            ).fetchall()
        )
        quarantine_by_entry = {
            r["entry_key"]: r
            for r in rows_to_dicts(
                conn.execute(
                    "SELECT * FROM quarantine_items WHERE status = '未平' ORDER BY id"
                ).fetchall()
            )
        }

        account_delta: dict[int, int] = {}
        settled = 0
        quarantined_new = 0
        settled_from_quarantine = 0
        blocked_permanent = 0
        blocked_confirm = 0

        # 正向：先入账（仅待归并），历史正向不重复入账。
        for f in pending_forwards:
            account_delta[f["account_id"]] = (
                account_delta.get(f["account_id"], 0) + f["amount_cents"]
            )
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, f["id"]),
            )
            settled += 1

        for r in pending_reversals:
            item = quarantine_by_entry.get(r["entry_key"])
            rechecking = item is not None
            if rechecking:
                policy_row, allowed = _gate(conn, policy_map, item)
                if not allowed:
                    if policy_row["policy"] == qp.POLICY_PERMANENT:
                        blocked_permanent += 1
                        action = qp.ACTION_BLOCK_PERMANENT
                    else:
                        blocked_confirm += 1
                        action = qp.ACTION_BLOCK_CONFIRM
                    # 禁门：只更新「本轮检查过」的元数据，处置字段（status /
                    # policy_at_quarantine / resolved_*）一律不动。
                    conn.execute(
                        """
                        UPDATE quarantine_items
                        SET last_checked_run_id = ?, last_checked_policy_version = ?,
                            retry_count = retry_count + 1
                        WHERE id = ?
                        """,
                        (run_id, policy_row["version"], item["id"]),
                    )
                    qp.append_proof(
                        conn,
                        quarantine_id=item["id"],
                        quarantine_key=item["entry_key"],
                        reason_code=item["reason_code"],
                        policy_version=policy_row["version"],
                        policy=policy_row["policy"],
                        action=action,
                        settle_run_id=run_id,
                        now=now,
                        detail=f"策略「{policy_row['policy']}」拦下，未触达余额",
                    )
                    continue
                if r["status"] != "已隔离":
                    # 隔离项对应流水已不在隔离态：不猜测，留给人工。
                    continue

            code, fwd = _judge(
                conn,
                r,
                fwd_by_key,
                used,
                allow_account_mismatch=bool(
                    rechecking and item["reason_code"] == "ACCOUNT_MISMATCH"
                ),
            )
            if code is None and fwd is not None:
                target = fwd["entry_key"]
                used[target] = used.get(target, 0) + r["amount_cents"]
                # 冲销额落在原单账户：已确认的账户不一致也不跨账户搬运余额。
                hit_account = fwd["account_id"]
                account_delta[hit_account] = (
                    account_delta.get(hit_account, 0) - r["amount_cents"]
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                settled += 1
                if rechecking:
                    policy_row = _policy_row(policy_map, item["reason_code"])
                    conn.execute(
                        """
                        UPDATE quarantine_items
                        SET status = '已再配', resolved_run_id = ?, resolved_at = ?,
                            last_checked_run_id = ?, last_checked_policy_version = ?
                        WHERE id = ? AND status = '未平'
                        """,
                        (
                            run_id,
                            now,
                            run_id,
                            policy_row["version"],
                            item["id"],
                        ),
                    )
                    qp.append_proof(
                        conn,
                        quarantine_id=item["id"],
                        quarantine_key=item["entry_key"],
                        reason_code=item["reason_code"],
                        policy_version=policy_row["version"],
                        policy=policy_row["policy"],
                        action=qp.ACTION_REBALANCE,
                        settle_run_id=run_id,
                        now=now,
                        detail=f"再配 {r['amount_cents']} 分，入原单账户 #{hit_account}",
                    )
                    settled_from_quarantine += 1
                continue

            # 未能配平
            if rechecking:
                new_row = _policy_row(policy_map, code)
                changed = code != item["reason_code"]
                if changed:
                    # 改判是一次**新决策**：为它钉死新的策略版本，旧决策仍可由
                    # 只追加的策略证明回溯。策略变更本身不会走到这里。
                    conn.execute(
                        """
                        UPDATE quarantine_items
                        SET reason_code = ?, reason = ?, policy_at_quarantine = ?,
                            policy_version = ?, retry_count = retry_count + 1,
                            last_checked_run_id = ?, last_checked_policy_version = ?
                        WHERE id = ?
                        """,
                        (
                            code,
                            qp.reason_text(code),
                            new_row["policy"],
                            new_row["version"],
                            run_id,
                            new_row["version"],
                            item["id"],
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE quarantine_items
                        SET retry_count = retry_count + 1,
                            last_checked_run_id = ?, last_checked_policy_version = ?
                        WHERE id = ?
                        """,
                        (run_id, new_row["version"], item["id"]),
                    )
                qp.append_proof(
                    conn,
                    quarantine_id=item["id"],
                    quarantine_key=item["entry_key"],
                    reason_code=code,
                    policy_version=new_row["version"],
                    policy=new_row["policy"],
                    action=(
                        qp.ACTION_RECLASSIFY if changed else qp.ACTION_RECHECK_FAILED
                    ),
                    settle_run_id=run_id,
                    now=now,
                    detail=(
                        f"原因码 {item['reason_code']} → {code}"
                        if changed
                        else "复扫仍未配平，保持未平"
                    ),
                )
            else:
                _insert_quarantine(
                    conn,
                    entry_key=r["entry_key"],
                    account_id=r["account_id"],
                    code=code,
                    amount_cents=r["amount_cents"],
                    run_id=run_id,
                    now=now,
                    policy_map=policy_map,
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined_new += 1

        for acc_id, delta in account_delta.items():
            if delta == 0:
                conn.execute(
                    "UPDATE accounts SET last_settle_run_id = ? WHERE id = ?",
                    (run_id, acc_id),
                )
                continue
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ?, last_settle_run_id = ? WHERE id = ?",
                (delta, run_id, acc_id),
            )

        conn.execute(
            """
            UPDATE settle_runs
            SET status = '完成',
                forward_count = ?,
                reversal_count = ?,
                settled_count = ?,
                quarantine_count = ?,
                net_cents = ?,
                reason_buckets_json = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (
                len(pending_forwards),
                len(pending_reversals),
                settled,
                quarantined_new,
                sum(account_delta.values()),
                _buckets_json(conn, run_id),
                _now(),
                run_id,
            ),
        )
        conn.commit()
    except Exception as exc:  # 策略执行与配平同一失败闭
        conn.rollback()
        try:
            _record_failed_run(conn, str(exc), _now())
            conn.commit()
        except Exception:
            conn.rollback()
        raise HTTPException(500, f"归并失败，已整体回滚：{exc}") from exc
    finally:
        conn.close()

    out = get_settle_run(run_id)
    out["本次新增隔离"] = quarantined_new
    out["本次再配"] = settled_from_quarantine
    out["永久拒绝拦下"] = blocked_permanent
    out["待确认拦下"] = blocked_confirm
    return out


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        out = dict(row)
        buckets = json.loads(out.get("reason_buckets_json") or "[]")
        out["原因分桶"] = buckets
        out.pop("reason_buckets_json", None)
        return out


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        all_bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]

        buckets = reason_buckets(conn, settle_run_id)
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE status = '未平'"
        ).fetchone()["c"]
        q_this_run = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]
        permanent_open = sum(
            b["open_count"] for b in buckets if b["policy"] == qp.POLICY_PERMANENT
        )
        confirm_open = sum(
            b["open_count"] for b in buckets if b["policy"] == qp.POLICY_CONFIRM
        )

        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev

        # 永久拒绝项必须零改动余额；此处给出可核对的同源口径。
        permanent_amount = sum(
            b["open_amount_cents"] for b in buckets if b["policy"] == qp.POLICY_PERMANENT
        )

        consistent = 1 if all_ledger_net == all_bal else 0
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "未平隔离数": q_count,
            "本轮新增隔离数": q_this_run,
            "永久拒绝未平数": permanent_open,
            "待确认未平数": confirm_open,
            "永久拒绝隔离金额分": permanent_amount,
            "原因分桶": buckets,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, reason_buckets_json, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                json.dumps(buckets, ensure_ascii=False),
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        out["原因分桶"] = buckets
        out.pop("reason_buckets_json", None)
        return out
