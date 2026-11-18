"""Показатели для руководства банка: сколько денег сохранено, сколько работы у сотрудников, насколько быстро
решаются апелляции и как часто система ошибается. Всё считается по случаям и переводам в базе.
"""
import json
import statistics
from datetime import datetime, timedelta

from . import engine

KEPT = ("returned", "cancelled", "withdrawn", "guardian_cancelled")   # остановленный перевод так и не ушёл


def _hours(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


def compute(conn, now: datetime, days: int = 30) -> dict:
    # Ровно `days` календарных дней, включая сегодняшний: тогда сумма по дням совпадает с итогом.
    since = datetime.combine(now.date() - timedelta(days=days - 1), datetime.min.time()).isoformat()
    stops = conn.execute(
        """SELECT k.*, t.amount FROM cases k JOIN transfers t ON t.id=k.transfer_id
           WHERE k.kind='intercept' AND k.created_at >= ?""", (since,)).fetchall()
    by = {}
    for k in stops:
        by.setdefault(k["status"], []).append(k)
    # Деньги не ушли получателю: вернули отправителю, отменил банк, клиент или его доверенное лицо.
    kept = [k for s in KEPT for k in by.get(s, [])]
    # Возвраты пострадавшим по жалобам считаются отдельно: это не «клиент сам вернул чужие деньги».
    not_refund = "id NOT IN (SELECT transfer_id FROM events WHERE action='refunded' AND transfer_id IS NOT NULL)"
    returns = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s FROM transfers
            WHERE return_of IS NOT NULL AND status='done' AND created_at >= ? AND {not_refund}""", (since,)).fetchone()
    protected = conn.execute(
        f"""SELECT COUNT(DISTINCT sender_id) AS n FROM transfers
            WHERE return_of IS NOT NULL AND status='done' AND created_at >= ? AND {not_refund}""",
        (since,)).fetchone()["n"]
    refunds = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(t.amount), 0) AS s FROM events e JOIN transfers t ON t.id=e.transfer_id
           WHERE e.action='refunded' AND e.at >= ?""", (since,)).fetchone()
    decided_stops = [k for k in stops if k["decided_at"]]
    wait = [_hours(k["created_at"], k["decided_at"]) for k in by.get("released", [])]

    blocks = conn.execute("SELECT * FROM cases WHERE kind='block' AND created_at >= ?", (since,)).fetchall()
    appeals = [b for b in blocks if b["submitted_at"]]
    decided = [b for b in appeals if b["decided_at"]]
    times = [_hours(b["submitted_at"], b["decided_at"]) for b in decided]
    in_time = [b for b in decided if b["decided_at"] <= b["due_at"]]
    overdue = [b for b in appeals if not b["decided_at"] and b["due_at"] < now.isoformat()]
    complaints = conn.execute("SELECT COUNT(*) AS n FROM complaints WHERE created_at >= ?", (since,)).fetchone()["n"]

    per_day = []
    for d in range(days - 1, -1, -1):
        day = (now - timedelta(days=d)).date().isoformat()
        amount = sum(k["amount"] for k in kept if k["created_at"][:10] == day)
        count = sum(1 for k in stops if k["created_at"][:10] == day)
        per_day.append({"day": day, "saved": amount, "stops": count})

    return {
        "days": days,
        "stops": len(stops),
        "saved_amount": sum(k["amount"] for k in kept),
        "kept_count": len(kept),
        "returned_count": returns["n"],
        "returned_amount": returns["s"],
        "protected_clients": protected,
        "self_resolved_share": round(100 * sum(len(by.get(k, [])) for k in ("returned", "withdrawn", "guardian_cancelled"))
                                     / len(stops), 1)
                               if stops else None,
        "analyst_stops": len(decided_stops),
        "released": len(by.get("released", [])),
        "released_share": round(100 * len(by.get("released", [])) / len(stops), 1) if stops else None,
        "release_wait_hours": round(statistics.median(wait), 1) if wait else None,
        "pending_stops": len(by.get("under_review", [])) + len(by.get("open", [])),
        "complaints": complaints,
        "victim_refunds": refunds["n"],
        "victim_refund_amount": refunds["s"],
        "partial_blocks": sum(b["scope"] == "partial" for b in blocks),
        "self_reports": conn.execute("SELECT COUNT(*) AS n FROM self_reports WHERE at >= ?", (since,)).fetchone()["n"],
        "guardian_cancelled": len(by.get("guardian_cancelled", [])),
        **_cooling_and_messages(conn, since),
        **_compensation(conn, since),
        **_loans(conn, since),
        **_rental(conn, since, now),
        "clearances": conn.execute("SELECT COUNT(*) AS n FROM clearances WHERE issued_at >= ?", (since,)).fetchone()["n"],
        "blocks": len(blocks),
        "appeals": len(appeals),
        "appeals_decided": len(decided),
        "appeal_hours_median": round(statistics.median(times), 1) if times else None,
        "appeals_in_time_share": round(100 * len(in_time) / len(decided), 1) if decided else None,
        "unblocked_share": round(100 * sum(b["status"] == "unblocked" for b in decided) / len(decided), 1) if decided else None,
        "appeals_overdue": len(overdue),
        "per_day": per_day,
    }


def linked_accounts(conn, min_sources: int = 2) -> list[dict]:
    """Счета, куда пытались отправить или отправили деньги клиенты из разных случаев.

    Одна карта в нескольких независимых случаях — почти всегда дроппер или организатор: невольные
    участники друг друга не знают, а «карта для возврата» у мошенника одна на многих.
    """
    rows = conn.execute(
        """SELECT dest, client_id, case_id, amount, at FROM (
             SELECT t.receiver_id AS dest, k.client_id, k.id AS case_id, t.amount, k.created_at AS at
             FROM cases k JOIN transfers t ON t.id=k.transfer_id
             UNION ALL
             SELECT k.client_id AS dest, i.sender_id AS client_id, k.id AS case_id, i.amount, k.created_at AS at
             FROM cases k JOIN transfers i ON i.id=k.incoming_transfer_id
             WHERE k.kind='block' AND k.transfer_id IS NULL)""").fetchall()
    acc = {}
    for r in rows:
        a = acc.setdefault(r["dest"], {"sources": set(), "cases": set(), "amount": 0, "last": ""})
        a["sources"].add(r["client_id"])
        a["cases"].add(r["case_id"])
        a["amount"] += r["amount"]
        a["last"] = max(a["last"], r["at"])
    out = []
    for dest, a in acc.items():
        if len(a["sources"]) < min_sources:
            continue
        c = conn.execute("SELECT id, name, status, account_opened_at FROM clients WHERE id=?", (dest,)).fetchone()
        out.append({"client_id": dest, "name": c["name"], "status": c["status"], "opened": c["account_opened_at"],
                    "sources": len(a["sources"]), "cases": sorted(a["cases"]), "amount": a["amount"], "last": a["last"]})
    return sorted(out, key=lambda x: (x["status"] == "blocked", -x["sources"], -x["amount"]))


def _compensation(conn, since: str) -> dict:
    """Сколько банку не пришлось бы возмещать (оценка).

    По закону о платежах банк возмещает клиенту перевод на счёт, который уже был в базе Антифрод-центра.
    Считаем переводы на такие счета, которые не ушли: остановленные движком, отклонённые по «чёрному» списку
    и отменённые в период охлаждения. Получатель должен быть в базе к моменту остановки или ухода денег.
    """
    q = ",".join("?" * len(KEPT))
    stops = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(t.amount),0) AS s FROM cases k JOIN transfers t ON t.id=k.transfer_id
            JOIN afc_list a ON a.client_id=t.receiver_id AND a.added_at <= k.created_at
            WHERE k.kind='intercept' AND k.created_at >= ? AND k.status IN ({q})""", (since, *KEPT)).fetchone()
    cooling = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(t.amount),0) AS s FROM transfers t
           JOIN afc_list a ON a.client_id=t.receiver_id AND a.added_at <= t.release_at
           WHERE t.release_at IS NOT NULL AND t.status='cancelled' AND t.created_at >= ?""", (since,)).fetchone()
    refused = [json.loads(r["details"] or "{}").get("amount", 0) for r in conn.execute(
        "SELECT details FROM events WHERE action='afc_refused' AND at >= ?", (since,))]
    return {"compensation_avoided": stops["s"] + cooling["s"] + sum(refused),
            "compensation_count": stops["n"] + cooling["n"] + len(refused),
            "compensation_refused": sum(refused)}


def _loans(conn, since: str) -> dict:
    """Схема «встречный кредит»: сколько переводов денег из свежего кредита остановлено и сколько кредитов вернули."""
    rows = conn.execute(
        """SELECT k.status, t.amount FROM cases k JOIN transfers t ON t.id=k.transfer_id
           WHERE k.kind='intercept' AND k.created_at >= ? AND k.reasons LIKE '%"loan_forward"%'""", (since,)).fetchall()
    return {"loan_stops": len(rows), "loan_saved": sum(r["amount"] for r in rows if r["status"] in KEPT),
            "loans_repaid": conn.execute("SELECT COUNT(*) AS n FROM loans WHERE repaid_at >= ?", (since,)).fetchone()["n"]}


def _rental(conn, since: str, now: datetime) -> dict:
    """«Аренда ИП»: сколько счетов ИП сейчас с признаками, сколько переводов остановлено и счетов приостановлено."""
    count = lambda kind: conn.execute(   # noqa: E731
        f"""SELECT COUNT(*) AS n FROM cases WHERE kind='{kind}' AND created_at >= ? AND reasons LIKE '%"ip_rental"%'""",
        (since,)).fetchone()["n"]
    accounts = sum(1 for r in conn.execute("SELECT id FROM clients WHERE kind='ip'").fetchall()
                   if engine.rental_signal(conn, r["id"], now))
    return {"rental_accounts": accounts, "rental_stops": count("intercept"), "rental_suspended": count("block")}


def _cooling_and_messages(conn, since: str) -> dict:
    """Период охлаждения и проверки сообщений: сколько крупных первых переводов отменили, что узнавали в сообщениях."""
    c = conn.execute("""SELECT COUNT(*) AS n, COALESCE(SUM(status='cancelled'),0) AS k,
                               COALESCE(SUM(CASE WHEN status='cancelled' THEN amount END),0) AS s
                        FROM transfers WHERE release_at IS NOT NULL AND created_at >= ?""", (since,)).fetchone()
    m = {r["scheme"] or r["verdict"]: r["n"] for r in conn.execute(
        """SELECT scheme, verdict, COUNT(*) AS n FROM message_checks WHERE at >= ? GROUP BY scheme, verdict""", (since,))}
    return {"cooling": c["n"], "cooling_cancelled": c["k"], "cooling_saved": c["s"],
            "message_checks": sum(m.values()), "message_danger": sum(v for k, v in m.items() if k not in ("caution", "none")),
            "message_schemes": m}
