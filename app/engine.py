"""Движок перехвата схемы «ошибочный перевод».

Ищет связку: входящий перевод от незнакомца -> исходящий третьему лицу
на похожую сумму. Каждый признак даёт баллы и текст причины, поэтому
решение можно объяснить и клиенту, и аналитику.
"""
import json
from datetime import datetime, timedelta

HOLD_THRESHOLD = 85
# Без чужих денег на счёте перевод останавливается только при сильном внешнем сигнале
# (звонок с подозрительного номера, получатель в «сером» списке) и новом получателе.
HOLD_THRESHOLD_NO_INCOMING = 60
CALL_SIGNAL_MINUTES = 15            # сколько длится сигнал оператора о подозрительном звонке
LOOKBACK_HOURS = 120
YOUNG_ACCOUNT_DAYS = 30
# Признаки вербовки в дропперы: деньги от нескольких незнакомцев за неделю на новый счёт или с пересылкой.
RECRUIT_DAYS = 7
RECRUIT_SENDERS = 3
RECRUIT_MIN_AMOUNT = 20_000
RECRUIT_ACCOUNT_DAYS = 90
SCAM_MESSAGE_MINUTES = 60           # сколько действует признак «клиент проверил сообщение, похожее на схему»
# Схема «встречный кредит»: клиента уговаривают взять кредит и сразу перевести деньги «на безопасный счёт».
LOAN_HOURS = 72                     # сколько после выдачи кредита следим, куда уходят его деньги
LOAN_SHARE = 0.5                    # признак срабатывает, когда новым получателям уходит половина кредита и больше
# «Перевод под диктовку»: что приложение замечает во время перевода (как в пилоте сессионного антифрода Нацбанка).
# Приложение передаёт только эти отметки, без содержимого буфера обмена, ссылок и номеров. Отметки могут лишь
# добавить баллы, поэтому подделать их в свою пользу нельзя. Учитываются только для новых получателей.
SESSION_SIGNALS = [
    ("on_call", 25, "Во время перевода идёт телефонный разговор"),
    ("pasted", 15, "Реквизиты получателя вставлены из буфера обмена"),
    ("from_link", 15, "Приложение открыто по ссылке из мессенджера"),
    ("new_device", 20, "Вход в приложение с нового устройства меньше суток назад"),
]
# «Касса онлайн-казино»: через карту дроппера идут ставки десятков игроков и выплаты выигрышей.
CASINO_DAYS = 7
CASINO_PAYERS = 12                  # столько людей, которые раньше не платили этому клиенту
CASINO_PAYEES = 6                   # столько новых получателей выплат
CASINO_AVG = 40_000                 # средняя ставка небольшая
CASINO_TWOWAY = 4                   # столько незнакомцев и платили, и получали деньги обратно (ставка и выигрыш)
# «Аренда ИП»: владелец за процент передаёт доступ к счёту ИП, через него проводят деньги обманутых людей.
RENT_DAYS = 7                       # за какой период смотрим на поступления
RENT_PAYERS = 3                     # столько частных лиц, которые раньше не платили этому ИП
RENT_TRANSIT = 0.7                  # такая доля поступлений от частных лиц ушла частным лицам, которым ИП раньше не переводил
RENT_FAST_HOURS = 48                # транзит — только «свежие» деньги: за 48 ч до перевода пришло не меньше 80% его суммы
RENT_FAST_SHARE = 0.8
RENT_QUIET_DAYS = 60                # «спящий» ИП: оборот за 60 дней до этого меньше RENT_QUIET_SHARE поступлений
RENT_QUIET_SHARE = 0.2              # или раньше частным лицам уходило меньше этой доли поступлений (поведение поменялось)
RENT_YOUNG_DAYS = 90                # или ИП открыт меньше 90 дней назад
# «Дроп-ферма»: один человек держит на своём телефоне карты нескольких дропперов и сам проводит по ним деньги.
FARM_ACCOUNTS = 3                   # с одного устройства входят в приложение 3 и больше разных людей
FARM_MARKED = 2                     # и у двоих из них банк уже видел признаки дроппера
FARM_YOUNG = 4                      # или четыре и больше счетов открыты недавно
FARM_YOUNG_DAYS = 90
DROPPER_CODES = ("stranger_incoming", "ip_rental", "casino_payout")   # остановленный перевод был пересылкой чужих денег


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _had_contact(conn, a: int, b: int, before: str, exclude_id: int | None = None) -> bool:
    row = conn.execute(
        """SELECT 1 FROM transfers
           WHERE status='done' AND created_at < ? AND id IS NOT ?
             AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
           LIMIT 1""",
        (before, exclude_id, a, b, b, a),
    ).fetchone()
    return row is not None


def _stranger_incomings(conn, client_id: int, now: datetime):
    """Недавние входящие от тех, с кем раньше не было переводов, ещё не возвращённые."""
    since = (now - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    rows = conn.execute(
        """SELECT t.* FROM transfers t
           WHERE t.receiver_id=? AND t.status='done' AND t.return_of IS NULL
             AND t.created_at >= ? AND t.created_at <= ?
             AND NOT EXISTS (SELECT 1 FROM transfers r
                             WHERE r.return_of = t.id AND r.status='done')""",
        (client_id, since, now.isoformat()),
    ).fetchall()
    return [r for r in rows
            if not _had_contact(conn, client_id, r["sender_id"], r["created_at"], r["id"])]


def _forwarded_to_new(conn, client_id: int, incoming) -> int:
    """Сколько денег после входящего ушло получателям, с которыми до него не было переводов."""
    rows = conn.execute(
        """SELECT receiver_id, amount FROM transfers
           WHERE sender_id=? AND status='done' AND return_of IS NULL
             AND created_at > ? AND receiver_id != ?""",
        (client_id, incoming["created_at"], incoming["sender_id"])).fetchall()
    return sum(r["amount"] for r in rows
               if not _had_contact(conn, client_id, r["receiver_id"], incoming["created_at"]))


def evaluate(conn, sender_id: int, receiver_id: int, amount: int, now: datetime, session: dict | None = None) -> dict:
    """Оценивает исходящий перевод до его проведения. session — отметки приложения о ходе перевода."""
    reasons: list[dict] = []
    score = 0

    def add(code: str, points: int, text: str):
        nonlocal score
        score += points
        reasons.append({"code": code, "points": points, "text": text})

    # У ИП платежи от незнакомых людей — обычная выручка: признаки «ошибочного перевода» к ним не применяются,
    # вместо них проверяется «аренда ИП» (ниже).
    business = _kind(conn, sender_id) == "ip"
    incomings = [] if business else _stranger_incomings(conn, sender_id, now)

    # Возврат тому же отправителю безопасен, но только в пределах того, что он прислал. Иначе мошенник
    # присылает 1 000 ₸ и просит перевести на ту же карту 150 000: такой перевод проверяется как обычный.
    from_receiver = [i for i in incomings if i["sender_id"] == receiver_id]
    if from_receiver and amount <= sum(i["amount"] for i in from_receiver):
        return {"score": 0, "decision": "allow", "reasons": [], "incoming_id": None, "return_to_sender": True}
    incomings = [i for i in incomings if i["sender_id"] != receiver_id]

    incoming = None
    if incomings:
        # Берём входящий, наиболее близкий по сумме к исходящему.
        incoming = min(incomings, key=lambda i: abs(i["amount"] - amount) / i["amount"])
        add("stranger_incoming", 25,
            "Недавно пришли деньги от человека, с которым раньше не было переводов")
        add("not_to_sender", 15,
            "Деньги уходят не тому, кто их прислал, а третьему лицу")
        if 0.8 * incoming["amount"] <= amount <= 1.02 * incoming["amount"]:
            add("amount_match", 30, "Сумма почти совпадает с полученной от незнакомца")
        elif not _had_contact(conn, sender_id, receiver_id, incoming["created_at"]):
            # Дробление: считаем всё, что после этого входящего уже ушло новым получателям.
            sent = _forwarded_to_new(conn, sender_id, incoming)
            # Срабатывает на той части, которая добирает сумму; если деньги уже ушли раньше, молчит.
            if 0 < sent < 0.8 * incoming["amount"] <= sent + amount <= 1.1 * incoming["amount"]:
                add("split_forward", 40,
                    "Вместе с прошлыми переводами новым получателям уходит почти вся сумма от незнакомца")
            elif len({i["sender_id"] for i in incomings}) >= 2 and _kind(conn, receiver_id) == "person":
                # Сбор: деньги нескольких незнакомцев уходят одним переводом. Так работает «подработка» дроппера.
                # Только частному лицу: оплата собранных денег бизнесу (турагентству, магазину) — обычное дело.
                pooled = sum(i["amount"] for i in incomings)
                if 0.8 * pooled <= amount <= 1.1 * pooled:
                    add("pooled_forward", 40, "Уходит почти вся сумма, которую прислали несколько незнакомых людей")
        gap = now - _dt(incoming["created_at"])
        if gap <= timedelta(hours=2):
            add("fast_forward", 15, "Перевод отправляется меньше чем через 2 часа после получения")
        elif gap <= timedelta(hours=24):
            add("same_day_forward", 10, "Перевод отправляется в течение суток после получения")

    new = not _had_contact(conn, sender_id, receiver_id, now.isoformat())
    if new:
        add("new_receiver", 10, "Клиент впервые переводит этому получателю")

    rec = conn.execute("SELECT account_opened_at FROM clients WHERE id=?", (receiver_id,)).fetchone()
    if rec and now - _dt(rec["account_opened_at"]) < timedelta(days=YOUNG_ACCOUNT_DAYS):
        add("young_receiver", 10, "Счёт получателя открыт меньше месяца назад")

    # Деньги свежего кредита уходят тому, с кем до кредита не было переводов. Одного этого признака мало
    # (честный клиент может оплатить кредитом покупку), остановка нужна вместе с новым счётом получателя,
    # звонком или «серым» списком. Части складываются: раздробить сумму не поможет.
    loan = fresh_loan(conn, sender_id, now)
    if loan and not _had_contact(conn, sender_id, receiver_id, loan["issued_at"]):
        sent = sum(r["amount"] for r in conn.execute(
            """SELECT receiver_id, amount FROM transfers WHERE sender_id=? AND status IN ('done','pending')
               AND return_of IS NULL AND created_at >= ?""", (sender_id, loan["issued_at"])).fetchall()
            if not _had_contact(conn, sender_id, r["receiver_id"], loan["issued_at"]))
        if sent + amount >= LOAN_SHARE * loan["amount"]:
            add("loan_forward", 40, "Деньги из кредита, выданного меньше трёх суток назад, уходят новому получателю")

    # «Касса онлайн-казино»: счёт принимает мелкие платежи десятков незнакомцев и раздаёт деньги другим незнакомцам.
    if (not business and _kind(conn, receiver_id) == "person"
            and not _had_contact(conn, sender_id, receiver_id, (now - timedelta(days=CASINO_DAYS)).isoformat())):
        sig = casino_signal(conn, sender_id, now, extra_receiver=receiver_id)
        if sig:
            add("casino_payout", 60, f"За неделю счёт получил деньги от {sig['payers']} незнакомых людей и переводит их "
                                     f"{sig['payees']} новым получателям: похоже на кассу онлайн-казино")

    # «Аренда ИП»: вместе с этим переводом частным лицам уходит большая часть денег незнакомых плательщиков.
    # Получатель «новый», если до этой недели ИП ему не переводил: арендатор гонит деньги на одни и те же 1–3 карты.
    # Признак сам по себе останавливает перевод: в нём уже три условия (см. rental_signal).
    if (business and _kind(conn, receiver_id) == "person"
            and not _had_contact(conn, sender_id, receiver_id, (now - timedelta(days=RENT_DAYS)).isoformat())):
        sig = rental_signal(conn, sender_id, now, extra=amount)
        if sig:
            add("ip_rental", 60, f"Счёт ИП за неделю получил деньги от {sig['payers']} незнакомых людей, и {sig['share']}% "
                                 "уходит частным лицам: похоже на «аренду ИП»")

    # Внешние сигналы: база Антифрод-центра и оператор связи («Телеком Антифрод»).
    if afc_status(conn, receiver_id) == "grey":
        add("afc_grey", 50, "Получатель в списке подозрительных счетов Антифрод-центра")
    if call_active(conn, sender_id, now):
        add("suspicious_call", 50, "Во время перевода клиенту звонят с номера, который оператор связи пометил как подозрительный")
    if new and scam_message(conn, sender_id, now):
        add("scam_message", 40, "Клиент недавно проверял сообщение, и оно похоже на схему мошенников")
    # «Перевод под диктовку»: клиента по шагам ведут по телефону или в мессенджере.
    if new and session:
        for code, points, text in SESSION_SIGNALS:
            if session.get(code):
                add(code, points, text)

    if incoming is not None:
        hold = score >= HOLD_THRESHOLD
    else:
        hold = score >= HOLD_THRESHOLD_NO_INCOMING
    return {
        "score": min(score, 100),
        "decision": "hold" if hold else "allow",
        "reasons": reasons,
        "incoming_id": incoming["id"] if incoming else None,
    }


def recruitment_signal(conn, client_id: int, now: datetime) -> dict | None:
    """Похоже, что счёт используют для приёма чужих денег: несколько незнакомцев за неделю.

    Одних входящих мало (так выглядит и честный продавец), поэтому нужен ещё один признак:
    счёт открыт недавно или деньги уже пересылались новым получателям.
    """
    since = (now - timedelta(days=RECRUIT_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT t.* FROM transfers t WHERE t.receiver_id=? AND t.status='done' AND t.return_of IS NULL
             AND t.amount >= ? AND t.created_at >= ? AND t.created_at <= ?
             AND NOT EXISTS (SELECT 1 FROM transfers r WHERE r.return_of=t.id AND r.status='done')
           ORDER BY t.created_at""", (client_id, RECRUIT_MIN_AMOUNT, since, now.isoformat())).fetchall()
    strangers = [r for r in rows if not _had_contact(conn, client_id, r["sender_id"], r["created_at"], r["id"])]
    senders = {r["sender_id"] for r in strangers}
    if len(senders) < RECRUIT_SENDERS:
        return None
    client = conn.execute("SELECT account_opened_at FROM clients WHERE id=?", (client_id,)).fetchone()
    young = now - _dt(client["account_opened_at"]) < timedelta(days=RECRUIT_ACCOUNT_DAYS)
    forwarded = any(find_forward(conn, client_id, r) for r in strangers)
    if not (young or forwarded):
        return None
    return {"senders": len(senders), "amount": sum(r["amount"] for r in strangers),
            "incoming_ids": [r["id"] for r in strangers], "last_at": strangers[-1]["created_at"],
            "young": young, "forwarded": forwarded}


def scam_message(conn, client_id: int, now: datetime) -> str | None:
    """Схема из сообщения, которое клиент проверил за последний час (если оно похоже на мошенничество)."""
    since = (now - timedelta(minutes=SCAM_MESSAGE_MINUTES)).isoformat()
    row = conn.execute("""SELECT scheme FROM message_checks WHERE client_id=? AND verdict='danger'
                          AND at >= ? AND at <= ? ORDER BY at DESC LIMIT 1""",
                       (client_id, since, now.isoformat())).fetchone()
    return row["scheme"] if row else None


def _kind(conn, client_id: int) -> str | None:
    row = conn.execute("SELECT kind FROM clients WHERE id=?", (client_id,)).fetchone()
    return row["kind"] if row else None


def rental_signal(conn, client_id: int, now: datetime, extra: int = 0) -> dict | None:
    """Похоже, что счётом ИП пользуется другой человек («аренда ИП»).

    Нужны все три признака: за неделю платили RENT_PAYERS и больше частных лиц, которые раньше не платили этому ИП;
    RENT_TRANSIT и больше всех поступлений от частных лиц за неделю сразу ушло частным лицам, которым ИП до этой
    недели не переводил (`extra` — перевод, который сейчас проверяет движок); поведение необычно для этого счёта —
    ИП почти не работал, открыт недавно или раньше частным лицам деньги почти не уходили.

    Честный магазин тоже получает деньги от незнакомцев, но платит поставщикам. Честная скупка платит частным
    лицам, но так было всегда. Разовая оплата частному лицу (ремонт) идёт из выручки за неделю и больше, а не из
    денег, пришедших только что, поэтому транзитом не считается.
    """
    def fresh_in(at: str) -> int:
        """Сколько денег от частных лиц пришло за RENT_FAST_HOURS до этого момента."""
        return conn.execute(
            """SELECT COALESCE(SUM(t.amount),0) AS s FROM transfers t JOIN clients s ON s.id=t.sender_id
               WHERE t.receiver_id=? AND t.status='done' AND t.return_of IS NULL AND s.kind='person'
                 AND t.created_at >= ? AND t.created_at <= ?""",
            (client_id, (_dt(at) - timedelta(hours=RENT_FAST_HOURS)).isoformat(), at)).fetchone()["s"]

    c = conn.execute("SELECT kind, account_opened_at FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c or c["kind"] != "ip":
        return None
    since = (now - timedelta(days=RENT_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT t.sender_id, t.amount FROM transfers t JOIN clients s ON s.id=t.sender_id
           WHERE t.receiver_id=? AND t.status='done' AND t.return_of IS NULL AND s.kind='person'
             AND t.created_at >= ? AND t.created_at <= ?
             AND NOT EXISTS (SELECT 1 FROM transfers r WHERE r.return_of=t.id AND r.status='done')""",
        (client_id, since, now.isoformat())).fetchall()
    senders = {r["sender_id"] for r in rows if not _had_contact(conn, client_id, r["sender_id"], since)}
    if len(senders) < RENT_PAYERS:
        return None
    # Долю транзита считаем от всех поступлений от частных лиц: у честного магазина много постоянных покупателей.
    inflow = sum(r["amount"] for r in rows)
    payers = {r["sender_id"] for r in rows}
    # Возврат денег самим плательщикам (отказ от покупки) — не транзит.
    outs = conn.execute(
        """SELECT t.receiver_id, t.amount, t.created_at FROM transfers t JOIN clients r ON r.id=t.receiver_id
           WHERE t.sender_id=? AND t.status IN ('done','pending') AND t.return_of IS NULL AND r.kind='person'
             AND t.created_at >= ? AND t.created_at <= ?""", (client_id, since, now.isoformat())).fetchall()
    fast = [o for o in outs if o["receiver_id"] not in payers
            and not _had_contact(conn, client_id, o["receiver_id"], since)
            and fresh_in(o["created_at"]) >= RENT_FAST_SHARE * o["amount"]]
    receivers = {o["receiver_id"] for o in fast}
    if extra and fresh_in(now.isoformat()) < RENT_FAST_SHARE * extra:
        extra = 0
    transit = sum(o["amount"] for o in fast) + extra
    if transit < RENT_TRANSIT * inflow:
        return None
    age = (now - _dt(c["account_opened_at"])).days
    prev = ((now - timedelta(days=RENT_DAYS + RENT_QUIET_DAYS)).isoformat(), since)
    before = conn.execute(
        """SELECT COALESCE(SUM(amount),0) AS s FROM transfers WHERE status='done' AND created_at >= ? AND created_at < ?
           AND (sender_id=? OR receiver_id=?)""", (*prev, client_id, client_id)).fetchone()["s"]
    prev_in, prev_out = (conn.execute(
        f"""SELECT COALESCE(SUM(t.amount),0) AS s FROM transfers t JOIN clients p ON p.id=t.{side}
            WHERE t.{own}=? AND t.status='done' AND t.return_of IS NULL AND p.kind='person'
              AND t.created_at >= ? AND t.created_at < ?""", (client_id, *prev)).fetchone()["s"]
        for side, own in (("sender_id", "receiver_id"), ("receiver_id", "sender_id")))
    quiet, young = before < RENT_QUIET_SHARE * inflow, age < RENT_YOUNG_DAYS
    changed = not quiet and prev_in > 0 and prev_out < RENT_QUIET_SHARE * prev_in
    if not (quiet or young or changed):
        return None
    share = min(100, round(100 * transit / inflow))
    money = lambda n: f"{n:,}".replace(",", " ") + " ₸"   # noqa: E731
    factors = [f"За {RENT_DAYS} дней платили {len(senders)} частных лиц, которые раньше не платили этому ИП; "
               f"всего от частных лиц поступило {money(inflow)}",
               f"{share}% этих денег сразу ушло частным лицам, которым ИП раньше не переводил "
               f"(получателей: {len(receivers)})"]
    if quiet:
        factors.append(f"До этого ИП почти не работал: оборот за {RENT_QUIET_DAYS} дней — {money(before)}")
    if changed:
        factors.append(f"Раньше частным лицам уходило {round(100 * prev_out / prev_in)}% поступлений, "
                       f"а теперь {share}%: счёт стал работать по-другому")
    if young:
        factors.append(f"Счёт ИП открыт {age} дн. назад")
    return {"payers": len(senders), "inflow": inflow, "transit": transit, "share": share, "receivers": len(receivers),
            "quiet": quiet, "young": young, "changed": changed, "age_days": age, "before": before, "factors": factors}


def casino_signal(conn, client_id: int, now: datetime, extra_receiver: int | None = None) -> dict | None:
    """Похоже, что через счёт принимают ставки и платят выигрыши нелегального онлайн-казино.

    Нужны все три признака за неделю: платили CASINO_PAYERS и больше людей, которые раньше не платили (средний
    платёж до CASINO_AVG); деньги ушли CASINO_PAYEES и больше новым получателям; CASINO_TWOWAY и больше незнакомцев
    и платили, и получали деньги обратно — как ставка и выигрыш. Честный продавец получает от многих, но платит
    немногим; бригадир платит многим, но получает от немногих; сбор на поездку уходит одному получателю;
    перекупщик покупает у одних, а продаёт другим. `extra_receiver` — получатель проверяемого перевода.
    """
    c = conn.execute("SELECT kind FROM clients WHERE id=?", (client_id,)).fetchone()
    # Только частные лица: у ИП честная скупка выглядит так же, для ИП работает «аренда ИП».
    if not c or c["kind"] != "person":
        return None
    since = (now - timedelta(days=CASINO_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT t.sender_id, t.amount FROM transfers t JOIN clients s ON s.id=t.sender_id
           WHERE t.receiver_id=? AND t.status='done' AND t.return_of IS NULL AND s.kind='person'
             AND t.created_at >= ? AND t.created_at <= ?""", (client_id, since, now.isoformat())).fetchall()
    if len(rows) < CASINO_PAYERS:
        return None
    payers = {r["sender_id"] for r in rows if not _had_contact(conn, client_id, r["sender_id"], since)}
    if len(payers) < CASINO_PAYERS or sum(r["amount"] for r in rows) / len(rows) > CASINO_AVG:
        return None
    outs = conn.execute(
        """SELECT t.receiver_id, t.amount FROM transfers t JOIN clients r ON r.id=t.receiver_id
           WHERE t.sender_id=? AND t.status IN ('done','pending') AND t.return_of IS NULL AND r.kind='person'
             AND t.created_at >= ? AND t.created_at <= ?""", (client_id, since, now.isoformat())).fetchall()
    payees = {o["receiver_id"] for o in outs if not _had_contact(conn, client_id, o["receiver_id"], since)}
    if extra_receiver is not None and not _had_contact(conn, client_id, extra_receiver, since):
        payees.add(extra_receiver)
    twoway = payees & payers
    if len(payees) < CASINO_PAYEES or len(twoway) < CASINO_TWOWAY:
        return None
    money = lambda n: f"{n:,}".replace(",", " ") + " ₸"   # noqa: E731
    inflow, avg = sum(r["amount"] for r in rows), sum(r["amount"] for r in rows) // len(rows)
    paid = sum(o["amount"] for o in outs if o["receiver_id"] in payees)
    return {"payers": len(payers), "payees": len(payees), "twoway": len(twoway), "inflow": inflow, "avg": avg, "paid": paid,
            "factors": [f"За {CASINO_DAYS} дней платили {len(payers)} человек, которые раньше не платили: "
                        f"{len(rows)} платежей на {money(inflow)}, в среднем {money(avg)}",
                        f"Деньги ушли {len(payees)} новым получателям: {money(paid)}",
                        f"{len(twoway)} из них и платили, и получали деньги обратно — как ставка и выигрыш",
                        "Так работает касса нелегального онлайн-казино: ставки игроков приходят на карту, "
                        "выигрыши уходят другим игрокам"]}


def dropper_marks(conn, ids) -> dict:
    """Что банк уже знает о счетах: база Антифрод-центра, ограничения, пересылка чужих денег, которую сотрудник отменил.

    Одна остановка перевода — ещё не признак: честного клиента тоже иногда останавливают, и он сам всё объясняет.
    """
    ids = list(ids)
    out: dict[int, list[str]] = {i: [] for i in ids}
    for part in range(0, len(ids), 500):
        chunk = ids[part:part + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT client_id, list FROM afc_list WHERE client_id IN ({q})", chunk):
            out[r["client_id"]].append("в чёрном списке Антифрод-центра" if r["list"] == "black"
                                       else "в сером списке Антифрод-центра")
        for r in conn.execute(f"SELECT id, status FROM clients WHERE id IN ({q}) AND status != 'active'", chunk):
            out[r["id"]].append("счёт приостановлен" if r["status"] == "blocked" else "заморожена спорная сумма")
        seen = set()
        for r in conn.execute(f"""SELECT client_id, reasons FROM cases WHERE client_id IN ({q}) AND kind='intercept'
                                  AND status='cancelled'""", chunk):
            if r["client_id"] not in seen and any(x.get("code") in DROPPER_CODES for x in json.loads(r["reasons"])):
                seen.add(r["client_id"])
                out[r["client_id"]].append("пересылал чужие деньги, сотрудник отменил перевод")
    return out


def farm_devices(conn, now: datetime) -> list[dict]:
    """Дроп-фермы: устройства, с которых входят в приложение счета нескольких разных людей.

    Семья на общем планшете — это 2–3 давних счёта без признаков дроппера, поэтому нужно FARM_ACCOUNTS и больше
    счетов и ещё одно из двух: у FARM_MARKED из них банк уже видел признаки дроппера (база Антифрод-центра,
    ограничения, остановленная пересылка) или FARM_YOUNG и больше счетов открыты меньше FARM_YOUNG_DAYS дней назад.
    Три студента с новыми счетами на одном ноутбуке в общежитии так не отмечаются.
    """
    rows = conn.execute("""SELECT d.device_id, d.client_id, d.model, c.account_opened_at FROM devices d
                           JOIN clients c ON c.id=d.client_id WHERE c.kind != 'external'
                           ORDER BY d.device_id, d.client_id""").fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["device_id"], []).append(r)
    cand = {d: rs for d, rs in by.items() if len(rs) >= FARM_ACCOUNTS}
    marks = dropper_marks(conn, {r["client_id"] for rs in cand.values() for r in rs})
    out = []
    for d, rs in cand.items():
        ids = [r["client_id"] for r in rs]
        young = [r["client_id"] for r in rs if now - _dt(r["account_opened_at"]) < timedelta(days=FARM_YOUNG_DAYS)]
        marked = [i for i in ids if marks[i]]
        if len(marked) < FARM_MARKED and len(young) < FARM_YOUNG:
            continue
        factors = [f"С одного телефона ({rs[0]['model']}) входят в приложение {len(ids)} разных людей"]
        if young:
            factors.append(f"Счетов, открытых меньше {FARM_YOUNG_DAYS} дней назад: {len(young)}")
        if marked:
            factors.append(f"У {len(marked)} из них банк уже видел признаки дроппера")
        out.append({"device_id": d, "model": rs[0]["model"], "accounts": ids, "young": young, "marked": marked,
                    "marks": {i: marks[i] for i in ids}, "factors": factors})
    return out


def fresh_loan(conn, client_id: int, now: datetime):
    """Последний непогашенный кредит, выданный за последние LOAN_HOURS часов."""
    since = (now - timedelta(hours=LOAN_HOURS)).isoformat()
    return conn.execute("""SELECT * FROM loans WHERE client_id=? AND repaid_at IS NULL AND issued_at >= ? AND issued_at <= ?
                           ORDER BY issued_at DESC LIMIT 1""", (client_id, since, now.isoformat())).fetchone()


def afc_status(conn, client_id: int) -> str | None:
    row = conn.execute("SELECT list FROM afc_list WHERE client_id=?", (client_id,)).fetchone()
    return row["list"] if row else None


def call_active(conn, client_id: int, now: datetime) -> bool:
    row = conn.execute("SELECT started_at FROM call_signals WHERE client_id=?", (client_id,)).fetchone()
    return bool(row) and timedelta(0) <= now - _dt(row["started_at"]) <= timedelta(minutes=CALL_SIGNAL_MINUTES)


def find_forward(conn, client_id: int, incoming) -> list:
    """Исходящие, которыми клиент переслал деньги этого входящего дальше (одним переводом или частями).

    Учитываются переводы в течение окна получателям, с которыми до входящего не было переводов.
    Пусто, если пересылки не было.
    """
    limit = (_dt(incoming["created_at"]) + timedelta(hours=LOOKBACK_HOURS)).isoformat()
    rows = conn.execute(
        """SELECT * FROM transfers WHERE sender_id=? AND status='done' AND return_of IS NULL
           AND receiver_id != ? AND created_at > ? AND created_at <= ? ORDER BY created_at, id""",
        (client_id, incoming["sender_id"], incoming["created_at"], limit)).fetchall()
    taken, total = [], 0
    for r in rows:
        if _had_contact(conn, client_id, r["receiver_id"], incoming["created_at"]):
            continue
        taken.append(r)
        total += r["amount"]
        if total >= 0.8 * incoming["amount"]:
            return taken if total <= 1.1 * incoming["amount"] else []
    return []


def forward_episodes(conn, client_id: int, until: datetime, days: int = 90) -> list[dict]:
    """Эпизоды «получил от незнакомца и переслал третьему лицу» за последние дни."""
    since = (until - timedelta(days=days)).isoformat()
    incomings = conn.execute(
        """SELECT * FROM transfers WHERE receiver_id=? AND status='done' AND return_of IS NULL
           AND created_at >= ? AND created_at <= ? ORDER BY created_at""",
        (client_id, since, until.isoformat())).fetchall()
    episodes = []
    for inc in incomings:
        if _had_contact(conn, client_id, inc["sender_id"], inc["created_at"], inc["id"]):
            continue
        limit = (_dt(inc["created_at"]) + timedelta(hours=LOOKBACK_HOURS)).isoformat()
        out = conn.execute(
            """SELECT * FROM transfers WHERE sender_id=? AND status='done' AND receiver_id != ? AND return_of IS NULL
               AND created_at > ? AND created_at <= ? AND amount BETWEEN ? AND ?
               ORDER BY created_at LIMIT 1""",
            (client_id, inc["sender_id"], inc["created_at"], limit,
             0.8 * inc["amount"], 1.02 * inc["amount"])).fetchone()
        if out:
            episodes.append({"incoming_id": inc["id"], "outgoing_id": out["id"], "from": inc["sender_id"]})
    return episodes


def assess_role(conn, client_id: int, case, now: datetime) -> dict:
    """Подсказка аналитику: невольный участник или организатор. Решение остаётся за человеком."""
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    factors: list[dict] = []
    score = 0

    def add(points: int, text: str):
        nonlocal score
        score += points
        factors.append({"points": points, "text": text})

    age = (now - _dt(client["account_opened_at"])).days
    if age >= 180:
        add(2, f"Счёт открыт давно: {age} дн. назад")
    elif age < 30:
        add(-2, f"Счёт открыт недавно: {age} дн. назад")

    episodes = forward_episodes(conn, client_id, now)
    if not episodes:
        pass                      # деньги остались на счёте: о пересылке судить не по чему
    elif len(episodes) == 1:
        add(2, "Пересылка чужих денег была один раз")
    elif len(episodes) >= 3:
        add(-3, f"Пересылка чужих денег повторялась, эпизодов за 90 дней: {len(episodes)}")
    else:
        add(-1, "Пересылка чужих денег была дважды")
    senders = {e["from"] for e in episodes}
    if len(senders) >= 3:
        add(-2, f"Деньги приходили от {len(senders)} разных незнакомых людей")

    first = min((e["incoming_id"] for e in episodes), default=case["incoming_transfer_id"])
    before = conn.execute("SELECT created_at FROM transfers WHERE id=?", (first,)).fetchone() if first else None
    cutoff = before["created_at"] if before else now.isoformat()
    usual = conn.execute(
        """SELECT COUNT(*) AS n FROM transfers WHERE status='done' AND created_at < ?
           AND (sender_id=? OR receiver_id=?)""", (cutoff, client_id, client_id)).fetchone()["n"]
    if usual >= 5:
        add(1, f"До этого счётом пользовались обычно: {usual} переводов")
    elif usual == 0:
        add(-1, "До этого по счёту не было ни одного перевода")

    warned = conn.execute("SELECT at FROM recruit_marks WHERE client_id=? AND action='dismissed' AND at <= ?",
                          (client_id, case["created_at"])).fetchone()
    if warned:
        add(-1, "Клиента предупреждали о признаках вербовки, он ответил, что знает отправителей")
    if rental_signal(conn, client_id, now):
        add(-2, "Похоже на «аренду ИП»: деньги многих незнакомых людей сразу уходят частным лицам")

    docs = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE case_id=?", (case["id"],)).fetchone()["n"]
    if case["explanation"] and docs:
        add(1, "Клиент сам объяснил ситуацию и приложил документы")

    if conn.execute("SELECT 1 FROM self_reports WHERE client_id=? AND at <= ?",
                    (client_id, case["created_at"])).fetchone():
        add(2, "Клиент сам сообщил банку, что его втянули в схему, до жалоб и ограничений")
    if conn.execute("SELECT 1 FROM events WHERE case_id=? AND action='refunded' AND actor LIKE 'client:%'",
                    (case["id"],)).fetchone():
        add(1, "Клиент сам вернул пострадавшему деньги, которые остались на счёте")

    if score >= 3:
        verdict, label = "unwitting", "Похоже на невольного участника"
    elif score <= -1:
        verdict, label = "organizer", "Похоже на организатора или осознанного дроппера"
    else:
        verdict, label = "unclear", "Данных недостаточно, нужна ручная проверка"
    return {"verdict": verdict, "label": label, "score": score, "factors": factors, "episodes": len(episodes)}
