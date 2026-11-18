"""Генератор синтетических данных и проверка движка.

Запуск:  python -m app.seed
Создаёт data.db, проигрывает все переводы по порядку через движок
и печатает, сколько схем поймано и сколько честных переводов остановлено зря.
"""
import json
import os
import random
from datetime import datetime, timedelta

from . import clock, db, engine

N_CLIENTS = 300
N_DROPPERS = 12
DAYS = 90          # длина истории
EVAL_DAYS = 45     # движок оцениваем только на последних днях: до этого копится история связей

FIRST = ["Айдана", "Нурлан", "Дина", "Ерлан", "Алия", "Тимур", "Мадина", "Арман", "Сауле",
         "Данияр", "Жанна", "Бекзат", "Асель", "Руслан", "Камила", "Олжас", "Анна", "Игорь"]
LAST = ["А.", "Б.", "К.", "М.", "Н.", "С.", "Т.", "Ж.", "О.", "Е."]

# Счета ИП: сколько каких. Честные: магазин (платит поставщикам), посредник (платит постоянным частным
# поставщикам), скупка (каждый раз платит новым частным лицам, но работает давно), новый магазин (открыт
# недавно, однажды платит частному лицу за ремонт). Арендованные: «спящий» ИП, новый ИП и ИП, который
# до аренды работал как обычный магазин (такие правила не ловят, это видно на странице качества).
BIZ_TYPES = {"shop": 10, "agent": 3, "buyer": 3, "new_shop": 6, "rent_quiet": 5, "rent_young": 3, "rent_active": 3}
BIZ_HONEST = ("shop", "agent", "buyer", "new_shop")
ACTIVITIES = ["Розничная торговля одеждой", "Продажа электроники", "Кафе", "Салон красоты",
              "Продажа автозапчастей", "Цветочный магазин", "Скупка и продажа техники"]


def generate_business(rng: random.Random, now: datetime, first_id: int):
    """ИП на синтетике. Свои клиенты (покупатели, поставщики, владельцы, карты дропперов) и свой генератор
    случайных чисел: переводы частных лиц из generate() и цифры качества по ним от этого не меняются."""
    start = now - timedelta(days=DAYS)
    clients, events = [], []
    ids = iter(range(first_id, first_id + 100_000))

    def client(kind, opened, true_kind="honest", label=None):
        cid = next(ids)
        assert cid < 1001, "синтетические клиенты не должны занимать номера демо-персонажей (1001 и дальше)"
        c = {"id": cid, "opened": opened, "young": False, "kind": true_kind, "type": kind, "label": label,
             "name": f"{'ИП ' if kind == 'ip' else ''}{rng.choice(FIRST)} {rng.choice(LAST)}",
             "activity": rng.choice(ACTIVITIES) if kind == "ip" else None}
        clients.append(c)
        return c["id"]

    def old():
        return now - timedelta(days=rng.randint(DAYS + 200, 2500))

    buyers = [client("person", old()) for _ in range(150)]
    suppliers = [client("ip", old(), label="supplier") for _ in range(5)]
    mules = [client("person", now - timedelta(days=rng.randint(5, 60)), "dropper") for _ in range(12)]

    def sales(ip, t0, t1, per_day):
        """Оплаты покупателей, в среднем per_day в день. Возвращает [(время, сумма)]."""
        income, t = [], t0
        while True:
            t += timedelta(hours=rng.expovariate(per_day / 24))
            if t >= t1:
                return income
            a = rng.randrange(3000, 60000, 500)
            events.append((t, rng.choice(buyers), ip, a, "biz_sale"))
            income.append((t, a))

    def payouts(ip, income, t0, t1, every, share, receivers, scen):
        """Каждые every дней ИП отдаёт долю share выручки за этот период получателям из receivers()."""
        t = t0
        while True:
            t_next = t + timedelta(days=rng.uniform(*every))
            if t_next >= t1:
                return
            got = sum(a for at, a in income if t <= at < t_next)
            if got:
                parts = receivers()
                for r in parts:
                    events.append((t_next + timedelta(minutes=rng.randint(1, 600)), ip, r,
                                   max(1000, int(got * rng.uniform(*share) / len(parts))), scen))
            t = t_next

    def honest_shop(ip, t0, label):
        owner = client("person", old())
        events.append((t0 + timedelta(hours=rng.randint(1, 30)), owner, ip, rng.randrange(20000, 100000, 1000),
                       "biz_owner_in"))
        income = sales(ip, t0 + timedelta(days=2), now, rng.uniform(1.5, 3.5))
        if label in ("shop", "new_shop"):
            mine = rng.sample(suppliers, rng.randint(1, 2))
            payouts(ip, income, t0 + timedelta(days=2), now, (5, 9), (0.45, 0.7), lambda: [rng.choice(mine)], "ipo_supplier")
            payouts(ip, income, t0 + timedelta(days=2), now, (12, 16), (0.1, 0.2), lambda: [owner], "ipo_owner")
        elif label == "agent":
            regular = [client("person", old()) for _ in range(6)]
            for r in regular:           # постоянные поставщики знакомы с самого начала
                events.append((t0 + timedelta(hours=rng.randint(1, 48)), ip, r, rng.randrange(5000, 20000, 500), "ipo_agent"))
            payouts(ip, income, t0 + timedelta(days=3), now, (2, 4), (0.6, 0.85),
                    lambda: rng.sample(regular, rng.randint(1, 2)), "ipo_agent")
        else:                           # скупка: каждый раз новые частные лица
            payouts(ip, income, t0 + timedelta(days=2), now, (2, 4), (0.55, 0.85),
                    lambda: [client("person", old()) for _ in range(rng.randint(1, 2))], "ipo_buyer")
        if label == "new_shop":         # однажды платит частному лицу за ремонт помещения
            at = t0 + timedelta(days=rng.uniform(10, max(11, (now - t0).days - 1)))
            week = sum(a for t, a in income if at - timedelta(days=7) <= t < at)
            if week:
                events.append((at, ip, client("person", old()), int(week * rng.uniform(0.2, 0.6)), "ipo_contractor"))

    def rented(ip, burst):
        """Доступ к счёту передали: деньги обманутых людей приходят и почти сразу уходят на карты дропперов."""
        t, pending, cards = burst, 0, rng.sample(mules, rng.randint(1, 3))
        for n in range(rng.randint(6, 15)):
            t += timedelta(hours=rng.uniform(1, 12))
            a = rng.randrange(20000, 250000, 1000)
            events.append((t, rng.choice(buyers), ip, a, "biz_victim"))
            pending += a
            if rng.random() < 0.45:
                events.append((t + timedelta(minutes=rng.randint(20, 360)), ip, rng.choice(cards),
                               int(pending * rng.uniform(0.85, 0.98)), "ipo_rent"))
                pending = 0
        if pending:
            events.append((t + timedelta(minutes=rng.randint(20, 360)), ip, rng.choice(cards),
                           int(pending * rng.uniform(0.85, 0.98)), "ipo_rent"))

    for label, n in BIZ_TYPES.items():
        for _ in range(n):
            if label == "new_shop":
                opened = now - timedelta(days=rng.randint(20, 80))
                honest_shop(client("ip", opened, label=label), opened, label)
                continue
            if label in ("shop", "agent", "buyer"):
                honest_shop(client("ip", old(), label=label), start, label)
                continue
            # Всплеск целиком в оцениваемом периоде и закончился больше недели назад: в демо и тестах
            # синтетические ИП не попадают в текущий список сотрудника.
            burst = now - timedelta(days=rng.uniform(16, EVAL_DAYS - 12))
            if label == "rent_quiet":
                ip = client("ip", old(), "dropper", label)
                for _ in range(rng.randint(0, 2)):      # почти без оборота: редкие мелкие пополнения
                    events.append((start + timedelta(days=rng.uniform(0, 20)), client("person", old()), ip,
                                   rng.randrange(1000, 5000, 500), "biz_owner_in"))
            elif label == "rent_young":
                opened = burst - timedelta(days=rng.randint(10, 40))
                ip = client("ip", opened, "dropper", label)
                events.append((opened + timedelta(hours=rng.randint(2, 30)), client("person", old()), ip,
                               rng.randrange(2000, 10000, 500), "biz_owner_in"))
            else:                                        # до аренды работал как обычный магазин
                ip = client("ip", old(), "dropper", label)
                owner = client("person", old())
                events.append((start + timedelta(hours=5), owner, ip, 50000, "biz_owner_in"))
                income = sales(ip, start + timedelta(days=1), burst, rng.uniform(1.5, 3))
                mine = [rng.choice(suppliers)]
                payouts(ip, income, start + timedelta(days=1), burst, (5, 9), (0.45, 0.7), lambda: mine, "ipo_supplier")
            rented(ip, burst)
    return clients, [e for e in events if e[0] <= now]


MODELS = ["Samsung Galaxy A15", "Redmi 13C", "iPhone 13", "Samsung Galaxy A54", "Honor X8", "Tecno Spark 20", "iPhone 15"]
DEVICE_FIRST_ID = 9001          # счета только для проверки дроп-ферм: у них нет переводов, только входы с устройств


def synthetic_devices(conn, rng: random.Random, now: datetime) -> dict:
    """Устройства для проверки правила «дроп-ферма». Отдельные счета, без переводов: на прежние цифры не влияют.

    Фермы — 3–6 молодых дропперских счетов на одном телефоне (в трёх из двенадцати молодой только один). Семьи —
    2–3 давних счёта на общем планшете. Общежитие — 3 студента с недавно открытыми счетами на одном ноутбуке.
    Часть счетов уже в «сером» списке Антифрод-центра: у ферм — каждый третий, у честных — редко (ошибочные
    сообщения банков). Возвращает {устройство: тип}.
    """
    ids, labels, clients, devices, grey = iter(range(DEVICE_FIRST_ID, 9999)), {}, [], [], []

    def person(young: bool, kind="honest"):
        cid = next(ids)
        days = rng.randint(10, 85) if young else rng.randint(DAYS + 200, 2500)
        clients.append((cid, f"{rng.choice(FIRST)} {rng.choice(LAST)}", (now - timedelta(days=days)).isoformat(), 0, kind))
        return cid

    def device(label, members):
        did, model = f"syn-{label}-{len(labels) + 1}", rng.choice(MODELS)
        labels[did] = label
        for m in members:
            devices.append((did, m, model, (now - timedelta(days=rng.randint(5, 60))).isoformat(), now.isoformat()))
            if rng.random() < (0.33 if label == "farm" else 0.03):
                grey.append((m, "grey", (now - timedelta(days=rng.randint(1, 30))).isoformat(), "Сообщения банков"))

    for _ in range(12):     # фермы: в основном молодые счета, в трёх из двенадцати молодой только один
        k = rng.randint(3, 6)
        young = k if len(labels) % 4 else 1
        device("farm", [person(i < young, "dropper") for i in range(k)])
    for _ in range(20):     # семьи
        device("family", [person(False) for _ in range(rng.choice([2, 2, 3]))])
    for _ in range(4):      # общежитие
        device("dorm", [person(True) for _ in range(3)])
    conn.executemany("INSERT INTO clients(id,name,account_opened_at,balance,true_kind) VALUES(?,?,?,?,?)", clients)
    conn.executemany("INSERT INTO devices(device_id,client_id,model,first_seen,last_seen) VALUES(?,?,?,?,?)", devices)
    conn.executemany("INSERT INTO afc_list(client_id,list,added_at,note) VALUES(?,?,?,?)", grey)
    return labels


CASINO_TYPES = {"casino": 6, "seller": 8, "collector": 6, "brigadier": 5, "reseller": 4}
CASINO_FIRST_ID = 5001          # отдельный диапазон номеров: не пересекается ни с ИП, ни с демо-персонажами


def generate_casino(rng: random.Random, now: datetime, first_id: int = CASINO_FIRST_ID):
    """Частные лица для проверки правила «касса онлайн-казино». Касса: ставки игроков приходят мелкими суммами,
    выигрыши уходят другим игрокам, часть денег — организатору. Честные двойники: продавец (много покупателей,
    платит поставщикам), сбор денег на поездку (уходит одному турагентству), бригадир (платит многим рабочим,
    получает от немногих заказчиков) и перекупщик билетов (тоже «многие ко многим» — самый трудный случай)."""
    start = now - timedelta(days=DAYS)
    clients, events = [], []
    ids = iter(range(first_id, first_id + 100_000))

    def client(kind="person", opened=None, true_kind="honest", label=None):
        cid = next(ids)
        assert cid < DEVICE_FIRST_ID, "синтетика казино не должна выходить за свой диапазон номеров"
        c = {"id": cid, "opened": opened or now - timedelta(days=rng.randint(DAYS + 200, 2500)), "young": False,
             "kind": true_kind, "type": kind, "label": label, "activity": "Туристическое агентство" if kind == "ip" else None,
             "name": f"{'ИП ' if kind == 'ip' else ''}{rng.choice(FIRST)} {rng.choice(LAST)}"}
        clients.append(c)
        return cid

    players = [client() for _ in range(400)]
    buyers = [client() for _ in range(200)]

    def at(t0, t1):
        return t0 + (t1 - t0) * rng.random()

    for label, n in CASINO_TYPES.items():
        for _ in range(n):
            if label == "casino":
                desk = client(opened=now - timedelta(days=rng.randint(20, 80)), true_kind="dropper", label=label)
                boss = client(opened=now - timedelta(days=rng.randint(5, 40)), true_kind="dropper")
                t0 = now - timedelta(days=rng.uniform(16, EVAL_DAYS - 12))
                regulars, seen = rng.sample(players, rng.randint(25, 45)), []
                for d in range(rng.randint(5, 9)):
                    day = t0 + timedelta(days=d)
                    bettors = rng.sample(regulars, rng.randint(3, 8))
                    for p in bettors:
                        events.append((at(day, day + timedelta(hours=20)), p, desk, rng.randrange(2000, 30000, 500), "cs_bet"))
                    seen += [p for p in bettors if p not in seen]
                    # Выигрыш получает тот, кто уже делал ставку: деньги идут в обе стороны.
                    for p in rng.sample(seen, min(len(seen), rng.randint(1, 3))):
                        events.append((at(day + timedelta(hours=20, minutes=30), day + timedelta(hours=23)), desk, p,
                                       rng.randrange(5000, 80000, 1000), "cso_payout"))
                    if d % 2 == 1:
                        events.append((day + timedelta(hours=23, minutes=30), desk, boss, rng.randrange(40000, 150000, 5000),
                                       "cso_forward"))
            elif label == "seller":
                me = client(label=label)
                mine = [client() for _ in range(2)]
                for s in mine:                       # поставщики знакомы с начала периода
                    events.append((start + timedelta(hours=rng.randint(1, 48)), me, s, rng.randrange(10000, 50000, 1000), "cso_supplier"))
                t, income = start + timedelta(days=2), 0
                while t < now:
                    t += timedelta(hours=rng.expovariate(rng.uniform(1.5, 4) / 24))
                    if t >= now:
                        break
                    a = rng.randrange(3000, 40000, 500)
                    events.append((t, rng.choice(buyers), me, a, "cs_sale"))
                    income += a
                    if income > 150000:
                        events.append((t + timedelta(hours=rng.randint(1, 30)), me, rng.choice(mine), int(income * 0.6), "cso_supplier"))
                        income = 0
            elif label == "collector":
                me = client(label=label)
                agency = client(kind="ip")
                t0, total = now - timedelta(days=rng.uniform(16, EVAL_DAYS - 12)), 0
                for _ in range(rng.randint(15, 30)):
                    a = rng.randrange(5000, 20000, 500)
                    events.append((at(t0, t0 + timedelta(days=3)), client(), me, a, "cs_collect"))
                    total += a
                events.append((t0 + timedelta(days=3, hours=5), me, agency, total, "cso_agency"))
            elif label == "brigadier":
                me = client(label=label)
                customers = [client() for _ in range(2)]
                workers = [client() for _ in range(8)]
                for w in workers:                    # постоянная бригада знакома с начала периода
                    events.append((start + timedelta(hours=rng.randint(1, 72)), me, w, rng.randrange(20000, 60000, 1000), "cso_worker"))
                for week in range(1, DAYS // 7):
                    w0 = start + timedelta(days=7 * week)
                    for c in customers:
                        events.append((at(w0, w0 + timedelta(days=2)), c, me, rng.randrange(100000, 400000, 10000), "cs_customer"))
                    crew = rng.sample(workers, rng.randint(5, 8)) + [client() for _ in range(rng.randint(0, 2))]
                    for w in crew:
                        events.append((at(w0 + timedelta(days=3), w0 + timedelta(days=6)), me, w,
                                       rng.randrange(20000, 70000, 1000), "cso_worker"))
            else:                                    # перекупщик билетов: покупает у одних, продаёт другим
                me = client(label=label)
                for week in range(4):
                    w0 = now - timedelta(days=30 - 7 * week)
                    for _ in range(rng.randint(10, 20)):
                        events.append((at(w0, w0 + timedelta(days=7)), me, client(), rng.randrange(10000, 35000, 500), "cso_resell"))
                    for _ in range(rng.randint(15, 30)):
                        events.append((at(w0, w0 + timedelta(days=7)), client(), me, rng.randrange(15000, 40000, 500), "cs_ticket"))
    return clients, [e for e in events if e[0] <= now]


def generate(rng: random.Random, now: datetime, biz_seed: int | None = None):
    start = now - timedelta(days=DAYS)
    clients = []
    for i in range(1, N_CLIENTS + 1):
        dropper = i > N_CLIENTS - N_DROPPERS
        # У большинства дропперских счетов возраст маленький, но не у всех.
        young = dropper and rng.random() < 0.7
        if young:
            opened = now - timedelta(days=rng.randint(30, EVAL_DAYS - 2))
        else:
            opened = now - timedelta(days=rng.randint(DAYS + 40, 2000))
        clients.append({"id": i, "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                        "opened": opened, "young": young,
                        "kind": "dropper" if dropper else "honest"})
    honest = [c["id"] for c in clients if c["kind"] == "honest"]
    droppers = [c for c in clients if c["kind"] == "dropper"]
    contacts = {h: rng.sample([x for x in honest if x != h], rng.randint(3, 8)) for h in honest}

    events = []  # (time, sender, receiver, amount, scenario)

    def t_rand(recent=False):
        # Схемы и похожие на них случаи кладём в оцениваемый период.
        if recent:
            return now - timedelta(seconds=rng.randint(4 * 86400, EVAL_DAYS * 86400))
        return start + timedelta(seconds=rng.randint(0, DAYS * 86400 - 4 * 86400))

    # 1. Обычные переводы между знакомыми.
    for _ in range(12000):
        s = rng.choice(honest)
        events.append((t_rand(), s, rng.choice(contacts[s]), rng.randrange(1000, 80000, 500), "normal"))

    def stranger_for(u):
        return rng.choice([x for x in honest if x != u and x not in contacts[u] and u not in contacts[x]])

    # 2. Схема «ошибочный перевод»: жертва -> невольный участник -> дроппер.
    for k in range(60):
        u = rng.choice(honest)
        victim, d = stranger_for(u), rng.choice(droppers)
        # Молодой дропперский счёт используют в первые недели после открытия.
        t0 = d["opened"] + timedelta(hours=rng.randint(2, 24 * 24)) if d["young"] else t_rand(True)
        amount = rng.randrange(50000, 500000, 5000)
        events.append((t0, victim, u, amount, "tri_in"))
        r = rng.random()
        if r < 0.80:    # типичный случай
            events.append((t0 + timedelta(minutes=rng.randint(5, 1200)), u, d["id"],
                           int(amount * rng.uniform(0.9, 1.0)), "tri_out"))
        elif r < 0.90:  # трудный: деньги дробят на две части
            for part in (0.55, 0.45):
                events.append((t0 + timedelta(minutes=rng.randint(5, 600)), u, d["id"],
                               int(amount * part), "tri_split"))
        else:           # трудный: пересылают через несколько дней
            events.append((t0 + timedelta(hours=rng.randint(80, 95)), u, d["id"], amount, "tri_late"))

    # 3. Честные ситуации, похожие на схему.
    for _ in range(150):  # продавец получил оплату от незнакомца и платит знакомому поставщику
        u = rng.choice(honest); t0 = t_rand(True)
        events.append((t0, stranger_for(u), u, rng.randrange(5000, 200000, 500), "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 1400)), u, rng.choice(contacts[u]),
                       rng.randrange(5000, 200000, 500), "benign_supplier"))
    for _ in range(80):   # получил от незнакомца и платит новому человеку другую сумму
        u = rng.choice(honest); t0 = t_rand(True)
        events.append((t0, stranger_for(u), u, rng.randrange(5000, 200000, 500), "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 1400)), u, stranger_for(u),
                       rng.randrange(2000, 200000, 500), "benign_new"))
    for _ in range(40):   # получил от незнакомца и честно вернул ему же
        u = rng.choice(honest); t0 = t_rand(True); s = stranger_for(u)
        a = rng.randrange(5000, 200000, 500)
        events.append((t0, s, u, a, "benign_in"))
        events.append((t0 + timedelta(minutes=rng.randint(10, 600)), u, s, a, "benign_return"))

    if biz_seed is not None:
        biz_clients, biz_events = generate_business(random.Random(biz_seed * 7919 + 101), now, N_CLIENTS + 1)
        cs_clients, cs_events = generate_casino(random.Random(biz_seed * 104729 + 7), now)
        clients += biz_clients + cs_clients
        events += biz_events + cs_events
    events.sort(key=lambda e: e[0])
    return clients, events


def populate(conn, seed: int = 7, now: datetime | None = None, evaluate: bool = True, reset: bool = False,
             with_history: bool = False, with_business: bool = True) -> dict:
    """Заполняет базу. evaluate=True дополнительно проигрывает переводы через движок и считает качество.

    with_history=True добавляет историю случаев за 30 дней для панели эффективности (см. app/history.py).
    with_business=True добавляет синтетические счета ИП для проверки правил «аренды ИП» (страница качества);
    демо-серверу они не нужны, без них он запускается быстрее.
    """
    if reset:
        for table in ("devices", "safes", "interbank", "loans", "self_reports", "message_checks", "clearances", "guardians", "recruit_marks", "afc_list", "call_signals", "events", "complaints", "incoming_marks", "documents", "cases", "transfers", "clients", "meta"):
            conn.execute(f"DELETE FROM {table}")
    now = (now or clock.now()).replace(microsecond=0)
    rng = random.Random(seed)
    clients, events = generate(rng, now, biz_seed=seed if with_business else None)
    conn.executemany(
        "INSERT INTO clients(id,name,account_opened_at,balance,true_kind,kind,activity) VALUES(?,?,?,?,?,?,?)",
        [(c["id"], c["name"], c["opened"].isoformat(), rng.randrange(20000, 900000, 1000), c["kind"],
          c.get("type", "person"), c.get("activity")) for c in clients])

    stats = {"tri": 0, "tri_caught": 0, "honest": 0, "honest_held": 0, "clients": len(clients), "events": len(events),
             "scored": [], "held": [], "passed": [], "ip_scored": [], "now": now,
             "ip_labels": {c["id"]: c["label"] for c in clients if c.get("type") == "ip" and c["id"] < CASINO_FIRST_ID},
             "cs_labels": {c["id"]: c["label"] for c in clients if c["id"] >= CASINO_FIRST_ID and c.get("label")},
             "cs_scored": []}
    for t, s, r, amount, scen in events:
        if evaluate and scen.startswith("cso_") and t >= now - timedelta(days=EVAL_DAYS):
            # Платежи кассы казино и честных двойников: что останавливает движок.
            res = engine.evaluate(conn, s, r, amount, t)
            stats["cs_scored"].append((scen, res["decision"] == "hold", amount))
        if evaluate and scen.startswith("ipo_") and t >= now - timedelta(days=EVAL_DAYS):
            # Платежи ИП: останавливает ли движок транзит арендованного ИП и не мешает ли честным.
            res = engine.evaluate(conn, s, r, amount, t)
            key = f"ipo_{stats['ip_labels'][s]}" if scen == "ipo_rent" else scen   # транзит — по типу арендованного ИП
            stats["ip_scored"].append((key, res["decision"] == "hold", amount))
        outgoing = scen == "normal" or scen.startswith(("tri_out", "tri_split", "tri_late", "benign_s",
                                                         "benign_n", "benign_r"))
        if evaluate and outgoing and t >= now - timedelta(days=EVAL_DAYS):
            res = engine.evaluate(conn, s, r, amount, t)
            held = res["decision"] == "hold"
            scheme = scen.startswith("tri_")
            if scheme:
                stats["tri"] += 1; stats["tri_caught"] += held
            else:
                stats["honest"] += 1; stats["honest_held"] += held
            # Для страницы качества: оценка и могла ли сработать остановка при любом пороге.
            stats["scored"].append((res["score"], res["incoming_id"] is not None, scen))
        cur = conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                           (s, r, amount, t.isoformat(), scen))
        if evaluate and outgoing and t >= now - timedelta(days=EVAL_DAYS):
            # Для истории случаев: что движок остановил и что пропустил.
            (stats["held"] if held else stats["passed"]).append(
                {"id": cur.lastrowid, "t": t, "sender": s, "receiver": r, "amount": amount, "scen": scen, "res": res})

    if with_history:
        from . import history
        history.simulate(conn, stats, now, random.Random(seed + 1))
    stats["device_labels"] = synthetic_devices(conn, random.Random(seed * 31 + 3), now) if with_business else {}
    _demo_personas(conn, now)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('seeded_at',?)", (now.isoformat(),))
    conn.commit()
    return stats


def build(path: str | None = None, seed: int = 7, now: datetime | None = None, verbose: bool = True):
    """Создаёт базу с нуля и печатает качество движка на этих данных."""
    path = path or db.DB_PATH
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    stats = populate(conn, seed, now)
    if verbose:
        caught, held = stats["tri_caught"], stats["honest_held"]
        print(f"Клиентов: {stats['clients']}, переводов: {stats['events']}")
        print(f"Поймано переводов дропперам: {caught} из {stats['tri']} ({100 * caught / stats['tri']:.0f}%)")
        print(f"Честных переводов остановлено зря: {held} из {stats['honest']} "
              f"({100 * held / stats['honest']:.2f}%)")
        print(f"Среди всех остановок настоящих схем: {100 * caught / max(1, caught + held):.0f}%")
    return conn, stats


def _demo_personas(conn, now: datetime):
    """Персонажи для показа на защите."""
    rows = [
        # Последнее число — остаток до демо-переводов; итоговый баланс считается по самим переводам ниже.
        (1001, "Айдана (студентка)", now - timedelta(days=400), 12000, "honest"),
        (1002, "Незнакомый отправитель", now - timedelta(days=900), 450000, "honest"),
        (1003, "Карта «для возврата»", now - timedelta(days=6), 0, "dropper"),
        (1004, "Мама", now - timedelta(days=2000), 180000, "honest"),
        (1005, "Данияр (продаёт на маркетплейсе)", now - timedelta(days=700), 14500, "honest"),
        (1006, "Гульнар (купила телефон по объявлению)", now - timedelta(days=1200), 290000, "honest"),
        (1007, "Руслан (счёту 12 дней)", now - timedelta(days=12), 0, "dropper"),
        (1008, "Инвестплатформа «Профит»", now - timedelta(days=40), 0, "dropper"),
        (1009, "«Безопасный счёт»", now - timedelta(days=3), 0, "dropper"),
        (1010, "Ерлан (нашёл «подработку»)", now - timedelta(days=45), 8000, "honest"),
        (1011, "Оптовый склад (поставщик Данияра)", now - timedelta(days=1500), 320000, "honest"),
        (1012, "Тимур (студент, отдал карту «работодателю»)", now - timedelta(days=210), 6000, "honest"),
        (1013, "Болат (взял кредит по звонку «из банка»)", now - timedelta(days=1500), 23000, "honest"),
        (1014, "ИП Нурлан (сдал ИП «в аренду»)", now - timedelta(days=800), 15000, "dropper"),
        (1015, "ИП Асель (магазин одежды)", now - timedelta(days=1100), 40000, "honest"),
        # Счета в другом банке: под ними нельзя войти, но на них можно переводить и получать от них деньги.
        (1016, "Карта в Банке «Б» (…4471)", now - timedelta(days=9), 0, "dropper"),
        (1017, "Клиент Банка «Б»", now - timedelta(days=2000), 900000, "honest"),
        (1018, "Сауле (купила билеты по объявлению)", now - timedelta(days=1500), 260000, "honest"),
        (1019, "Арман (продаёт билеты на концерты)", now - timedelta(days=900), 21000, "honest"),
        (1020, "Дина (получила перевод из другого банка)", now - timedelta(days=600), 17000, "honest"),
        (1021, "Самат (студент, «принимает платежи» за процент)", now - timedelta(days=50), 4000, "dropper"),
    ]
    conn.executemany(
        "INSERT INTO clients(id,name,account_opened_at,balance,true_kind) VALUES(?,?,?,?,?)",
        [(i, n, o.isoformat(), b, k) for i, n, o, b, k in rows])
    conn.executemany("UPDATE clients SET kind='ip', activity=? WHERE id=?", [
        ("Оптовая торговля продуктами", 1011), ("Розничная торговля одеждой", 1014), ("Магазин одежды", 1015)])
    conn.execute("UPDATE clients SET kind='external', bank='Банк «Б»' WHERE id IN (1016, 1017)")
    tr = [
        (1004, 1001, 15000, now - timedelta(days=200), "demo"),
        (1004, 1001, 20000, now - timedelta(days=9), "demo"),
        (1001, 1004, 5000, now - timedelta(days=3), "demo"),
        (1002, 1001, 150000, now - timedelta(minutes=20), "demo_in"),
    ]
    conn.executemany(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
        [(s, r, a, t.isoformat(), sc) for s, r, a, t, sc in tr])

    # У Данияра обычная история продавца: оплаты от покупателей и расчёты с поставщиком.
    # Поставщик — знакомый получатель: ему Данияр сможет платить и при соразмерной блокировке.
    for n, (other, amount, incoming) in enumerate([(21, 18000, True), (35, 42000, True), (1011, 30000, False),
                                                   (52, 9500, True), (1011, 25000, False), (64, 27000, True)]):
        pair = (other, 1005) if incoming else (1005, other)
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (*pair, amount, (now - timedelta(days=60 - 8 * n)).isoformat(), "demo"))

    # «Треугольник»: Гульнар оплатила телефон по объявлению на карту Данияра, а мошенник
    # попросил Данияра «вернуть ошибочный перевод» на другую карту. Данияр переслал деньги.
    # Блокировка появится, когда Гульнар пожалуется на перевод: так связаны обе половины продукта.
    t_in = now - timedelta(days=2, hours=3)
    conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                 (1006, 1005, 200000, t_in.isoformat(), "demo_in"))
    conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                 (1005, 1003, 195000, (t_in + timedelta(minutes=40)).isoformat(), "demo_out"))

    # Руслан за пять дней четыре раза переслал деньги от разных незнакомцев: для контраста в панели аналитика.
    last = None
    for n, (victim, amount) in enumerate([(11, 120000), (27, 300000), (43, 85000), (58, 210000)]):
        t_in = now - timedelta(days=5 - n, hours=2)
        t_out = t_in + timedelta(minutes=12)
        cur = conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                           (victim, 1007, amount, t_in.isoformat(), "demo_in"))
        inc_id = cur.lastrowid
        res = engine.evaluate(conn, 1007, 1003, int(amount * 0.95), t_out)
        cur = conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                           (1007, 1003, int(amount * 0.95), t_out.isoformat(), "demo_out"))
        last = (cur.lastrowid, inc_id, res, t_out)
    conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (1007, last[0], last[1], "block", last[2]["score"],
         json.dumps(last[2]["reasons"], ensure_ascii=False), (last[3] + timedelta(hours=1)).isoformat()))
    conn.execute("UPDATE clients SET status='blocked' WHERE id=1007")
    # Мама давно знает дочь и назначила её доверенным лицом: об остановленных переводах мамы узнает Айдана.
    conn.execute("INSERT INTO guardians(client_id,guardian_id,added_at) VALUES(1004,1001,?)",
                 ((now - timedelta(days=60)).isoformat(),))

    # Ерлану в чате предложили «подработку»: принимать переводы и пересылать их. За два дня пришли деньги
    # от трёх незнакомых людей, дальше он пока ничего не отправлял.
    for n, (sender, amount) in enumerate([(61, 45000), (66, 60000), (69, 38000)]):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (sender, 1010, amount, (now - timedelta(hours=40 - 15 * n)).isoformat(), "demo_in"))

    # Тимур откликнулся на «подработку для студентов» и отдал карту. Обычная студенческая история,
    # а за последние сутки на карту пришли деньги от двух незнакомых людей.
    for n, (pair, amount) in enumerate([((17, 1012), 25000), ((1012, 17), 4000), ((17, 1012), 30000),
                                        ((1012, 23), 6500), ((17, 1012), 25000), ((1012, 23), 3000)]):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (*pair, amount, (now - timedelta(days=150 - 25 * n)).isoformat(), "demo"))
    for n, (sender, amount) in enumerate([(72, 40000), (77, 55000)]):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (sender, 1012, amount, (now - timedelta(hours=20 - 9 * n)).isoformat(), "demo_in"))

    # Болату позвонила «служба безопасности банка»: на него якобы оформляют кредит, и чтобы «перехватить» заявку,
    # нужно взять встречный кредит и перевести деньги на «безопасный счёт». Кредит он уже взял (ниже), до этого
    # счётом пользовался обычно.
    for n, (pair, amount) in enumerate([((1013, 31), 15000), ((44, 1013), 120000), ((1013, 31), 22000),
                                        ((1013, 57), 8000), ((44, 1013), 120000), ((1013, 57), 9500)]):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (*pair, amount, (now - timedelta(days=170 - 28 * n)).isoformat(), "demo"))

    # Нурлан полгода назад торговал одеждой, потом ИП «спал». Три дня назад он за процент передал доступ к счёту:
    # деньги семи незнакомых людей пришли и почти сразу ушли на карты частных лиц. Полчаса назад пришла ещё оплата.
    def tr_at(s, r, amount, at, scen="demo"):
        conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,scenario) VALUES(?,?,?,?,?)",
                     (s, r, amount, at.isoformat(), scen))
    for n, (buyer, amount) in enumerate([(111, 18000), (112, 32000), (113, 15500), (114, 41000), (115, 26000)]):
        tr_at(buyer, 1014, amount, now - timedelta(days=200 - 15 * n))
    tr_at(1014, 1011, 30000, now - timedelta(days=170))
    tr_at(1014, 1011, 25000, now - timedelta(days=130))
    for buyer, amount, hours in [(101, 120000, 70), (102, 95000, 62), (103, 210000, 50), (104, 60000, 44),
                                 (105, 150000, 30), (106, 85000, 20), (107, 130000, 9)]:
        tr_at(buyer, 1014, amount, now - timedelta(hours=hours), "demo_in")
    tr_at(1014, 290, 400000, now - timedelta(hours=43), "demo_out")
    tr_at(1014, 292, 350000, now - timedelta(hours=8), "demo_out")
    tr_at(108, 1014, 180000, now - timedelta(minutes=30), "demo_in")

    # Асель — честный магазин: покупатели платят каждый день, за последнюю неделю чаще, деньги уходят поставщику.
    # Незнакомых плательщиков много, но это выручка: «арендой ИП» это не считается.
    income = 0
    for d in range(80, 0, -1):
        for k in range(1 if d > 7 else 2):
            if d > 7 and d % 2:
                continue
            amount = 8000 + (d * 7919 + k * 104729) % 32000 // 500 * 500
            tr_at(120 + (d * 2 + k) % 160, 1015, amount, now - timedelta(days=d, hours=3 + 5 * k))
            income += amount
        if d % 10 == 0:
            tr_at(1015, 1011, int(income * 0.55) // 1000 * 1000, now - timedelta(days=d, hours=-6))
            income = 0

    # Деньги уходят в другой банк. Сауле купила билеты у Армана, а мошенник попросил Армана «вернуть ошибочный
    # перевод» на карту в Банке «Б». Когда Сауле пожалуется, цепочка дойдёт до другого банка: туда уйдёт запрос.
    for n, (pair, amount) in enumerate([((1019, 81), 12000), ((83, 1019), 35000), ((1019, 81), 9000),
                                        ((86, 1019), 28000), ((1019, 89), 15000)]):
        tr_at(*pair, amount, now - timedelta(days=140 - 25 * n))
    t_buy = now - timedelta(days=1, hours=6)
    tr_at(1018, 1019, 90000, t_buy, "demo_in")
    tr_at(1019, 1016, 88000, t_buy + timedelta(minutes=25), "demo_out")
    # Деньги приходят из другого банка. Клиент Банка «Б» перевёл Дине 140 000 ₸, Дина «вернула ошибочный перевод»
    # на «карту для возврата». Банк «Б» пришлёт запрос через Антифрод-центр (в демо его имитирует сотрудник).
    for n, (pair, amount) in enumerate([((1020, 91), 8000), ((94, 1020), 22000), ((1020, 91), 6500),
                                        ((97, 1020), 30000), ((1020, 94), 5000)]):
        tr_at(*pair, amount, now - timedelta(days=120 - 22 * n))
    t_got = now - timedelta(hours=20)
    tr_at(1017, 1020, 140000, t_got, "demo_in")
    tr_at(1020, 1003, 135000, t_got + timedelta(minutes=40), "demo_out")

    # Самату в чате предложили «принимать платежи» за 3%: через его карту идут ставки игроков онлайн-казино
    # (мелкие суммы от двух десятков незнакомцев) и выплаты выигрышей семерым из этих игроков.
    for n, (pair, amount) in enumerate([((1021, 141), 3000), ((143, 1021), 5000), ((1021, 141), 2500)]):
        tr_at(*pair, amount, now - timedelta(days=45 - 12 * n))
    for n in range(20):
        tr_at(200 + n, 1021, 3000 + (n * 7919) % 22 * 1000, now - timedelta(hours=90 - 4 * n), "demo_in")
    for n, amount in enumerate([18000, 42000, 9000, 27000, 15000, 33000, 21000]):
        tr_at(1021, (200, 203, 206, 209, 212, 214, 217)[n], amount, now - timedelta(hours=85 - 11 * n), "demo_out")

    # Устройства входа в приложение. У каждого своё; у Мамы и Айданы общий семейный планшет (два давних счёта —
    # это не ферма). Два «дроп-телефона»: на них работают карта «для возврата», «Безопасный счёт», Руслан,
    # Тимур (отдал доступ к приложению), ИП Нурлан (сдал ИП «в аренду»), Самат (касса казино) и два дропперских
    # счёта из синтетики. На одном телефоне больше трёх счетов разных людей — так выглядит дроп-ферма.
    devs = []
    for cid in list(range(1001, 1016)) + [1018, 1019, 1020, 1021]:
        devs.append((f"own-{cid}", cid, MODELS[cid % len(MODELS)], (now - timedelta(days=300)).isoformat(),
                     (now - timedelta(days=1)).isoformat()))
    for cid in (1001, 1004):
        devs.append(("family-tablet", cid, "Samsung Galaxy Tab A9", (now - timedelta(days=400)).isoformat(),
                     (now - timedelta(days=2)).isoformat()))
    for did, model, members in [("drop-3f9c", "Samsung Galaxy A15", (1003, 1009, 1007, 1012, 1014, 290)),
                                ("drop-81ad", "Redmi 13C", (1021, 1009, 292, 295))]:
        for cid in members:
            devs.append((did, cid, model, (now - timedelta(days=5)).isoformat(), (now - timedelta(hours=2)).isoformat()))
    conn.executemany("INSERT INTO devices(device_id,client_id,model,first_seen,last_seen) VALUES(?,?,?,?,?)", devs)

    # Имитация базы Антифрод-центра: «чёрный» список — подтверждённые мошенники, «серый» — подозрительные.
    conn.executemany("INSERT INTO afc_list(client_id,list,added_at,note) VALUES(?,?,?,?)", [
        (1008, "black", (now - timedelta(days=20)).isoformat(), "Лжеинвестиционная платформа, подтверждено"),
        (1003, "grey", (now - timedelta(days=4)).isoformat(), "Сообщения банков о переводах от незнакомцев"),
    ])

    # Балансы персонажей сходятся с их переводами: на экране видно, где на самом деле лежат деньги.
    conn.execute("""UPDATE clients SET balance = balance
                      + COALESCE((SELECT SUM(amount) FROM transfers WHERE receiver_id=clients.id AND status='done'), 0)
                      - COALESCE((SELECT SUM(amount) FROM transfers WHERE sender_id=clients.id AND status='done'), 0)
                    WHERE id BETWEEN 1001 AND 1999""")
    # У Мамы сбережения 1 500 000 ₸ лежат в «Сейфе» уже месяц: перевести их через приложение нельзя.
    conn.execute("UPDATE clients SET balance = balance + 1500000 WHERE id=1004")
    conn.execute("INSERT INTO safes(client_id,amount,updated_at) VALUES(1004,1500000,?)",
                 ((now - timedelta(days=30)).isoformat(),))
    # Кредит наличными зачисляется на счёт напрямую (это деньги банка, а не перевод от клиента).
    conn.execute("INSERT INTO loans(client_id,amount,issued_at) VALUES(1013,800000,?)",
                 ((now - timedelta(minutes=40)).isoformat(),))
    conn.execute("UPDATE clients SET balance = balance + 800000 WHERE id=1013")
    case_id = conn.execute("SELECT id FROM cases WHERE client_id=1007").fetchone()["id"]
    conn.execute("INSERT INTO events(at,actor,action,case_id,transfer_id,details) VALUES(?,?,?,?,?,?)",
                 ((last[3] + timedelta(hours=1)).isoformat(), "system", "blocked", case_id, last[0],
                  json.dumps({"text": "Антифрод-система приостановила операции: повторяющиеся пересылки"},
                             ensure_ascii=False)))


if __name__ == "__main__":
    build()
