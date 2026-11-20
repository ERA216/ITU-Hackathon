"""Сквозные тесты. Каждый тест получает свою копию базы, порядок запуска не важен."""
import base64
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

_tmp = tempfile.mkdtemp()
os.environ["CA_DB"] = os.path.join(_tmp, "boot.db")
os.environ["CA_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ["CA_ANALYST_PIN"] = "4321"
os.environ["CA_DEMO_HISTORY"] = "0"      # история за 30 дней проверяется отдельно, остальным тестам она не нужна
os.environ["CA_DOC_KEY_FILE"] = os.path.join(_tmp, "doc.key")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, engine, main, seed  # noqa: E402

TEMPLATE = os.path.join(_tmp, "template.db")
_conn, STATS = seed.build(TEMPLATE, verbose=False)
_conn.close()

STUDENT, STRANGER, DROP_CARD, MOM, SELLER, VICTIM, TRANSIT = 1001, 1002, 1003, 1004, 1005, 1006, 1007
FAKE_INVEST, SAFE_ACCOUNT = 1008, 1009
RECRUIT, SUPPLIER, CARD_GIVER = 1010, 1011, 1012
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
B64 = lambda data: base64.b64encode(data).decode()  # noqa: E731


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    shutil.copy(TEMPLATE, tmp_path / "t.db")
    main.conn = db.connect(str(tmp_path / "t.db"))
    main.UPLOADS = str(tmp_path / "uploads")
    main.SESSIONS.clear()
    main._pin_failures.clear()
    yield
    main.conn.close()


def client(cid):
    c = TestClient(main.app)
    assert c.post("/login", json={"role": "client", "client_id": cid}).status_code == 200
    return c


def analyst(employee="aruzhan"):
    c = TestClient(main.app)
    assert c.post("/login", json={"role": "analyst", "pin": "4321", "employee": employee}).status_code == 200
    return c


def second_key(view):
    """Правило двух ключей: действие, которое запросил первый сотрудник, подтверждает второй."""
    r = analyst("medet").post(f"/analyst/approvals/{view['approval']['id']}/confirm")
    assert r.status_code == 200, r.text
    return r.json()


def send(c, to, amount):
    return c.post("/transfers", json={"receiver_id": to, "amount": amount})


def account(c, cid):
    return c.get(f"/clients/{cid}").json()


def feed_item(c, cid, sender_id):
    return next(t for t in account(c, cid)["feed"] if t["direction"] == "in" and t["sender_id"] == sender_id)


def block_seller():
    """Гульнар жалуется на перевод Данияру; он уже переслал деньги, поэтому его блокируют."""
    victim = client(VICTIM)
    tid = next(t["id"] for t in account(victim, VICTIM)["feed"] if t["receiver_id"] == SELLER and t["amount"] == 200000)
    r = victim.post(f"/transfers/{tid}/complaint").json()
    assert r["outcome"] == "blocked"
    return victim, tid


# ---------- Движок ----------

def test_engine_quality_on_synthetic_data():
    assert STATS["tri_caught"] / STATS["tri"] >= 0.75
    assert STATS["honest_held"] / STATS["honest"] <= 0.01


def test_return_to_same_sender_is_never_held():
    c = db.connect(":memory:")
    now = datetime(2026, 11, 12, 12, 0)
    for i in (1, 2):
        c.execute("INSERT INTO clients(id,name,account_opened_at) VALUES(?,?,?)",
                  (i, f"c{i}", (now - timedelta(days=500)).isoformat()))
    c.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at) VALUES(2,1,90000,?)",
              ((now - timedelta(minutes=30)).isoformat(),))
    assert engine.evaluate(c, 1, 2, 90000, now)["decision"] == "allow"


def test_splitting_the_amount_is_caught_on_the_second_part():
    st = client(STUDENT)
    # Гульнар — получатель со старым счётом, с которым у студентки не было переводов.
    first = send(st, VICTIM, 80000).json()
    assert first["status"] == "done"
    second = send(st, VICTIM, 70000).json()
    assert second["status"] == "held"
    assert "split_forward" in {r["code"] for r in second["reasons"]}


# ---------- Деньги ----------

def test_ordinary_transfer_passes():
    assert send(client(STUDENT), MOM, 3000).json()["status"] == "done"


def test_scheme_is_held_and_safely_returned_once():
    st, an = client(STUDENT), analyst()
    before = account(an, STRANGER)["balance"]
    drop_before = account(an, DROP_CARD)["balance"]
    r = send(st, DROP_CARD, 150000).json()
    assert r["status"] == "held" and r["safe_return"]["to_id"] == STRANGER
    assert {"stranger_incoming", "not_to_sender", "amount_match"} <= {x["code"] for x in r["reasons"]}
    assert account(an, DROP_CARD)["balance"] == drop_before   # дропперу ничего не ушло

    ret = st.post(f"/cases/{r['case_id']}/return-to-sender").json()
    assert ret["status"] == "returned" and ret["amount"] == 150000
    assert account(an, STRANGER)["balance"] == before + 150000
    assert st.post(f"/cases/{r['case_id']}/return-to-sender").status_code == 409
    assert account(st, STUDENT)["balance"] == 42000


def test_paused_transfer_reserves_money():
    st = client(STUDENT)
    assert send(st, DROP_CARD, 150000).json()["status"] == "held"
    acc = account(st, STUDENT)
    assert acc["held"] == 150000 and acc["available"] == 42000
    assert send(st, MOM, 100000).status_code == 400        # зарезервированное потратить нельзя
    assert send(st, MOM, 40000).json()["status"] == "done"


def test_repeated_attempt_reuses_the_case_and_cannot_return_twice():
    st, an = client(STUDENT), analyst()
    a = send(st, DROP_CARD, 150000).json()
    b = send(st, DROP_CARD, 140000).json()
    assert a["case_id"] == b["case_id"]
    assert account(st, STUDENT)["held"] == 140000          # первая попытка отменена, резерв один
    assert len([c for c in an.get("/analyst/cases").json() if c["client_id"] == STUDENT]) == 1
    assert st.post(f"/cases/{b['case_id']}/return-to-sender").status_code == 200
    assert st.post(f"/cases/{b['case_id']}/return-to-sender").status_code == 409
    assert account(st, STUDENT)["balance"] == 42000        # вернули ровно один раз


def test_return_is_refused_when_money_is_spent_and_nothing_changes():
    st = client(STUDENT)
    assert send(st, MOM, 30000).json()["status"] == "done"
    assert send(st, MOM, 20000).json()["status"] == "done"   # часть чужих денег потрачена
    r = send(st, DROP_CARD, 140000).json()
    assert r["status"] == "held"
    before = account(st, STUDENT)
    refused = st.post(f"/cases/{r['case_id']}/return-to-sender")
    assert refused.status_code == 400 and "142 000" in refused.json()["detail"]
    after = account(st, STUDENT)
    assert after["balance"] == before["balance"] == 142000 and after["held"] == 140000
    assert st.get(f"/cases/{r['case_id']}").json()["case_status"] == "open"


# ---------- Вход и права ----------

def test_anonymous_and_foreign_access_is_refused():
    anon = TestClient(main.app)
    assert anon.get(f"/clients/{STUDENT}").status_code == 401
    assert anon.get("/analyst/cases").status_code == 401
    assert send(anon, MOM, 1000).status_code == 401

    st, mom = client(STUDENT), client(MOM)
    assert mom.get(f"/clients/{STUDENT}").status_code == 403
    assert mom.get(f"/clients/{STUDENT}/case").status_code == 403
    assert mom.get("/analyst/cases").status_code == 401
    assert mom.post("/demo/reset").status_code == 401
    r = send(st, DROP_CARD, 150000).json()
    for path in ("return-to-sender", "insist"):
        assert mom.post(f"/cases/{r['case_id']}/{path}").status_code == 404
    assert mom.get(f"/cases/{r['case_id']}").status_code == 404
    # Отправитель берётся из сессии: подставить чужой счёт в запрос нельзя.
    forged = mom.post("/transfers", json={"sender_id": STUDENT, "receiver_id": STRANGER, "amount": 1000}).json()
    assert forged["status"] == "done"
    # У Мамы ещё 1 500 000 ₸ сбережений в «Сейфе».
    assert account(st, STUDENT)["balance"] == 192000 and account(mom, MOM)["balance"] == 1649000


def test_analyst_code_is_checked_and_rate_limited():
    anon = TestClient(main.app)
    for _ in range(5):
        assert anon.post("/login", json={"role": "analyst", "pin": "0000"}).status_code == 401
    assert anon.post("/login", json={"role": "analyst", "pin": "4321"}).status_code == 429
    assert anon.post("/login", json={"role": "client", "client_id": 5}).status_code == 403


# ---------- Демо-данные ----------

def test_empty_database_is_filled_on_first_request(tmp_path):
    main.conn.close()
    main.conn = db.connect(str(tmp_path / "empty.db"))
    assert len(TestClient(main.app).get("/demo/clients").json()) == 22


def test_stale_demo_data_is_recreated():
    old = (datetime.now() - timedelta(days=4)).replace(microsecond=0).isoformat()
    main.conn.execute("UPDATE meta SET value=? WHERE key='seeded_at'", (old,))
    main.conn.execute("UPDATE transfers SET created_at=? WHERE scenario='demo_in'", (old,))
    main.conn.commit()
    TestClient(main.app).get("/demo/clients")
    assert send(client(STUDENT), DROP_CARD, 150000).json()["status"] == "held"


def test_analyst_can_reset_demo():
    st, an = client(STUDENT), analyst()
    send(st, MOM, 5000)
    assert an.post("/demo/reset").status_code == 200
    assert account(an, STUDENT)["balance"] == 192000
    assert st.get(f"/clients/{STUDENT}").status_code == 401     # клиентские сессии сброшены


# ---------- Апелляция и документы ----------

def test_uploads_are_restricted():
    block_seller()
    sel, mom, an = client(SELLER), client(MOM), analyst()
    cid = sel.get(f"/clients/{SELLER}/case").json()["case_id"]
    up = lambda name, data, kind="chat", who=sel: who.post(  # noqa: E731
        f"/cases/{cid}/documents", json={"kind": kind, "filename": name, "content_base64": B64(data)})
    assert up("x.html", b"<script>alert(1)</script>").status_code == 400
    assert up("x.png", b"not really a png").status_code == 400
    assert up("x.png", PNG, who=mom).status_code == 404
    ok = up("../../чат.html", PNG).json()
    assert ok["filename"].endswith(".png") and "/" not in ok["filename"] and "html" not in ok["filename"]

    assert mom.get(f"/documents/{ok['id']}").status_code == 404
    got = an.get(f"/documents/{ok['id']}")
    assert got.content == PNG and got.headers["content-type"] == "image/png"
    assert "attachment" in got.headers["content-disposition"]
    # На диске документ лежит зашифрованным и без имени клиента.
    stored = os.listdir(main.UPLOADS)
    assert stored == [f"{ok['id']}.enc"]
    raw = open(os.path.join(main.UPLOADS, stored[0]), "rb").read()
    assert PNG not in raw and b"PNG" not in raw
    with open(os.path.join(main.UPLOADS, stored[0]), "wb") as f:     # подмена файла на диске
        f.write(raw[:-5] + b"xxxxx")
    assert an.get(f"/documents/{ok['id']}").status_code == 409
    assert got.headers["x-content-type-options"] == "nosniff"

    for _ in range(main.MAX_DOCS - 1):
        assert up("a.png", PNG, kind="other").status_code == 200
    assert up("a.png", PNG, kind="other").status_code == 400      # лимит файлов
    big = sel.post(f"/cases/{cid}/documents", content=b"x" * (main.MAX_BODY + 1),
                   headers={"content-type": "application/json"})
    assert big.status_code == 413


def test_blocked_client_appeals_and_analyst_decides():
    sel, an = client(SELLER), analyst()
    assert send(sel, MOM, 1000).json()["status"] == "done"      # до жалобы Данияр ничего не знает
    block_seller()
    assert send(sel, DROP_CARD, 1000).status_code == 403
    case = sel.get(f"/clients/{SELLER}/case").json()
    assert case["status"] == "open" and "200 000 ₸" in case["summary"] and "40 мин" in case["summary"]
    cid = case["case_id"]
    text = "Покупатель оплатил товар и попросил вернуть деньги на другую карту."
    assert sel.post(f"/cases/{cid}/submit", json={"explanation": "не я"}).status_code == 400
    assert sel.post(f"/cases/{cid}/submit", json={"explanation": text}).status_code == 400   # нет переписки
    doc = {"kind": "chat", "filename": "chat.png", "content_base64": B64(PNG)}
    assert sel.post(f"/cases/{cid}/documents", json=doc).status_code == 200
    submitted = sel.post(f"/cases/{cid}/submit", json={"explanation": text}).json()
    assert submitted["status"] == "under_review" and submitted["due_at"] > submitted["submitted_at"]
    assert sel.post(f"/cases/{cid}/documents", json=doc).status_code == 409

    by_client = {c["client_id"]: c for c in an.get("/analyst/cases").json()}
    assert by_client[SELLER]["role"]["verdict"] == "unwitting"
    assert by_client[TRANSIT]["role"]["verdict"] == "organizer" and by_client[TRANSIT]["role"]["episodes"] == 4
    card = an.get(f"/analyst/cases/{cid}").json()
    assert card["incoming"]["amount"] == 200000 and card["explanation"] and card["documents"]

    transit = by_client[TRANSIT]["case_id"]
    assert an.post(f"/analyst/cases/{transit}/decision", json={"decision": "reject"}).status_code == 400
    kept = an.post(f"/analyst/cases/{transit}/decision",
                   json={"decision": "reject", "comment": "Четыре пересылки от разных людей за пять дней."}).json()
    assert kept["status"] == "kept" and kept["client_status"] == "blocked"

    done = an.post(f"/analyst/cases/{cid}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "unblocked" and done["client_status"] == "active"
    actions = [h["action"] for h in done["history"]]
    assert actions == ["complaint", "blocked", "document", "appeal", "decision", "cleared"]
    assert done["history"][-2]["details"]["role"] == "Похоже на невольного участника"
    assert an.post(f"/analyst/cases/{cid}/decision", json={"decision": "approve"}).status_code == 409
    assert sel.get(f"/clients/{SELLER}/case").json()["status"] == "unblocked"
    assert send(sel, MOM, 1000).json()["status"] == "done"


def test_client_sees_what_happened_to_a_paused_transfer():
    st, an = client(STUDENT), analyst()
    r = send(st, DROP_CARD, 150000).json()
    row = lambda: next(t for t in account(st, STUDENT)["feed"] if t["id"] == r["transfer_id"])  # noqa: E731
    assert row()["case_status"] == "open"
    queue = {c["case_id"]: c for c in an.get("/analyst/cases").json()}
    assert queue[r["case_id"]]["status_text"] == "Клиент ещё не выбрал действие"
    assert not queue[r["case_id"]]["needs_decision"]

    assert st.post(f"/cases/{r['case_id']}/insist").json()["status"] == "under_review"
    assert send(st, DROP_CARD, 150000).status_code == 409           # повторить в обход проверки нельзя
    out = an.post(f"/analyst/cases/{r['case_id']}/decision",
                  json={"decision": "reject", "comment": "Получатель похож на дропперский счёт."}).json()
    assert out["status"] == "cancelled"
    seen = row()
    assert seen["status"] == "cancelled" and seen["case_status"] == "cancelled"
    assert seen["decision_comment"] == "Получатель похож на дропперский счёт."
    acc = account(st, STUDENT)
    assert acc["held"] == 0 and acc["available"] == 192000          # резерв снят


def test_analyst_can_release_a_paused_transfer():
    st, an = client(STUDENT), analyst()
    drop_before = account(an, DROP_CARD)["balance"]
    r = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{r['case_id']}/insist")
    assert an.post(f"/analyst/cases/{r['case_id']}/decision", json={"decision": "approve"}).json()["status"] == "released"
    assert account(st, STUDENT)["balance"] == 42000 and account(an, DROP_CARD)["balance"] == drop_before + 150000


# ---------- Предупреждение, жалоба, сроки ----------

def test_incoming_from_stranger_is_flagged_and_can_be_returned_from_the_feed():
    st, an = client(STUDENT), analyst()
    item = feed_item(st, STUDENT, STRANGER)
    assert item["warning"] and not item["returned"]
    assert not feed_item(st, STUDENT, MOM)["warning"]                 # мама — знакомый отправитель
    before = account(an, STRANGER)["balance"]
    assert st.post(f"/incoming/{item['id']}/return").json()["amount"] == 150000
    assert account(an, STRANGER)["balance"] == before + 150000
    assert not feed_item(st, STUDENT, STRANGER)["warning"]
    assert st.post(f"/incoming/{item['id']}/return").status_code == 409
    # Вернуть чужой входящий нельзя.
    assert client(MOM).post(f"/incoming/{item['id']}/return").status_code == 404


def test_return_from_feed_closes_a_paused_forward():
    st = client(STUDENT)
    r = send(st, DROP_CARD, 150000).json()
    item = feed_item(st, STUDENT, STRANGER)
    assert st.post(f"/incoming/{item['id']}/return").status_code == 200
    assert st.get(f"/cases/{r['case_id']}").json()["case_status"] == "returned"
    acc = account(st, STUDENT)
    assert acc["held"] == 0 and acc["balance"] == 42000


def test_known_sender_hides_the_warning_but_not_the_interception():
    st = client(STUDENT)
    item = feed_item(st, STUDENT, STRANGER)
    assert st.post(f"/incoming/{item['id']}/known").status_code == 200
    assert not feed_item(st, STUDENT, STRANGER)["warning"]
    assert send(st, DROP_CARD, 150000).json()["status"] == "held"


def test_complaint_blocks_the_forwarder_and_creates_a_case():
    an = analyst()
    victim, tid = block_seller()
    queue = {c["client_id"]: c for c in an.get("/analyst/cases").json()}
    case = queue[SELLER]
    assert case["from_complaint"] and case["role"]["verdict"] == "unwitting"
    card = an.get(f"/analyst/cases/{case['case_id']}").json()
    assert card["reasons"][0]["code"] == "complaint" and card["outgoing"]["amount"] == 195000
    assert [h["action"] for h in card["history"]] == ["complaint", "blocked"]
    assert victim.post(f"/transfers/{tid}/complaint").status_code == 409
    assert client(MOM).post(f"/transfers/{tid}/complaint").status_code == 404   # жалуется только отправитель


def test_complaint_when_money_is_still_there_warns_the_receiver():
    stranger, st = client(STRANGER), client(STUDENT)
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    st.post(f"/incoming/{tid}/known")                                   # даже если получатель скрыл предупреждение
    r = stranger.post(f"/transfers/{tid}/complaint").json()
    assert r["outcome"] == "warned"
    item = feed_item(st, STUDENT, STRANGER)
    assert item["warning"] and item["complained"]
    assert account(st, STUDENT)["status"] == "active"


def test_complaint_after_return_says_so():
    stranger, st = client(STRANGER), client(STUDENT)
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    st.post(f"/incoming/{tid}/return")
    assert stranger.post(f"/transfers/{tid}/complaint").json()["outcome"] == "already_returned"


def test_overdue_appeals_go_first():
    an = analyst()
    block_seller()
    sel = client(SELLER)
    cid = sel.get(f"/clients/{SELLER}/case").json()["case_id"]
    sel.post(f"/cases/{cid}/documents", json={"kind": "chat", "filename": "c.png", "content_base64": B64(PNG)})
    sel.post(f"/cases/{cid}/submit", json={"explanation": "Покупатель оплатил товар и попросил вернуть деньги."})
    queue = an.get("/analyst/cases").json()
    assert not queue[0]["overdue"]
    main.conn.execute("UPDATE cases SET due_at=? WHERE id=?", ((datetime.now() - timedelta(hours=1)).isoformat(), cid))
    main.conn.commit()
    queue = an.get("/analyst/cases").json()
    assert queue[0]["case_id"] == cid and queue[0]["overdue"]


def test_old_database_is_rebuilt(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE cases (id INTEGER PRIMARY KEY, client_id INTEGER)")
    old.commit()
    old.close()
    main.conn.close()
    main.conn = db.connect(str(path))
    assert len(TestClient(main.app).get("/demo/clients").json()) == 22


def test_quality_page_is_served_with_current_threshold():
    anon = TestClient(main.app)
    assert anon.get("/quality").status_code == 200
    q = anon.get("/quality.json").json()
    assert q["threshold"] == engine.HOLD_THRESHOLD
    cur = next(c for c in q["curve"] if c["threshold"] == q["threshold"])
    low, mean, high = cur["recall"]
    assert low <= mean <= high and mean >= 75


def test_kazakh_language_for_client_messages():
    st = client(STUDENT)
    st.cookies.set("ca_lang", "kk")
    r = send(st, DROP_CARD, 150000).json()
    texts = {x["code"]: x["text"] for x in r["reasons"]}
    assert texts["stranger_incoming"].startswith("Жақында")
    assert r["safe_return"]["to_name"] == "Бейтаныс жіберуші"
    assert send(st, MOM, 10_000_000).json()["detail"].startswith("Кідіртілген")
    names = {p["id"]: p["name"] for p in st.get("/demo/clients").json()}
    assert names[MOM] == "Анам"

    block_seller()
    sel = client(SELLER)
    sel.cookies.set("ca_lang", "kk")
    case = sel.get(f"/clients/{SELLER}/case").json()
    assert case["status_text"] == "Құжаттарыңызды күтеміз" and "40 мин" in case["summary"]
    assert case["summary"].startswith("Сіз бұрын")
    # Без cookie всё по-русски, а записи в базе и панель аналитика не зависят от языка клиента.
    assert client(SELLER).get(f"/clients/{SELLER}/case").json()["status_text"] == "Ждём ваши документы"
    card = analyst().get(f"/analyst/cases/{case['case_id']}").json()
    assert card["reasons"][0]["text"].startswith("Отправитель денег")


def test_complaint_follows_the_chain_to_the_card_that_holds_the_money():
    an = analyst()
    victim, tid = block_seller()
    queue = {c["client_id"]: c for c in an.get("/analyst/cases").json()}
    assert SELLER in queue and DROP_CARD in queue
    holder = an.get(f"/analyst/cases/{queue[DROP_CARD]['case_id']}").json()
    assert holder["outgoing"] is None and holder["hop"] == 2
    assert holder["reasons"][0]["code"] == "complaint_chain"
    assert holder["role"]["verdict"] == "organizer"
    assert [(x["hop"], x["role"]) for x in holder["chain"]] == [(0, "victim"), (1, "forwarded"), (2, "holder")]
    assert [x["current"] for x in holder["chain"][1:]] == [False, True]
    assert account(an, DROP_CARD)["status"] == "blocked"
    # Конечная карта видит понятную причину без пересылки.
    card = client(DROP_CARD).get(f"/clients/{DROP_CARD}/case").json()
    assert card["summary"].startswith("На ваш счёт поступило 195 000 ₸")
    # Цепочка короче лимита: на ней ничего больше не заблокировано.
    assert len([c for c in queue.values() if c["from_complaint"]]) == 2


def test_chain_does_not_block_accounts_that_already_returned_the_money():
    st, stranger = client(STUDENT), client(STRANGER)
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    st.post(f"/incoming/{tid}/return")
    r = stranger.post(f"/transfers/{tid}/complaint").json()
    assert r["outcome"] == "already_returned"
    assert all(c["client_id"] != STUDENT for c in analyst().get("/analyst/cases").json())


# ---------- Ошибки второй ревизии ----------

def test_paused_transfer_of_a_blocked_client_cannot_be_released():
    st, an = client(STUDENT), analyst()
    r = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{r['case_id']}/insist")
    main.conn.execute("UPDATE clients SET status='blocked' WHERE id=?", (STUDENT,))
    main.conn.commit()
    drop_before = account(an, DROP_CARD)["balance"]
    assert an.post(f"/analyst/cases/{r['case_id']}/decision", json={"decision": "approve"}).status_code == 409
    assert account(an, DROP_CARD)["balance"] == drop_before
    out = an.post(f"/analyst/cases/{r['case_id']}/decision",
                  json={"decision": "reject", "comment": "Счёт приостановлен, перевод отменён."})
    assert out.json()["status"] == "cancelled"


def test_blocked_client_cannot_move_money_by_returning():
    block_seller()
    card = client(DROP_CARD)
    inc = feed_item(card, DROP_CARD, SELLER)
    assert account(card, DROP_CARD)["balance"] >= inc["amount"]          # деньги на карте есть
    assert card.post(f"/incoming/{inc['id']}/return").status_code == 403


def test_one_approval_does_not_lift_another_pending_block():
    an = analyst()
    main.conn.execute("""INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
                         SELECT client_id,transfer_id,incoming_transfer_id,'block',score,reasons,created_at
                         FROM cases WHERE client_id=?""", (TRANSIT,))
    main.conn.commit()
    first, second = [c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == TRANSIT]
    # Руслан по правилам похож на организатора: снять ограничения можно только вдвоём.
    asked = an.post(f"/analyst/cases/{first}/decision", json={"decision": "approve"}).json()
    assert asked["approval"]["kind"] == "release" and asked["needs_decision"]
    assert second_key(asked)["case"]["client_status"] == "blocked"
    # Клиент видит ещё не решённую блокировку, а не ту, что уже сняли.
    assert client(TRANSIT).get(f"/clients/{TRANSIT}/case").json()["status"] == "open"
    asked = an.post(f"/analyst/cases/{second}/decision", json={"decision": "approve"}).json()
    assert second_key(asked)["case"]["client_status"] == "active"


def test_demo_balances_match_the_transfers():
    an = analyst()
    for cid in (STUDENT, STRANGER, DROP_CARD, MOM, SELLER, VICTIM, TRANSIT):
        assert account(an, cid)["balance"] >= 0
    assert account(an, STUDENT)["balance"] == 192000 and account(an, MOM)["balance"] == 1650000   # 1,5 млн — в сейфе
    assert account(an, MOM)["available"] == 150000 and account(an, MOM)["safe"]["amount"] == 1500000
    assert account(an, DROP_CARD)["balance"] > 195000      # деньги из цепочки действительно лежат на карте


def test_times_are_kazakhstan_time_even_on_a_utc_server(monkeypatch):
    import time
    from app import clock
    if not hasattr(time, "tzset"):
        pytest.skip("в Windows нельзя сменить часовой пояс процесса")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        kz = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)
        assert abs((clock.now() - kz).total_seconds()) < 5
    finally:
        monkeypatch.undo()
        time.tzset()


def test_empty_amount_gets_a_readable_error_and_complaints_are_limited():
    st = client(STUDENT)
    r = st.post("/transfers", json={"receiver_id": MOM, "amount": 0})
    assert r.status_code == 422 and r.json()["detail"].startswith("Проверьте введённые данные")
    for _ in range(main.COMPLAINT_LIMIT):
        tid = send(st, MOM, 1000).json()["transfer_id"]
        assert st.post(f"/transfers/{tid}/complaint").status_code == 200
    tid = send(st, MOM, 1000).json()["transfer_id"]
    assert st.post(f"/transfers/{tid}/complaint").status_code == 429


# ---------- Эффективность, связанные счета, выгрузка для Антифрод-центра ----------

HISTORY_DB = os.path.join(_tmp, "history.db")
_h = db.connect(HISTORY_DB)
seed.populate(_h, evaluate=True, with_history=True, with_business=False)   # как на демо-сервере
_h.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('history','1')")
_h.commit()
_h.close()


@pytest.fixture
def with_history(tmp_path):
    main.conn.close()
    shutil.copy(HISTORY_DB, tmp_path / "h.db")
    main.conn = db.connect(str(tmp_path / "h.db"))


def test_new_analyst_pages_are_closed_to_clients():
    st = client(STUDENT)
    for path in ("/analyst/metrics", "/analyst/linked"):
        assert st.get(path).status_code == 401
    assert st.post("/analyst/cases/1/report").status_code == 401
    assert st.post(f"/analyst/accounts/{DROP_CARD}/suspend").status_code == 401


def test_metrics_add_up(with_history):
    m = analyst().get("/analyst/metrics").json()
    assert m["simulated_history"] and m["stops"] > 20
    assert m["kept_count"] + m["released"] + m["pending_stops"] == m["stops"]
    assert m["saved_amount"] >= m["returned_amount"] > 0
    assert 50 < m["self_resolved_share"] < 90
    assert sum(d["stops"] for d in m["per_day"]) == m["stops"] and len(m["per_day"]) == 30


def test_metrics_follow_live_actions():
    an, st = analyst(), client(STUDENT)
    before = an.get("/analyst/metrics").json()
    r = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{r['case_id']}/return-to-sender")
    after = an.get("/analyst/metrics").json()
    assert after["stops"] == before["stops"] + 1 and after["protected_clients"] == before["protected_clients"] + 1
    assert after["saved_amount"] == before["saved_amount"] + 150000
    assert not after["simulated_history"]


def test_linked_accounts_are_droppers_and_can_be_suspended(with_history):
    an = analyst()
    linked = an.get("/analyst/linked").json()
    assert len(linked) >= 5
    kinds = {main.conn.execute("SELECT true_kind FROM clients WHERE id=?", (a["client_id"],)).fetchone()[0] for a in linked}
    assert kinds == {"dropper"}                                    # на синтетике ни одного честного счёта
    target = next(a for a in linked if a["status"] == "active")
    case = an.post(f"/analyst/accounts/{target['client_id']}/suspend").json()
    assert case["client_status"] == "blocked" and case["reasons"][0]["code"] == "linked"
    assert case["linked"]["is_client"] and case["linked"]["sources"] == target["sources"]
    assert an.post(f"/analyst/accounts/{target['client_id']}/suspend").status_code == 409
    # Клиент видит понятную причину.
    view = an.get(f"/clients/{target['client_id']}/case").json()          # то, что увидит клиент
    assert view["summary"].startswith("На ваш счёт поступали деньги от нескольких клиентов")


def test_demo_drop_card_becomes_linked_after_the_complaint():
    an = analyst()
    assert all(a["client_id"] != DROP_CARD for a in an.get("/analyst/linked").json())
    block_seller()
    linked = {a["client_id"]: a for a in an.get("/analyst/linked").json()}
    assert linked[DROP_CARD]["sources"] == 2                        # Руслан и Данияр
    seller_case = next(c for c in an.get("/analyst/cases").json() if c["client_id"] == SELLER)
    card = an.get(f"/analyst/cases/{seller_case['case_id']}").json()
    assert card["linked"]["client_id"] == DROP_CARD and not card["linked"]["is_client"]
    assert {o["name"] for o in card["linked"]["other_cases"]} >= {"Руслан (счёту 12 дней)"}


def test_antifraud_report():
    an = analyst()
    block_seller()
    case_id = next(c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == SELLER)
    r = an.post(f"/analyst/cases/{case_id}/report")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rep = r.json()
    assert rep["client"]["id"] == SELLER and [o["amount_kzt"] for o in rep["operations"]] == [200000, 195000]
    assert rep["reasons"][0]["code"] == "complaint" and len(rep["complaint_chain"]) == 3
    assert set(rep["technical"]) >= {"ip", "geolocation", "phone_numbers"}
    history = an.get(f"/analyst/cases/{case_id}").json()["history"]
    assert history[-1]["action"] == "report"


# ---------- Списки Антифрод-центра и сигнал оператора о звонке ----------

def test_black_listed_recipient_is_refused():
    mom, an = client(MOM), analyst()
    assert mom.get(f"/recipients/{FAKE_INVEST}/check").json()["list"] == "black"
    r = send(mom, FAKE_INVEST, 50000)
    assert r.status_code == 403 and "подтверждённых мошенников" in r.json()["detail"]
    assert account(an, FAKE_INVEST)["balance"] == 0 and account(mom, MOM)["available"] == 150000


def test_grey_listed_recipient_is_paused_and_can_be_withdrawn():
    mom = client(MOM)
    check = mom.get(f"/recipients/{DROP_CARD}/check").json()
    assert check["list"] == "grey" and check["text"]
    assert mom.get(f"/recipients/{STUDENT}/check").json()["list"] is None
    r = send(mom, DROP_CARD, 30000).json()
    assert r["status"] == "held" and r["kind"] == "grey" and r["safe_return"] is None
    assert "afc_grey" in {x["code"] for x in r["reasons"]}
    assert mom.post(f"/cases/{r['case_id']}/return-to-sender").status_code == 400
    assert account(mom, MOM)["held"] == 30000
    assert mom.post(f"/cases/{r['case_id']}/withdraw").json()["status"] == "withdrawn"
    acc = account(mom, MOM)
    assert acc["held"] == 0 and acc["available"] == 150000


def test_call_signal_pauses_a_transfer_to_a_new_recipient_only():
    mom, an = client(MOM), analyst()
    assert send(mom, SAFE_ACCOUNT, 1000).json()["status"] == "done"      # без звонка обычный перевод проходит
    assert mom.post("/telecom/signal", json={"active": True}).json()["number"]
    assert account(mom, MOM)["call"] is not None
    assert send(mom, STUDENT, 5000).json()["status"] == "done"           # знакомому — можно и во время звонка
    r = send(mom, VICTIM, 100000).json()                                  # новый получатель во время звонка
    assert r["status"] == "held" and r["kind"] == "call"
    card = an.get(f"/analyst/cases/{r['case_id']}").json()
    assert card["incoming"] is None and "suspicious_call" in {x["code"] for x in card["reasons"]}
    assert [h["action"] for h in card["history"]] == ["intercepted", "call_signal", "guardian_notified"]
    # Сигнал гаснет через 15 минут или по команде.
    assert mom.post("/telecom/signal", json={"active": False}).json() is None
    assert send(mom, SAFE_ACCOUNT, 1000).json()["status"] == "done"


def test_call_signal_expires():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    old = (main._now() - timedelta(minutes=engine.CALL_SIGNAL_MINUTES + 1)).isoformat()
    main.conn.execute("UPDATE call_signals SET started_at=?", (old,))
    main.conn.commit()
    assert account(mom, MOM)["call"] is None
    assert send(mom, VICTIM, 100000).json()["status"] == "pending"   # не остановлен; крупный первый перевод ждёт час


def test_signals_strengthen_the_scheme_reasons():
    st = client(STUDENT)
    st.post("/telecom/signal", json={"active": True})
    r = send(st, DROP_CARD, 150000).json()
    codes = {x["code"] for x in r["reasons"]}
    assert r["kind"] == "scheme" and {"afc_grey", "suspicious_call", "amount_match"} <= codes


def test_kazakh_texts_for_new_signals():
    mom = client(MOM)
    mom.cookies.set("ca_lang", "kk")
    assert send(mom, FAKE_INVEST, 50000).json()["detail"].startswith("Аударым мүмкін емес")
    assert mom.get(f"/recipients/{DROP_CARD}/check").json()["text"].startswith("Алушы Антифрод")


# ---------- Третья ревизия: обходы и защита сервера ----------

def test_small_bait_does_not_unlock_big_transfer_to_same_card():
    """Мошенник шлёт 1 000 ₸ и просит перевести на ту же карту больше: это не «возврат», проверка идёт."""
    drop, mom = client(DROP_CARD), client(MOM)
    assert send(drop, MOM, 1000).json()["status"] == "done"
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, DROP_CARD, 140000).json()
    assert r["status"] == "held"
    # Вернуть ровно присланное по-прежнему можно без остановки.
    mom.post("/telecom/signal", json={"active": False})
    assert send(mom, DROP_CARD, 1000).json()["status"] == "done"


def _appeal(cid, case_id):
    c = client(cid)
    c.post(f"/cases/{case_id}/documents", json={"kind": "chat", "filename": "a.png", "content_base64": B64(PNG)})
    assert c.post(f"/cases/{case_id}/submit", json={"explanation": "Мне написали, что деньги пришли по ошибке"}).status_code == 200
    return c


def test_kept_block_survives_approval_of_another_case():
    _, tid = block_seller()
    a = analyst()
    first = client(SELLER).get(f"/clients/{SELLER}/case").json()["case_id"]
    _appeal(SELLER, first)
    assert a.post(f"/analyst/cases/{first}/decision",
                  json={"decision": "reject", "comment": "Объяснение не подтверждено"}).status_code == 200
    main.conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at,status)
           VALUES(?,NULL,?,'block',40,'[{"code":"linked","points":40,"text":"x"}]',?,'under_review')""",
        (SELLER, tid, main._now().isoformat()))
    main.conn.commit()
    second = main.conn.execute("SELECT MAX(id) FROM cases").fetchone()[0]
    assert a.post(f"/analyst/cases/{second}/decision", json={"decision": "approve"}).status_code == 200
    assert account(client(SELLER), SELLER)["status"] == "limited"


def test_paused_transfer_to_newly_blacklisted_receiver_cannot_be_released():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, SAFE_ACCOUNT, 100000).json()
    assert r["status"] == "held"
    assert mom.post(f"/cases/{r['case_id']}/insist").json()["status"] == "guardian"
    assert client(STUDENT).post(f"/guardian/requests/{r['case_id']}/confirm").status_code == 200
    main.conn.execute("INSERT INTO afc_list(client_id,list,added_at) VALUES(?,'black',?)",
                      (SAFE_ACCOUNT, main._now().isoformat()))
    main.conn.commit()
    a = analyst()
    assert a.post(f"/analyst/cases/{r['case_id']}/decision", json={"decision": "approve"}).status_code == 409
    assert a.post(f"/analyst/cases/{r['case_id']}/decision",
                  json={"decision": "reject", "comment": "Получатель в базе мошенников"}).status_code == 200


def test_no_return_to_blacklisted_sender():
    student = client(STUDENT)
    main.conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(?,?,?,?,'done')",
                      (FAKE_INVEST, STUDENT, 50000, main._now().isoformat()))
    main.conn.execute("UPDATE clients SET balance=balance+50000 WHERE id=?", (STUDENT,))
    main.conn.commit()
    tid = feed_item(student, STUDENT, FAKE_INVEST)["id"]
    assert student.post(f"/incoming/{tid}/return").status_code == 403


def test_sessions_expire_on_server():
    c = client(STUDENT)
    assert c.get("/me").status_code == 200
    for v in main.SESSIONS.values():
        v["expires"] = 0
    assert c.get("/me").status_code == 401


def test_pin_lockout_is_per_address():
    attacker = TestClient(main.app, client=("10.0.0.66", 5000))
    for _ in range(5):
        assert attacker.post("/login", json={"role": "analyst", "pin": "0000"}).status_code == 401
    assert attacker.post("/login", json={"role": "analyst", "pin": "4321"}).status_code == 429
    analyst()          # сотрудник с другого адреса входит


def test_security_headers_and_chunked_body():
    c = TestClient(main.app)
    h = c.get("/").headers
    assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
    r = c.post("/login", content=iter([b'{"role":"client","client_id":1001}']),
               headers={"content-type": "application/json"})
    assert r.status_code == 411


# ---------- Справка, доверенное лицо, вербовка ----------

ERLAN = 1010


def _unblock_seller():
    block_seller()
    seller = client(SELLER)
    case = seller.get(f"/clients/{SELLER}/case").json()["case_id"]
    _appeal(SELLER, case)
    a = analyst()
    assert a.post(f"/analyst/cases/{case}/decision", json={"decision": "approve"}).json()["certificate"]["valid"]
    return seller, a, case


def test_certificate_is_issued_on_unblock_and_verifiable():
    seller, a, case = _unblock_seller()
    cert = seller.get(f"/clients/{SELLER}/case").json()["certificate"]
    assert cert["valid"] and cert["number"].startswith("ЧС-")
    page = seller.get(f"/clients/{SELLER}/certificate")
    assert page.status_code == 200 and cert["number"] in page.text and cert["code"] in page.text
    assert "юридической силы" in page.text
    # Другой клиент чужую справку не получит, а проверка по коду работает без входа.
    assert client(VICTIM).get(f"/clients/{SELLER}/certificate").status_code == 403
    check = TestClient(main.app).get(f"/verify/{cert['code'].lower()}")
    assert "действительна" in check.text and "маркетплейсе" not in check.text
    assert "не найдена" in TestClient(main.app).get("/verify/ZZZZ<b>").text
    # Счёт снова приостановили: справка отзывается.
    main.conn.execute(
        """INSERT INTO cases(client_id,incoming_transfer_id,kind,score,reasons,created_at)
           SELECT client_id,incoming_transfer_id,'block',40,reasons,created_at FROM cases WHERE id=?""", (case,))
    main._refresh_status(SELLER)
    main.conn.commit()
    assert "недействительна" in TestClient(main.app).get(f"/verify/{cert['code']}").text
    assert seller.get(f"/clients/{SELLER}/certificate").status_code == 404


def test_kept_block_gives_no_certificate():
    victim, _ = block_seller()
    case = client(SELLER).get(f"/clients/{SELLER}/case").json()["case_id"]
    _appeal(SELLER, case)
    r = analyst().post(f"/analyst/cases/{case}/decision", json={"decision": "reject", "comment": "Объяснение не подтверждено"})
    assert r.json()["certificate"] is None
    assert client(SELLER).get(f"/clients/{SELLER}/certificate").status_code == 404


def test_guardian_is_notified_and_can_cancel():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, SAFE_ACCOUNT, 100000).json()
    assert r["status"] == "held" and r["guardian"].startswith("Айдана")
    daughter = client(STUDENT)
    req = account(daughter, STUDENT)["guardian_requests"]
    assert len(req) == 1 and req[0]["ward"] == "Мама" and req[0]["amount"] == 100000
    # Подтвердить, пока мама сама не просила, нельзя; отменить можно сразу.
    assert daughter.post(f"/guardian/requests/{r['case_id']}/confirm").status_code == 409
    before = account(mom, MOM)["available"]
    assert daughter.post(f"/guardian/requests/{r['case_id']}/cancel").status_code == 200
    assert account(mom, MOM)["available"] == before + 100000
    assert account(daughter, STUDENT)["guardian_requests"] == []
    # Чужой человек отменить не может.
    r2 = send(mom, SAFE_ACCOUNT, 50000).json()
    assert client(VICTIM).post(f"/guardian/requests/{r2['case_id']}/cancel").status_code == 404
    m = analyst().get("/analyst/metrics").json()
    assert m["guardian_cancelled"] == 1


def test_insist_goes_to_guardian_before_analyst():
    mom = client(MOM)
    mom.post("/telecom/signal", json={"active": True})
    r = send(mom, SAFE_ACCOUNT, 100000).json()
    assert mom.post(f"/cases/{r['case_id']}/insist").json()["status"] == "guardian"
    a = analyst()
    assert not any(c["case_id"] == r["case_id"] for c in a.get("/analyst/cases").json() if c["needs_decision"])
    assert client(STUDENT).post(f"/guardian/requests/{r['case_id']}/confirm").status_code == 200
    card = a.get(f"/analyst/cases/{r['case_id']}").json()
    assert card["needs_decision"] and card["guardian"]["state"] == "confirmed"
    assert "guardian_confirmed" in [h["action"] for h in card["history"]]


def test_guardian_rules():
    mom = client(MOM)
    g = mom.get("/guardian").json()
    assert g["guardian"]["id"] == STUDENT
    # Сменить сразу нельзя; отключение — только через сутки.
    assert mom.post("/guardian", json={"guardian_id": VICTIM}).status_code == 409
    removing = mom.post("/guardian/remove").json()["guardian"]
    assert removing["remove_at"]
    r = (mom.post("/telecom/signal", json={"active": True}), send(mom, SAFE_ACCOUNT, 90000).json())[1]
    assert r["guardian"]                                  # пока сутки не прошли, дочь всё ещё узнаёт
    main.conn.execute("UPDATE guardians SET remove_at=? WHERE client_id=?",
                      ((main._now() - timedelta(minutes=1)).isoformat(), MOM))
    main.conn.commit()
    g = mom.get("/guardian").json()
    assert g["guardian"] is None and [c["id"] for c in g["candidates"]] == [STUDENT]
    # Получатель без давней истории переводов доверенным лицом стать не может.
    assert mom.post("/guardian", json={"guardian_id": SAFE_ACCOUNT}).status_code == 400
    assert mom.post("/guardian", json={"guardian_id": STUDENT}).json()["guardian"]["id"] == STUDENT


def test_recruitment_warning_and_return_all():
    erlan = client(ERLAN)
    acc = account(erlan, ERLAN)
    assert acc["recruit"]["senders"] == 3 and acc["recruit"]["amount"] == 143000
    before = acc["available"]
    r = erlan.post("/recruit/return-all").json()
    assert r["returned"] == 3
    acc = account(erlan, ERLAN)
    assert acc["recruit"] is None and acc["available"] == before - 143000


def test_recruitment_dismiss_is_shown_to_analyst():
    erlan = client(ERLAN)
    assert erlan.post("/recruit/dismiss").status_code == 200
    assert account(erlan, ERLAN)["recruit"] is None
    # Ерлан всё же пересылает всё одним переводом: сбор денег нескольких незнакомцев останавливается.
    r = send(erlan, DROP_CARD, 140000).json()
    assert r["status"] == "held"
    codes = [x["code"] for x in analyst().get(f"/analyst/cases/{r['case_id']}").json()["reasons"]]
    assert "pooled_forward" in codes
    card = analyst().get(f"/analyst/cases/{r['case_id']}").json()
    assert card["recruit_warned"]
    assert any("вербовк" in f["text"] for f in card["role"]["factors"])


def test_honest_seller_has_no_recruitment_warning():
    assert account(client(SELLER), SELLER)["recruit"] is None


# ---------- Проверка сообщения и период охлаждения ----------

SCAM_SAFE = "Служба безопасности банка. На ваше имя пытаются оформить кредит. Переведите деньги на безопасный счёт. Никому не сообщайте."
SCAM_MISTAKEN = "Я по ошибке перевела вам 150 000 тенге, верните срочно на карту 4400 0000 1234 5678"


def test_message_check_recognizes_schemes_and_keeps_no_text():
    mom = client(MOM)
    r = mom.post("/messages/check", json={"text": SCAM_SAFE}).json()
    assert r["verdict"] == "danger" and r["scheme"] == "safe_account" and len(r["advice"]) == 3
    assert "служба безопасности" in r["matched"]
    assert {f["code"] for f in r["flags"]} == {"secrecy"}
    for text, scheme in [("Гарантированный доход 3% в день, вложите от 50 000", "investment"),
                         ("Подработка: принимать переводы на свою карту и переводить дальше, 5% от суммы", "job"),
                         ("Банктің қауіпсіздік қызметі. Ақшаны қауіпсіз шотқа аударыңыз.", "safe_account")]:
        assert mom.post("/messages/check", json={"text": text}).json()["scheme"] == scheme
    honest = mom.post("/messages/check", json={"text": "Скинь 5000 за билеты в кино, встречаемся в 19:00"}).json()
    assert honest["verdict"] == "none" and honest["scheme"] is None
    assert mom.post("/messages/check", json={"text": "Срочно перейдите https://bonus.top"}).json()["verdict"] == "caution"
    assert mom.post("/messages/check", json={"text": "   "}).status_code == 400
    assert mom.post("/messages/check", json={"text": "x" * 4001}).status_code == 422
    # Текст сообщения не хранится: в таблице только схема и итог.
    cols = [r[1] for r in main.conn.execute("PRAGMA table_info(message_checks)")]
    assert cols == ["id", "client_id", "scheme", "verdict", "at"]


def test_mistaken_message_offers_return_of_foreign_money():
    st = client(STUDENT)
    r = st.post("/messages/check", json={"text": SCAM_MISTAKEN}).json()
    assert r["scheme"] == "mistaken" and r["return_incoming"]["amount"] == 150000
    assert st.post(f"/incoming/{r['return_incoming']['id']}/return").status_code == 200
    assert st.post("/messages/check", json={"text": SCAM_MISTAKEN}).json()["return_incoming"] is None


def test_scam_message_strengthens_next_transfer_to_new_receiver():
    mom = client(MOM)
    mom.post("/messages/check", json={"text": SCAM_SAFE})
    r = send(mom, SAFE_ACCOUNT, 50000).json()
    assert r["status"] == "held" and r["kind"] == "message"
    # Знакомому человеку перевод проходит.
    assert send(mom, STUDENT, 3000).json()["status"] == "done"


def test_big_first_transfer_waits_and_can_be_cancelled():
    mom = client(MOM)
    before = account(mom, MOM)["available"]
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    assert r["status"] == "pending" and r["release_at"]
    acc = account(mom, MOM)
    assert acc["available"] == before - 120000 and acc["cooling"][0]["id"] == r["transfer_id"]
    # Получатель денег пока не видит.
    assert all(t["id"] != r["transfer_id"] for t in account(client(SAFE_ACCOUNT), SAFE_ACCOUNT)["feed"])
    assert mom.post(f"/transfers/{r['transfer_id']}/cancel").json()["status"] == "cancelled"
    assert account(mom, MOM)["available"] == before
    assert mom.post(f"/transfers/{r['transfer_id']}/cancel").status_code == 409
    assert client(STUDENT).post(f"/transfers/{r['transfer_id']}/cancel").status_code == 404


def test_cooling_transfer_goes_through_after_the_period():
    mom, safe = client(MOM), client(SAFE_ACCOUNT)
    before = account(safe, SAFE_ACCOUNT)["balance"]
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    main.conn.execute("UPDATE transfers SET release_at=? WHERE id=?",
                      ((main._now() - timedelta(seconds=1)).isoformat(), r["transfer_id"]))
    main.conn.commit()
    assert account(safe, SAFE_ACCOUNT)["balance"] == before + 120000
    assert account(mom, MOM)["cooling"] == []


def test_cooling_rules():
    mom = client(MOM)
    main.conn.execute("UPDATE clients SET balance=balance+500000 WHERE id=?", (MOM,))
    main.conn.commit()
    assert send(mom, STUDENT, 100000).json()["status"] == "done"          # давно знакомый получатель
    assert send(mom, SAFE_ACCOUNT, 30000).json()["status"] == "done"      # небольшая сумма
    assert send(mom, SAFE_ACCOUNT, 80000).json()["status"] == "pending"   # части за сутки складываются


def test_cooling_stops_if_receiver_is_blacklisted_meanwhile():
    mom = client(MOM)
    before = account(mom, MOM)["available"]
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    main.conn.execute("INSERT INTO afc_list(client_id,list,added_at) VALUES(?,'black',?)",
                      (SAFE_ACCOUNT, main._now().isoformat()))
    main.conn.execute("UPDATE transfers SET release_at=? WHERE id=?",
                      ((main._now() - timedelta(seconds=1)).isoformat(), r["transfer_id"]))
    main.conn.commit()
    assert account(mom, MOM)["available"] == before


def test_guardian_can_cancel_cooling_transfer():
    mom, daughter = client(MOM), client(STUDENT)
    r = send(mom, SAFE_ACCOUNT, 120000).json()
    g = account(daughter, STUDENT)["guardian_cooling"]
    assert len(g) == 1 and g[0]["sender"] == "Мама"
    assert client(VICTIM).post(f"/guardian/transfers/{r['transfer_id']}/cancel").status_code == 404
    assert daughter.post(f"/guardian/transfers/{r['transfer_id']}/cancel").status_code == 200
    m = analyst().get("/analyst/metrics").json()
    assert m["cooling"] == 1 and m["cooling_cancelled"] == 1 and m["cooling_saved"] == 120000


def test_returning_foreign_money_by_hand_is_not_delayed():
    st = client(STUDENT)
    assert send(st, STRANGER, 150000).json()["status"] == "done"


# ---------- Соразмерная блокировка ----------

def test_unwitting_forwarder_gets_only_the_disputed_sum_frozen():
    sel, an = client(SELLER), analyst()
    before = account(sel, SELLER)["available"]
    block_seller()
    acc = account(sel, SELLER)
    # Данияр получил 200 000 и переслал 195 000: на счёте осталось 5 000 чужих, их и замораживаем.
    assert acc["status"] == "limited" and acc["frozen"] == 5000 and acc["available"] == before - 5000
    case = sel.get(f"/clients/{SELLER}/case").json()
    assert case["scope"] == "partial" and case["frozen"] == 5000 and "5 000 ₸" in case["summary"]
    # Знакомому поставщику платить можно, новым получателям нельзя, замороженное потратить нельзя.
    assert send(sel, SUPPLIER, 10000).json()["status"] == "done"
    assert send(sel, MOM, 1000).status_code == 403
    assert send(sel, SUPPLIER, acc["available"]).status_code == 400
    # Карта, на которой лежат деньги, по правилам не похожа на невольного участника: счёт закрыт целиком.
    assert account(an, DROP_CARD)["status"] == "blocked"
    card = an.get(f"/analyst/cases/{case['case_id']}").json()
    assert card["scope"] == "partial" and card["can_escalate"]
    assert card["history"][-1]["details"]["scope"] == "partial"


def test_analyst_can_turn_partial_freeze_into_full_block():
    sel, an = client(SELLER), analyst()
    block_seller()
    cid = sel.get(f"/clients/{SELLER}/case").json()["case_id"]
    assert client(MOM).post(f"/analyst/cases/{cid}/escalate").status_code == 401
    card = an.post(f"/analyst/cases/{cid}/escalate").json()
    assert card["scope"] == "full" and card["client_status"] == "blocked" and not card["can_escalate"]
    assert send(sel, SUPPLIER, 1000).status_code == 403
    assert an.post(f"/analyst/cases/{cid}/escalate").status_code == 409
    assert card["history"][-1]["action"] == "escalated"


def test_partial_freeze_is_lifted_with_certificate():
    seller, a, case = _unblock_seller()
    acc = account(seller, SELLER)
    assert acc["status"] == "active" and acc["frozen"] == 0
    assert send(seller, MOM, 1000).json()["status"] == "done"


def test_cooling_transfer_of_a_limited_client_does_not_leave():
    sel = client(SELLER)
    r = send(sel, MOM, 20000).json()                       # новый получатель, но сумма небольшая: уходит сразу
    assert r["status"] == "done"
    tid = main.conn.execute(
        """INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,release_at)
           VALUES(?,?,10000,?,'pending',?)""",
        (SELLER, STUDENT, main._now().isoformat(), (main._now() + timedelta(minutes=30)).isoformat())).lastrowid
    main.conn.commit()
    block_seller()
    # Срок охлаждения вышел уже после заморозки: перевод не уходит.
    main.conn.execute("UPDATE transfers SET release_at=? WHERE id=?",
                      ((main._now() - timedelta(minutes=1)).isoformat(), tid))
    main.conn.commit()
    account(sel, SELLER)
    assert main.conn.execute("SELECT status FROM transfers WHERE id=?", (tid,)).fetchone()["status"] == "cancelled"


# ---------- Пострадавший: статус жалобы и возврат денег ----------

def test_victim_sees_complaint_status_and_gets_the_money_back():
    an = analyst()
    before = account(client(VICTIM), VICTIM)["balance"]
    victim, tid = block_seller()
    comp = account(victim, VICTIM)["complaints"][0]
    assert comp["stage"] == "checking" and comp["frozen"] == 200000 and comp["returned"] == 0
    assert "200 000 ₸" in comp["text"]
    queue = {c["client_id"]: c for c in an.get("/analyst/cases").json()}
    drop, sel_case = queue[DROP_CARD]["case_id"], queue[SELLER]["case_id"]
    # Сотрудник возвращает то, что осталось на конечной карте.
    assert an.get(f"/analyst/cases/{drop}").json()["refundable"] == 195000
    asked = an.post(f"/analyst/cases/{drop}/refund").json()          # крупный возврат — двумя ключами
    assert asked["approval"]["summary"] == "Вернуть пострадавшему 195 000 ₸" and asked["refundable"] == 195000
    assert an.post(f"/analyst/approvals/{asked['approval']['id']}/confirm").status_code == 409   # сам себе нельзя
    assert an.post(f"/analyst/cases/{drop}/refund").status_code == 409                          # уже ждёт второго
    card = second_key(asked)["case"]
    assert card["refundable"] == 0 and card["complaint"]["returned"] == 195000 and card["approval"] is None
    assert [h["action"] for h in card["history"][-2:]] == ["refunded", "approval_confirmed"]
    assert "Медет К." in card["history"][-1]["who"] and "Аружан С." in card["history"][-1]["text"]
    assert an.post(f"/analyst/cases/{drop}/refund").status_code == 409
    comp = account(victim, VICTIM)["complaints"][0]
    assert comp["returned"] == 195000 and comp["frozen"] == 5000 and comp["text"].startswith("Вам уже вернули 195 000 ₸")
    # Данияр сам возвращает остаток: чужой случай вернуть нельзя.
    sel = client(SELLER)
    assert sel.get(f"/clients/{SELLER}/case").json()["refundable"] == 5000
    assert client(MOM).post(f"/cases/{sel_case}/refund").status_code == 404
    assert sel.post(f"/cases/{sel_case}/refund").json() == {"status": "refunded", "amount": 5000}
    assert sel.post(f"/cases/{sel_case}/refund").status_code == 409
    acc = account(victim, VICTIM)
    assert acc["balance"] == before + 200000 and acc["complaints"][0]["stage"] == "returned"
    # Возврат не выглядит для пострадавшего как «чужие деньги» и не считается «возвратом кнопкой».
    refunds = [t for t in acc["feed"] if t["direction"] == "in" and t["return_of"]]
    assert len(refunds) == 2 and not any(t["warning"] for t in refunds) and any(t["refund"] for t in refunds)
    role = an.get(f"/analyst/cases/{sel_case}").json()["role"]
    assert any("сам вернул" in f["text"] for f in role["factors"])
    m = an.get("/analyst/metrics").json()
    assert m["victim_refunds"] == 2 and m["victim_refund_amount"] == 200000 and m["partial_blocks"] == 1
    # Замороженное ушло пострадавшему: Данияр больше ничего не должен, но ограничения снимает сотрудник.
    assert account(sel, SELLER)["frozen"] == 0 and account(sel, SELLER)["status"] == "limited"


def test_refund_never_exceeds_what_the_victim_sent():
    an = analyst()
    victim, tid = block_seller()
    drop = next(c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == DROP_CARD)
    # Данияр успел вернуть пострадавшей часть сам: с карты уйдёт не больше остатка долга.
    main.conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,'done',?)",
        (SELLER, VICTIM, 50000, main._now().isoformat(), tid))
    main.conn.commit()
    assert second_key(an.post(f"/analyst/cases/{drop}/refund").json())["case"]["complaint"]["returned"] == 200000
    assert account(victim, VICTIM)["complaints"][0]["stage"] == "returned"


def test_complaint_status_when_money_is_still_with_the_receiver():
    stranger, st = client(STRANGER), client(STUDENT)
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    stranger.post(f"/transfers/{tid}/complaint")
    assert account(stranger, STRANGER)["complaints"][0]["stage"] == "warned"
    st.post(f"/incoming/{tid}/return")
    assert account(stranger, STRANGER)["complaints"][0]["stage"] == "returned"


# ---------- «Меня втянули в схему» ----------

WORDS = "В чате предложили подработку, я отдал карту и пароль от приложения куратору."


def test_self_report_about_a_handed_over_card_closes_the_account_and_gives_a_confirmation():
    tim, an = client(CARD_GIVER), analyst()
    assert tim.post("/self-report", json={"kind": "card", "details": "коротко"}).status_code == 400
    assert tim.post("/self-report", json={"kind": "nope", "details": WORDS}).status_code == 400
    r = tim.post("/self-report", json={"kind": "card", "details": WORDS}).json()
    assert r["report"]["number"].startswith("СО-") and "приостановлен" in r["text"]
    assert account(tim, CARD_GIVER)["status"] == "blocked"
    case = tim.get(f"/clients/{CARD_GIVER}/case").json()
    assert case["status"] == "under_review" and case["due_at"] and case["summary"].startswith("Вы сами сообщили")
    page = tim.get(f"/clients/{CARD_GIVER}/self-report")
    assert page.status_code == 200 and r["report"]["number"] in page.text and "232-1" in page.text
    assert "Решение принимают правоохранительные органы" in page.text and "куратору" in page.text
    assert client(MOM).get(f"/clients/{CARD_GIVER}/self-report").status_code == 403
    check = TestClient(main.app).get(f"/verify/{r['report']['code']}")
    assert "клиент сам обратился" in check.text and "куратору" not in check.text and "студент" not in check.text
    # Сотрудник видит обращение в очереди, слова клиента и учитывает их в подсказке.
    row = next(c for c in an.get("/analyst/cases").json() if c["client_id"] == CARD_GIVER)
    assert row["self_reported"] and row["needs_decision"] and row["role"]["verdict"] == "unwitting"
    card = an.get(f"/analyst/cases/{row['case_id']}").json()
    assert card["self_report"]["kind"] == "card" and card["self_report"]["details"] == WORDS
    assert card["frozen"] == 95000 and card["history"][0]["action"] == "self_report"
    # Повторно, пока идёт проверка, сообщить нельзя; после решения сотрудника счёт открывается со справкой.
    assert tim.post("/self-report", json={"kind": "card", "details": WORDS}).status_code == 409
    done = an.post(f"/analyst/cases/{row['case_id']}/decision", json={"decision": "approve"}).json()
    assert done["client_status"] == "active" and done["certificate"]["valid"]
    assert an.get("/analyst/metrics").json()["self_reports"] == 1


def test_self_report_about_transit_freezes_only_foreign_money():
    er = client(RECRUIT)
    before = account(er, RECRUIT)["available"]
    r = er.post("/self-report", json={"kind": "transit", "details": WORDS}).json()
    assert "только чужие деньги" in r["text"]
    acc = account(er, RECRUIT)
    assert acc["status"] == "limited" and acc["frozen"] == 143000 and acc["available"] == before - 143000
    assert acc["self_report"]["kind"] == "transit" and acc["recruit"] is not None
    assert send(er, MOM, 1000).status_code == 403            # новый получатель
    assert er.post("/recruit/return-all").status_code == 400  # замороженное уходит только по решению банка


def test_self_report_about_an_offer_changes_nothing():
    st = client(STUDENT)
    r = st.post("/self-report", json={"kind": "offer"}).json()
    assert "Ограничений по вашему счёту нет" in r["text"]
    assert account(st, STUDENT)["status"] == "active"
    assert analyst().get("/analyst/cases").json() == [c for c in analyst().get("/analyst/cases").json()
                                                      if c["client_id"] != STUDENT]
    # Если случай всё же возникнет, сотрудник увидит, что клиент предупреждал банк заранее.
    held = send(st, DROP_CARD, 150000).json()
    st.post(f"/cases/{held['case_id']}/insist")
    role = analyst().get(f"/analyst/cases/{held['case_id']}").json()["role"]
    assert any("сам сообщил банку" in f["text"] for f in role["factors"])


def test_restricted_client_cannot_self_report_and_kazakh_texts():
    block_seller()
    sel = client(SELLER)
    assert sel.post("/self-report", json={"kind": "transit", "details": WORDS}).status_code == 409
    sel.cookies.set("ca_lang", "kk")
    case = sel.get(f"/clients/{SELLER}/case").json()
    assert "тек даулы соманы бұғаттады" in case["summary"]
    assert send(sel, MOM, 1000).json()["detail"].startswith("Банк мән-жайын анықтап жатқанда")
    victim = client(VICTIM)
    victim.cookies.set("ca_lang", "kk")
    assert account(victim, VICTIM)["complaints"][0]["text"].startswith("Банк мән-жайын анықтап жатыр")
    tim = client(CARD_GIVER)
    tim.cookies.set("ca_lang", "kk")
    assert tim.post("/self-report", json={"kind": "card", "details": WORDS}).json()["text"].startswith("Хабарлағаныңызға")
    assert "құқық қорғау" in tim.get(f"/clients/{CARD_GIVER}/self-report").text


# ---------- Схемы осени 2026: письмо «от Нацбанка», ложное уведомление, счёт «от налоговой» ----------

def test_new_message_schemes_are_recognized_in_both_languages():
    mom = client(MOM)
    for text, scheme in [
        ("Национальный Банк РК. Исх. № 08-2/1147. Для безопасного возврата средств переведите их на депозит Нацбанка.",
         "fake_letter"),
        ("Ұлттық Банк. Хат № 08-2/1147. Ақшаны қауіпсіз қайтару үшін оны Ұлттық банктің шотына аударыңыз.", "fake_letter"),
        ("eGov: на ваше имя оформлена цифровая доверенность. Если это не вы, позвоните в поддержку по номеру +7 700 418 22 90",
         "fake_alert"),
        ("Сіздің атыңызға цифрлық сенімхат рәсімделді. Егер бұл сіз болмасаңыз, нөмірге хабарласыңыз.", "fake_alert"),
        ("Одобрена кредитная заявка на 2 000 000 тенге. Если вы не подавали заявку, перезвоните по номеру 8 800 000 00 00",
         "fake_alert"),
        ("КГД РК: у вас налоговая задолженность 48 300 тенге. Счёт на оплату выставлен, иначе арест на счета.", "fake_invoice"),
        ("МКК: сізде салық берешегі бар. Төлем шоты жіберілді, әйтпесе өсімпұл есептеледі.", "fake_invoice"),
        # «Встречный кредит» — это та же схема «безопасный счёт».
        ("Чтобы отменить чужую заявку, оформите встречный кредит и переведите деньги на резервный счёт.", "safe_account"),
    ]:
        r = mom.post("/messages/check", json={"text": text}).json()
        assert (r["scheme"], r["verdict"]) == (scheme, "danger"), text
        assert len(r["advice"]) == 3 and r["title"]
    # Похожие честные сообщения не помечаются.
    for text in ["Счёт на оплату за ноябрь во вложении", "Оплатите штраф до пятницы",
                 "Подписал доверенность у нотариуса на машину", "Национальный банк повысил базовую ставку"]:
        assert mom.post("/messages/check", json={"text": text}).json()["verdict"] != "danger", text
    mom.cookies.set("ca_lang", "kk")
    r = mom.post("/messages/check", json={"text": "КГД: налоговая задолженность, счёт на оплату выставлен"}).json()
    assert r["title"].startswith("«Салық органынан»")


def test_new_scheme_message_strengthens_next_transfer():
    mom = client(MOM)
    mom.post("/messages/check", json={"text": "Национальный банк. Для безопасного возврата средств переведите на депозит Нацбанка"})
    r = send(mom, SAFE_ACCOUNT, 50000).json()
    assert r["status"] == "held" and r["kind"] == "message"


# ---------- Свежий кредит: схема «встречный кредит» ----------

BORROWER = 1013


def test_loan_money_to_new_young_account_is_held_and_loan_repaid():
    bolat = client(BORROWER)
    acc = account(bolat, BORROWER)
    assert acc["loan"]["amount"] == 800000 and acc["available"] == 1008500
    loan_in = next(t for t in acc["feed"] if t.get("loan"))
    assert loan_in["direction"] == "in" and not loan_in["warning"]       # кредит — не «чужие деньги»
    assert acc["recruit"] is None
    r = send(bolat, SAFE_ACCOUNT, 800000).json()
    assert r["status"] == "held" and r["kind"] == "loan" and r["loan"]["amount"] == 800000
    assert "loan_forward" in {x["code"] for x in r["reasons"]}
    # Вернуть кредит может только его владелец.
    assert client(MOM).post(f"/loans/{r['loan']['id']}/repay").status_code == 404
    ok = bolat.post(f"/loans/{r['loan']['id']}/repay").json()
    assert ok == {"status": "repaid", "amount": 800000}
    acc = account(bolat, BORROWER)
    assert acc["available"] == 208500 and acc["held"] == 0 and acc["loan"] is None
    assert any(t.get("loan") and t["direction"] == "out" for t in acc["feed"])
    case = main.conn.execute("SELECT status FROM cases WHERE id=?", (r["case_id"],)).fetchone()
    assert case["status"] == "withdrawn"
    assert bolat.post(f"/loans/{r['loan']['id']}/repay").status_code == 409
    assert main.conn.execute("SELECT 1 FROM events WHERE action='loan_repaid' AND case_id=?", (r["case_id"],)).fetchone()


def test_loan_money_to_old_account_waits_but_is_not_held():
    # Честный клиент оплачивает кредитом покупку у продавца со старым счётом: только период охлаждения.
    r = send(client(BORROWER), MOM, 500000).json()
    assert r["status"] == "pending"
    assert "loan_forward" in {x["code"] for x in r["reasons"]}


def test_loan_split_into_parts_is_caught():
    bolat = client(BORROWER)
    first = send(bolat, SAFE_ACCOUNT, 300000).json()
    assert first["status"] == "pending"                  # меньше половины кредита: признака ещё нет
    second = send(bolat, SAFE_ACCOUNT, 200000).json()
    assert second["status"] == "held" and second["kind"] == "loan"


def test_old_loan_is_not_a_signal():
    main.conn.execute("UPDATE loans SET issued_at=? WHERE client_id=?",
                      ((main._now() - timedelta(hours=engine.LOAN_HOURS + 1)).isoformat(), BORROWER))
    main.conn.commit()
    r = send(client(BORROWER), SAFE_ACCOUNT, 800000).json()
    assert r["status"] == "pending" and "loan_forward" not in {x["code"] for x in r["reasons"]}
    assert account(client(BORROWER), BORROWER)["loan"] is None


def test_loan_repay_needs_the_money_and_rolls_back():
    bolat = client(BORROWER)
    assert send(bolat, MOM, 300000).json()["status"] == "pending"    # 300 000 зарезервированы до ухода
    held = send(bolat, SAFE_ACCOUNT, 700000).json()
    assert held["status"] == "held"
    r = bolat.post(f"/loans/{held['loan']['id']}/repay")
    assert r.status_code == 400 and "708 500" in r.json()["detail"]
    # Ничего не изменилось: остановленный перевод на месте, кредит не погашен.
    assert main.conn.execute("SELECT status FROM cases WHERE id=?", (held["case_id"],)).fetchone()["status"] == "open"
    assert account(bolat, BORROWER)["loan"] is not None


def test_guardian_sees_loan_kind():
    mom = client(MOM)
    main.conn.execute("INSERT INTO loans(client_id,amount,issued_at) VALUES(?,?,?)",
                      (MOM, 400000, main._now().isoformat()))
    main.conn.execute("UPDATE clients SET balance = balance + 400000 WHERE id=?", (MOM,))
    main.conn.commit()
    r = send(mom, SAFE_ACCOUNT, 400000).json()
    assert r["status"] == "held" and r["kind"] == "loan"
    req = account(client(STUDENT), STUDENT)["guardian_requests"][0]
    assert req["kind"] == "loan" and req["amount"] == 400000


def test_loan_texts_in_kazakh():
    bolat = client(BORROWER)
    bolat.cookies.set("ca_lang", "kk")
    r = send(bolat, SAFE_ACCOUNT, 800000).json()
    assert any(x["text"].startswith("Үш тәуліктен аз") for x in r["reasons"])
    assert bolat.post("/loans/999/repay").json()["detail"] == "Несие табылмады"


# ---------- Возмещение, которого банку не пришлось платить ----------

def test_compensation_counts_blacklist_refusals_and_listed_stops():
    an = analyst()
    before = an.get("/analyst/metrics").json()
    mom, st = client(MOM), client(STUDENT)
    r = send(mom, FAKE_INVEST, 50000)
    assert r.status_code == 403 and "Антифрод" in r.json()["detail"]
    held = send(st, DROP_CARD, 150000).json()                         # «Карта для возврата» в «сером» списке
    st.post(f"/cases/{held['case_id']}/return-to-sender")
    # Остановка на счёт не из базы Антифрод-центра в эту сумму не входит.
    loan = send(client(BORROWER), SAFE_ACCOUNT, 800000).json()
    client(BORROWER).post(f"/cases/{loan['case_id']}/withdraw")
    after = an.get("/analyst/metrics").json()
    assert after["compensation_avoided"] - before["compensation_avoided"] == 200000
    assert after["compensation_count"] - before["compensation_count"] == 2
    assert after["compensation_refused"] - before["compensation_refused"] == 50000
    assert after["loan_stops"] == before["loan_stops"] + 1 and after["loan_saved"] == before["loan_saved"] + 800000


def test_cooling_transfer_to_account_listed_meanwhile_counts_as_avoided_compensation():
    an, mom = analyst(), client(MOM)
    before = an.get("/analyst/metrics").json()["compensation_avoided"]
    r = send(mom, VICTIM, 120000).json()
    assert r["status"] == "pending"
    # Пока перевод ждёт, получатель попадает в базу Антифрод-центра, и клиент отменяет перевод.
    main.conn.execute("INSERT INTO afc_list(client_id,list,added_at) VALUES(?,?,?)",
                      (VICTIM, "grey", main._now().isoformat()))
    main.conn.commit()
    assert mom.post(f"/transfers/{r['transfer_id']}/cancel").status_code == 200
    assert an.get("/analyst/metrics").json()["compensation_avoided"] == before + 120000


def test_history_lists_droppers_only_after_first_stop(with_history):
    rows = main.conn.execute(
        """SELECT a.client_id, a.added_at, c.true_kind,
                  (SELECT MIN(k.created_at) FROM cases k JOIN transfers t ON t.id=k.transfer_id
                   WHERE k.kind='intercept' AND t.receiver_id=a.client_id) AS first
           FROM afc_list a JOIN clients c ON c.id=a.client_id WHERE a.client_id < 1001""").fetchall()
    assert rows and {r["true_kind"] for r in rows} == {"dropper"}
    assert all(r["added_at"] > r["first"] for r in rows)
    m = analyst().get("/analyst/metrics").json()
    assert 0 < m["compensation_avoided"] < m["saved_amount"]


# ---------- «Аренда ИП» ----------

RENTED_IP, HONEST_SHOP = 1014, 1015


def test_rented_ip_has_signal_and_its_transit_is_held():
    nur = client(RENTED_IP)
    acc = account(nur, RENTED_IP)
    assert acc["kind"] == "ip" and acc["activity"]
    assert acc["rental"]["payers"] == 8 and acc["rental"]["share"] >= 70
    assert not any(t.get("warning") for t in acc["feed"])         # выручка ИП — не «возможно, чужие деньги»
    assert acc["recruit"] is None
    r = send(nur, DROP_CARD, 150000).json()
    assert r["status"] == "held" and r["kind"] == "rental" and r["safe_return"] is None
    assert "ip_rental" in {x["code"] for x in r["reasons"]}
    card = analyst().get(f"/analyst/cases/{r['case_id']}").json()
    assert card["client_kind"] == "ip" and card["rental"]["quiet"] and len(card["rental"]["factors"]) == 3
    assert any("аренду ИП" in f["text"] for f in card["role"]["factors"])


def test_honest_shop_has_no_signal_and_pays_freely():
    asel = client(HONEST_SHOP)
    acc = account(asel, HONEST_SHOP)
    assert acc["kind"] == "ip" and acc["rental"] is None
    assert not any(t.get("warning") for t in acc["feed"])
    week = main._now() - timedelta(days=engine.RENT_DAYS)
    payers = main.conn.execute("SELECT COUNT(DISTINCT sender_id) FROM transfers WHERE receiver_id=? AND created_at >= ?",
                               (HONEST_SHOP, week.isoformat())).fetchone()[0]
    assert payers >= engine.RENT_PAYERS                          # незнакомых покупателей много, но это не транзит
    assert send(asel, SUPPLIER, 50000).json()["status"] == "done"
    assert send(asel, MOM, 20000).json()["status"] == "done"     # небольшая оплата частному лицу


def test_ip_owner_does_not_get_scheme_rules_for_customer_money():
    # Честный магазин получил оплату и сразу платит новому частному лицу ту же сумму: у частного лица это была бы
    # схема «ошибочный перевод», у ИП — обычная работа, пока нет признаков аренды.
    res = engine.evaluate(main.conn, HONEST_SHOP, MOM, 30000, main._now())
    assert res["decision"] == "allow" and res["incoming_id"] is None


def test_analyst_business_tab_and_suspend():
    an = analyst()
    listed = [a["client_id"] for a in an.get("/analyst/business").json()]
    assert RENTED_IP in listed and HONEST_SHOP not in listed and SUPPLIER not in listed
    assert client(STUDENT).get("/analyst/business").status_code == 401
    assert client(STUDENT).post(f"/analyst/business/{RENTED_IP}/suspend").status_code == 401
    assert an.post(f"/analyst/business/{HONEST_SHOP}/suspend").status_code == 404
    case = an.post(f"/analyst/business/{RENTED_IP}/suspend").json()
    assert case["client_status"] == "blocked" and case["reasons"][0]["code"] == "ip_rental"
    assert an.post(f"/analyst/business/{RENTED_IP}/suspend").status_code == 409
    nur = client(RENTED_IP)
    assert nur.get(f"/clients/{RENTED_IP}/case").json()["summary"].startswith("Похоже, счётом вашего ИП")
    assert account(nur, RENTED_IP)["rental"] is None                 # счёт закрыт: вместо карточки — блокировка
    rows = {a["client_id"]: a for a in an.get("/analyst/business").json()}
    assert rows[RENTED_IP]["status"] == "blocked"
    m = an.get("/analyst/metrics").json()
    assert m["rental_accounts"] == len(rows) and m["rental_suspended"] == 1


def test_rented_ip_owner_can_report_himself():
    nur = client(RENTED_IP)
    assert nur.post("/self-report", json={"kind": "card", "details": WORDS}).status_code == 200
    assert account(nur, RENTED_IP)["status"] == "blocked"


def test_rental_texts_in_kazakh():
    nur = client(RENTED_IP)
    nur.cookies.set("ca_lang", "kk")
    r = send(nur, DROP_CARD, 150000).json()
    assert any(x["text"].startswith("ЖК шотына") for x in r["reasons"])
    analyst().post(f"/analyst/business/{RENTED_IP}/suspend")
    assert nur.get(f"/clients/{RENTED_IP}/case").json()["summary"].startswith("Сіздің ЖК шотыңызды")


def test_rental_rules_on_synthetic_business_accounts():
    # Платежи честных ИП движок не останавливает, транзит арендованных — в основном останавливает.
    by = {}
    for scen, held, _ in STATS["ip_scored"]:
        by.setdefault(scen, []).append(held)
    for honest in ("ipo_supplier", "ipo_owner", "ipo_agent", "ipo_buyer", "ipo_contractor"):
        assert by[honest] and not any(by[honest]), honest
    rent = by["ipo_rent_quiet"] + by["ipo_rent_young"]
    assert sum(rent) / len(rent) >= 0.7
    assert any(by["ipo_rent_active"])            # и у ИП, который до аренды работал как обычный магазин
    # Правила отмечают арендованные ИП и не отмечают честные магазины, посредников и скупку.
    from app import quality
    pick = {}
    for label, n in (("shop", 3), ("buyer", 2), ("agent", 1), ("rent_quiet", 2), ("rent_young", 1), ("rent_active", 1)):
        ids = [cid for cid, lb in STATS["ip_labels"].items() if lb == label][:n]
        pick |= {cid: label for cid in ids}
    flags = quality.business_flags(main.conn, pick, STATS["now"])
    assert len(flags) == 10
    for cid, flagged in flags.items():
        assert flagged == STATS["ip_labels"][cid].startswith("rent_"), STATS["ip_labels"][cid]


# ---------- Граф денег в карточке случая ----------

def graph_of(an, case_id):
    g = an.get(f"/analyst/cases/{case_id}").json()["graph"]
    return {n["id"]: n for n in g["nodes"]}, {(e["from"], e["to"]): e for e in g["edges"]}


def test_graph_shows_complaint_chain_and_shared_card():
    block_seller()
    an = analyst()
    case_id = next(c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == SELLER)
    nodes, edges = graph_of(an, case_id)
    assert nodes[VICTIM]["role"] == "victim" and nodes[SELLER]["role"] == "current"
    assert edges[(VICTIM, SELLER)]["amount"] == 200000 and edges[(SELLER, DROP_CARD)]["amount"] == 195000
    assert (TRANSIT, DROP_CARD) in edges                  # Руслан переводил на ту же карту в другом случае
    assert nodes[VICTIM]["col"] < nodes[SELLER]["col"] < nodes[DROP_CARD]["col"]
    assert nodes[DROP_CARD]["status"] == "blocked" and nodes[DROP_CARD]["case_id"]
    assert nodes[SELLER]["status"] == "limited" and nodes[SELLER]["frozen"] == 5000
    # Все четыре пересылки Руслана складываются в одну стрелку.
    ruslan = next(c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == TRANSIT)
    _, edges = graph_of(an, ruslan)
    assert edges[(TRANSIT, DROP_CARD)]["count"] == 4
    report = an.post(f"/analyst/cases/{case_id}/report").json()
    assert report["money_graph"]["transfers"] and report["money_graph"]["accounts"]


def test_graph_marks_stopped_and_returned_transfers():
    st, an = client(STUDENT), analyst()
    held = send(st, DROP_CARD, 150000).json()
    _, edges = graph_of(an, held["case_id"])
    assert edges[(STUDENT, DROP_CARD)]["status"] == "held"
    st.post(f"/cases/{held['case_id']}/return-to-sender")
    _, edges = graph_of(an, held["case_id"])
    assert edges[(STUDENT, DROP_CARD)]["status"] == "cancelled"
    assert edges[(STRANGER, STUDENT)]["returned"] == 150000
    assert (STUDENT, STRANGER) not in edges              # возврат — подпись у исходного перевода, а не отдельная стрелка


def test_graph_collapses_crowded_column(monkeypatch):
    an = analyst()
    r = send(client(RENTED_IP), DROP_CARD, 150000).json()
    nodes, edges = graph_of(an, r["case_id"])
    assert sum(e["count"] for (a, b), e in edges.items() if b == RENTED_IP) == 8     # все восемь плательщиков
    monkeypatch.setattr(main, "GRAPH_MAX_COLUMN", 4)
    nodes, edges = graph_of(an, r["case_id"])
    group = [n for n in nodes.values() if n["role"] == "group"]
    assert len(group) == 1 and group[0]["name"] == "ещё 5 счетов"
    assert max(sum(1 for n in nodes.values() if n["col"] == c) for c in {n["col"] for n in nodes.values()}) <= 4
    # Деньги при сворачивании не теряются.
    assert sum(e["amount"] for (a, b), e in edges.items() if b == RENTED_IP) == 1030000


# ---------- «Перевод под диктовку» ----------

def send_s(c, to, amount, **session):
    return c.post("/transfers", json={"receiver_id": to, "amount": amount, "session": session})


def test_dictated_transfer_to_new_account_is_held():
    mom = client(MOM)
    assert send(mom, SAFE_ACCOUNT, 50000).json()["status"] == "done"          # без признаков — проходит
    r = send_s(mom, STUDENT, 50000, on_call=True, pasted=True, from_link=True).json()
    assert r["status"] == "done" and not r["reasons"]      # давно знакомому получателю признаки сессии не мешают
    r = send_s(mom, CARD_GIVER, 50000, on_call=True, pasted=True, from_link=True).json()
    assert r["status"] == "held" and r["kind"] == "dictation"
    assert {"on_call", "pasted", "from_link"} <= {x["code"] for x in r["reasons"]}
    assert account(client(STUDENT), STUDENT)["guardian_requests"][0]["kind"] == "dictation"
    mom.post(f"/cases/{r['case_id']}/withdraw")
    m = analyst().get("/analyst/metrics").json()
    assert m["dictation_stops"] == 1 and m["dictation_saved"] == 50000


def test_one_session_signal_alone_does_not_stop():
    r = send_s(client(MOM), VICTIM, 30000, on_call=True).json()               # разговор во время перевода — не повод
    assert r["status"] == "done" and "on_call" in {x["code"] for x in r["reasons"]}


def test_session_signals_kazakh_and_unknown_fields():
    mom = client(MOM)
    mom.cookies.set("ca_lang", "kk")
    r = send_s(mom, CARD_GIVER, 50000, on_call=True, pasted=True, new_device=True).json()
    assert any(x["text"].startswith("Аударым кезінде") for x in r["reasons"])
    assert mom.post("/transfers", json={"receiver_id": VICTIM, "amount": 1000, "session": {"on_call": "да"}}).status_code == 422


# ---------- Жалоба через другой банк ----------

BANK_CARD, BANK_CLIENT, SAULE, ARMAN, DINA = 1016, 1017, 1018, 1019, 1020


def saule_complains():
    saule = client(SAULE)
    tid = next(t["id"] for t in account(saule, SAULE)["feed"] if t["receiver_id"] == ARMAN and t["amount"] == 90000)
    return saule, saule.post(f"/transfers/{tid}/complaint").json()


def test_accounts_in_other_bank_cannot_log_in():
    people = {p["id"]: p for p in TestClient(main.app).get("/demo/clients").json()}
    assert people[BANK_CARD]["external"] and not people[SAULE]["external"]
    assert TestClient(main.app).post("/login", json={"role": "client", "client_id": BANK_CARD}).status_code == 403


def test_complaint_follows_money_into_other_bank():
    saule, r = saule_complains()
    assert r["outcome"] == "blocked" and "другой банк" in r["text"]
    an = analyst()
    req = an.get("/analyst/interbank").json()["requests"][0]
    assert (req["direction"], req["status"], req["amount"], req["bank"]) == ("out", "sent", 88000, "Банк «Б»")
    assert req["victim"].startswith("Сауле") and req["ours"].startswith("Арман") and req["cases"]
    card = account(saule, SAULE)["complaints"][0]
    assert card["stage"] == "checking" and card["frozen"] == 2000 and "ответ ожидаем до" in card["banks"][0]
    # Граф показывает счёт в другом банке и что с запросом.
    nodes, _ = graph_of(an, req["cases"][0])
    assert nodes[BANK_CARD]["kind"] == "external" and nodes[BANK_CARD]["request"]["status"] == "sent"
    # Банк «Б» заморозил деньги и вернул их пострадавшей.
    assert an.post(f"/analyst/interbank/{req['id']}/answer", json={"result": "frozen"}).json()["status"] == "frozen"
    assert an.post(f"/analyst/interbank/{req['id']}/answer", json={"result": "gone"}).status_code == 409
    card = account(saule, SAULE)["complaints"][0]
    assert card["frozen"] == 90000 and "заморозил 88 000" in card["banks"][0]
    before = account(saule, SAULE)["balance"]
    assert an.post(f"/analyst/interbank/{req['id']}/return").json()["returned"] == 88000
    assert an.post(f"/analyst/interbank/{req['id']}/return").status_code == 409
    acc = account(saule, SAULE)
    assert acc["balance"] == before + 88000 and any(t.get("refund") for t in acc["feed"])
    assert acc["complaints"][0]["returned"] == 88000
    m = an.get("/analyst/metrics").json()
    assert (m["interbank_out"], m["interbank_frozen"], m["interbank_returned"]) == (1, 88000, 88000)


def test_other_bank_answers_money_is_gone():
    saule, _ = saule_complains()
    an = analyst()
    rid = an.get("/analyst/interbank").json()["requests"][0]["id"]
    an.post(f"/analyst/interbank/{rid}/answer", json={"result": "gone"})
    assert "уже сняты" in account(saule, SAULE)["complaints"][0]["banks"][0]
    assert an.post(f"/analyst/interbank/{rid}/return").status_code == 409
    assert an.post(f"/analyst/interbank/{rid}/answer", json={"result": "maybe"}).status_code == 409


def test_request_from_other_bank_is_traced_and_answered_automatically():
    an = analyst()
    demo = an.get("/analyst/interbank").json()["incoming_demo"]
    tr = next(t for t in demo if t["receiver_id"] == DINA)
    assert tr["amount"] == 140000
    r = an.post("/analyst/interbank/incoming", json={"transfer_id": tr["id"]}).json()
    assert (r["direction"], r["status"], r["frozen"]) == ("in", "answered", 140000)
    assert "заморожено 140 000" in r["note"] and len(r["cases"]) == 2
    assert an.post("/analyst/interbank/incoming", json={"transfer_id": tr["id"]}).status_code == 409
    dina = account(client(DINA), DINA)
    assert dina["status"] == "limited" and dina["frozen"] == 5000           # невольный участник: заморожен остаток
    card = an.get(f"/analyst/cases/{r['cases'][0]}").json()
    assert any(h["who"] == "Банк «Б»" for h in card["history"])
    # Обычный перевод нашего клиента — не запрос другого банка.
    own = next(t["id"] for t in account(client(STUDENT), STUDENT)["feed"] if t["direction"] == "in")
    assert an.post("/analyst/interbank/incoming", json={"transfer_id": own}).status_code == 400
    assert client(STUDENT).post("/analyst/interbank/incoming", json={"transfer_id": tr["id"]}).status_code == 401
    m = an.get("/analyst/metrics").json()
    assert (m["interbank_in"], m["interbank_in_frozen"]) == (1, 140000)


# ---------- «Сейф» ----------

def test_safe_keeps_money_from_transfers_and_opens_only_after_a_day():
    mom, guard = client(MOM), client(STUDENT)
    # В сейфе 1,5 млн: перевести больше доступного нельзя, отказ остаётся в журнале.
    r = send(mom, SELLER, 400000)
    assert r.status_code == 400 and "сейф" in r.json()["detail"]
    assert main.conn.execute("SELECT COUNT(*) FROM events WHERE action='safe_refused'").fetchone()[0] == 1
    # Открыть через сутки: пока деньги в сейфе, доверенное лицо видит просьбу и может её отменить.
    v = mom.post("/safe/unlock", json={"amount": 500000, "way": "delay"}).json()
    assert v["amount"] == 1500000 and v["unlock_amount"] == 500000 and v["unlock_at"]
    assert account(mom, MOM)["available"] == 150000
    asked = account(guard, STUDENT)["guardian_safe"]
    assert asked[0]["ward_id"] == MOM and asked[0]["amount"] == 500000
    assert client(SELLER).post(f"/guardian/safe/{MOM}/cancel").status_code == 404     # чужой отменить не может
    assert guard.post(f"/guardian/safe/{MOM}/cancel").json()["unlock_amount"] is None
    assert mom.post("/safe/keep").status_code == 409                                    # просьбы больше нет
    # Новая просьба; сутки прошли — деньги доступны.
    mom.post("/safe/unlock", json={"amount": 500000, "way": "delay"})
    main.conn.execute("UPDATE safes SET unlock_at=? WHERE client_id=?",
                      ((main._now() - timedelta(minutes=1)).isoformat(), MOM))
    acc = account(mom, MOM)
    assert acc["safe"]["amount"] == 1000000 and acc["safe"]["unlock_at"] is None and acc["available"] == 650000
    assert send(mom, SELLER, 400000).json()["status"] in ("done", "pending", "held")
    m = analyst().get("/analyst/metrics").json()
    assert (m["safe_refused"], m["safe_refused_amount"], m["safe_unlock_requests"], m["safe_unlock_cancelled"]) == (1, 400000, 2, 1)


def test_safe_lock_and_open_in_branch():
    c = client(STUDENT)
    free = account(c, STUDENT)["available"]
    assert c.post("/safe/lock", json={"amount": free + 1}).status_code == 400
    assert c.post("/safe/lock", json={"amount": 100000}).json()["amount"] == 100000
    assert account(c, STUDENT)["available"] == free - 100000
    assert c.post("/safe/unlock", json={"amount": 200000, "way": "branch"}).status_code == 400
    assert c.post("/safe/unlock", json={"amount": 100000, "way": "teleport"}).status_code == 400
    assert c.post("/safe/unlock", json={"amount": 100000, "way": "branch"}).json()["amount"] == 0
    assert account(c, STUDENT)["available"] == free
    assert client(STUDENT).post("/safe/lock", json={"amount": 0}).status_code in (400, 422)


def test_safe_texts_in_kazakh():
    mom = client(MOM)
    mom.cookies.set("ca_lang", "kk")
    r = send(mom, SELLER, 400000)
    assert r.status_code == 400 and "сейфте жатыр" in r.json()["detail"]


# ---------- «Касса онлайн-казино» ----------

SAMAT = 1021


def test_casino_desk_is_warned_and_its_payout_is_held():
    samat = client(SAMAT)
    acc = account(samat, SAMAT)
    assert (acc["casino"]["payers"], acc["casino"]["payees"], acc["casino"]["twoway"]) == (20, 7, 7)
    assert acc["recruit"] is None                     # точнее подходит предупреждение о кассе казино
    r = send(samat, VICTIM, 25000).json()
    assert r["status"] == "held" and r["kind"] == "casino"
    assert "casino_payout" in {x["code"] for x in r["reasons"]}
    an = analyst()
    card = an.get(f"/analyst/cases/{r['case_id']}").json()
    assert card["casino"]["twoway"] == 7 and len(card["casino"]["factors"]) == 4
    assert an.get("/analyst/metrics").json()["casino_stops"] == 1


def test_casino_rule_needs_money_both_ways():
    # Деньги от двух десятков незнакомцев и выплаты семерым новым людям, но ни один из получателей сам не платил:
    # так работает перекупщик (покупает у одних, продаёт другим), это не касса казино.
    now = main._now()
    main.conn.execute("UPDATE transfers SET receiver_id = receiver_id + 30 WHERE sender_id=? AND scenario='demo_out'", (SAMAT,))
    assert engine.casino_signal(main.conn, SAMAT, now) is None
    # Сбор денег на поездку, оплаченный турагентству (ИП), — не «сбор» дроппера; та же сумма частному лицу — «сбор».
    pooled = sum(r[0] for r in main.conn.execute(
        "SELECT amount FROM transfers WHERE receiver_id=? AND scenario='demo_in'", (SAMAT,)))
    to_ip = engine.evaluate(main.conn, SAMAT, RENTED_IP, pooled, now)
    to_person = engine.evaluate(main.conn, SAMAT, VICTIM, pooled, now)
    assert "pooled_forward" not in {x["code"] for x in to_ip["reasons"]}
    assert "pooled_forward" in {x["code"] for x in to_person["reasons"]}


def test_casino_texts_in_kazakh():
    samat = client(SAMAT)
    samat.cookies.set("ca_lang", "kk")
    r = send(samat, VICTIM, 25000).json()
    assert any(x["text"].startswith("Шот ондаған бейтаныс") for x in r["reasons"])


def test_casino_rule_on_synthetic_accounts():
    from app import quality
    flags = quality.casino_flags(main.conn, STATS["cs_labels"], STATS["now"])
    for cid, flagged in flags.items():
        assert flagged == (STATS["cs_labels"][cid] == "casino"), STATS["cs_labels"][cid]
    by = {}
    for scen, held, _ in STATS["cs_scored"]:
        by.setdefault(scen, []).append(held)
    for honest in ("cso_supplier", "cso_agency", "cso_worker"):
        assert by[honest] and not any(by[honest]), honest
    assert sum(by["cso_payout"]) / len(by["cso_payout"]) >= 0.4


# ---------- Дроп-фермы и сети дропперов ----------

def test_drop_farm_is_found_and_family_tablet_is_not():
    farms = {f["device_id"]: f for f in engine.farm_devices(main.conn, main._now())}
    assert set(farms) >= {"drop-3f9c", "drop-81ad"} and "family-tablet" not in farms
    assert set(farms["drop-81ad"]["accounts"]) == {SAMAT, SAFE_ACCOUNT, 292, 295, 1023}
    # Синтетика: семьи и общежития не отмечаются, фермы — в основном отмечаются.
    from app import quality
    marks = quality.farm_flags(main.conn, STATS["device_labels"], STATS["now"])
    assert not any(f for label, f in marks if label != "farm")
    farm = [f for label, f in marks if label == "farm"]
    assert sum(farm) / len(farm) >= 0.5


def test_network_tab_shows_farms_and_suspends_accounts_on_drop_phones():
    an = analyst()
    nets = an.get("/analyst/networks").json()
    net = next(n for n in nets if len(n["farms"]) == 2)
    assert net["to_suspend"] >= 6 and net["inflow"] > 0
    full = an.get(f"/analyst/networks/{net['key']}").json()
    ids = {m["id"] for m in full["members"]}
    assert {DROP_CARD, CARD_GIVER, RENTED_IP, SAMAT, SAFE_ACCOUNT} <= ids and MOM not in ids and STUDENT not in ids
    nodes = {n["id"]: n for n in full["graph"]["nodes"]}
    assert nodes["phone-1"]["kind"] == "device" and nodes[SAMAT]["tag"].startswith("телефон")
    assert any(e["status"] == "device" and e["to"] == SAMAT for e in full["graph"]["edges"])
    # Приостановка: только счета на дроп-телефонах; второй раз — нечего приостанавливать.
    assert client(STUDENT).post(f"/analyst/networks/{net['key']}/suspend").status_code == 401
    asked = an.post(f"/analyst/networks/{net['key']}/suspend").json()       # сразу много счетов — двумя ключами
    assert asked["approval"]["kind"] == "network" and asked["to_suspend"] == net["to_suspend"]
    assert an.post(f"/analyst/networks/{net['key']}/suspend").status_code == 409
    second_key(asked)
    assert an.get(f"/analyst/networks/{net['key']}").json()["to_suspend"] == 0
    assert an.post(f"/analyst/networks/{net['key']}/suspend").status_code == 409
    timur = client(CARD_GIVER)
    assert account(timur, CARD_GIVER)["status"] == "blocked"
    assert timur.get(f"/clients/{CARD_GIVER}/case").json()["summary"].startswith("В ваш счёт входили с телефона")
    case_id = next(m["case_id"] for m in an.get(f"/analyst/networks/{net['key']}").json()["members"] if m["id"] == CARD_GIVER)
    card = an.get(f"/analyst/cases/{case_id}").json()
    assert card["reasons"][0]["code"] == "network" and card["network"]["key"] == net["key"]
    m = an.get("/analyst/metrics").json()
    assert m["networks"] == len(nets) and m["network_farms"] >= 2
    assert an.get("/analyst/networks/n0").status_code == 404


def test_network_report_and_device_in_case_report():
    an = analyst()
    key = next(n["key"] for n in an.get("/analyst/networks").json() if n["farms"])
    r = an.post(f"/analyst/networks/{key}/report")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rep = r.json()
    assert {d["model"] for d in rep["devices"]} >= {"Samsung Galaxy A15", "Redmi 13C"}
    assert any(a["id"] == SAMAT for a in rep["accounts"])
    assert main.conn.execute("SELECT COUNT(*) FROM events WHERE action='network_report'").fetchone()[0] == 1
    # В выгрузке по случаю теперь есть устройства входа клиента.
    samat = client(SAMAT)
    case_id = send(samat, VICTIM, 25000).json()["case_id"]
    rep = an.post(f"/analyst/cases/{case_id}/report").json()
    assert {d["device_id"] for d in rep["technical"]["devices"]} == {f"own-{SAMAT}", "drop-81ad"}


def test_network_suspension_text_in_kazakh():
    an = analyst()
    key = next(n["key"] for n in an.get("/analyst/networks").json() if n["farms"])
    second_key(an.post(f"/analyst/networks/{key}/suspend").json())
    timur = client(CARD_GIVER)
    timur.cookies.set("ca_lang", "kk")
    assert timur.get(f"/clients/{CARD_GIVER}/case").json()["summary"].startswith("Сіздің шотыңызға бірнеше")


# ---------- Журнал, который нельзя подделать ----------

def test_journal_detects_changed_and_deleted_records():
    an = analyst()
    block_seller()
    j = an.get("/analyst/journal").json()
    assert j["ok"] and j["count"] > 3 and j["broken"] is None
    assert not main.conn.execute("SELECT 1 FROM events WHERE hash IS NULL").fetchone()
    assert client(STUDENT).get("/analyst/journal").status_code == 401
    assert client(STUDENT).post("/demo/journal/tamper").status_code == 401
    # «Сотрудник-сообщник» правит запись прямо в базе: проверка находит именно её.
    t = an.post("/demo/journal/tamper").json()
    j = an.get("/analyst/journal").json()
    assert not j["ok"] and j["broken"]["id"] == t["id"]
    # Новые записи после подделки цепочку не «чинят».
    an.post(f"/analyst/cases/{an.get('/analyst/cases').json()[0]['case_id']}/report")
    assert an.get("/analyst/journal").json()["broken"]["id"] == t["id"]


def test_journal_detects_a_deleted_record():
    an = analyst()
    block_seller()
    ids = [r[0] for r in main.conn.execute("SELECT id FROM events ORDER BY id")]
    main.conn.execute("DELETE FROM events WHERE id=?", (ids[1],))
    main.conn.commit()
    assert an.get("/analyst/journal").json()["broken"]["id"] == ids[2]


# ---------- Правило двух ключей и скрытые данные ----------

def test_employees_and_switch():
    an = analyst()
    assert an.get("/analyst/employees").json()["name"] == "Аружан С."
    assert an.post("/analyst/employee", json={"employee": "medet"}).json()["name"] == "Медет К."
    assert an.get("/me").json()["analyst"]["name"] == "Медет К."
    assert an.post("/analyst/employee", json={"employee": "boss"}).status_code == 400
    assert client(STUDENT).post("/analyst/employee", json={"employee": "medet"}).status_code == 401


def test_second_key_can_reject_and_requester_can_cancel():
    an, an2 = analyst(), analyst("medet")
    key = next(n["key"] for n in an.get("/analyst/networks").json() if n["farms"])
    a = an.post(f"/analyst/networks/{key}/suspend").json()["approval"]
    assert [x["id"] for x in an2.get("/analyst/approvals").json()] == [a["id"]]
    assert an.post(f"/analyst/approvals/{a['id']}/reject").json()["approval"]["status"] == "rejected"   # отменил сам
    assert an2.post(f"/analyst/approvals/{a['id']}/confirm").status_code == 409
    assert an.get(f"/analyst/networks/{key}").json()["to_suspend"] > 0                                # ничего не закрыто
    b = an.post(f"/analyst/networks/{key}/suspend").json()["approval"]
    assert an2.post(f"/analyst/approvals/{b['id']}/reject").json()["approval"]["decided_by"] == "Медет К."
    assert an2.get("/analyst/approvals").json() == []
    texts = [r[0] for r in main.conn.execute("SELECT details FROM events WHERE action='approval_rejected'")]
    assert "запрос отменён" in texts[0] and "отклонил второй сотрудник" in texts[1]


def test_small_refund_and_unwitting_release_need_one_key():
    an = analyst()
    victim, tid = block_seller()
    main.conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,'done',?)",
        (SELLER, VICTIM, 120000, main._now().isoformat(), tid))
    main.conn.commit()
    queue = {c["client_id"]: c["case_id"] for c in an.get("/analyst/cases").json()}
    card = an.post(f"/analyst/cases/{queue[DROP_CARD]}/refund").json()       # 80 000 ₸ — меньше порога
    assert card["approval"] is None and card["complaint"]["returned"] == 200000
    # Данияр по правилам похож на невольного участника: снять ограничения может один сотрудник.
    card = an.post(f"/analyst/cases/{queue[SELLER]}/decision", json={"decision": "approve"}).json()
    assert card["approval"] is None and card["client_status"] == "active"


def test_personal_data_is_hidden_and_every_view_is_logged():
    an = analyst()
    case = an.get("/analyst/cases").json()[0]
    card = an.get(f"/analyst/cases/{case['case_id']}").json()
    assert "••••••" in card["pii"]["iin"] and "•• ••• ••" in card["pii"]["phone"] and card["pii_log"] == []
    assert an.post(f"/analyst/clients/{case['client_id']}/pii", json={"reason": ""}).status_code == 400
    assert an.post(f"/analyst/clients/{case['client_id']}/pii", json={"reason": "afc"}).status_code == 400
    full = an.post(f"/analyst/clients/{case['client_id']}/pii", json={"reason": "appeal"}).json()
    assert len(full["iin"]) == 12 and full["iin"].isdigit() and full["log"][0]["employee"] == "Аружан С."
    assert client(STUDENT).post(f"/analyst/clients/{MOM}/pii", json={"reason": "contact"}).status_code == 401
    # Выгрузка для Антифрод-центра — тоже просмотр данных: он записывается с причиной.
    rep = an.post(f"/analyst/cases/{case['case_id']}/report").json()
    assert rep["client"]["iin"] == full["iin"]
    card = an.get(f"/analyst/cases/{case['case_id']}").json()
    assert [x["reason"] for x in card["pii_log"]] == ["afc", "appeal"]
    # Клиент видит, когда и зачем банк открывал его данные.
    me = client(case["client_id"]) if 1001 <= case["client_id"] <= 1999 else None
    if me:
        assert [x["reason"] for x in account(me, case["client_id"])["data_access"]] == ["afc", "appeal"]
    assert account(client(MOM), MOM)["data_access"] == []


# ---------- «Счёт открыли без меня» ----------

VENERA, VENERA_2 = 1022, 1023


def test_account_opened_without_me_is_stopped_by_the_owner():
    ven = client(VENERA)
    acc = account(ven, VENERA)["own_accounts"]
    assert len(acc) == 1 and acc[0]["id"] == VENERA_2 and acc[0]["via"] == "remote" and not acc[0]["own_device"]
    assert (acc[0]["inflow"], acc[0]["senders"], acc[0]["device"]) == (415000, 3, "Redmi 13C")
    # Под этим счётом не войти, и персонажем он не показывается.
    assert TestClient(main.app).post("/login", json={"role": "client", "client_id": VENERA_2}).status_code == 403
    assert VENERA_2 not in {p["id"] for p in TestClient(main.app).get("/demo/clients").json()}
    assert client(MOM).post(f"/accounts/{VENERA_2}/claim", json={"answer": "not_me"}).status_code == 404
    assert ven.post(f"/accounts/{VENERA_2}/claim", json={"answer": "maybe"}).status_code == 400
    r = ven.post(f"/accounts/{VENERA_2}/claim", json={"answer": "not_me"}).json()
    acc = r["accounts"][0]
    assert acc["answer"] == "not_me" and acc["status"] == "blocked" and acc["frozen"] == 115000 and acc["code"]
    assert ven.post(f"/accounts/{VENERA_2}/claim", json={"answer": "mine"}).status_code == 409
    assert account(ven, VENERA)["status"] == "active"                    # сама Венера не дроппер
    # Подтверждение для полиции и суда, его проверка по коду.
    page = ven.get(f"/clients/{VENERA}/identity-claim/{VENERA_2}").text
    assert "без моего ведома" in page and "115 000 ₸" in page and acc["number"] in page
    assert client(MOM).get(f"/clients/{VENERA}/identity-claim/{VENERA_2}").status_code == 403
    assert "клиент сообщил банку о счёте" in TestClient(main.app).get(f"/verify/{acc['code']}").text
    # Сотрудник видит случай: кража личности, счёт открыт удалённо с чужого устройства.
    an = analyst()
    case = next(c for c in an.get("/analyst/cases").json() if c["client_id"] == VENERA_2)
    assert case["needs_decision"]
    card = an.get(f"/analyst/cases/{case['case_id']}").json()
    assert card["reasons"][0]["code"] == "identity_theft" and card["identity"]["answer"] == "not_me"
    assert card["identity"]["owner_id"] == VENERA and not card["identity"]["owner_device"]
    assert card["history"][0]["action"] == "identity_theft"


def test_account_owner_can_confirm_it_was_her():
    ven = client(VENERA)
    assert ven.post(f"/accounts/{VENERA_2}/claim", json={"answer": "mine"}).json()["accounts"] == []
    assert account(ven, VENERA)["own_accounts"] == []
    assert main.conn.execute("SELECT status FROM clients WHERE id=?", (VENERA_2,)).fetchone()[0] == "active"


def test_identity_claim_texts_in_kazakh():
    ven = client(VENERA)
    ven.cookies.set("ca_lang", "kk")
    assert ven.post(f"/accounts/{VENERA_2}/claim", json={"answer": "not_me"}).json()["text"].startswith("Шот тоқтатылды")
    assert "келісімімсіз" in ven.get(f"/clients/{VENERA}/identity-claim/{VENERA_2}").text


# ---------- Стартовая страница и «Показ за 3 минуты» ----------

def test_landing_client_screen_and_tour_script_are_served():
    c = TestClient(main.app)
    home = c.get("/")
    assert home.status_code == 200 and "Показ за 3 минуты" in home.text and 'src="/tour.js"' in home.text
    assert "x-frame-options" in home.headers
    assert 'id="who"' in c.get("/client").text                         # экран клиента переехал на /client
    js = c.get("/tour.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "tourStart" in js.text
    for page in ("/client", "/analyst", "/analyst/stats"):
        assert 'src="/tour.js"' in c.get(page).text, page
    # Во всех меню «Экран клиента» ведёт на /client, а название проекта — на стартовую страницу.
    for page in ("/client", "/analyst", "/analyst/stats", "/quality"):
        html = c.get(page).text
        assert 'href="/client"' in html and 'class="brand" href="/"' in html, page


# ---------- «Светофор получателя» и группы персонажей ----------

def test_recipient_traffic_light():
    st = client(STUDENT)
    risk = lambda rid: st.get(f"/recipients/{rid}/check").json()["risk"]   # noqa: E731
    assert risk(DROP_CARD)["level"] == "red" and risk(DROP_CARD)["text"].startswith("Получатель в списке")
    assert risk(FAKE_INVEST)["level"] == "red"
    assert risk(SAFE_ACCOUNT)["level"] == "red" and risk(CARD_GIVER)["level"] == "red"     # дроп-телефон, сеть
    assert risk(MOM)["level"] == "green" and risk(MOM)["text"].startswith("Вы давно")
    # Перевод-«приманка» 20 минут назад отправителя знакомым не делает.
    assert risk(STRANGER)["level"] == "green" and not risk(STRANGER)["text"].startswith("Вы давно")
    assert risk(RECRUIT)["level"] == "yellow" and "незнакомых" in risk(RECRUIT)["text"]
    assert risk(BANK_CARD)["level"] == "yellow" and "другом банке" in risk(BANK_CARD)["text"]
    main.conn.execute("INSERT INTO clients(id,name,account_opened_at,balance) VALUES(1990,'Новый счёт',?,0)",
                      ((main._now() - timedelta(days=3)).isoformat(),))
    main.conn.commit()
    assert risk(1990)["level"] == "yellow" and "недавно" in risk(1990)["text"]
    st.cookies.set("ca_lang", "kk")
    assert risk(MOM)["text"].startswith("Сіз бұл адаммен")


def test_every_persona_is_in_a_demo_group():
    import re
    html = open(os.path.join(main.STATIC, "index.html"), encoding="utf-8").read()
    block = html[html.index("const GROUPS"):html.index("async function loadPeople")]
    grouped = {int(x) for x in re.findall(r"\b1\d{3}\b", block)}
    people = {p["id"] for p in TestClient(main.app).get("/demo/clients").json() if not p["external"]}
    assert people == grouped


# ---------- Заявление в полицию ----------

def test_police_statement_is_ready_after_a_complaint():
    victim, tid = block_seller()
    comp = account(victim, VICTIM)["complaints"][0]
    assert comp["number"].startswith("Ж-2")
    page = victim.get(f"/clients/{VICTIM}/police-statement/{comp['id']}").text
    assert "200 000 ₸" in page and comp["number"] in page and "qamqor.gov.kz" in page and "102" in page
    assert "Данияр" in page and "Карта «для возврата»" not in page      # чужие имена из цепочки не раскрываются
    assert "заморожено" in page and "не подаёт" in page
    assert client(MOM).get(f"/clients/{VICTIM}/police-statement/{comp['id']}").status_code == 403
    assert victim.get(f"/clients/{VICTIM}/police-statement/99999").status_code == 404
    victim.cookies.set("ca_lang", "kk")
    assert "АРЫЗ" in victim.get(f"/clients/{VICTIM}/police-statement/{comp['id']}").text


def test_police_statement_mentions_the_other_bank():
    saule, _ = saule_complains()
    comp = account(saule, SAULE)["complaints"][0]
    page = saule.get(f"/clients/{SAULE}/police-statement/{comp['id']}").text
    assert "Банк «Б»" in page and "Антифрод-центр" in page


# ---------- Решения сотрудников по признакам, калькулятор, скорость и настройка весов ----------

def test_rule_feedback_from_analyst_decisions(with_history):
    import json
    fb = analyst().get("/analyst/metrics").json()["rule_feedback"]
    assert fb and all(f["stops"] >= 3 and 0 <= f["released"] <= f["stops"] for f in fb)
    assert [f["share"] for f in fb] == sorted((f["share"] for f in fb), reverse=True)
    real = {r["code"]: r["points"] for c in main.conn.execute("SELECT reasons FROM cases") for r in json.loads(c["reasons"])}
    assert all(real[f["code"]] == f["points"] for f in fb)


def test_stats_page_has_calculator_rules_and_journal():
    html = TestClient(main.app).get("/analyst/stats").text
    for part in ('id="calc"', 'id="rules"', 'id="journal"', "setupCalc", "rule_feedback"):
        assert part in html, part


def test_quality_json_has_speed_and_weights_for_tuning():
    q = TestClient(main.app).get("/quality.json").json()
    assert q["speed"]["count"] > 10000 and 0 < q["speed"]["median_ms"] <= q["speed"]["p95_ms"] < 100
    tun = q["tuning"]
    assert tun["weights"]["stranger_incoming"] == 25 and tun["weights"]["amount_match"] == 30
    # Пересчёт по сочетаниям признаков с текущими весами даёт ту же долю пойманных схем, что и страница.
    caught = sum(c["n"] for c in tun["combos"] if c["scheme"] and c["inc"]
                 and sum(tun["weights"][k] for k in c["codes"]) >= q["threshold"])
    scheme = sum(c["n"] for c in tun["combos"] if c["scheme"])
    cur = next(c for c in q["curve"] if c["threshold"] == q["threshold"])
    assert abs(100 * caught / scheme - cur["recall"][1]) < 1.5
    assert 'id="tune"' in TestClient(main.app).get("/quality").text


# ---------- Исправления после проверки 20 ноября ----------

def test_action_from_another_tab_is_not_made_on_behalf_of_another_persona():
    c = client(STUDENT)                                                  # вкладка с Айданой
    c.post("/login", json={"role": "client", "client_id": VICTIM})       # в другой вкладке вошли как Гульнар
    before = main.conn.execute("SELECT COUNT(*) FROM transfers WHERE sender_id=?", (VICTIM,)).fetchone()[0]
    r = c.post("/transfers", json={"receiver_id": MOM, "amount": 1000}, headers={"X-Client-Id": str(STUDENT)})
    assert r.status_code == 409 and r.headers["x-session-changed"] == "1"
    assert main.conn.execute("SELECT COUNT(*) FROM transfers WHERE sender_id=?", (VICTIM,)).fetchone()[0] == before
    # Совпадает — работает; без номера (старые вызовы, тур) — как раньше.
    assert c.get(f"/recipients/{MOM}/check", headers={"X-Client-Id": str(VICTIM)}).status_code == 200
    assert c.get(f"/recipients/{MOM}/check").status_code == 200


def test_decision_closes_the_waiting_release_request():
    an, an2 = analyst(), analyst("medet")
    case_id = next(c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == TRANSIT)
    a = an.post(f"/analyst/cases/{case_id}/decision", json={"decision": "approve"}).json()["approval"]
    card = an.post(f"/analyst/cases/{case_id}/decision", json={"decision": "reject", "comment": "Оставляем: счёт дропперский"}).json()
    assert card["status"] == "kept" and card["approval"] is None
    assert an.get("/analyst/approvals").json() == []
    assert not next(c for c in an.get("/analyst/cases").json() if c["case_id"] == case_id)["second_key"]
    assert an2.post(f"/analyst/approvals/{a['id']}/confirm").status_code == 409
    assert "уже принято решение" in card["history"][-1]["text"]


def test_refund_request_is_closed_when_the_client_returns_the_money_himself():
    an = analyst()
    block_seller()
    drop = next(c["case_id"] for c in an.get("/analyst/cases").json() if c["client_id"] == DROP_CARD)
    an.post(f"/analyst/cases/{drop}/refund")
    assert client(DROP_CARD).post(f"/cases/{drop}/refund").json()["amount"] == 195000
    assert an.get("/analyst/approvals").json() == [] and an.get(f"/analyst/cases/{drop}").json()["approval"] is None


def test_stale_network_request_is_closed_on_confirm():
    an, an2 = analyst(), analyst("medet")
    key = next(n["key"] for n in an.get("/analyst/networks").json() if n["farms"])
    a = an.post(f"/analyst/networks/{key}/suspend").json()["approval"]
    for m in an.get(f"/analyst/networks/{key}").json()["members"]:
        if m["phones"] and m["status"] == "active":
            main.conn.execute("UPDATE clients SET status='blocked' WHERE id=?", (m["id"],))
    main.conn.commit()
    r = an2.post(f"/analyst/approvals/{a['id']}/confirm")
    assert r.status_code == 409 and "Просьба закрыта" in r.json()["detail"]
    assert an.get("/analyst/approvals").json() == []


def test_network_key_is_its_drop_phone_and_survives_new_members():
    an = analyst()
    net = next(n for n in an.get("/analyst/networks").json() if n["farms"])
    assert net["key"] == "drop-3f9c"
    main.conn.execute("INSERT INTO afc_list(client_id,list,added_at,note) VALUES(5,'grey',?,'тест')", (main._now().isoformat(),))
    main.conn.execute("INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(5,?,1000,?,'done')",
                      (DROP_CARD, main._now().isoformat()))
    main.conn.commit()
    again = an.get("/analyst/networks/drop-3f9c").json()
    assert again["accounts"] > net["accounts"] and 5 in {m["id"] for m in again["members"]}


def test_safe_does_not_block_returning_money_to_the_sender():
    st = client(STUDENT)
    assert st.post("/safe/lock", json={"amount": account(st, STUDENT)["balance"]}).status_code == 200
    tid = feed_item(st, STUDENT, STRANGER)["id"]
    assert st.post(f"/incoming/{tid}/return").json()["status"] == "returned"
    acc = account(st, STUDENT)
    assert acc["safe"]["amount"] == acc["balance"] == 42000                  # сейф уменьшился вместе со счётом
    main.conn.execute("UPDATE clients SET balance = balance + 100000 WHERE id=?", (STUDENT,))
    main.conn.commit()
    assert account(st, STUDENT)["safe"]["amount"] == 42000                   # и сам обратно не вырос


def test_branch_unlock_caps_the_waiting_request():
    mom = client(MOM)
    mom.post("/safe/unlock", json={"amount": 1500000, "way": "delay"})
    v = mom.post("/safe/unlock", json={"amount": 1000000, "way": "branch"}).json()
    assert (v["amount"], v["unlock_amount"]) == (500000, 500000)
    v = mom.post("/safe/unlock", json={"amount": 500000, "way": "branch"}).json()
    assert (v["amount"], v["unlock_amount"], v["unlock_at"]) == (0, None, None)


def test_identity_claim_on_a_suspended_account_extends_the_same_case():
    an = analyst()
    key = next(n["key"] for n in an.get("/analyst/networks").json() if n["farms"])
    second_key(an.post(f"/analyst/networks/{key}/suspend").json())
    client(VENERA).post(f"/accounts/{VENERA_2}/claim", json={"answer": "not_me"})
    cases = [c for c in an.get("/analyst/cases").json() if c["client_id"] == VENERA_2]
    assert len(cases) == 1 and cases[0]["needs_decision"]
    card = an.get(f"/analyst/cases/{cases[0]['case_id']}").json()
    assert [r["code"] for r in card["reasons"]] == ["identity_theft", "network"] and card["frozen"] == 115000


def test_no_personal_data_for_another_bank_client():
    assert analyst().post(f"/analyst/clients/{BANK_CARD}/pii", json={"reason": "contact"}).status_code == 404
