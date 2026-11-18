"""Честная оценка качества движка на нескольких наборах синтетических данных.

Запуск:  python -m app.quality
Пишет app/static/quality.json, который показывает страница /quality.
Каждый набор данных создаётся с другим зерном случайности, поэтому видно не одно удачное число, а разброс.
"""
import json
import os
import statistics
import tempfile
import time
from datetime import datetime, timedelta

from . import clock, engine, seed

SEEDS = range(1, 11)
THRESHOLDS = list(range(40, 101, 5))
OUT = os.path.join(os.path.dirname(__file__), "static", "quality.json")

SCENARIOS = {
    "tri_out": ("Схема", "Переслал всю сумму одним переводом"),
    "tri_split": ("Схема", "Раздробил сумму на две части"),
    "tri_late": ("Схема", "Переслал через 3–4 дня"),
    "benign_supplier": ("Честный", "Получил оплату и заплатил знакомому поставщику"),
    "benign_new": ("Честный", "Получил от незнакомца и заплатил новому человеку"),
    "benign_return": ("Честный", "Вернул деньги тому же отправителю"),
    "normal": ("Честный", "Обычный перевод знакомому"),
}


# Счета ИП: какие правила «аренды ИП» отметили и какие платежи ИП остановил движок.
BIZ_ACCOUNTS = {
    "rent_quiet": ("Арендованный", "ИП почти не работал, потом пошёл транзит"),
    "rent_young": ("Арендованный", "ИП открыт недавно, сразу транзит"),
    "rent_active": ("Арендованный", "До аренды работал как обычный магазин"),
    # Честные ИП
    "shop": ("Честный", "Магазин: много покупателей, платит поставщикам"),
    "agent": ("Честный", "Посредник: платит постоянным частным поставщикам"),
    "buyer": ("Честный", "Скупка: каждый раз платит новым частным лицам"),
    "new_shop": ("Честный", "Новый магазин, однажды заплатил частному лицу за ремонт"),
}
BIZ_PAYMENTS = {
    "ipo_rent_quiet": ("Арендованный", "Транзит: ИП почти не работал"),
    "ipo_rent_young": ("Арендованный", "Транзит: ИП открыт недавно"),
    "ipo_rent_active": ("Арендованный", "Транзит: до аренды работал как магазин"),
    "ipo_supplier": ("Честный", "Оплата поставщику"),
    "ipo_owner": ("Честный", "Перевод владельцу ИП"),
    "ipo_agent": ("Честный", "Оплата постоянному частному поставщику"),
    "ipo_buyer": ("Честный", "Скупка: оплата новому частному лицу"),
    "ipo_contractor": ("Честный", "Новый магазин: оплата частному лицу за ремонт"),
}


# «Касса онлайн-казино» на частных лицах.
CASINO_ACCOUNTS = {
    "casino": ("Касса казино", "Ставки игроков приходят, выигрыши уходят другим игрокам"),
    "seller": ("Честный", "Продавец: много покупателей, платит поставщикам"),
    "collector": ("Честный", "Сбор денег на поездку: всё уходит турагентству"),
    "brigadier": ("Честный", "Бригадир: платит многим рабочим, получает от двух заказчиков"),
    "reseller": ("Честный", "Перекупщик билетов: покупает у одних, продаёт другим"),
}
CASINO_PAYMENTS = {
    "cso_payout": ("Касса казино", "Выплата выигрыша игроку"),
    "cso_forward": ("Касса казино", "Перевод организатору"),
    "cso_supplier": ("Честный", "Продавец: оплата поставщику"),
    "cso_agency": ("Честный", "Сбор: оплата турагентству"),
    "cso_worker": ("Честный", "Бригадир: оплата рабочему"),
    "cso_resell": ("Честный", "Перекупщик: покупка билета у частного лица"),
}


# Устройства входа: какие из них правило «дроп-ферма» отмечает.
DEVICES = {
    "farm": ("Дроп-ферма", "Карты нескольких дропперов на одном телефоне"),
    "family": ("Честный", "Семья: 2–3 давних счёта на общем планшете"),
    "dorm": ("Честный", "Общежитие: 3 студента с новыми счетами на одном ноутбуке"),
}


def held(score: int, has_incoming: bool, threshold: int) -> bool:
    return has_incoming and score >= threshold


def business_flags(conn, labels: dict, now: datetime, days: int = seed.EVAL_DAYS) -> dict:
    """Какие ИП правила «аренды» отметили хотя бы раз за последние дни (проверка в конце каждого дня)."""
    return {cid: any(engine.rental_signal(conn, cid, now - timedelta(days=d)) for d in range(days))
            for cid, label in labels.items() if label in BIZ_ACCOUNTS}


def casino_flags(conn, labels: dict, now: datetime, days: int = seed.EVAL_DAYS) -> dict:
    """Какие частные лица правила «кассы казино» отметили хотя бы раз (проверка в конце каждого дня)."""
    return {cid: any(engine.casino_signal(conn, cid, now - timedelta(days=d)) for d in range(days))
            for cid, label in labels.items() if label in CASINO_ACCOUNTS}


def farm_flags(conn, labels: dict, now: datetime) -> list[tuple]:
    """(тип устройства, отмечено ли как дроп-ферма) для синтетических устройств."""
    found = {f["device_id"] for f in engine.farm_devices(conn, now)}
    return [(label, did in found) for did, label in labels.items()]


def _table(items: list, titles: dict, flagged: str | None = None) -> list[dict]:
    """Сводка по группам: для счетов — сколько отмечено, для платежей — сколько остановлено и на какую сумму."""
    out = []
    for k, (g, title) in titles.items():
        rows = [x for x in items if x[0] == k]
        if flagged:
            out.append({"key": k, "group": g, "title": title, "total": len(rows), "flagged": sum(1 for _, f in rows if f)})
        else:
            out.append({"key": k, "group": g, "title": title, "total": len(rows), "stopped": sum(1 for _, h, _ in rows if h),
                        "amount": sum(a for _, _, a in rows), "amount_stopped": sum(a for _, h, a in rows if h)})
    return out


def run() -> dict:
    tmp = tempfile.mkdtemp()
    per_seed, accounts, payments, cs_accounts, cs_payments, devices = [], [], [], [], [], []
    for s in SEEDS:
        conn, stats = seed.build(os.path.join(tmp, f"q{s}.db"), seed=s, verbose=False)
        flags = business_flags(conn, stats["ip_labels"], stats["now"])
        cflags = casino_flags(conn, stats["cs_labels"], stats["now"])
        devices += farm_flags(conn, stats["device_labels"], stats["now"])
        conn.close()
        per_seed.append(stats["scored"])
        accounts += [(stats["ip_labels"][cid], f) for cid, f in flags.items()]
        payments += stats["ip_scored"]
        cs_accounts += [(stats["cs_labels"][cid], f) for cid, f in cflags.items()]
        cs_payments += stats["cs_scored"]

    curve = []
    for t in THRESHOLDS:
        recall, false_rate, precision = [], [], []
        for scored in per_seed:
            scheme = [held(sc, inc, t) for sc, inc, scen in scored if scen.startswith("tri_")]
            honest = [held(sc, inc, t) for sc, inc, scen in scored if not scen.startswith("tri_")]
            caught, false = sum(scheme), sum(honest)
            recall.append(100 * caught / len(scheme))
            false_rate.append(100 * false / len(honest))
            precision.append(100 * caught / max(1, caught + false))
        curve.append({
            "threshold": t,
            "recall": [round(min(recall), 1), round(statistics.mean(recall), 1), round(max(recall), 1)],
            "false_rate": [round(min(false_rate), 2), round(statistics.mean(false_rate), 2), round(max(false_rate), 2)],
            "precision": [round(min(precision), 1), round(statistics.mean(precision), 1), round(max(precision), 1)],
        })

    by_scenario = []
    for key, (group, title) in SCENARIOS.items():
        rows = [(sc, inc) for scored in per_seed for sc, inc, scen in scored if scen == key]
        stopped = sum(held(sc, inc, engine.HOLD_THRESHOLD) for sc, inc in rows)
        by_scenario.append({"key": key, "group": group, "title": title, "total": len(rows), "stopped": stopped})

    return {
        "generated_at": clock.now().isoformat(),
        "seeds": len(SEEDS),
        "threshold": engine.HOLD_THRESHOLD,
        "transfers_per_seed": round(statistics.mean(len(s) for s in per_seed)),
        "curve": curve,
        "by_scenario": by_scenario,
        "business": {
            "accounts": [{"key": k, "group": g, "title": title, "total": sum(1 for lb, _ in accounts if lb == k),
                          "flagged": sum(1 for lb, f in accounts if lb == k and f)} for k, (g, title) in BIZ_ACCOUNTS.items()],
            "payments": [{"key": k, "group": g, "title": title, "total": sum(1 for sc, _, _ in payments if sc == k),
                          "stopped": sum(1 for sc, h, _ in payments if sc == k and h),
                          "amount": sum(a for sc, _, a in payments if sc == k),
                          "amount_stopped": sum(a for sc, h, a in payments if sc == k and h)}
                         for k, (g, title) in BIZ_PAYMENTS.items()],
        },
        "casino": {"accounts": _table(cs_accounts, CASINO_ACCOUNTS, flagged="flagged"),
                   "payments": _table(cs_payments, CASINO_PAYMENTS)},
        "devices": _table(devices, DEVICES, flagged="flagged"),
    }


if __name__ == "__main__":
    started = time.time()
    data = run()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    cur = next(c for c in data["curve"] if c["threshold"] == data["threshold"])
    print(f"Наборов данных: {data['seeds']}, порог {data['threshold']}, {time.time() - started:.0f} с")
    print(f"Ловит схем: {cur['recall'][0]}–{cur['recall'][2]}% (в среднем {cur['recall'][1]}%)")
    print(f"Зря останавливает честных: {cur['false_rate'][0]}–{cur['false_rate'][2]}%")
    print(f"Среди остановок настоящих схем: {cur['precision'][0]}–{cur['precision'][2]}%")
    for r in data["by_scenario"]:
        print(f"  {r['group']:8} {r['title']}: остановлено {r['stopped']} из {r['total']}")
    print("Аренда ИП, счета:")
    for r in data["business"]["accounts"]:
        print(f"  {r['group']:12} {r['title']}: отмечено {r['flagged']} из {r['total']}")
    print("Аренда ИП, платежи:")
    for r in data["business"]["payments"]:
        print(f"  {r['group']:12} {r['title']}: остановлено {r['stopped']} из {r['total']}")
    print("Касса казино, счета:")
    for r in data["casino"]["accounts"]:
        print(f"  {r['group']:12} {r['title']}: отмечено {r['flagged']} из {r['total']}")
    print("Касса казино, платежи:")
    for r in data["casino"]["payments"]:
        print(f"  {r['group']:12} {r['title']}: остановлено {r['stopped']} из {r['total']}")
    print("Дроп-фермы, устройства:")
    for r in data["devices"]:
        print(f"  {r['group']:12} {r['title']}: отмечено {r['flagged']} из {r['total']}")
