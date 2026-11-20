"""API демо-банка.  Запуск:  uvicorn app.main:app --reload"""
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta
from html import escape
from urllib.parse import parse_qs, quote

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response as RawResponse
from pydantic import BaseModel, Field

from . import clock, db, engine, i18n, journal, messages, metrics, seed, trainer, vault
from .i18n import t

conn = db.connect()
STATIC = os.path.join(os.path.dirname(__file__), "static")
UPLOADS = os.environ.get("CA_UPLOADS", os.path.join(os.path.dirname(__file__), "..", "uploads"))
ANALYST_PIN = os.environ.get("CA_ANALYST_PIN", "")      # для показа; в реальной системе вход сотрудника банка
if not ANALYST_PIN:
    # На открытом хостинге код из README знает любой: там без заданного кода сервер придумывает свой.
    ANALYST_PIN = secrets.token_hex(4) if os.environ.get("CA_PUBLIC") == "1" else "2026"
    print(f"[Чистый счёт] Код доступа аналитика: {ANALYST_PIN}  (задайте свой в CA_ANALYST_PIN)", flush=True)
SESSION_HOURS = 8
MAX_SESSIONS = 5000
PIN_TRIES_PER_IP = 5            # неверных кодов в минуту с одного адреса
PIN_TRIES_TOTAL = 30            # и со всех адресов вместе: от подбора с многих адресов
MAX_FILE = 5 * 1024 * 1024
MAX_BODY = 8 * 1024 * 1024      # файл 5 МБ в base64 занимает около 6,7 МБ
MAX_DOCS = 6                    # файлов на одну апелляцию
DEMO_HISTORY = os.environ.get("CA_DEMO_HISTORY", "1") == "1"   # история за 30 дней для панели эффективности
DEMO_TTL_HOURS = 48             # демо-данные старше этого пересоздаются: иначе «свежий» входящий устаревает
APPEAL_SLA_DAYS = 3             # за сколько дней банк обещает ответить на апелляцию
WARN_MIN_AMOUNT = 20_000        # входящие от незнакомцев от этой суммы получают предупреждение
COMPLAINT_DAYS = 30             # на перевод можно пожаловаться в течение стольких дней
COMPLAINT_LIMIT = 3             # жалоб от одного клиента за COMPLAINT_DAYS: защита от блокировок по ложным жалобам
CHAIN_HOPS = 3                  # на сколько звеньев жалоба проходит по цепочке переводов
INTERBANK_HOURS = 24            # срок ответа на запрос между банками через Антифрод-центр (в демо)
SAFE_UNLOCK_HOURS = 24          # «Сейф» через приложение открывается только через сутки
TWO_KEY_REFUND = 100_000        # возврат пострадавшему от этой суммы подтверждает второй сотрудник
# Сотрудники банка в демо. В банке у каждого свой вход; здесь код доступа один, сотрудника выбирают из списка.
EMPLOYEES = {"aruzhan": "Аружан С.", "medet": "Медет К."}
# Зачем сотрудник открывает скрытые персональные данные клиента. Причина видна клиенту и остаётся в журнале.
PII_REASONS = {"contact": "Связаться с клиентом", "appeal": "Проверка документов по апелляции",
               "police": "Запрос правоохранительных органов", "afc": "Выгрузка для Антифрод-центра"}
GUARDIAN_KNOWN_DAYS = 30        # доверенное лицо: человек, с которым переводы идут дольше этого срока
GUARDIAN_REMOVE_HOURS = 24      # отключение доверенного лица вступает в силу через сутки
COOLING_MIN = 100_000           # крупный перевод новому получателю уходит не сразу
COOLING_MINUTES = int(os.environ.get("CA_COOLING_MINUTES", "60"))
COOLING_KNOWN_HOURS = 24        # получатель «знакомый», если переводы с ним были раньше этого срока
FILE_TYPES = [(b"\x89PNG\r\n\x1a\n", ".png", "image/png"), (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
              (b"%PDF-", ".pdf", "application/pdf")]

# Какие документы просим при блокировке из-за пересылки чужих денег.
DOC_KINDS = {
    "chat": {"title": "Переписка с тем, кто просил переслать деньги", "required": True},
    "deal": {"title": "Объявление, договор или чек, если деньги были оплатой товара", "required": False},
    "other": {"title": "Другое подтверждение", "required": False},
}
STATUS_TEXT = {
    "open": "Ждём ваши документы",
    "under_review": "На рассмотрении у сотрудника банка",
    "unblocked": "Ограничения сняты",
    "kept": "Ограничения оставлены",
    "returned": "Деньги возвращены отправителю",
    "released": "Перевод разрешён",
    "cancelled": "Перевод отменён",
    "withdrawn": "Клиент отменил перевод",
    "guardian_cancelled": "Перевод отменило доверенное лицо",
}
EVENT_TEXT = {
    "blocked": "Операции по счёту приостановлены",
    "complaint": "Поступила жалоба отправителя",
    "intercepted": "Перевод остановлен системой",
    "returned": "Клиент вернул деньги отправителю",
    "insisted": "Клиент настаивает на переводе",
    "document": "Клиент приложил документ",
    "appeal": "Клиент подал апелляцию",
    "decision": "Решение сотрудника банка",
    "withdrawn": "Клиент отменил перевод",
    "call_signal": "Сигнал оператора: клиенту звонят с подозрительного номера",
    "report": "Сведения выгружены для Антифрод-центра",
    "guardian_notified": "Доверенному лицу сообщили об остановке перевода",
    "guardian_asked": "Клиент настаивает: перевод ждёт подтверждения доверенного лица",
    "guardian_confirmed": "Доверенное лицо подтвердило перевод, решение за сотрудником",
    "guardian_cancelled": "Доверенное лицо отменило перевод",
    "cleared": "Выдана справка о снятии ограничений",
    "recruit_dismissed": "Клиент ответил на предупреждение о вербовке: знает отправителей",
    "refunded": "Деньги возвращены пострадавшему",
    "escalated": "Сотрудник приостановил счёт полностью",
    "self_report": "Клиент сам сообщил банку, что его втянули в схему",
    "loan_repaid": "Клиент вернул кредит банку",
    "afc_refused": "Перевод получателю из «чёрного» списка Антифрод-центра не выполнен",
    "interbank_out": "Деньги ушли в другой банк: отправлен запрос на заморозку",
    "interbank_in": "Запрос другого банка через Антифрод-центр: перевод их клиента — мошенничество",
    "interbank_answer": "Ответ другого банка на запрос",
    "approval_requested": "Действие ждёт подтверждения второго сотрудника",
    "approval_confirmed": "Второй сотрудник подтвердил действие",
    "approval_rejected": "Второй сотрудник отклонил действие",
    "identity_theft": "Владелец сообщил, что не открывал этот счёт",
    "cash_done": "Клиент снял наличные в банкомате",
    "cash_face_asked": "Снятие денег незнакомца: банкомат просит проверку по лицу",
    "cash_delayed": "Крупное снятие наличных во время подозрительного звонка отложено на час",
    "cash_cancelled": "Снятие наличных отменено",
    "cash_expired": "Снятие наличных не завершено: банкомат не дождался клиента",
    "cash_stopped": "Снятие наличных отменено: по счёту ограничения",
}
INTERBANK_STATUS = {"sent": "Запрос отправлен, ждём ответ", "frozen": "Деньги заморожены в другом банке",
                    "gone": "Деньги уже сняты или ушли дальше", "answered": "Ответили автоматически"}
UNRESOLVED = ("open", "under_review", "kept")      # блокировка действует, пока случай в одном из этих статусов
SESSION_CODES = {code for code, _, _ in engine.SESSION_SIGNALS}   # признаки «перевода под диктовку»
SELF_REPORT_KINDS = {
    "card": "Передал карту, её данные или доступ к приложению другому человеку",
    "transit": "Принимал чужие деньги и пересылал их по чьей-то просьбе",
    "offer": "Предложили такую «подработку», но клиент ещё ничего не делал",
}
# Те же статусы словами для сотрудника банка.
ANALYST_STATUS = {
    ("block", "open"): "Апелляция не подана",
    ("intercept", "open"): "Клиент ещё не выбрал действие",
    ("block", "under_review"): "Ждёт решения",
    ("intercept", "under_review"): "Ждёт решения",
}

LOCK = threading.Lock()
SESSIONS: dict[str, dict] = {}
_pin_failures: list[tuple[float, str]] = []
# Защита страниц: не встраивать в чужие сайты, скрипты и запросы только со своего адреса.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'"),
}


def tx():
    """Один запрос — одна транзакция: при любой ошибке все изменения откатываются."""
    with LOCK:
        try:
            _settle_due()       # переводы, у которых закончился период охлаждения, уходят получателю
            _settle_safes()     # сейфы, которые просили открыть сутки назад, открываются
            _settle_cash()      # отложенные снятия наличных становятся доступны, неиспользованные истекают
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


app = FastAPI(title="Чистый счёт: защита от статуса дроппера", dependencies=[Depends(tx, scope="function")])


@app.exception_handler(RequestValidationError)
async def bad_input(request: Request, exc: RequestValidationError):
    """Вместо технического списка ошибок — фраза, которую можно показать клиенту."""
    return JSONResponse({"detail": t("bad_input")}, status_code=422)


@app.middleware("http")
async def guard(request: Request, call_next):
    size = request.headers.get("content-length")
    lang = request.cookies.get("ca_lang", "ru")
    i18n.lang.set(lang if lang in i18n.LANGS else "ru")
    if size and size.isdigit() and int(size) > MAX_BODY:
        return JSONResponse({"detail": t("too_big")}, status_code=413)
    if "transfer-encoding" in request.headers:
        # Тело без указанной длины обошло бы проверку размера выше: такие запросы не принимаем.
        return JSONResponse({"detail": t("too_big")}, status_code=411)
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    # Страницы и скрипты браузер каждый раз сверяет с сервером (по ETag — быстро): иначе после обновления
    # на хостинге он мог открыть старый тур или старый экран из своего кэша.
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


class LoginIn(BaseModel):
    role: str                     # client | analyst
    client_id: int | None = None
    pin: str = Field("", max_length=64)
    employee: str = Field("", max_length=32)    # сотрудник банка (в демо — из списка EMPLOYEES)


class SessionIn(BaseModel):
    """Что приложение заметило во время перевода. Только отметки: ни буфера обмена, ни ссылок, ни номеров."""
    on_call: bool = False       # идёт телефонный разговор
    pasted: bool = False        # реквизиты вставлены из буфера обмена
    from_link: bool = False     # приложение открыто по ссылке из мессенджера
    new_device: bool = False    # вход с нового устройства меньше суток назад


class TransferIn(BaseModel):
    receiver_id: int
    amount: int = Field(gt=0, le=100_000_000)
    session: SessionIn | None = None


class DocumentIn(BaseModel):
    kind: str
    filename: str = Field(max_length=255)
    content_base64: str = Field(max_length=7_200_000)


class AppealIn(BaseModel):
    explanation: str = Field(max_length=5000)


class DecisionIn(BaseModel):
    decision: str   # approve | reject
    comment: str = Field("", max_length=2000)


def _now() -> datetime:
    return clock.now()


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₸"


def log(actor: str, action: str, case_id: int | None = None, transfer_id: int | None = None, **details):
    """Журнал действий: кто, что и когда сделал по случаю. Пишется в той же транзакции, что и само действие."""
    conn.execute("INSERT INTO events(at,actor,action,case_id,transfer_id,details) VALUES(?,?,?,?,?,?)",
                 (_now().isoformat(), actor, action, case_id, transfer_id,
                  json.dumps(details, ensure_ascii=False) if details else None))
    journal.seal(conn)          # отпечаток записи вместе с отпечатком предыдущей: подделку видно при проверке


# ---------- Демо-данные ----------

def ensure_demo(force: bool = False) -> bool:
    """Создаёт данные при первом запуске и пересоздаёт устаревшие."""
    row = conn.execute("SELECT value FROM meta WHERE key='seeded_at'").fetchone()
    stale = row is None or _now() - datetime.fromisoformat(row["value"]) > timedelta(hours=DEMO_TTL_HOURS)
    if not (force or stale):
        return False
    seed.populate(conn, evaluate=DEMO_HISTORY, with_history=DEMO_HISTORY, reset=True, with_business=False)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('history',?)", ("1" if DEMO_HISTORY else "0",))
    shutil.rmtree(UPLOADS, ignore_errors=True)
    return True


ensure_demo()


# ---------- Вход и права ----------

def session(ca_client: str | None = Cookie(None), ca_analyst: str | None = Cookie(None)) -> dict:
    """Сессии клиента и сотрудника независимы: экран клиента и панель можно держать открытыми рядом."""
    now = time.time()
    s = {role: v if v and v["expires"] > now else None
         for role, v in (("client", SESSIONS.get(ca_client or "")), ("analyst", SESSIONS.get(ca_analyst or "")))}
    if not (s["client"] or s["analyst"]):
        raise HTTPException(401, t("login"))
    return s


def as_client(s: dict = Depends(session), x_client_id: str | None = Header(None)) -> dict:
    if not s["client"]:
        raise HTTPException(401, t("login_client"))
    # Вход клиента общий на весь браузер. Экран сообщает, какой персонаж на нём открыт: если в другой вкладке
    # вошли под другим, действие не выполняется от чужого имени — экран входит заново и повторяет запрос.
    if x_client_id and x_client_id != str(s["client"]["client_id"]):
        raise HTTPException(409, t("session_changed"), headers={"X-Session-Changed": "1"})
    return s["client"]


def as_analyst(s: dict = Depends(session)) -> dict:
    if not s["analyst"]:
        raise HTTPException(401, "Войдите как сотрудник банка")
    return s["analyst"]


def _client(cid: int):
    row = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, t("client_not_found"))
    return row


def _own_client(s: dict, cid: int):
    """Клиент видит только свой счёт; сотрудник банка видит любой."""
    if not s["analyst"] and s["client"]["client_id"] != cid:
        raise HTTPException(403, t("not_yours"))
    return _client(cid)


def _own_case(client: dict, case_id: int, kind: str):
    case = conn.execute("SELECT * FROM cases WHERE id=? AND kind=?", (case_id, kind)).fetchone()
    if not case or case["client_id"] != client["client_id"]:
        raise HTTPException(404, t("case_not_found"))   # чужой случай неотличим от несуществующего
    return case


@app.get("/demo/clients")
def demo_clients():
    """Персонажи для показа: под ними входит экран клиента."""
    ensure_demo()
    # Второй счёт на имя персонажа (в демо — открытый без его ведома) отдельным персонажем не показывается.
    rows = conn.execute("""SELECT id, name, kind FROM clients WHERE id BETWEEN 1001 AND 1999 AND owner_id IS NULL
                           ORDER BY id""").fetchall()
    # Счета в другом банке нужны как получатели и отправители переводов, войти под ними нельзя.
    return [{"id": r["id"], "name": i18n.name(r["id"], r["name"]), "external": r["kind"] == "external"} for r in rows]


@app.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    """Демо-вход. Клиент выбирает персонажа, сотрудник банка вводит код доступа."""
    now = time.time()
    if body.role == "analyst":
        ip = request.client.host if request.client else "?"
        _pin_failures[:] = [f for f in _pin_failures if now - f[0] < 60]
        # Лимит на адрес: чужие ошибки не запирают вход сотруднику. Общий лимит выше: от подбора с многих адресов.
        if sum(f[1] == ip for f in _pin_failures) >= PIN_TRIES_PER_IP or len(_pin_failures) >= PIN_TRIES_TOTAL:
            raise HTTPException(429, "Слишком много попыток. Подождите минуту")
        if not secrets.compare_digest(body.pin.encode(), ANALYST_PIN.encode()):
            _pin_failures.append((now, ip))
            raise HTTPException(401, "Неверный код доступа")
        emp = body.employee if body.employee in EMPLOYEES else next(iter(EMPLOYEES))
        s = {"role": "analyst", "name": EMPLOYEES[emp], "employee": emp}
    elif body.role == "client":
        c = _client(body.client_id or 0)
        if not 1001 <= c["id"] <= 1999 or c["kind"] == "external" or c["owner_id"]:
            raise HTTPException(403, "Войти можно только под демо-персонажем")
        s = {"role": "client", "client_id": c["id"], "name": c["name"]}
    else:
        raise HTTPException(400, "Неизвестная роль")
    for old in [k for k, v in SESSIONS.items() if v["expires"] <= now]:
        SESSIONS.pop(old)
    while len(SESSIONS) >= MAX_SESSIONS:            # память сервера не растёт от бесконечных входов
        SESSIONS.pop(next(iter(SESSIONS)))
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = s | {"expires": now + SESSION_HOURS * 3600}
    response.set_cookie(f"ca_{body.role}", token, httponly=True, samesite="strict", max_age=SESSION_HOURS * 3600,
                        secure=os.environ.get("CA_HTTPS") == "1")   # на хостинге с https куки не уходят по http
    return s


@app.post("/logout")
def logout(response: Response, role: str = "client",
           ca_client: str | None = Cookie(None), ca_analyst: str | None = Cookie(None)):
    if role not in ("client", "analyst"):
        raise HTTPException(400, "Неизвестная роль")
    SESSIONS.pop((ca_analyst if role == "analyst" else ca_client) or "", None)
    response.delete_cookie(f"ca_{role}")
    return {"ok": True}


@app.get("/me")
def me(s: dict = Depends(session)):
    return s


class EmployeeIn(BaseModel):
    employee: str = Field(max_length=32)


@app.post("/analyst/employee")
def switch_employee(body: EmployeeIn, s: dict = Depends(as_analyst)):
    """Демо: войти как другой сотрудник, чтобы подтвердить действие вторым ключом. В банке у каждого свой вход."""
    if body.employee not in EMPLOYEES:
        raise HTTPException(400, "Нет такого сотрудника")
    s["employee"], s["name"] = body.employee, EMPLOYEES[body.employee]
    return {"employee": body.employee, "name": s["name"], "employees": EMPLOYEES}


@app.get("/analyst/employees")
def employees(s: dict = Depends(as_analyst)):
    return {"employee": s.get("employee"), "name": s["name"], "employees": EMPLOYEES}


# ---------- Экран клиента: счёт и переводы ----------

@app.get("/", include_in_schema=False)
def landing():
    """Стартовая страница: что это за проект, главные цифры, карта защит и «Показ за 3 минуты»."""
    return FileResponse(os.path.join(STATIC, "landing.html"))


@app.get("/client", include_in_schema=False)
def client_app():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/tour.js", include_in_schema=False)
def tour_script():
    return FileResponse(os.path.join(STATIC, "tour.js"), media_type="text/javascript")


def _refresh_status(cid: int) -> str:
    """Статус счёта по нерешённым блокировкам: полная закрывает счёт, соразмерная замораживает спорную сумму.

    Справка о прежнем снятии ограничений перестаёт действовать, как только появляется новое ограничение.
    """
    scopes = {r["scope"] for r in conn.execute(
        f"""SELECT scope FROM cases WHERE client_id=? AND kind='block'
            AND status IN ({",".join("?" * len(UNRESOLVED))})""", (cid, *UNRESOLVED))}
    status = "blocked" if "full" in scopes else "limited" if scopes else "active"
    conn.execute("UPDATE clients SET status=? WHERE id=?", (status, cid))
    if status != "active":
        _stop_cash(cid)
        conn.execute("UPDATE clearances SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL",
                     (_now().isoformat(), cid))
    return status


def _held_sum(cid: int) -> int:
    # Деньги под перевод на паузе или в периоде охлаждения и под снятие наличных, которое ждёт у банкомата,
    # зарезервированы: потратить их ещё раз нельзя.
    return conn.execute("""SELECT COALESCE(SUM(amount),0) AS s FROM transfers
                           WHERE sender_id=? AND status IN ('held','pending')""", (cid,)).fetchone()["s"] + _cash_reserved(cid)


def _frozen_sum(cid: int, except_case: int | None = None) -> int:
    """Спорные деньги, замороженные соразмерной блокировкой: остальным клиент пользуется."""
    return conn.execute(
        f"""SELECT COALESCE(SUM(frozen),0) AS s FROM cases WHERE client_id=? AND kind='block' AND scope='partial'
            AND status IN ({",".join("?" * len(UNRESOLVED))}) AND id IS NOT ?""",
        (cid, *UNRESOLVED, except_case)).fetchone()["s"]


def _safe_sum(cid: int) -> int:
    """Деньги в «Сейфе»: их нельзя перевести через приложение (не больше, чем есть на счёте)."""
    row = conn.execute("SELECT amount FROM safes WHERE client_id=?", (cid,)).fetchone()
    return max(0, min(row["amount"], _client(cid)["balance"])) if row else 0


def _available(cid: int) -> int:
    return _client(cid)["balance"] - _held_sum(cid) - _frozen_sum(cid) - _safe_sum(cid)


def _limited_since(cid: int) -> str | None:
    """С какого момента получатель считается «новым» для клиента с соразмерной блокировкой:
    со спорного поступления (или с начала случая, если поступления нет)."""
    rows = conn.execute(
        f"""SELECT k.created_at, i.created_at AS inc_at FROM cases k LEFT JOIN transfers i ON i.id=k.incoming_transfer_id
            WHERE k.client_id=? AND k.kind='block' AND k.scope='partial'
            AND k.status IN ({",".join("?" * len(UNRESOLVED))})""", (cid, *UNRESOLVED)).fetchall()
    return min((r["inc_at"] or r["created_at"] for r in rows), default=None)


def _move(sender_id: int, receiver_id: int, amount: int):
    conn.execute("UPDATE clients SET balance = balance - ? WHERE id=?", (amount, sender_id))
    conn.execute("UPDATE clients SET balance = balance + ? WHERE id=?", (amount, receiver_id))
    _trim_safe(sender_id)


def _trim_safe(cid: int):
    """В сейфе не больше, чем на счёте: если деньги ушли (например, возврат отправителю), сейф уменьшается
    и потом сам не «вырастает» обратно за счёт новых поступлений."""
    conn.execute("""UPDATE safes SET amount = MIN(amount, MAX(0, (SELECT balance FROM clients WHERE id=?)))
                    WHERE client_id=?""", (cid, cid))


def _spendable_for_return(cid: int) -> int:
    """Сколько можно вернуть отправителю или банку. Деньги в сейфе этому не мешают: вернуть отправителю
    (в пределах присланного) или погасить кредит — безопасно, сейф защищает от переводов мошенникам."""
    return max(0, _available(cid) + _safe_sum(cid))


@app.get("/clients/{cid}")
def client_feed(cid: int, s: dict = Depends(session)):
    """Профиль и лента операций клиента."""
    c = _own_client(s, cid)
    rows = conn.execute(
        """SELECT t.id, t.amount, t.created_at, t.status, t.return_of,
                  t.sender_id, s.name AS sender, t.receiver_id, r.name AS receiver,
                  k.id AS case_id, k.status AS case_status, k.decision_comment, k.guardian_state, t.release_at
           FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
           LEFT JOIN cases k ON k.transfer_id=t.id AND k.kind='intercept'
           WHERE t.sender_id=? OR (t.receiver_id=? AND t.status='done')   -- получатель не видит остановленные
           ORDER BY t.created_at DESC, t.id DESC LIMIT 30""",
        (cid, cid)).fetchall()
    held, frozen, safe, cash = _held_sum(cid), _frozen_sum(cid), _safe_sum(cid), _cash_reserved(cid)
    feed = []
    for r in rows:
        item = dict(r) | {"direction": "in" if r["receiver_id"] == cid else "out",
                          "sender": i18n.name(r["sender_id"], r["sender"]),
                          "receiver": i18n.name(r["receiver_id"], r["receiver"])}
        if item["direction"] == "in":
            item |= _incoming_flags(cid, r)
            # Возврат по жалобе приходит от счёта из цепочки, а не от того, кому клиент переводил.
            item["refund"] = bool(r["return_of"]) and _incoming_row(r["return_of"])["receiver_id"] != r["sender_id"]
        else:
            item["can_complain"] = _can_complain(r)
            item["complained"] = bool(conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?",
                                                   (r["id"],)).fetchone())
        feed.append(item)
    feed = sorted(feed + _loan_feed(cid) + _cash_feed(cid), key=lambda x: x["created_at"], reverse=True)[:30]
    active = _cash_active(cid)
    return {"id": c["id"], "name": c["name"], "balance": c["balance"], "held": held - cash, "frozen": frozen,
            "available": max(0, c["balance"] - held - frozen - safe), "status": c["status"], "feed": feed,
            "cash": _cash_view(active) if active else None,
            "guardian_cash": _guardian_cash(cid),
            "loan": _loan_view(cid) if c["status"] != "blocked" else None,
            "safe": _safe_view(cid),
            "guardian_safe": _guardian_safe(cid),
            "kind": c["kind"], "activity": i18n.activity(c["activity"]),
            "rental": _rental_view(cid) if c["kind"] == "ip" and c["status"] != "blocked" else None,
            "casino": _casino_view(cid) if c["status"] != "blocked" else None,
            "own_accounts": _own_accounts(cid),
            "data_access": [{"at": r["at"], "reason": r["reason"]} for r in conn.execute(
                "SELECT reason, at FROM data_access WHERE client_id=? ORDER BY id DESC LIMIT 10", (cid,))],
            "call": _call_info(cid),
            "complaints": _complaints_view(cid),
            "self_report": _self_report_view(cid),
            "recruit": _recruit_view(cid) if c["status"] != "blocked" else None,
            "guardian_requests": _guardian_requests(cid),
            "cooling": _cooling_list("t.sender_id=?", (cid,)),
            "now": _now().isoformat(),          # для отсчёта времени на экране: время банка, а не браузера
            "guardian_cooling": _cooling_list(
                """t.sender_id IN (SELECT client_id FROM guardians WHERE guardian_id=?
                                   AND (remove_at IS NULL OR remove_at > ?))""", (cid, _now().isoformat()))}


def _incoming_row(tid: int):
    return conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()


def _returned_sum(inc_id: int) -> int:
    """Сколько денег этого перевода уже вернули его отправителю: получатель сам или счета дальше по цепочке."""
    return conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM transfers WHERE return_of=? AND status='done'",
                        (inc_id,)).fetchone()["s"]


def _returned(inc_id: int) -> bool:
    inc = _incoming_row(inc_id)
    return bool(inc) and _returned_sum(inc_id) >= inc["amount"]


def _incoming_flags(cid: int, r) -> dict:
    """Нужно ли предупредить клиента, что пришли, возможно, чужие деньги."""
    inc = _incoming_row(r["id"])
    complained = bool(conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?", (inc["id"],)).fetchone())
    flags = {"warning": False, "complained": complained, "returned": _returned(inc["id"])}
    # ИП получает деньги от незнакомых людей каждый день: это выручка, предупреждать о «чужих деньгах» не нужно.
    if (inc["status"] != "done" or inc["return_of"] or flags["returned"] or _client(cid)["kind"] == "ip"
            or engine.find_forward(conn, cid, inc)):
        return flags
    fresh = _now() - datetime.fromisoformat(inc["created_at"]) <= timedelta(hours=engine.LOOKBACK_HOURS)
    stranger = not engine._had_contact(conn, cid, inc["sender_id"], inc["created_at"], inc["id"])
    known = bool(conn.execute("SELECT 1 FROM incoming_marks WHERE transfer_id=?", (inc["id"],)).fetchone())
    flags["warning"] = complained or (fresh and stranger and not known and inc["amount"] >= WARN_MIN_AMOUNT)
    return flags


def _can_complain(r) -> bool:
    if r["status"] != "done" or r["return_of"]:
        return False
    return _now() - datetime.fromisoformat(r["created_at"]) <= timedelta(days=COMPLAINT_DAYS)


def _intercept_view(case) -> dict:
    inc = conn.execute(
        """SELECT t.id, t.amount, t.sender_id, c.name FROM transfers t
           JOIN clients c ON c.id=t.sender_id WHERE t.id=?""", (case["incoming_transfer_id"],)).fetchone() \
        if case["incoming_transfer_id"] else None
    codes = [r.get("code") for r in json.loads(case["reasons"])]
    tr = conn.execute(
        """SELECT t.amount, t.receiver_id, c.name FROM transfers t JOIN clients c ON c.id=t.receiver_id WHERE t.id=?""",
        (case["transfer_id"],)).fetchone()
    loan = _loan_view(case["client_id"]) if "loan_forward" in codes else None
    return {"case_id": case["id"], "transfer_id": case["transfer_id"], "status": "held",
            "case_status": case["status"], "score": case["score"],
            "reasons": i18n.reasons(json.loads(case["reasons"])),
            "amount": tr["amount"], "receiver": i18n.name(tr["receiver_id"], tr["name"]),
            # Что показать клиенту в окне остановки: схема с чужими деньгами, «аренда ИП», деньги из свежего кредита,
            # звонок или подозрительный получатель.
            "kind": "casino" if "casino_payout" in codes else "scheme" if inc else ("rental" if "ip_rental" in codes else
                                          "loan" if "loan_forward" in codes else
                                          "call" if "suspicious_call" in codes else
                                          "dictation" if set(codes) & SESSION_CODES else
                                          "message" if "scam_message" in codes else "grey"),
            "loan": loan,
            "guardian": _guardian_name(case["guardian_id"]), "guardian_state": case["guardian_state"],
            "safe_return": {"to_id": inc["sender_id"], "to_name": i18n.name(inc["sender_id"], inc["name"]),
                            "amount": inc["amount"]} if inc else None}


@app.post("/transfers")
def create_transfer(body: TransferIn, s: dict = Depends(as_client)):
    """Создаёт перевод от имени вошедшего клиента. Если движок видит схему, перевод останавливается."""
    sender_id = s["client_id"]
    sender = _client(sender_id)
    _client(body.receiver_id)
    if sender_id == body.receiver_id:
        raise HTTPException(400, t("self_transfer"))
    if sender["status"] == "blocked":
        raise HTTPException(403, t("account_blocked"))
    if engine.afc_status(conn, body.receiver_id) == "black":
        # Отказ записываем в журнал (ответ без исключения, иначе запись откатится): по закону банк возмещает клиенту
        # перевод на счёт из базы Антифрод-центра, и панель эффективности считает, сколько возмещать не пришлось.
        log(f"client:{sender_id}", "afc_refused", None, None, receiver=body.receiver_id, amount=body.amount)
        return JSONResponse({"detail": t("afc_black")}, status_code=403)
    if sender["status"] == "limited" and not engine._had_contact(conn, sender_id, body.receiver_id,
                                                                 _limited_since(sender_id)):
        # Соразмерная блокировка: свои деньги доступны, но только для тех, кому клиент переводил и раньше.
        raise HTTPException(403, t("limited_new_receiver"))
    now = _now()
    res = engine.evaluate(conn, sender_id, body.receiver_id, body.amount, now,
                          body.session.model_dump() if body.session else None)
    # На один «чужой» входящий существует не больше одного незакрытого случая.
    prev = None
    if res["decision"] == "hold" and res["incoming_id"] is not None:
        prev = conn.execute(
            """SELECT * FROM cases WHERE client_id=? AND incoming_transfer_id=? AND kind='intercept'
               AND status IN ('open','under_review')""", (sender_id, res["incoming_id"])).fetchone()
        if prev and prev["status"] == "under_review":
            raise HTTPException(409, t("under_review_dup"))
        if prev:   # новая попытка заменяет прежнюю, её резерв освобождается
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (prev["transfer_id"],))
    if _available(sender_id) < body.amount:
        safe = _safe_sum(sender_id)
        if safe and _available(sender_id) + safe >= body.amount:
            # Перевести можно было бы только деньги из сейфа. Отказ пишем в журнал: так видно, сколько сейф уберёг.
            log(f"client:{sender_id}", "safe_refused", None, None, receiver=body.receiver_id, amount=body.amount)
            return JSONResponse({"detail": t("no_funds_safe", m=_money(safe))}, status_code=400)
        raise HTTPException(400, t("no_funds_frozen" if _frozen_sum(sender_id) else "no_funds"))

    if res["decision"] != "hold":
        if not res.get("return_to_sender") and _needs_cooling(sender_id, body.receiver_id, body.amount, now):
            release = (now + timedelta(minutes=COOLING_MINUTES)).isoformat()
            cur = conn.execute("""INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,release_at)
                                  VALUES(?,?,?,?,'pending',?)""",
                               (sender_id, body.receiver_id, body.amount, now.isoformat(), release))
            log("system", "cooling_started", None, cur.lastrowid, release_at=release)
            return {"transfer_id": cur.lastrowid, "status": "pending", "release_at": release,
                    "amount": body.amount, "score": res["score"], "reasons": res["reasons"]}
        cur = conn.execute(
            "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(?,?,?,?,'done')",
            (sender_id, body.receiver_id, body.amount, now.isoformat()))
        _move(sender_id, body.receiver_id, body.amount)
        return {"transfer_id": cur.lastrowid, "status": "done", "score": res["score"], "reasons": res["reasons"]}

    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status) VALUES(?,?,?,?,'held')",
        (sender_id, body.receiver_id, body.amount, now.isoformat()))
    reasons = json.dumps(res["reasons"], ensure_ascii=False)
    if prev:
        conn.execute("UPDATE cases SET transfer_id=?, score=?, reasons=? WHERE id=?",
                     (cur.lastrowid, res["score"], reasons, prev["id"]))
        case_id = prev["id"]
    else:
        case_id = conn.execute(
            """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (sender_id, cur.lastrowid, res["incoming_id"], "intercept", res["score"], reasons,
             now.isoformat())).lastrowid
    log("system", "intercepted", case_id, cur.lastrowid, score=res["score"], amount=body.amount)
    if any(r["code"] == "suspicious_call" for r in res["reasons"]):
        log("system", "call_signal", case_id, cur.lastrowid)
    g = _guardian_of(sender_id)
    if g and not prev:
        conn.execute("UPDATE cases SET guardian_id=?, guardian_state='notified' WHERE id=?", (g["guardian_id"], case_id))
        log("system", "guardian_notified", case_id, cur.lastrowid, guardian=g["guardian_id"])
    return _intercept_view(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


@app.get("/cases/{case_id}")
def intercept_case(case_id: int, s: dict = Depends(as_client)):
    """Остановленный перевод: клиент может вернуться к выбору позже."""
    return _intercept_view(_own_case(s, case_id, "intercept"))


def _return_incoming(client: dict, inc) -> dict:
    """Безопасный возврат: деньги уходят только на счёт, с которого пришли, и только один раз."""
    cid = client["client_id"]
    if _client(cid)["status"] == "blocked":
        # Пока счёт приостановлен, деньгами распоряжается только банк: иначе их можно увести в обход проверки.
        raise HTTPException(403, t("account_blocked"))
    if _returned(inc["id"]):
        raise HTTPException(409, t("already_returned"))
    if engine.afc_status(conn, inc["sender_id"]) == "black":
        # Деньги пришли со счёта подтверждённого мошенника: возврат отдал бы их ему же. Разбирается банк.
        raise HTTPException(403, t("afc_black_return"))
    # Остановленные пересылки этих денег больше не нужны: их резерв освобождается.
    cases = conn.execute(
        """SELECT * FROM cases WHERE client_id=? AND incoming_transfer_id=? AND kind='intercept'
           AND status IN ('open','under_review')""", (cid, inc["id"])).fetchall()
    for case in cases:
        conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
        conn.execute("UPDATE cases SET status='returned' WHERE id=?", (case["id"],))
    amount = inc["amount"] - _returned_sum(inc["id"])      # часть могли уже вернуть пострадавшему по жалобе
    _release_cash(cid, amount)
    available = _spendable_for_return(cid)
    if available < amount:
        raise HTTPException(400, t("return_no_funds", available=_money(available), amount=_money(amount)))
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,?,?)",
        (cid, inc["sender_id"], amount, _now().isoformat(), "done", inc["id"]))
    _move(cid, inc["sender_id"], amount)
    for case in cases or [None]:
        log(f"client:{cid}", "returned", case["id"] if case else None, inc["id"], amount=amount)
    return {"status": "returned", "return_transfer_id": cur.lastrowid, "amount": amount}


@app.post("/cases/{case_id}/return-to-sender")
def return_to_sender(case_id: int, s: dict = Depends(as_client)):
    """Возврат из окна остановленного перевода."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] != "open":
        raise HTTPException(409, t("decided"))
    if not case["incoming_transfer_id"]:
        raise HTTPException(400, t("no_return"))
    return _return_incoming(s, _incoming_row(case["incoming_transfer_id"]))


@app.post("/cases/{case_id}/withdraw")
def withdraw(case_id: int, s: dict = Depends(as_client)):
    """Клиент сам отменяет остановленный перевод: деньги остаются у него."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] not in ("open", "under_review"):
        raise HTTPException(409, t("decided"))
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
    conn.execute("UPDATE cases SET status='withdrawn' WHERE id=?", (case_id,))
    log(f"client:{s['client_id']}", "withdrawn", case_id, case["transfer_id"])
    return {"status": "withdrawn"}


@app.get("/recipients/{rid}/check")
def check_recipient(rid: int, s: dict = Depends(as_client)):
    """Проверка получателя до перевода: база Антифрод-центра (в демо — имитация) и «светофор получателя»."""
    _client(rid)
    status = engine.afc_status(conn, rid)
    return {"list": status, "text": t(f"afc_{status}_check") if status else None,
            "risk": _recipient_risk(s["client_id"], rid)}


def _recipient_risk(sender: int, rid: int) -> dict:
    """«Светофор получателя»: насколько рискованно переводить этому получателю, по данным банка.

    Красный — счёт в базе Антифрод-центра, ограничен банком, работает с дроп-телефона, входит в сеть дропперов
    или встречается в нескольких независимых случаях. Жёлтый — новый счёт, деньги многих незнакомцев
    за неделю или счёт в другом банке, о котором у нас нет данных. Зелёный — признаков нет. Подробностей о чужом
    счёте клиент не видит: только уровень и общую причину.
    """
    now, c = _now(), _client(rid)
    afc = engine.afc_status(conn, rid)
    if afc:
        return {"level": "red", "text": t(f"afc_{afc}_check")}
    # «Знакомый» — с кем переводы были раньше чем неделю назад: свежий перевод-«приманка» знакомым не делает.
    known = engine._had_contact(conn, sender, rid, (now - timedelta(days=7)).isoformat())
    suspect = (c["status"] != "active"
               or any(rid in f["accounts"] for f in engine.farm_devices(conn, now))
               or any(rid in n["members"] for n in metrics.networks(conn, now))
               or any(a["client_id"] == rid for a in metrics.linked_accounts(conn)))
    if suspect:
        return {"level": "red", "text": t("risk.red")}
    if c["kind"] == "external":
        return {"level": "yellow", "text": t("risk.external")}
    if not known and now - datetime.fromisoformat(c["account_opened_at"]) < timedelta(days=engine.YOUNG_ACCOUNT_DAYS):
        return {"level": "yellow", "text": t("risk.young")}
    if not known and (engine.recruitment_signal(conn, rid, now) or engine.casino_signal(conn, rid, now)
                      or (c["kind"] == "ip" and engine.rental_signal(conn, rid, now))):
        return {"level": "yellow", "text": t("risk.flow")}
    return {"level": "green", "text": t("risk.known" if known else "risk.green")}


class CallSignalIn(BaseModel):
    active: bool


@app.post("/telecom/signal")
def telecom_signal(body: CallSignalIn, s: dict = Depends(as_client)):
    """Сигнал оператора связи о подозрительном звонке клиенту (как в пилоте «Телеком Антифрод»).

    В банке сигнал приходит от оператора по защищённому каналу; в демо его включает сам клиент кнопкой.
    """
    cid = s["client_id"]
    if body.active:
        conn.execute("INSERT OR REPLACE INTO call_signals(client_id,number,started_at) VALUES(?,?,?)",
                     (cid, "+7 708 *** 41 19", _now().isoformat()))
    else:
        conn.execute("DELETE FROM call_signals WHERE client_id=?", (cid,))
    return _call_info(cid)


def _call_info(cid: int) -> dict | None:
    if not engine.call_active(conn, cid, _now()):
        return None
    row = conn.execute("SELECT number, started_at FROM call_signals WHERE client_id=?", (cid,)).fetchone()
    return {"number": row["number"], "started_at": row["started_at"]}


def _own_incoming(client: dict, tid: int):
    inc = _incoming_row(tid)
    if not inc or inc["receiver_id"] != client["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if inc["status"] != "done" or inc["return_of"]:
        raise HTTPException(400, t("cannot_return"))
    return inc


@app.post("/incoming/{tid}/return")
def return_incoming(tid: int, s: dict = Depends(as_client)):
    """Возврат прямо из списка операций, до того как мошенник попросит переслать деньги."""
    return _return_incoming(s, _own_incoming(s, tid))


@app.post("/incoming/{tid}/known")
def mark_known(tid: int, s: dict = Depends(as_client)):
    """Клиент знает отправителя: предупреждение у этого перевода скрывается. Перехват при пересылке остаётся."""
    inc = _own_incoming(s, tid)
    conn.execute("INSERT OR IGNORE INTO incoming_marks(transfer_id,created_at) VALUES(?,?)",
                 (inc["id"], _now().isoformat()))
    return {"ok": True}


def _trace_complaint(tr, complaint_id: int, now: datetime, complainant: int, actor: str | None = None) -> list[int]:
    """Идёт по цепочке от перевода пострадавшего и приостанавливает счета, через которые прошли его деньги.

    Звено, которое переслало деньги дальше, получает случай с этой пересылкой. Счёт, на котором деньги
    остались (начиная со второго звена), тоже приостанавливается: это обычно конечная карта дроппера.
    Первый получатель, который ещё ничего не переслал, не блокируется, а получает предупреждение.
    Если деньги ушли на счёт в другом банке, приостановить его мы не можем: туда через Антифрод-центр
    уходит запрос на заморозку (в демо — имитация, ответ даёт сотрудник во вкладке «Другие банки»).
    """
    actor = actor or f"client:{complainant}"
    created, seen, frontier = [], {tr["sender_id"]}, [tr]
    for hop in range(1, CHAIN_HOPS + 1):
        nxt = []
        for inc in frontier:
            holder = inc["receiver_id"]
            if holder in seen or _returned(inc["id"]):
                continue
            seen.add(holder)
            other = _client(holder)
            if other["kind"] == "external":
                due = now + timedelta(hours=INTERBANK_HOURS)
                conn.execute("""INSERT INTO interbank(direction,bank,complaint_id,transfer_id,amount,status,sent_at,due_at)
                                VALUES('out',?,?,?,?,'sent',?,?)""",
                             (other["bank"], complaint_id, inc["id"], inc["amount"], now.isoformat(), due.isoformat()))
                log("system", "interbank_out", None, inc["id"], bank=other["bank"], amount=inc["amount"], hop=hop,
                    text=f"Деньги ушли в {other['bank']}: через Антифрод-центр отправлен запрос на заморозку "
                         f"{_money(inc['amount'])}, ответ до {due.strftime('%d.%m %H:%M')}")
                continue
            fwd = engine.find_forward(conn, holder, inc)
            sent = sum(r["amount"] for r in fwd)
            # Деньги, снятые наличными, тоже ушли со счёта: снять их — то же, что переслать дальше.
            cashed = _cashed_out(holder, inc, sent)
            if not fwd and not cashed and hop == 1:
                continue
            reason = "complaint" if hop == 1 else "complaint_chain"
            reasons = [{"code": reason, "points": 30, "text": i18n.MSG["reason." + reason][0]}]
            if fwd:
                first = fwd[0]
                res = engine.evaluate(conn, holder, first["receiver_id"], first["amount"],
                                      datetime.fromisoformat(first["created_at"]))
                reasons += res["reasons"]
                score, out_id = min(100, res["score"] + 30), first["id"]
                text = f"деньги переслали дальше ({_money(sent)})"
                nxt.extend(fwd)
            else:
                score, out_id = 30, None
                text = f"на счёте остались деньги из цепочки ({_money(inc['amount'])})"
            if cashed:
                reasons.append({"code": "cash_out", "points": 30, "text": i18n.MSG["reason.cash_out"][0], "amount": cashed})
                score = min(100, score + 30)
                text = (text + "; " if fwd else "") + f"деньги сняты наличными ({_money(cashed)})"
            # Сколько денег из цепочки осталось на этом счёте: их банк замораживает в любом случае.
            frozen = max(0, inc["amount"] - sent - cashed)
            case_id = conn.execute(
                """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at,
                                     complaint_id,hop,frozen) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (holder, out_id, inc["id"], "block", score, json.dumps(reasons, ensure_ascii=False),
                 now.isoformat(), complaint_id, hop, frozen)).lastrowid
            # Соразмерная блокировка: если по правилам клиент похож на невольного участника, замораживается
            # только спорная сумма. Остальных (и всех, в ком система не уверена) счёт закрывается целиком.
            case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            role = engine.assess_role(conn, holder, case, now)
            partial = role["verdict"] == "unwitting"
            if partial:
                conn.execute("UPDATE cases SET scope='partial' WHERE id=?", (case_id,))
                text = (f"Заморожено {_money(frozen)}: {text}. Остальными деньгами клиент пользуется, "
                        "но только для знакомых получателей")
            else:
                text = "Операции приостановлены: " + text
            _refresh_status(holder)
            log(actor, "interbank_in" if actor.startswith("bank:") else "complaint", case_id, tr["id"], amount=tr["amount"])
            log("system", "blocked", case_id, out_id or inc["id"], text=text, hop=hop,
                scope="partial" if partial else "full", role=role["label"])
            created.append(case_id)
        frontier = nxt
    return created


def _cashed_out(cid: int, inc, sent: int) -> int:
    """Сколько денег этого входящего клиент снял наличными: деньги незнакомцев, выданные банкоматом после него
    (в окне движка). Как и с переводами, считаем, что сначала тратятся свои деньги."""
    until = (datetime.fromisoformat(inc["created_at"]) + timedelta(hours=engine.LOOKBACK_HOURS)).isoformat()
    s = conn.execute("""SELECT COALESCE(SUM(foreign_amount),0) AS s FROM cash_withdrawals WHERE client_id=?
                        AND status='done' AND created_at >= ? AND created_at <= ?""",
                     (cid, inc["created_at"], until)).fetchone()["s"]
    return max(0, min(s, inc["amount"] - sent))


@app.post("/transfers/{tid}/complaint")
def complain(tid: int, s: dict = Depends(as_client)):
    """Отправитель сообщает, что перевёл деньги мошенникам. Банк проверяет, куда ушли деньги дальше."""
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
    if not tr or tr["sender_id"] != s["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if not _can_complain(tr):
        raise HTTPException(400, t("complaint_old", days=COMPLAINT_DAYS))
    if conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?", (tid,)).fetchone():
        raise HTTPException(409, t("complaint_dup"))
    since = (_now() - timedelta(days=COMPLAINT_DAYS)).isoformat()
    if conn.execute("SELECT COUNT(*) AS n FROM complaints WHERE complainant_id=? AND created_at >= ?",
                    (s["client_id"], since)).fetchone()["n"] >= COMPLAINT_LIMIT:
        raise HTTPException(429, t("complaint_limit", n=COMPLAINT_LIMIT))
    now = _now()
    complaint_id = conn.execute(
        "INSERT INTO complaints(transfer_id,complainant_id,outcome,created_at) VALUES(?,?,?,?)",
        (tid, s["client_id"], "pending", now.isoformat())).lastrowid
    if _returned(tid):
        outcome, text, cases = "already_returned", t("complaint_already_returned"), []
    else:
        cases = _trace_complaint(tr, complaint_id, now, s["client_id"])
        asked = conn.execute("SELECT bank FROM interbank WHERE complaint_id=? AND direction='out'",
                             (complaint_id,)).fetchall()
        outcome = "blocked" if cases or asked else "warned"
        text = t("complaint_blocked", n=len(cases)) if cases else t("complaint_warned") if not asked else ""
        if asked:
            text = (text + " " + t("complaint_interbank", b=asked[0]["bank"])).strip()
    conn.execute("UPDATE complaints SET outcome=?, case_id=? WHERE id=?",
                 (outcome, cases[0] if cases else None, complaint_id))
    if not cases:
        log(f"client:{s['client_id']}", "complaint", None, tid, amount=tr["amount"], outcome=outcome)
    return {"outcome": outcome, "text": text}


# ---------- Что стало с жалобой: пострадавший видит, где его деньги ----------

def _complaint_state(p) -> dict:
    """Сколько денег пострадавшего заморожено в цепочке и сколько ему уже вернули."""
    tr = _incoming_row(p["transfer_id"])
    cases = conn.execute("SELECT * FROM cases WHERE complaint_id=?", (p["id"],)).fetchall()
    asks = conn.execute("SELECT * FROM interbank WHERE complaint_id=? AND direction='out'", (p["id"],)).fetchall()
    returned = min(tr["amount"], _returned_sum(tr["id"]))
    # Заморожено и в нашем банке, и в других банках по нашим запросам (за вычетом того, что они уже вернули).
    frozen = min(tr["amount"] - returned, sum(k["frozen"] for k in cases if k["status"] in UNRESOLVED)
                 + sum(a["frozen"] - a["returned"] for a in asks if a["status"] == "frozen"))
    if returned >= tr["amount"]:
        stage = "returned"
    elif any(k["status"] in ("open", "under_review") for k in cases) or any(a["status"] == "sent" for a in asks):
        stage = "checking"
    elif frozen:
        stage = "frozen"
    elif p["outcome"] == "warned":
        stage = "warned"
    else:
        stage = "closed"
    return {"amount": tr["amount"], "returned": returned, "frozen": frozen, "stage": stage, "transfer": tr, "asks": asks}


def _complaints_view(cid: int) -> list[dict]:
    out = []
    for p in conn.execute("SELECT * FROM complaints WHERE complainant_id=? ORDER BY id DESC LIMIT 5", (cid,)):
        st = _complaint_state(p)
        who = _client(st["transfer"]["receiver_id"])
        vals = {"m": _money(st["amount"]), "r": _money(st["returned"]), "f": _money(st["frozen"]),
                "left": _money(st["amount"] - st["returned"])}
        text = t("complaint.stage." + st["stage"], **vals)
        if st["returned"] and st["stage"] != "returned":
            text = t("complaint.part", **vals) + " " + text
        # Что с деньгами, которые ушли в другой банк.
        banks = [t("complaint.interbank." + a["status"], b=a["bank"], m=_money(a["frozen"] if a["status"] == "frozen" else a["amount"]),
                   d=datetime.fromisoformat(a["due_at"]).strftime("%d.%m %H:%M")) for a in st["asks"]]
        out.append({"id": p["id"], "created_at": p["created_at"], "amount": st["amount"], "number": _complaint_number(p),
                    "receiver": i18n.name(who["id"], who["name"]), "stage": st["stage"],
                    "returned": st["returned"], "frozen": st["frozen"], "text": text, "banks": banks})
    return out


def _complaint_number(p) -> str:
    return f"Ж-{p['created_at'][:4]}-{p['id']:05d}"


@app.get("/clients/{cid}/police-statement/{pid}", include_in_schema=False)
def police_statement(cid: int, pid: int, s: dict = Depends(session)):
    """Готовый текст заявления в полицию по жалобе: даты, сумма, получатель, что заморожено в цепочке, номер обращения.

    Банк не подаёт заявление за клиента и не обещает исхода: пострадавший проверяет текст, дописывает, как с ним
    связались мошенники, и подаёт сам — по номеру 102, на qamqor.gov.kz или в отделении полиции. Имена других
    людей в цепочке в текст не попадают: их полиция запросит у банка по номеру обращения.
    """
    c = _own_client(s, cid)
    p = conn.execute("SELECT * FROM complaints WHERE id=? AND complainant_id=?", (pid, cid)).fetchone()
    if not p:
        raise HTTPException(404, t("police.none"))
    st = _complaint_state(p)
    tr = st["transfer"]
    who = _client(tr["receiver_id"])
    d = lambda x: datetime.fromisoformat(x).strftime("%d.%m.%Y %H:%M")  # noqa: E731
    hops = conn.execute("SELECT COUNT(*) AS n FROM cases WHERE complaint_id=?", (pid,)).fetchone()["n"]
    banks = sorted({a["bank"] for a in st["asks"]})
    fill = lambda key: f'<span class="fill">[{escape(t(key))}]</span>'  # noqa: E731
    chain = t("police.chain", n=hops, f=_money(st["frozen"]), r=_money(st["returned"])) if hops else t("police.no_chain")
    if banks:
        chain += " " + t("police.banks", b=", ".join(banks))
    body = f"""<h1>{escape(t("police.title"))}</h1>
<div class="muted">{escape(t("police.lead", n=_complaint_number(p), d=d(p["created_at"])))}</div>
<p class="demo" style="margin-top:12px">{escape(t("police.how"))}</p>
<div class="paper">
<p style="text-align:right">{escape(t("police.to"))}<br>{escape(t("police.from"))} {escape(i18n.name(c["id"], c["name"]).split(" (")[0])}<br>
{escape(t("police.phone"))} {fill("police.fill_phone")}</p>
<h2 style="text-align:center">{escape(t("police.head"))}</h2>
<p>{escape(t("police.p1", d=d(tr["created_at"]), m=_money(tr["amount"]), r=i18n.name(who["id"], who["name"]).split(" (")[0]))} {fill("police.fill_what")}</p>
<p>{escape(t("police.p2"))} {fill("police.fill_how")}</p>
<p>{escape(t("police.p3", d=d(p["created_at"]), n=_complaint_number(p)))} {escape(chain)}</p>
<p>{escape(t("police.p4"))}</p>
<p>{escape(t("police.sign"))}</p>
</div>
<button onclick="print()">{escape(t("cert.print"))}</button>
<div class="demo">{escape(t("police.note"))}</div>"""
    return _page(t("police.title"), body)


def _refundable(case) -> int:
    """Сколько денег из цепочки можно вернуть пострадавшему со счёта этого случая прямо сейчас."""
    if case["kind"] != "block" or not case["complaint_id"] or case["status"] not in UNRESOLVED:
        return 0
    p = conn.execute("SELECT * FROM complaints WHERE id=?", (case["complaint_id"],)).fetchone()
    owed = _incoming_row(p["transfer_id"])["amount"] - _returned_sum(p["transfer_id"])
    cid = case["client_id"]
    on_account = _client(cid)["balance"] - _held_sum(cid) - _frozen_sum(cid, except_case=case["id"])
    return max(0, min(case["frozen"], owed, on_account))


def _refund(case, actor: str, employee: str | None = None) -> dict:
    """Возвращает пострадавшему деньги из цепочки, которые остались на счёте. Больше, чем он перевёл, не уйдёт."""
    amount = _refundable(case)
    if not amount:
        raise HTTPException(409, t("refund_none"))
    p = conn.execute("SELECT * FROM complaints WHERE id=?", (case["complaint_id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,?,?)",
        (case["client_id"], p["complainant_id"], amount, _now().isoformat(), "done", p["transfer_id"]))
    _move(case["client_id"], p["complainant_id"], amount)
    conn.execute("UPDATE cases SET frozen = frozen - ? WHERE id=?", (amount, case["id"]))
    log(actor, "refunded", case["id"], cur.lastrowid, amount=amount,
        text=f"Пострадавшему возвращено {_money(amount)}" + (": клиент вернул сам" if actor != "analyst" else ""),
        **({"employee": employee} if employee else {}))
    return {"status": "refunded", "amount": amount}


@app.post("/cases/{case_id}/refund")
def refund_by_client(case_id: int, s: dict = Depends(as_client)):
    """Клиент сам возвращает пострадавшему замороженные деньги, не дожидаясь решения банка."""
    case = _own_case(s, case_id, "block")
    r = _refund(case, f"client:{s['client_id']}")
    if not _refundable(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()):
        _close_approvals(("refund",), case_id, "клиент вернул деньги сам")
    return r


@app.post("/analyst/cases/{case_id}/refund")
def refund_by_analyst(case_id: int, s: dict = Depends(as_analyst)):
    """Сотрудник возвращает пострадавшему деньги, замороженные на счёте из цепочки.

    В банке для этого нужно основание: согласие владельца счёта либо решение суда или органа
    уголовного преследования. От TWO_KEY_REFUND возврат подтверждает второй сотрудник.
    """
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    amount = _refundable(case)
    if not amount:
        raise HTTPException(409, t("refund_none"))
    if amount >= TWO_KEY_REFUND:
        # Крупный возврат один сотрудник не проводит: так деньги нельзя увести «возвратом» на подставной счёт.
        _ask_second_key("refund", case_id, f"Вернуть пострадавшему {_money(amount)}", {}, s, case_id)
        return _analyst_case(case_id)
    _refund(case, "analyst", s["name"])
    if not _refundable(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()):
        _close_approvals(("refund",), case_id, "возвращать уже нечего")
    return _analyst_case(case_id)


@app.post("/cases/{case_id}/insist")
def insist(case_id: int, s: dict = Depends(as_client)):
    """Клиент настаивает на переводе: деньги остаются на паузе, случай уходит аналитику."""
    case = _own_case(s, case_id, "intercept")
    if case["status"] != "open":
        raise HTTPException(409, t("decided"))
    if case["guardian_id"] and case["guardian_state"] in ("notified", "asked"):
        # Перевод уходит сотруднику только после подтверждения доверенного лица.
        if case["guardian_state"] == "notified":
            conn.execute("UPDATE cases SET guardian_state='asked' WHERE id=?", (case_id,))
            log(f"client:{s['client_id']}", "guardian_asked", case_id, case["transfer_id"])
        name = _guardian_name(case["guardian_id"])
        return {"status": "guardian", "guardian": name, "text": t("guardian_wait", name=name)}
    conn.execute("UPDATE cases SET status='under_review' WHERE id=?", (case_id,))
    log(f"client:{s['client_id']}", "insisted", case_id, case["transfer_id"])
    return {"status": "under_review"}


# ---------- Апелляция: клиент заблокирован и объясняет, что произошло ----------

def _minutes_text(minutes: int) -> str:
    if minutes < 60:
        return t("min", m=minutes)
    return t("hour_min", h=minutes // 60, m=minutes % 60) if minutes < 1440 else t("days", d=minutes // 1440)


def _doc_title(kind: str, for_client: bool) -> str:
    if for_client and f"doc.{kind}" in i18n.MSG:
        return t(f"doc.{kind}")
    return DOC_KINDS.get(kind, {}).get("title", kind)


def _documents(case_id: int, for_client: bool = False) -> list[dict]:
    rows = conn.execute("SELECT id, kind, filename, size, uploaded_at FROM documents WHERE case_id=? ORDER BY id",
                        (case_id,)).fetchall()
    return [dict(d) | {"title": _doc_title(d["kind"], for_client)} for d in rows]


def _case_view(case) -> dict:
    inc = conn.execute("SELECT amount, created_at FROM transfers WHERE id=?",
                       (case["incoming_transfer_id"],)).fetchone()
    out = conn.execute("SELECT amount, created_at FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone()
    first = json.loads(case["reasons"])[0].get("code")
    if first == "self_report":
        summary = t("summary_self")
    elif first == "ip_rental":
        summary = t("summary_rental")
    elif first == "network":
        summary = t("summary_network")
    elif not out and inc and any(r.get("code") == "cash_out" for r in json.loads(case["reasons"])):
        cash = next(r for r in json.loads(case["reasons"]) if r.get("code") == "cash_out").get("amount", 0)
        summary = t("summary_cash", inc=_money(inc["amount"]), cash=_money(cash))
    elif out:
        gap = int((datetime.fromisoformat(out["created_at"]) - datetime.fromisoformat(inc["created_at"])).total_seconds() // 60)
        summary = t("summary", inc=_money(inc["amount"]), gap=_minutes_text(gap), out=_money(out["amount"]))
    elif first == "linked" or not inc:
        summary = t("summary_linked")
    else:
        summary = t("summary_holder", inc=_money(inc["amount"]))
    partial = case["scope"] == "partial"
    summary += " " + (t("summary_partial", m=_money(case["frozen"])) if partial else t("summary_full"))
    docs = _documents(case["id"], for_client=True)
    have = {d["kind"] for d in docs}
    refundable = _refundable(case) if case["status"] in ("open", "under_review") else 0
    return {
        "case_id": case["id"],
        "status": case["status"],
        "status_text": t("status." + case["status"]),
        "scope": case["scope"],
        "frozen": case["frozen"] if partial else 0,
        # Клиент может сам вернуть пострадавшему замороженные деньги, не дожидаясь решения банка.
        "refundable": refundable,
        "summary": summary,
        "reasons": i18n.reasons(json.loads(case["reasons"])),
        "explanation": case["explanation"],
        "decision_comment": case["decision_comment"],
        "submitted_at": case["submitted_at"],
        "due_at": case["due_at"],
        "required": [{"kind": k, "title": _doc_title(k, True), "required": v["required"], "uploaded": k in have}
                     for k, v in DOC_KINDS.items()],
        "documents": docs,
        "max_documents": MAX_DOCS,
        "can_submit": case["status"] == "open",
        "certificate": _clearance_view(case["id"]),
    }


@app.get("/clients/{cid}/case")
def client_case(cid: int, s: dict = Depends(session)):
    """Последний случай блокировки клиента: причина простым языком, документы, статус."""
    _own_client(s, cid)
    # Сначала нерешённая блокировка: снятие одной не должно скрывать другую.
    case = conn.execute(
        """SELECT * FROM cases WHERE client_id=? AND kind='block'
           ORDER BY status IN ('open','under_review') DESC, id DESC LIMIT 1""", (cid,)).fetchone()
    if not case:
        raise HTTPException(404, t("no_block"))
    return _case_view(case)


@app.post("/cases/{case_id}/documents")
def upload_document(case_id: int, body: DocumentIn, s: dict = Depends(as_client)):
    """Клиент прикладывает документ к апелляции: только PNG, JPEG или PDF до 5 МБ."""
    case = _own_case(s, case_id, "block")
    if case["status"] != "open":
        raise HTTPException(409, t("docs_sent"))
    if body.kind not in DOC_KINDS:
        raise HTTPException(400, t("doc_kind"))
    if conn.execute("SELECT COUNT(*) AS n FROM documents WHERE case_id=?", (case_id,)).fetchone()["n"] >= MAX_DOCS:
        raise HTTPException(400, t("doc_count", n=MAX_DOCS))
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except Exception:
        raise HTTPException(400, t("doc_broken"))
    if not data or len(data) > MAX_FILE:
        raise HTTPException(400, t("doc_size"))
    kind = next(((ext, media) for sig, ext, media in FILE_TYPES if data.startswith(sig)), None)
    if not kind:
        raise HTTPException(400, t("doc_type"))
    # Имя и расширение задаёт сервер по содержимому файла, а не по тому, что прислал клиент.
    stem = re.sub(r"[^\w\- ]", "_", os.path.splitext(os.path.basename(body.filename))[0])[:60] or "file"
    name = stem + kind[0]
    cur = conn.execute("INSERT INTO documents(case_id,kind,filename,size,media_type,uploaded_at) VALUES(?,?,?,?,?,?)",
                       (case_id, body.kind, name, len(data), kind[1], _now().isoformat()))
    os.makedirs(UPLOADS, exist_ok=True)
    # На диске только зашифрованный файл под номером, без имени и расширения клиента.
    path = os.path.join(UPLOADS, f"{cur.lastrowid}.enc")
    with open(path, "wb") as f:
        f.write(vault.encrypt(data))
    conn.execute("UPDATE documents SET path=? WHERE id=?", (path, cur.lastrowid))
    log(f"client:{s['client_id']}", "document", case_id, None, filename=name, title=DOC_KINDS[body.kind]["title"])
    return {"id": cur.lastrowid, "kind": body.kind, "filename": name, "size": len(data)}


@app.post("/cases/{case_id}/submit")
def submit_appeal(case_id: int, body: AppealIn, s: dict = Depends(as_client)):
    """Клиент отправляет апелляцию: объяснение и обязательные документы."""
    case = _own_case(s, case_id, "block")
    if case["status"] != "open":
        raise HTTPException(409, t("appeal_sent"))
    text = body.explanation.strip()
    if len(text) < 20:
        raise HTTPException(400, t("appeal_short"))
    have = {r["kind"] for r in conn.execute("SELECT kind FROM documents WHERE case_id=?", (case_id,))}
    missing = [_doc_title(k, True) for k, v in DOC_KINDS.items() if v["required"] and k not in have]
    if missing:
        raise HTTPException(400, t("appeal_missing", docs="; ".join(missing)))
    now = _now()
    conn.execute("UPDATE cases SET status='under_review', explanation=?, submitted_at=?, due_at=? WHERE id=?",
                 (text[:2000], now.isoformat(), (now + timedelta(days=APPEAL_SLA_DAYS)).isoformat(), case_id))
    log(f"client:{s['client_id']}", "appeal", case_id, None)
    return _case_view(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, s: dict = Depends(session)):
    """Документ видят только его владелец и сотрудник банка. Отдаётся как вложение."""
    doc = conn.execute(
        "SELECT d.*, k.client_id FROM documents d JOIN cases k ON k.id=d.case_id WHERE d.id=?", (doc_id,)).fetchone()
    if not doc or (not s["analyst"] and doc["client_id"] != s["client"]["client_id"]):
        raise HTTPException(404, t("doc_not_found"))
    if not doc["path"] or not os.path.exists(doc["path"]):
        raise HTTPException(404, t("doc_not_found"))
    try:
        with open(doc["path"], "rb") as f:
            data = vault.decrypt(f.read())
    except vault.InvalidToken:
        # Файл подменён или зашифрован другим ключом: отдавать нечего, но и падать не нужно.
        raise HTTPException(409, t("doc_broken"))
    return RawResponse(data, media_type=doc["media_type"], headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc['filename'])}",
        "Cache-Control": "no-store"})


# ---------- Доверенное лицо: родственник узнаёт об остановленном переводе ----------

def _guardian_of(cid: int):
    """Действующее доверенное лицо клиента. Отключение вступает в силу только через сутки."""
    row = conn.execute("SELECT * FROM guardians WHERE client_id=?", (cid,)).fetchone()
    if row and row["remove_at"] and row["remove_at"] <= _now().isoformat():
        conn.execute("DELETE FROM guardians WHERE client_id=?", (cid,))
        return None
    return row


def _guardian_name(gid: int | None) -> str | None:
    if not gid:
        return None
    row = conn.execute("SELECT name FROM clients WHERE id=?", (gid,)).fetchone()
    return i18n.name(gid, row["name"]) if row else None


def _guardian_candidates(cid: int) -> list[dict]:
    """Люди, с которыми клиент давно обменивается переводами: новый «знакомый» мошенника сюда не попадёт."""
    before = (_now() - timedelta(days=GUARDIAN_KNOWN_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT DISTINCT c.id, c.name FROM transfers t
           JOIN clients c ON c.id = CASE WHEN t.sender_id=? THEN t.receiver_id ELSE t.sender_id END
           WHERE t.status='done' AND (t.sender_id=? OR t.receiver_id=?) AND t.created_at <= ?
             AND c.status='active' AND c.id NOT IN (SELECT client_id FROM afc_list)
           ORDER BY c.id""", (cid, cid, cid, before)).fetchall()
    return [{"id": r["id"], "name": i18n.name(r["id"], r["name"])} for r in rows if r["id"] != cid]


def _guardian_requests(gid: int) -> list[dict]:
    """Остановленные переводы тех, для кого клиент — доверенное лицо."""
    rows = conn.execute(
        """SELECT * FROM cases WHERE guardian_id=? AND kind='intercept' AND status IN ('open','under_review')
           ORDER BY created_at DESC""", (gid,)).fetchall()
    out = []
    for k in rows:
        ward = _client(k["client_id"])
        view = _intercept_view(k)
        out.append({"case_id": k["id"], "ward": i18n.name(ward["id"], ward["name"]), "amount": view["amount"],
                    "receiver": view["receiver"], "reasons": view["reasons"], "kind": view["kind"],
                    "state": k["guardian_state"], "status": k["status"], "created_at": k["created_at"]})
    return out


class GuardianIn(BaseModel):
    guardian_id: int


@app.get("/guardian")
def guardian_settings(s: dict = Depends(as_client)):
    cid = s["client_id"]
    g = _guardian_of(cid)
    return {"guardian": {"id": g["guardian_id"], "name": _guardian_name(g["guardian_id"]),
                         "remove_at": g["remove_at"]} if g else None,
            "candidates": [] if g else _guardian_candidates(cid)}


@app.post("/guardian")
def set_guardian(body: GuardianIn, s: dict = Depends(as_client)):
    cid = s["client_id"]
    if _guardian_of(cid):
        raise HTTPException(409, t("guardian_exists"))
    if body.guardian_id not in {c["id"] for c in _guardian_candidates(cid)}:
        raise HTTPException(400, t("guardian_not_candidate"))
    conn.execute("INSERT OR REPLACE INTO guardians(client_id,guardian_id,added_at,remove_at) VALUES(?,?,?,NULL)",
                 (cid, body.guardian_id, _now().isoformat()))
    log(f"client:{cid}", "guardian_set", None, None, guardian=body.guardian_id)
    return guardian_settings(s)


@app.post("/guardian/remove")
def remove_guardian(s: dict = Depends(as_client)):
    """Отключение через сутки: если мошенник уговорит «убрать контроль», родственник успеет вмешаться."""
    cid = s["client_id"]
    g = _guardian_of(cid)
    if not g:
        raise HTTPException(404, t("guardian_none"))
    if not g["remove_at"]:
        conn.execute("UPDATE guardians SET remove_at=? WHERE client_id=?",
                     ((_now() + timedelta(hours=GUARDIAN_REMOVE_HOURS)).isoformat(), cid))
        log(f"client:{cid}", "guardian_remove", None, None, guardian=g["guardian_id"])
    return guardian_settings(s)


@app.post("/guardian/keep")
def keep_guardian(s: dict = Depends(as_client)):
    cid = s["client_id"]
    if not _guardian_of(cid):
        raise HTTPException(404, t("guardian_none"))
    conn.execute("UPDATE guardians SET remove_at=NULL WHERE client_id=?", (cid,))
    return guardian_settings(s)


def _guarded_case(s: dict, case_id: int):
    case = conn.execute("SELECT * FROM cases WHERE id=? AND kind='intercept'", (case_id,)).fetchone()
    if not case or case["guardian_id"] != s["client_id"] or case["status"] not in ("open", "under_review"):
        raise HTTPException(404, t("guardian_case"))
    return case


@app.post("/guardian/requests/{case_id}/cancel")
def guardian_cancel(case_id: int, s: dict = Depends(as_client)):
    """Доверенное лицо отменяет перевод: деньги остаются у того, кого он защищает."""
    case = _guarded_case(s, case_id)
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
    conn.execute("UPDATE cases SET status='guardian_cancelled', guardian_state='cancelled', decided_at=? WHERE id=?",
                 (_now().isoformat(), case_id))
    log(f"client:{s['client_id']}", "guardian_cancelled", case_id, case["transfer_id"])
    return {"status": "guardian_cancelled"}


@app.post("/guardian/requests/{case_id}/confirm")
def guardian_confirm(case_id: int, s: dict = Depends(as_client)):
    """Подтверждение доверенного лица не проводит перевод само: дальше решает сотрудник банка."""
    case = _guarded_case(s, case_id)
    if case["status"] != "open" or case["guardian_state"] != "asked":
        raise HTTPException(409, t("guardian_not_asked"))
    conn.execute("UPDATE cases SET status='under_review', guardian_state='confirmed' WHERE id=?", (case_id,))
    log(f"client:{s['client_id']}", "guardian_confirmed", case_id, case["transfer_id"])
    return {"status": "under_review"}


# ---------- Справка о снятии ограничений ----------

def _clearance_code() -> str:
    while True:
        code = secrets.token_hex(4).upper()
        if not conn.execute("SELECT 1 FROM clearances WHERE code=?", (code,)).fetchone():
            return code


def _issue_clearance(case):
    """Ограничения сняты: клиенту справка, в Антифрод-центр — сведения о снятии (в демо — запись в истории)."""
    now = _now()
    cid = conn.execute("INSERT INTO clearances(case_id,client_id,code,issued_at) VALUES(?,?,?,?)",
                       (case["id"], case["client_id"], _clearance_code(), now.isoformat())).lastrowid
    log("system", "cleared", case["id"], None,
        text=f"Выдана справка № {_clearance_number(cid, now.isoformat())}; в Антифрод-центр переданы сведения "
             "о снятии ограничений и выводе банка (имитация)")


def _clearance_number(cid: int, issued_at: str) -> str:
    return f"ЧС-{issued_at[:4]}-{cid:05d}"


def _clearance_view(case_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM clearances WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if not row:
        return None
    return {"number": _clearance_number(row["id"], row["issued_at"]), "issued_at": row["issued_at"],
            "valid": row["revoked_at"] is None, "revoked_at": row["revoked_at"], "code": row["code"]}


def _page(title: str, body: str) -> RawResponse:
    html_doc = f"""<!DOCTYPE html><html lang="{i18n.lang.get()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<style>body{{margin:0;background:#fff;color:#1d1d1e;font:16px/1.5 "Roboto Flex",Roboto,"Segoe UI",system-ui,-apple-system,sans-serif}}
main{{max-width:720px;margin:24px auto;background:#fff;border:1px solid #d6eadf;border-radius:16px;padding:28px 24px}}
h1{{font-size:22px;margin:0 0 4px;color:#1d1d1e}} .muted{{color:#587366;font-size:14px}}
.ok{{background:#e6f7f0;color:#00875c;border-radius:12px;padding:12px 14px;font-weight:600}}
.bad{{background:#fdeeee;color:#c62828;border-radius:12px;padding:12px 14px;font-weight:600}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:16px 0}} dt{{color:#587366}} dd{{margin:0}}
.demo{{border:1px dashed #b9dccb;color:#587366;border-radius:10px;padding:8px 12px;font-size:13px;margin-top:20px}}
.paper{{border:1px solid #d6eadf;border-radius:12px;padding:16px 18px;margin:16px 0}} .paper h2{{font-size:18px;margin:14px 0}}
.fill{{background:#fff5df;color:#6b4a00;border-radius:6px;padding:0 4px}}
button{{font:inherit;font-weight:500;border:0;border-radius:12px;padding:12px 16px;background:#00ab75;color:#fff;cursor:pointer}}
@media print{{body{{background:#fff}} main{{margin:0}} button{{display:none}}}}</style></head>
<body><main>{body}</main></body></html>"""
    return RawResponse(html_doc, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/clients/{cid}/certificate", include_in_schema=False)
def certificate(cid: int, request: Request, s: dict = Depends(session)):
    """Справка для клиента: её можно распечатать или показать в другом банке. Подлинность проверяется по коду."""
    c = _own_client(s, cid)
    row = conn.execute("""SELECT * FROM clearances WHERE client_id=? AND revoked_at IS NULL
                          ORDER BY id DESC LIMIT 1""", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, t("no_certificate"))
    case = conn.execute("SELECT * FROM cases WHERE id=?", (row["case_id"],)).fetchone()
    blocked_at = conn.execute("SELECT MIN(at) AS at FROM events WHERE case_id=? AND action='blocked'",
                              (case["id"],)).fetchone()["at"] or case["created_at"]
    number = _clearance_number(row["id"], row["issued_at"])
    verify = str(request.base_url) + f"verify/{row['code']}"
    d = lambda x: ".".join(reversed(x[:10].split("-")))  # noqa: E731   2026-11-13 -> 13.11.2026
    body = f"""<h1>{escape(t("cert.title"))}</h1><div class="muted">№ {escape(number)} · {d(row["issued_at"])}</div>
<dl><dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]))}</dd>
<dt>{escape(t("cert.blocked"))}</dt><dd>{d(blocked_at)}</dd>
<dt>{escape(t("cert.cleared"))}</dt><dd>{d(case["decided_at"] or row["issued_at"])}</dd></dl>
<p>{escape(t("cert.text"))}</p><p>{escape(t("cert.afc"))}</p>
<p class="muted">{escape(t("cert.verify"))}<br><b>{escape(verify)}</b><br>{escape(t("cert.code"))}: <b>{row["code"]}</b></p>
<button onclick="print()">{escape(t("cert.print"))}</button>
<div class="demo">{escape(t("cert.demo"))}</div>"""
    return _page(t("cert.title"), body)


@app.get("/verify/{code}", include_in_schema=False)
def verify_certificate(code: str):
    """Проверка справки без входа: другой банк или работодатель вводит код с документа."""
    code = code.strip().upper()
    ok = bool(re.fullmatch(r"[0-9A-F]{8}", code))
    row = conn.execute("SELECT * FROM clearances WHERE code=?", (code,)).fetchone() if ok else None
    report = conn.execute("SELECT * FROM self_reports WHERE code=?", (code,)).fetchone() if ok and not row else None
    claim = conn.execute("SELECT * FROM account_claims WHERE code=?", (code,)).fetchone() if ok and not (row or report) else None
    if claim:
        # Подтверждение «счёт открыт без моего ведома»: номер, дата и имя, без подробностей.
        c = _client(claim["owner_id"])
        body = f"""<h1>{escape(t("claim.check"))}</h1><p class="ok">{escape(t("claim.valid"))}</p>
<dl><dt>{escape(t("cert.number"))}</dt><dd>{escape(_claim_number(claim))}</dd>
<dt>{escape(t("claim.when"))}</dt><dd>{escape(claim["at"][:16].replace("T", " "))}</dd>
<dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]).split(" (")[0])}</dd></dl>
<div class="demo">{escape(t("cert.demo"))}</div>"""
        return _page(t("claim.check"), body)
    if report:
        # Подтверждение обращения: кто и когда сам сообщил банку. Подробностей страница не показывает.
        c = _client(report["client_id"])
        body = f"""<h1>{escape(t("sr.check"))}</h1><p class="ok">{escape(t("sr.valid"))}</p>
<dl><dt>{escape(t("cert.number"))}</dt><dd>{escape(_self_report_number(report))}</dd>
<dt>{escape(t("sr.at"))}</dt><dd>{escape(report["at"][:16].replace("T", " "))}</dd>
<dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]).split(" (")[0])}</dd></dl>
<div class="demo">{escape(t("cert.demo"))}</div>"""
        return _page(t("sr.check"), body)
    if not row:
        return _page(t("cert.check"), f'<h1>{escape(t("cert.check"))}</h1><p class="bad">{escape(t("cert.unknown"))}</p>'
                                      f'<div class="demo">{escape(t("cert.demo"))}</div>')
    c = _client(row["client_id"])
    short = i18n.name(c["id"], c["name"]).split(" (")[0]          # без лишних подробностей о клиенте
    number = _clearance_number(row["id"], row["issued_at"])
    state = (f'<p class="ok">{escape(t("cert.valid"))}</p>' if row["revoked_at"] is None else
             f'<p class="bad">{escape(t("cert.revoked", d=row["revoked_at"][:10]))}</p>')
    body = f"""<h1>{escape(t("cert.check"))}</h1>{state}
<dl><dt>{escape(t("cert.number"))}</dt><dd>{escape(number)}</dd><dt>{escape(t("cert.issued"))}</dt><dd>{row["issued_at"][:10]}</dd>
<dt>{escape(t("cert.client"))}</dt><dd>{escape(short)}</dd></dl><div class="demo">{escape(t("cert.demo"))}</div>"""
    return _page(t("cert.check"), body)


# ---------- Период охлаждения: крупный первый перевод новому получателю уходит через час ----------

def _needs_cooling(sender_id: int, receiver_id: int, amount: int, now: datetime) -> bool:
    """Новый получатель и крупная сумма. Части за сутки складываются: раздробить сумму не поможет."""
    since = (now - timedelta(hours=COOLING_KNOWN_HOURS)).isoformat()
    if engine._had_contact(conn, sender_id, receiver_id, since):
        return False
    recent = conn.execute("""SELECT COALESCE(SUM(amount),0) AS s FROM transfers WHERE sender_id=? AND receiver_id=?
                             AND status IN ('done','pending') AND created_at >= ?""",
                          (sender_id, receiver_id, since)).fetchone()["s"]
    return recent + amount >= COOLING_MIN


def _settle_due():
    """Переводы, у которых закончился период охлаждения. Если за это время счёт отправителя или получателя
    приостановили или получатель попал в чёрный список, перевод не уходит, деньги остаются у клиента."""
    now = _now().isoformat()
    for tr in conn.execute("SELECT * FROM transfers WHERE status='pending' AND release_at <= ?", (now,)).fetchall():
        sender, receiver = _client(tr["sender_id"]), _client(tr["receiver_id"])
        # Отложенный перевод всегда идёт новому получателю: при любом ограничении по счёту он не уходит.
        stop = ("sender_blocked" if sender["status"] != "active" else
                "receiver_blocked" if receiver["status"] == "blocked" else
                "receiver_black" if engine.afc_status(conn, receiver["id"]) == "black" else None)
        if stop:
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (tr["id"],))
            log("system", "cooling_stopped", None, tr["id"], reason=stop)
        else:
            conn.execute("UPDATE transfers SET status='done' WHERE id=?", (tr["id"],))
            _move(tr["sender_id"], tr["receiver_id"], tr["amount"])
            log("system", "cooling_done", None, tr["id"])


def _cooling_list(where: str, args: tuple) -> list[dict]:
    rows = conn.execute(
        f"""SELECT t.id, t.amount, t.created_at, t.release_at, t.sender_id, s.name AS sender, t.receiver_id, r.name AS receiver
            FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
            WHERE t.status='pending' AND {where} ORDER BY t.release_at""", args).fetchall()
    return [dict(r) | {"sender": i18n.name(r["sender_id"], r["sender"]),
                       "receiver": i18n.name(r["receiver_id"], r["receiver"])} for r in rows]


def _cancel_pending(tr, actor: str):
    conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (tr["id"],))
    log(actor, "cooling_cancelled", None, tr["id"])
    return {"status": "cancelled", "amount": tr["amount"]}


@app.post("/transfers/{tid}/cancel")
def cancel_pending(tid: int, s: dict = Depends(as_client)):
    """Клиент передумал, пока перевод в периоде охлаждения: деньги остаются у него."""
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
    if not tr or tr["sender_id"] != s["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if tr["status"] != "pending":
        raise HTTPException(409, t("cooling_none"))
    return _cancel_pending(tr, f"client:{s['client_id']}")


@app.post("/guardian/transfers/{tid}/cancel")
def guardian_cancel_pending(tid: int, s: dict = Depends(as_client)):
    """Доверенное лицо отменяет крупный перевод близкого человека, пока тот не ушёл."""
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
    g = _guardian_of(tr["sender_id"]) if tr else None
    if not tr or not g or g["guardian_id"] != s["client_id"]:
        raise HTTPException(404, t("transfer_not_found"))
    if tr["status"] != "pending":
        raise HTTPException(409, t("cooling_none"))
    return _cancel_pending(tr, f"client:{s['client_id']}")

# ---------- Снятие наличных по QR-коду банкомата ----------
# Карта не нужна: банкомат показывает QR-код, клиент сканирует его в приложении (в демо — кнопка). Дропперы снимают
# чужие деньги наличными, потому что наличные уже не вернуть. Поэтому деньги незнакомцев за последние 5 суток
# (то же окно, что у предупреждения «Возможно, это чужие деньги» и у движка) по одному QR не выдаются: банкомат
# просит подтвердить снятие по лицу, и банк знает, кто их получил.
# Крупное снятие во время звонка с подозрительного номера откладывается на час: так работает схема «курьер»
# («снимите деньги и передайте нашему сотруднику»). Доверенное лицо видит отложенное снятие и может его отменить.

CASH_STEP = 1000                # банкомат выдаёт купюры: сумма кратна 1 000 ₸
CASH_FOREIGN_HOURS = engine.LOOKBACK_HOURS   # деньги незнакомцев за это время — только с проверкой по лицу
CASH_WAIT_MINUTES = 15          # сколько банкомат ждёт проверки по лицу
CASH_READY_MINUTES = 30         # после задержки снять можно в течение этого времени


class CashIn(BaseModel):
    amount: int = Field(gt=0, le=100_000_000)


def _cash_reserved(cid: int) -> int:
    return conn.execute("""SELECT COALESCE(SUM(amount),0) AS s FROM cash_withdrawals
                           WHERE client_id=? AND status IN ('pending','ready')""", (cid,)).fetchone()["s"]


def _cash_active(cid: int):
    return conn.execute("""SELECT * FROM cash_withdrawals WHERE client_id=? AND status IN ('pending','ready')
                           ORDER BY id DESC""", (cid,)).fetchone()


def _foreign_cash(cid: int, now: datetime) -> tuple[int, list]:
    """Деньги незнакомцев за последние 5 суток, которые клиент ещё не вернул и не снял по лицу: сумма и входящие.

    Как и движок, считаем, что клиент сначала тратит свои деньги, а чужие остаются на счёте. Отметка «Я знаю
    отправителя» снятие по лицу не отменяет: её может нажать и дроппер. Мелкие суммы складываются (касса
    онлайн-казино и «подработка» собирают деньги многих людей по чуть-чуть); меньше WARN_MIN_AMOUNT всего —
    проверки нет. Деньги, которые клиент уже переслал дальше, и замороженные банком на счёте уже не лежат
    или и так недоступны: их не считаем.
    """
    if _client(cid)["kind"] == "ip":
        return 0, []                # выручка ИП от незнакомых покупателей — обычное дело
    since = (now - timedelta(hours=CASH_FOREIGN_HOURS)).isoformat()
    left = {}
    for i in engine._stranger_incomings(conn, cid, now):
        if i["created_at"] >= since:
            rest = i["amount"] - sum(f["amount"] for f in engine.find_forward(conn, cid, i))
            if rest > 0:
                left[i["id"]] = (i, rest)
    if sum(rest for _, rest in left.values()) < WARN_MIN_AMOUNT:
        return 0, []
    incs = [i for i, _ in left.values()]
    used = conn.execute("""SELECT COALESCE(SUM(foreign_amount),0) AS s FROM cash_withdrawals WHERE client_id=?
                           AND status IN ('done','pending','ready') AND created_at >= ?""",
                        (cid, min(i["created_at"] for i in incs))).fetchone()["s"]
    return max(0, sum(rest for _, rest in left.values()) - used - _frozen_sum(cid)), incs


def _cash_view(w) -> dict:
    out = {k: w[k] for k in ("id", "amount", "foreign_amount", "status", "created_at", "release_at", "expires_at")}
    out["return_incoming"] = None
    if w["foreign_amount"] and w["status"] in ("pending", "ready"):
        # Вместо снятия чужие деньги можно вернуть отправителю: предлагаем последний такой входящий.
        _, incs = _foreign_cash(w["client_id"], _now())
        inc = max(incs, key=lambda i: i["created_at"], default=None)
        if inc:
            who = _client(inc["sender_id"])
            out["return_incoming"] = {"id": inc["id"], "amount": inc["amount"], "sender": i18n.name(who["id"], who["name"])}
    return out


def _cash_feed(cid: int) -> list[dict]:
    """Снятия наличных в ленте операций клиента."""
    base = {"cash": True, "status": "done", "return_of": None, "case_id": None, "sender_id": None, "receiver_id": None,
            "warning": False, "returned": False, "complained": False, "refund": False, "can_complain": False,
            "direction": "out"}
    return [base | {"id": f"cash-{w['id']}", "amount": w["amount"], "created_at": w["done_at"], "face": bool(w["face"])}
            for w in conn.execute("""SELECT * FROM cash_withdrawals WHERE client_id=? AND status='done'
                                     ORDER BY id DESC LIMIT 30""", (cid,))]


def _guardian_cash(gid: int) -> list[dict]:
    """Для доверенного лица: близкий человек снимает крупную сумму во время подозрительного звонка."""
    rows = conn.execute(
        """SELECT w.*, c.name FROM cash_withdrawals w JOIN guardians g ON g.client_id=w.client_id
           JOIN clients c ON c.id=w.client_id
           WHERE g.guardian_id=? AND (g.remove_at IS NULL OR g.remove_at > ?)
           AND w.status IN ('pending','ready') AND w.release_at IS NOT NULL ORDER BY w.id""",
        (gid, _now().isoformat())).fetchall()
    return [{"id": r["id"], "ward_id": r["client_id"], "ward": i18n.name(r["client_id"], r["name"]),
             "amount": r["amount"], "status": r["status"], "release_at": r["release_at"]} for r in rows]


def _cash_give(cid: int, amount: int):
    conn.execute("UPDATE clients SET balance = balance - ? WHERE id=?", (amount, cid))
    _trim_safe(cid)


def _cash_cancel(w, actor: str, action: str = "cash_cancelled") -> dict:
    conn.execute("UPDATE cash_withdrawals SET status='cancelled' WHERE id=?", (w["id"],))
    log(actor, action, None, None, client=w["client_id"], amount=w["amount"])
    return {"status": "cancelled", "amount": w["amount"]}


def _stop_cash(cid: int, actor: str = "system", action: str = "cash_stopped"):
    """Снятия, которые ждут у банкомата или отложены, отменяются, резерв освобождается."""
    for w in conn.execute("SELECT * FROM cash_withdrawals WHERE client_id=? AND status IN ('pending','ready')",
                          (cid,)).fetchall():
        _cash_cancel(w, actor, action)


def _release_cash(cid: int, need: int):
    """Безопасная операция (вернуть деньги отправителю, всем отправителям, кредит банку) важнее снятия, которое
    ждёт у банкомата: если на неё не хватает денег из-за резерва под снятие, снятие отменяется."""
    if _spendable_for_return(cid) < need and _cash_reserved(cid):
        _stop_cash(cid, f"client:{cid}", "cash_cancelled")


def _settle_cash():
    """Отложенное снятие становится доступным через час; снятие, которое банкомат не дождался, истекает."""
    now = _now()
    for w in conn.execute("SELECT * FROM cash_withdrawals WHERE status='pending' AND release_at <= ?",
                          (now.isoformat(),)).fetchall():
        if _client(w["client_id"])["status"] != "active":
            _cash_cancel(w, "system", "cash_stopped")
        else:
            until = (datetime.fromisoformat(w["release_at"]) + timedelta(minutes=CASH_READY_MINUTES)).isoformat()
            conn.execute("UPDATE cash_withdrawals SET status='ready', expires_at=? WHERE id=?", (until, w["id"]))
    for w in conn.execute("SELECT * FROM cash_withdrawals WHERE status='ready' AND expires_at <= ?",
                          (now.isoformat(),)).fetchall():
        conn.execute("UPDATE cash_withdrawals SET status='expired' WHERE id=?", (w["id"],))
        log("system", "cash_expired", None, None, client=w["client_id"], amount=w["amount"])


@app.post("/cash/withdraw")
def cash_withdraw(body: CashIn, s: dict = Depends(as_client)):
    """Клиент отсканировал QR-код банкомата. Наличные выдаются сразу, после проверки по лицу (деньги незнакомцев
    за последние сутки) или через час (крупная сумма во время звонка с подозрительного номера)."""
    cid, now = s["client_id"], _now()
    if _client(cid)["status"] == "blocked":
        raise HTTPException(403, t("account_blocked"))
    if body.amount % CASH_STEP:
        raise HTTPException(400, t("cash_step", m=_money(CASH_STEP)))
    if _cash_active(cid):
        raise HTTPException(409, t("cash_active"))
    available = _available(cid)
    if available < body.amount:
        safe = _safe_sum(cid)
        if safe and available + safe >= body.amount:
            # Снять можно было бы только деньги из сейфа. Отказ пишем в журнал: так видно, сколько сейф уберёг.
            log(f"client:{cid}", "safe_refused", None, None, amount=body.amount, cash=True)
            return JSONResponse({"detail": t("cash_no_funds_safe", m=_money(safe))}, status_code=400)
        raise HTTPException(400, t("no_funds_frozen" if _frozen_sum(cid) else "no_funds"))
    foreign, _ = _foreign_cash(cid, now)
    part = max(0, body.amount - max(0, available - foreign))     # сколько чужих денег в этой сумме
    delay = engine.call_active(conn, cid, now) and body.amount >= COOLING_MIN
    if delay:
        release = (now + timedelta(minutes=COOLING_MINUTES)).isoformat()
        wid = conn.execute("""INSERT INTO cash_withdrawals(client_id,amount,foreign_amount,status,created_at,release_at)
                              VALUES(?,?,?,'pending',?,?)""", (cid, body.amount, part, now.isoformat(), release)).lastrowid
        log("system", "cash_delayed", None, None, client=cid, amount=body.amount, release_at=release)
    elif part:
        until = (now + timedelta(minutes=CASH_WAIT_MINUTES)).isoformat()
        wid = conn.execute("""INSERT INTO cash_withdrawals(client_id,amount,foreign_amount,status,created_at,expires_at)
                              VALUES(?,?,?,'ready',?,?)""", (cid, body.amount, part, now.isoformat(), until)).lastrowid
        log("system", "cash_face_asked", None, None, client=cid, amount=body.amount, foreign=part)
    else:
        wid = conn.execute("""INSERT INTO cash_withdrawals(client_id,amount,status,created_at,done_at)
                              VALUES(?,?,'done',?,?)""", (cid, body.amount, now.isoformat(), now.isoformat())).lastrowid
        _cash_give(cid, body.amount)
        log(f"client:{cid}", "cash_done", None, None, amount=body.amount)
    return _cash_view(conn.execute("SELECT * FROM cash_withdrawals WHERE id=?", (wid,)).fetchone())


def _own_cash(s: dict, wid: int):
    w = conn.execute("SELECT * FROM cash_withdrawals WHERE id=?", (wid,)).fetchone()
    if not w or w["client_id"] != s["client_id"]:
        raise HTTPException(404, t("cash_not_found"))
    return w


@app.post("/cash/{wid}/take")
def cash_take(wid: int, s: dict = Depends(as_client)):
    """Банкомат выдаёт наличные: после проверки по лицу (в демо — кнопка) или когда прошёл час задержки."""
    w, cid = _own_cash(s, wid), s["client_id"]
    if w["status"] == "pending":
        raise HTTPException(409, t("cash_wait", t=w["release_at"][11:16]))
    if w["status"] != "ready":
        raise HTTPException(409, t("cash_none"))
    if _client(cid)["status"] == "blocked":
        raise HTTPException(403, t("account_blocked"))
    if _available(cid) < 0:
        # Пока снятие ждало, часть денег заморозили (например, по жалобе): выдать всю сумму уже нельзя.
        raise HTTPException(400, t("no_funds_frozen"))
    face = 1 if w["foreign_amount"] else 0
    conn.execute("UPDATE cash_withdrawals SET status='done', done_at=?, face=? WHERE id=?", (_now().isoformat(), face, wid))
    _cash_give(cid, w["amount"])
    log(f"client:{cid}", "cash_done", None, None, amount=w["amount"], foreign=w["foreign_amount"], face=bool(face))
    return {"status": "done", "amount": w["amount"], "face": bool(face)}


@app.post("/cash/{wid}/cancel")
def cash_cancel(wid: int, s: dict = Depends(as_client)):
    """Клиент передумал снимать: деньги остаются на счёте."""
    w = _own_cash(s, wid)
    if w["status"] not in ("pending", "ready"):
        raise HTTPException(409, t("cash_none"))
    return _cash_cancel(w, f"client:{s['client_id']}")


@app.post("/guardian/cash/{wid}/cancel")
def guardian_cash_cancel(wid: int, s: dict = Depends(as_client)):
    """Доверенное лицо отменяет отложенное снятие близкого человека."""
    w = conn.execute("SELECT * FROM cash_withdrawals WHERE id=?", (wid,)).fetchone()
    g = _guardian_of(w["client_id"]) if w else None
    if not w or not g or g["guardian_id"] != s["client_id"] or not w["release_at"]:
        raise HTTPException(404, t("cash_not_found"))
    if w["status"] not in ("pending", "ready"):
        raise HTTPException(409, t("cash_none"))
    return _cash_cancel(w, f"client:{s['client_id']}")


# ---------- Проверка QR-кода перед оплатой ----------
# Мошенники клеят свой QR-код поверх настоящего на парковках, в кафе и магазинах. Платёжный QR банка выдаётся
# только счёту бизнеса, поэтому до оплаты приложение показывает, кто на самом деле получит деньги, и его риск.
# В демо QR-код — строка вида bccpay://pay?acc=<счёт>&name=<что написано на наклейке>&amount=<сумма>.

QR_PREFIX = "bccpay://pay?"


class QrIn(BaseModel):
    payload: str = Field(max_length=1000)


def _parse_qr(payload: str) -> tuple[int, str, int]:
    if not payload.startswith(QR_PREFIX):
        raise HTTPException(400, t("qr.bad"))
    q = parse_qs(payload[len(QR_PREFIX):])
    acc, amount = (q.get("acc") or [""])[0], (q.get("amount") or ["0"])[0]
    if not acc.isdigit() or not amount.isdigit() or int(amount) > 100_000_000:
        raise HTTPException(400, t("qr.bad"))
    return int(acc), (q.get("name") or [""])[0].strip()[:80], int(amount)


@app.post("/qr/check")
def check_qr(body: QrIn, s: dict = Depends(as_client)):
    """Кто получит деньги по QR-коду. Деньги не уходят: оплата — обычным переводом со всеми проверками."""
    cid = s["client_id"]
    rid, claimed, amount = _parse_qr(body.payload.strip())
    rc = conn.execute("SELECT * FROM clients WHERE id=?", (rid,)).fetchone()
    if not rc:
        raise HTTPException(404, t("qr.unknown"))
    if rid == cid:
        raise HTTPException(400, t("self_transfer"))
    risk = _recipient_risk(cid, rid)
    if rc["kind"] == "person":
        verdict, why = "danger", "qr.person"         # платёжный QR не может вести на карту частного лица
    elif risk["level"] == "red":
        verdict, why = "danger", "qr.red"
    elif rc["kind"] == "external":
        verdict, why = "caution", "qr.external"
    elif risk["level"] == "yellow":
        verdict, why = "caution", "qr.yellow"
    else:
        verdict, why = "ok", "qr.ok"
    conn.execute("INSERT INTO qr_checks(client_id,receiver_id,verdict,at) VALUES(?,?,?,?)",
                 (cid, rid, verdict, _now().isoformat()))
    # Имя частного лица не показываем: достаточно того, что это не магазин и не парковка.
    receiver = (t("qr.who_person") if rc["kind"] == "person" else
                t("qr.who_external", b=rc["bank"] or "") if rc["kind"] == "external" else
                t("qr.who_ip", n=i18n.name(rid, rc["name"]).split(" (")[0], a=i18n.activity(rc["activity"]) or ""))
    return {"verdict": verdict, "title": t(f"qr.{verdict}.title"), "text": t(why, n=claimed or "—"),
            "claimed": claimed, "amount": amount, "receiver": receiver,
            "receiver_id": rid if verdict != "danger" else None, "risk": risk}


# ---------- Тренажёр против мошенников ----------
# Короткие учебные ситуации: звонки, сообщения, предложения «подработки». Клиент выбирает, что сделать, и сразу видит
# объяснение. Банк хранит только результат и номера ситуаций с ошибкой: так видно, каким схемам учить в первую очередь.

class TrainerAnswerIn(BaseModel):
    qid: str = Field(max_length=32)
    choice: str = Field(max_length=8)


class TrainerFinishIn(BaseModel):
    answers: dict[str, str] = Field(max_length=40)


@app.get("/trainer")
def trainer_questions(s: dict = Depends(as_client)):
    last = conn.execute("SELECT score, total, at FROM trainer_results WHERE client_id=? ORDER BY id DESC LIMIT 1",
                        (s["client_id"],)).fetchone()
    return {"questions": trainer.questions(i18n.lang.get()), "last": dict(last) if last else None}


@app.post("/trainer/answer")
def trainer_answer(body: TrainerAnswerIn, s: dict = Depends(as_client)):
    r = trainer.check(body.qid, body.choice, i18n.lang.get())
    if r is None:
        raise HTTPException(400, t("bad_input"))
    return r


@app.post("/trainer/finish")
def trainer_finish(body: TrainerFinishIn, s: dict = Depends(as_client)):
    r = trainer.score(body.answers, i18n.lang.get())
    if r is None:
        raise HTTPException(400, t("bad_input"))
    conn.execute("INSERT INTO trainer_results(client_id,score,total,wrong,at) VALUES(?,?,?,?,?)",
                 (s["client_id"], r["score"], r["total"], json.dumps(r["wrong"]), _now().isoformat()))
    return r


# ---------- «Сейф»: сбережения, которые нельзя перевести через приложение ----------
# Как Money Lock в банках Сингапура. Положить деньги в сейф можно сразу, открыть — только в отделении
# (в демо — кнопка) или через сутки. Доверенное лицо видит запрос и может его отменить: мошенник не уговорит
# «срочно перевести все сбережения» за один звонок.

class SafeIn(BaseModel):
    amount: int = Field(gt=0, le=100_000_000)


class SafeUnlockIn(BaseModel):
    amount: int = Field(gt=0, le=100_000_000)
    way: str = "delay"          # delay: через сутки | branch: в отделении по удостоверению (в демо — сразу)


def _safe_row(cid: int):
    return conn.execute("SELECT * FROM safes WHERE client_id=?", (cid,)).fetchone()


def _safe_view(cid: int) -> dict:
    row = _safe_row(cid)
    pending = row and row["unlock_at"]
    return {"amount": _safe_sum(cid), "unlock_amount": row["unlock_amount"] if pending else None,
            "unlock_at": row["unlock_at"] if pending else None}


def _guardian_safe(gid: int) -> list[dict]:
    """Для доверенного лица: близкий человек попросил открыть сейф."""
    rows = conn.execute(
        """SELECT s.*, c.name FROM safes s JOIN guardians g ON g.client_id=s.client_id JOIN clients c ON c.id=s.client_id
           WHERE g.guardian_id=? AND (g.remove_at IS NULL OR g.remove_at > ?) AND s.unlock_at IS NOT NULL""",
        (gid, _now().isoformat())).fetchall()
    return [{"ward_id": r["client_id"], "ward": i18n.name(r["client_id"], r["name"]), "amount": r["unlock_amount"],
             "unlock_at": r["unlock_at"]} for r in rows]


def _settle_safes():
    """Сутки прошли — деньги из сейфа становятся доступны."""
    now = _now().isoformat()
    for r in conn.execute("SELECT * FROM safes WHERE unlock_at IS NOT NULL AND unlock_at <= ?", (now,)).fetchall():
        conn.execute("""UPDATE safes SET amount = MAX(0, amount - ?), unlock_amount=NULL, unlock_at=NULL,
                        unlock_requested_at=NULL, updated_at=? WHERE client_id=?""", (r["unlock_amount"], now, r["client_id"]))
        log("system", "safe_opened", None, None, client=r["client_id"], amount=r["unlock_amount"])


@app.post("/safe/lock")
def safe_lock(body: SafeIn, s: dict = Depends(as_client)):
    """Положить деньги в сейф: сразу, без ожидания."""
    cid = s["client_id"]
    if _client(cid)["status"] == "blocked":
        raise HTTPException(403, t("account_blocked"))
    if _available(cid) < body.amount:
        raise HTTPException(400, t("safe_no_funds", m=_money(max(0, _available(cid)))))
    now = _now().isoformat()
    conn.execute("""INSERT INTO safes(client_id,amount,updated_at) VALUES(?,?,?)
                    ON CONFLICT(client_id) DO UPDATE SET amount = MIN(amount, ?) + excluded.amount, updated_at=excluded.updated_at""",
                 (cid, body.amount, now, _client(cid)["balance"]))
    log(f"client:{cid}", "safe_locked", None, None, amount=body.amount)
    return _safe_view(cid)


@app.post("/safe/unlock")
def safe_unlock(body: SafeUnlockIn, s: dict = Depends(as_client)):
    """Открыть сейф: в отделении (в демо — сразу) или через сутки. Через сутки — с уведомлением доверенного лица."""
    cid = s["client_id"]
    if body.way not in ("delay", "branch"):
        raise HTTPException(400, t("safe_way"))
    if body.amount > _safe_sum(cid):
        raise HTTPException(400, t("safe_too_much", m=_money(_safe_sum(cid))))
    now = _now()
    if body.way == "branch":
        # Просьба «через сутки» не может быть больше того, что осталось в сейфе; ничего не осталось — просьбы нет.
        conn.execute("""UPDATE safes SET amount = amount - ?, updated_at=?,
                          unlock_amount = CASE WHEN amount - ? > 0 THEN MIN(unlock_amount, amount - ?) END,
                          unlock_at = CASE WHEN amount - ? > 0 THEN unlock_at END,
                          unlock_requested_at = CASE WHEN amount - ? > 0 THEN unlock_requested_at END
                        WHERE client_id=?""",
                     (body.amount, now.isoformat(), body.amount, body.amount, body.amount, body.amount, cid))
        log(f"client:{cid}", "safe_opened", None, None, amount=body.amount, way="branch")
    else:
        at = (now + timedelta(hours=SAFE_UNLOCK_HOURS)).isoformat()
        conn.execute("UPDATE safes SET unlock_amount=?, unlock_at=?, unlock_requested_at=? WHERE client_id=?",
                     (body.amount, at, now.isoformat(), cid))
        log(f"client:{cid}", "safe_unlock_requested", None, None, amount=body.amount, unlock_at=at)
    return _safe_view(cid)


def _safe_keep(cid: int, actor: str) -> dict:
    row = _safe_row(cid)
    if not row or not row["unlock_at"]:
        raise HTTPException(409, t("safe_no_request"))
    conn.execute("UPDATE safes SET unlock_amount=NULL, unlock_at=NULL, unlock_requested_at=NULL WHERE client_id=?", (cid,))
    log(actor, "safe_unlock_cancelled", None, None, client=cid, amount=row["unlock_amount"])
    return _safe_view(cid)


@app.post("/safe/keep")
def safe_keep(s: dict = Depends(as_client)):
    """Клиент передумал открывать сейф."""
    return _safe_keep(s["client_id"], f"client:{s['client_id']}")


@app.post("/guardian/safe/{ward_id}/cancel")
def guardian_safe_cancel(ward_id: int, s: dict = Depends(as_client)):
    """Доверенное лицо отменяет открытие сейфа близкого человека."""
    g = _guardian_of(ward_id)
    if not g or g["guardian_id"] != s["client_id"]:
        raise HTTPException(404, t("guardian_case"))
    return _safe_keep(ward_id, f"client:{s['client_id']}")


# ---------- «Аренда ИП»: счётом предпринимателя пользуется другой человек ----------

def _rental_view(cid: int) -> dict | None:
    """Предупреждение владельцу ИП: деньги многих незнакомых людей сразу уходят частным лицам."""
    sig = engine.rental_signal(conn, cid, _now())
    return {k: sig[k] for k in ("payers", "inflow", "share")} if sig else None


def _casino_view(cid: int) -> dict | None:
    """Предупреждение владельцу карты: через неё, похоже, проходят ставки и выигрыши онлайн-казино."""
    sig = engine.casino_signal(conn, cid, _now())
    return {k: sig[k] for k in ("payers", "payees", "twoway", "inflow")} if sig else None


def _rental_accounts() -> list[dict]:
    """Счета ИП с признаками «аренды»: для вкладки сотрудника банка."""
    now, out = _now(), []
    for c in conn.execute("SELECT * FROM clients WHERE kind='ip' ORDER BY id").fetchall():
        sig = engine.rental_signal(conn, c["id"], now)
        if sig:
            out.append({"client_id": c["id"], "name": c["name"], "activity": c["activity"], "status": c["status"],
                        "opened": c["account_opened_at"]} | sig)
    return sorted(out, key=lambda x: (x["status"] == "blocked", -x["inflow"]))


# ---------- Свежий кредит: схема «встречный кредит» ----------
# Кредит — деньги банка, а не перевод между клиентами: выдача и погашение меняют баланс напрямую
# и в движке не считаются «входящим от незнакомца».

def _loan_view(cid: int) -> dict | None:
    """Свежий непогашенный кредит: клиенту показываем, что его можно вернуть банку."""
    loan = engine.fresh_loan(conn, cid, _now())
    return {"id": loan["id"], "amount": loan["amount"], "issued_at": loan["issued_at"]} if loan else None


def _loan_feed(cid: int) -> list[dict]:
    """Выдача и погашение кредита в ленте операций клиента."""
    out = []
    for ln in conn.execute("SELECT * FROM loans WHERE client_id=?", (cid,)).fetchall():
        base = {"loan": True, "amount": ln["amount"], "status": "done", "return_of": None, "case_id": None,
                "sender_id": None, "receiver_id": None, "warning": False, "returned": False, "complained": False,
                "refund": False, "can_complain": False}
        out.append(base | {"id": f"loan-{ln['id']}", "direction": "in", "created_at": ln["issued_at"]})
        if ln["repaid_at"]:
            out.append(base | {"id": f"loan-{ln['id']}-repaid", "direction": "out", "created_at": ln["repaid_at"]})
    return out


@app.post("/loans/{loan_id}/repay")
def repay_loan(loan_id: int, s: dict = Depends(as_client)):
    """Клиент возвращает кредит банку. Остановленные переводы этих денег отменяются, резерв освобождается."""
    cid = s["client_id"]
    loan = conn.execute("SELECT * FROM loans WHERE id=? AND client_id=?", (loan_id, cid)).fetchone()
    if not loan:
        raise HTTPException(404, t("loan_not_found"))
    if loan["repaid_at"]:
        raise HTTPException(409, t("loan_repaid"))
    if _client(cid)["status"] == "blocked":
        raise HTTPException(403, t("account_blocked"))
    cases = [k for k in conn.execute(
        """SELECT * FROM cases WHERE client_id=? AND kind='intercept' AND status IN ('open','under_review')""",
        (cid,)).fetchall() if any(r.get("code") == "loan_forward" for r in json.loads(k["reasons"]))]
    for case in cases:
        conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (case["transfer_id"],))
        conn.execute("UPDATE cases SET status='withdrawn' WHERE id=?", (case["id"],))
        log(f"client:{cid}", "withdrawn", case["id"], case["transfer_id"])
    _release_cash(cid, loan["amount"])
    available = _spendable_for_return(cid)
    if available < loan["amount"]:
        raise HTTPException(400, t("loan_no_funds", available=_money(available), amount=_money(loan["amount"])))
    conn.execute("UPDATE clients SET balance = balance - ? WHERE id=?", (loan["amount"], cid))
    _trim_safe(cid)
    conn.execute("UPDATE loans SET repaid_at=? WHERE id=?", (_now().isoformat(), loan_id))
    for case in cases or [None]:
        log(f"client:{cid}", "loan_repaid", case["id"] if case else None, None, amount=loan["amount"])
    return {"status": "repaid", "amount": loan["amount"]}


# ---------- Проверка подозрительного сообщения ----------

class MessageIn(BaseModel):
    text: str = Field(max_length=4000)


@app.post("/messages/check")
def check_message(body: MessageIn, s: dict = Depends(as_client)):
    """Клиент вставляет текст из мессенджера. Текст не сохраняется: в базу пишется только найденная схема."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, t("msg.empty"))
    cid, now = s["client_id"], _now()
    r = messages.check(text)
    conn.execute("INSERT INTO message_checks(client_id,scheme,verdict,at) VALUES(?,?,?,?)",
                 (cid, r["scheme"], r["verdict"], now.isoformat()))
    key = r["scheme"] or r["verdict"]
    out = {"verdict": r["verdict"], "scheme": r["scheme"], "title": t(f"msg.{key}.title"), "text": t(f"msg.{key}.text"),
           "advice": [t(f"msg.{key}.a{i}") for i in (1, 2, 3)] if r["scheme"] else [],
           "matched": r["matched"], "flags": [{"code": f, "text": t(f"msg.flag.{f}")} for f in r["flags"]],
           "return_incoming": None}
    if r["scheme"] == "mistaken" and _client(cid)["status"] != "blocked":
        # Если чужие деньги уже на счёте, сразу предлагаем вернуть их отправителю.
        inc = max(engine._stranger_incomings(conn, cid, now), key=lambda i: i["created_at"], default=None)
        if inc:
            who = _client(inc["sender_id"])
            out["return_incoming"] = {"id": inc["id"], "amount": inc["amount"], "sender": i18n.name(who["id"], who["name"])}
    return out


# ---------- Предупреждение о вербовке в дропперы ----------

def _recruit_view(cid: int) -> dict | None:
    if _client(cid)["kind"] == "ip":
        return None                    # для ИП вместо вербовки проверяется «аренда ИП»
    if engine.casino_signal(conn, cid, _now()):
        return None                    # точнее подходит предупреждение о кассе онлайн-казино
    sig = engine.recruitment_signal(conn, cid, _now())
    if not sig:
        return None
    mark = conn.execute("SELECT at FROM recruit_marks WHERE client_id=?", (cid,)).fetchone()
    if mark and mark["at"] >= sig["last_at"]:
        return None                    # клиент уже ответил; новый незнакомец покажет предупреждение снова
    return {"senders": sig["senders"], "amount": sig["amount"], "incoming_ids": sig["incoming_ids"]}


@app.post("/recruit/return-all")
def recruit_return_all(s: dict = Depends(as_client)):
    """Вернуть всем отправителям сразу: так клиент выходит из схемы, не став звеном цепочки."""
    cid = s["client_id"]
    sig = _recruit_view(cid)
    if not sig:
        raise HTTPException(409, t("recruit_none"))
    _release_cash(cid, sig["amount"])
    available = _spendable_for_return(cid)
    if available < sig["amount"]:
        raise HTTPException(400, t("return_no_funds", available=_money(available), amount=_money(sig["amount"])))
    for tid in sig["incoming_ids"]:
        _return_incoming(s, _incoming_row(tid))
    return {"returned": len(sig["incoming_ids"]), "amount": sig["amount"]}


@app.post("/recruit/dismiss")
def recruit_dismiss(s: dict = Depends(as_client)):
    """«Это мои покупатели или знакомые». Ответ сохраняется: его увидит сотрудник, если случай дойдёт до него."""
    cid = s["client_id"]
    sig = _recruit_view(cid)
    if not sig:
        raise HTTPException(409, t("recruit_none"))
    conn.execute("INSERT OR REPLACE INTO recruit_marks(client_id,action,at) VALUES(?,'dismissed',?)",
                 (cid, _now().isoformat()))
    log(f"client:{cid}", "recruit_dismissed", None, None, senders=sig["senders"], amount=sig["amount"])
    return {"ok": True}


# ---------- «Меня втянули в схему»: клиент сам сообщает банку ----------

class SelfReportIn(BaseModel):
    kind: str                       # card | transit | offer
    details: str = Field("", max_length=3000)


def _self_report_number(row) -> str:
    return f"СО-{row['at'][:4]}-{row['id']:05d}"


def _self_report_view(cid: int) -> dict | None:
    row = conn.execute("SELECT * FROM self_reports WHERE client_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "kind": row["kind"], "at": row["at"], "number": _self_report_number(row),
            "code": row["code"]}


@app.post("/self-report")
def self_report(body: SelfReportIn, s: dict = Depends(as_client)):
    """Клиент сам рассказывает банку, что его втянули в схему, пока на него никто не пожаловался.

    Передал карту или доступ: счёт закрывается целиком, потому что им управляет чужой человек.
    Принимал и пересылал: замораживаются только чужие деньги на счёте. Только предложили: ограничений нет.
    Обращение сразу попадает сотруднику банка, клиент получает подтверждение с датой и кодом проверки.
    """
    cid, now = s["client_id"], _now()
    if body.kind not in SELF_REPORT_KINDS:
        raise HTTPException(400, t("bad_input"))
    if _client(cid)["status"] != "active":
        raise HTTPException(409, t("sr.restricted"))
    details = body.details.strip()[:2000]
    if body.kind != "offer" and len(details) < 20:
        raise HTTPException(400, t("sr.short"))
    while True:
        code = secrets.token_hex(4).upper()
        if not conn.execute("SELECT 1 FROM clearances WHERE code=? UNION SELECT 1 FROM self_reports WHERE code=?",
                            (code, code)).fetchone():
            break
    case_id = None
    if body.kind != "offer":
        # Чужие деньги, которые ещё на счёте, и последняя пересылка: по ней сотрудник увидит карту сборщика.
        _stop_cash(cid)                 # резерв под снятие наличных тоже замораживается
        foreign = engine._stranger_incomings(conn, cid, now)
        episode = (engine.forward_episodes(conn, cid, now) or [None])[-1]
        inc_id = episode["incoming_id"] if episode else max((i["id"] for i in foreign), default=None)
        on_account = sum(i["amount"] for i in foreign if not engine.find_forward(conn, cid, i))
        frozen = max(0, min(on_account, _available(cid)))
        scope = "full" if body.kind == "card" else "partial"
        reasons = [{"code": "self_report", "points": 0, "text": "Клиент сам сообщил банку: " + SELF_REPORT_KINDS[body.kind][0].lower() + SELF_REPORT_KINDS[body.kind][1:]}]
        case_id = conn.execute(
            """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,status,explanation,
                                 submitted_at,due_at,created_at,scope,frozen)
               VALUES(?,?,?,'block',0,?,'under_review',?,?,?,?,?,?)""",
            (cid, episode["outgoing_id"] if episode else None, inc_id, json.dumps(reasons, ensure_ascii=False),
             details, now.isoformat(), (now + timedelta(days=APPEAL_SLA_DAYS)).isoformat(), now.isoformat(),
             scope, frozen)).lastrowid
        _refresh_status(cid)
        text = ("Клиент сам сообщил, что передал карту или доступ: счёт приостановлен, карта и доступ "
                "к приложению подлежат перевыпуску (в демо — запись в истории)" if scope == "full" else
                f"Клиент сам сообщил, что принимал и пересылал чужие деньги: заморожено {_money(frozen)}, "
                "остальным клиент пользуется, но только для знакомых получателей")
        log(f"client:{cid}", "self_report", case_id, inc_id, text=text, kind=body.kind, scope=scope)
    rid = conn.execute("INSERT INTO self_reports(client_id,kind,details,case_id,code,at) VALUES(?,?,?,?,?,?)",
                       (cid, body.kind, details or None, case_id, code, now.isoformat())).lastrowid
    if case_id is None:
        log(f"client:{cid}", "self_report", None, None, kind=body.kind)
    return {"report": _self_report_view(cid), "text": t("sr.done." + body.kind), "report_id": rid}


@app.get("/clients/{cid}/self-report", include_in_schema=False)
def self_report_page(cid: int, request: Request, s: dict = Depends(session)):
    """Подтверждение обращения: дата, время и что именно клиент сообщил. Можно приложить к заявлению в полицию."""
    c = _own_client(s, cid)
    row = conn.execute("SELECT * FROM self_reports WHERE client_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, t("sr.none"))
    verify = str(request.base_url) + f"verify/{row['code']}"
    body = f"""<h1>{escape(t("sr.title"))}</h1>
<div class="muted">№ {escape(_self_report_number(row))} · {escape(row["at"][:16].replace("T", " "))}</div>
<dl><dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]))}</dd>
<dt>{escape(t("sr.what"))}</dt><dd>{escape(t("sr.kind." + row["kind"]))}</dd>
<dt>{escape(t("sr.before"))}</dt><dd>{escape(t("sr.before_yes"))}</dd></dl>
{f'<p>{escape(t("sr.words"))} «{escape(row["details"])}»</p>' if row["details"] else ''}
<p>{escape(t("sr.text"))}</p><p>{escape(t("sr.law"))}</p>
<p class="muted">{escape(t("sr.verify"))}<br><b>{escape(verify)}</b><br>{escape(t("cert.code"))}: <b>{row["code"]}</b></p>
<button onclick="print()">{escape(t("cert.print"))}</button>
<div class="demo">{escape(t("cert.demo"))}</div>"""
    return _page(t("sr.title"), body)


# ---------- Панель аналитика ----------

@app.get("/analyst", include_in_schema=False)
def analyst_app():
    return FileResponse(os.path.join(STATIC, "analyst.html"))


def _transfer_view(tid: int | None) -> dict | None:
    if tid is None:
        return None
    t = conn.execute(
        """SELECT t.id, t.amount, t.created_at, t.status, s.name AS sender, r.name AS receiver,
                  r.account_opened_at AS receiver_opened
           FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
           WHERE t.id=?""", (tid,)).fetchone()
    return dict(t)


def _pending(case) -> bool:
    return case["status"] == "under_review" or (case["kind"] == "block" and case["status"] == "open")


def _analyst_row(case) -> dict:
    client = _client(case["client_id"])
    return {
        "case_id": case["id"], "kind": case["kind"], "status": case["status"],
        "status_text": "Ждёт подтверждения доверенного лица"
                       if case["status"] == "open" and case["guardian_state"] == "asked" else
                       ANALYST_STATUS.get((case["kind"], case["status"]), STATUS_TEXT.get(case["status"], case["status"])),
        "client_id": client["id"], "client": client["name"], "score": case["score"],
        "created_at": case["created_at"],
        "role": engine.assess_role(conn, case["client_id"], case, _now()),
        "needs_decision": _pending(case),
        "due_at": case["due_at"],
        "overdue": bool(_pending(case) and case["due_at"] and case["due_at"] < _now().isoformat()),
        "from_complaint": case["complaint_id"] is not None,
        "hop": case["hop"],
        "scope": case["scope"] if case["kind"] == "block" else None,
        "frozen": case["frozen"],
        "self_reported": json.loads(case["reasons"])[0].get("code") == "self_report",
        "second_key": bool(_pending_approval("refund", case["id"]) or _pending_approval("release", case["id"])),
    }


@app.get("/analyst/cases")
def analyst_queue(s: dict = Depends(as_analyst)):
    """Очередь: сначала случаи, которые ждут решения."""
    rows = [_analyst_row(c) for c in conn.execute("SELECT * FROM cases ORDER BY created_at DESC, id DESC").fetchall()]
    order = {"under_review": 0, "open": 1}
    # Сначала просроченные, затем ждущие решения по ближайшему сроку.
    return sorted(rows, key=lambda r: (not r["needs_decision"], not r["overdue"], order.get(r["status"], 2),
                                       r["due_at"] or "9999"))


def _chain(case) -> list[dict]:
    """Все счета, приостановленные по той же жалобе: сотрудник видит цепочку целиком."""
    if case["complaint_id"] is None:
        return []
    rows = conn.execute(
        """SELECT k.id, k.hop, k.status, k.transfer_id, k.scope, k.frozen, c.name, i.amount FROM cases k
           JOIN clients c ON c.id=k.client_id JOIN transfers i ON i.id=k.incoming_transfer_id
           WHERE k.complaint_id=? ORDER BY k.hop, k.id""", (case["complaint_id"],)).fetchall()
    victim = conn.execute(
        """SELECT c.name, t.amount FROM complaints p JOIN transfers t ON t.id=p.transfer_id
           JOIN clients c ON c.id=p.complainant_id WHERE p.id=?""", (case["complaint_id"],)).fetchone()
    # Звенья в другом банке: приостановить их мы не можем, туда ушёл запрос через Антифрод-центр.
    asks = conn.execute("""SELECT i.*, c.name FROM interbank i JOIN transfers t ON t.id=i.transfer_id
                           JOIN clients c ON c.id=t.receiver_id WHERE i.complaint_id=? AND i.direction='out'""",
                        (case["complaint_id"],)).fetchall()
    return [{"case_id": None, "hop": 0, "name": victim["name"], "amount": victim["amount"], "role": "victim",
             "status_text": "Пожаловался на мошенничество"}] + [
        {"case_id": r["id"], "hop": r["hop"], "name": r["name"], "amount": r["amount"],
         "role": "forwarded" if r["transfer_id"] else "holder", "current": r["id"] == case["id"],
         "scope": r["scope"], "frozen": r["frozen"] if r["status"] in UNRESOLVED else 0,
         "status_text": ANALYST_STATUS.get(("block", r["status"]), STATUS_TEXT.get(r["status"], r["status"]))}
        for r in rows] + [
        {"case_id": None, "hop": None, "name": a["name"], "amount": a["amount"], "role": "external", "bank": a["bank"],
         "frozen": a["frozen"] - a["returned"] if a["status"] == "frozen" else 0,
         "status_text": f"{a['bank']}: {INTERBANK_STATUS[a['status']].lower()}"}
        for a in asks]


def _history(case_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM events WHERE case_id=? ORDER BY at, id", (case_id,)).fetchall()
    out = []
    for e in rows:
        details = json.loads(e["details"]) if e["details"] else {}
        who = {"system": "Система", "analyst": "Сотрудник банка"}.get(
            e["actor"], e["actor"][5:] if e["actor"].startswith("bank:") else "Клиент")
        if e["actor"] == "analyst" and details.get("employee"):
            who = f"Сотрудник банка ({details['employee']})"
        out.append({"at": e["at"], "who": who, "action": e["action"],
                    "text": details.get("text") or EVENT_TEXT.get(e["action"], e["action"]), "details": details})
    return out


def _analyst_case(case_id: int) -> dict:
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    client = _client(case["client_id"])
    linked = _linked_for(case)
    rental = engine.rental_signal(conn, client["id"], _now()) if client["kind"] == "ip" else None
    return _analyst_row(case) | {
        "account_opened_at": client["account_opened_at"],
        "client_status": client["status"],
        "client_kind": client["kind"], "activity": client["activity"],
        "rental": rental,
        "casino": engine.casino_signal(conn, client["id"], _now()),
        "approval": _approval_view(_pending_approval("refund", case_id) or _pending_approval("release", case_id)),
        "pii": _pii_masked(client["id"]),
        "pii_log": _pii_log(client["id"]),
        "identity": _identity_view(client),
        "network": next(({"key": n["key"], "accounts": len(n["members"]), "farms": len(n["farms"])}
                         for n in metrics.networks(conn, _now()) if client["id"] in n["members"]), None),
        "graph": _graph(case, linked, rental),
        "incoming": _transfer_view(case["incoming_transfer_id"]),
        "outgoing": _transfer_view(case["transfer_id"]),
        "reasons": json.loads(case["reasons"]),
        "explanation": case["explanation"],
        "documents": _documents(case_id),
        "decision_comment": case["decision_comment"],
        "decided_at": case["decided_at"],
        "submitted_at": case["submitted_at"],
        "history": _history(case_id),
        "chain": _chain(case),
        "linked": linked,
        "guardian": {"name": _guardian_name(case["guardian_id"]), "state": case["guardian_state"]}
                    if case["guardian_id"] else None,
        "certificate": _clearance_view(case_id),
        "recruit_warned": bool(conn.execute(
            "SELECT 1 FROM recruit_marks WHERE client_id=? AND at <= ?", (case["client_id"], case["created_at"])).fetchone()),
        "refundable": _refundable(case),
        "complaint": _analyst_complaint(case),
        "self_report": _analyst_self_report(case),
        "cash": _analyst_cash(case),
        "can_escalate": case["kind"] == "block" and case["scope"] == "partial" and case["status"] in UNRESOLVED,
    }


def _analyst_cash(case) -> list[dict]:
    """Снятия наличных клиента за неделю до случая и после: дропперы снимают чужие деньги, чтобы их не вернули.
    Снятие денег незнакомца подтверждено по лицу — банк знает, кто их получил."""
    since = (datetime.fromisoformat(case["created_at"]) - timedelta(days=7)).isoformat()
    rows = conn.execute("""SELECT * FROM cash_withdrawals WHERE client_id=? AND created_at >= ?
                           AND (status='done' OR foreign_amount > 0 OR release_at IS NOT NULL) ORDER BY id""",
                        (case["client_id"], since)).fetchall()
    return [{"amount": w["amount"], "foreign_amount": w["foreign_amount"], "status": w["status"], "face": bool(w["face"]),
             "delayed": bool(w["release_at"]), "at": w["done_at"] or w["created_at"]} for w in rows]


def _analyst_complaint(case) -> dict | None:
    if case["complaint_id"] is None:
        return None
    st = _complaint_state(conn.execute("SELECT * FROM complaints WHERE id=?", (case["complaint_id"],)).fetchone())
    return {k: st[k] for k in ("amount", "returned", "frozen", "stage")}


def _analyst_self_report(case) -> dict | None:
    row = conn.execute("SELECT * FROM self_reports WHERE case_id=?", (case["id"],)).fetchone()
    if not row:
        return None
    return {"kind": row["kind"], "kind_text": SELF_REPORT_KINDS[row["kind"]], "details": row["details"],
            "number": _self_report_number(row), "at": row["at"], "code": row["code"]}


GRAPH_MAX_COLUMN = 8            # больше счетов в одном столбце графа сворачиваются в «ещё N счетов»


def _graph(case, linked: dict | None, rental: dict | None) -> dict:
    """Граф денег вокруг случая: счета и переводы между ними, чтобы сотрудник видел схему целиком.

    Берутся: переводы самого случая, вся цепочка по той же жалобе, все пересылки чужих денег этим клиентом
    за 90 дней, переводы на ту же «связанную» карту из других случаев и, у ИП с признаками аренды, транзит за неделю.
    Возвраты не рисуются отдельной стрелкой: сумма возврата подписывается у исходного перевода.
    """
    cid, now = case["client_id"], _now()
    tids = {case["incoming_transfer_id"], case["transfer_id"]}
    if case["complaint_id"] is not None:
        for k in conn.execute("SELECT incoming_transfer_id, transfer_id FROM cases WHERE complaint_id=?",
                              (case["complaint_id"],)):
            tids |= {k["incoming_transfer_id"], k["transfer_id"]}
    for e in engine.forward_episodes(conn, cid, now):
        tids |= {e["incoming_id"], e["outgoing_id"]}
    if linked:
        # Из других случаев с той же картой — только сам перевод на неё, без их предыстории: граф остаётся читаемым.
        for k in conn.execute(f"""SELECT incoming_transfer_id, transfer_id FROM cases
                                  WHERE id IN ({",".join("?" * len(linked["cases"]))})""", linked["cases"]):
            tids.add(k["transfer_id"] or k["incoming_transfer_id"])
    if rental:
        since = (now - timedelta(days=engine.RENT_DAYS)).isoformat()
        tids |= {r["id"] for r in conn.execute(
            """SELECT t.id FROM transfers t JOIN clients a ON a.id=t.sender_id JOIN clients b ON b.id=t.receiver_id
               WHERE (t.receiver_id=? AND a.kind='person' OR t.sender_id=? AND b.kind='person')
                 AND t.created_at >= ? AND t.return_of IS NULL""", (cid, cid, since))}
    tids.discard(None)
    rows = conn.execute(f"""SELECT * FROM transfers WHERE id IN ({",".join("?" * len(tids))}) AND return_of IS NULL
                            ORDER BY created_at""", list(tids)).fetchall() if tids else []
    return _graph_build(rows, {cid}, cid)


def _plural(n: int, one: str, few: str, many: str) -> str:
    return one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _graph_build(rows, ids: set, current: int | None, tags: dict | None = None, phones: list | None = None) -> dict:
    """Счета и стрелки графа по списку переводов: столбцы, роли, свёрнутые лишние счета.

    `ids` — счета, которые рисуются, даже если переводов у них нет; `current` — клиент случая (у графа сети его нет);
    `tags` — короткие пометки у счетов (например, «телефон 1»), такие счета не сворачиваются; `phones` — дроп-телефоны
    сети: телефон рисуется отдельным узлом, пунктир ведёт к счетам, в которые с него входят.
    """
    now, tags = _now(), tags or {}
    # Переводы между одной парой счетов складываются в одну стрелку; проведённые и остановленные — отдельно.
    edges: dict[tuple, dict] = {}
    for p in phones or []:
        for a in p["accounts"]:
            edges[(f"phone-{p['n']}", a["id"], "device")] = {
                "from": f"phone-{p['n']}", "to": a["id"], "amount": 0, "count": 0, "returned": 0,
                "status": "device", "last_at": ""}
    for t in rows:
        moved = t["status"] == "done"
        a, b = t["sender_id"], t["receiver_id"]
        e = edges.setdefault((a, b, moved), {
            "from": a, "to": b, "amount": 0, "count": 0, "returned": 0,
            "status": "done" if moved else t["status"], "last_at": t["created_at"]})
        e["amount"] += t["amount"]
        e["count"] += 1
        e["returned"] += _returned_sum(t["id"]) if moved else 0
        e["last_at"] = t["created_at"]
        if not moved and t["status"] in ("held", "pending"):
            e["status"] = t["status"]
    ids = set(ids) | {x for e in edges.values() for x in (e["from"], e["to"])}

    # Столбцы: счёт стоит как можно правее, сразу перед теми, кому переводил (самый длинный путь до конца цепочки).
    # Так стрелки не перескакивают через столбцы и не идут сквозь другие счета. Стрелка, которая замыкает круг
    # (деньги вернулись туда, откуда пришли), в расчёт не берётся, иначе граф растягивается на десятки столбцов;
    # сначала учитываются крупные переводы.
    succ: dict = {i: set() for i in ids}

    def reaches(a, b) -> bool:
        stack, seen = [a], set()
        while stack:
            x = stack.pop()
            if x == b:
                return True
            if x not in seen:
                seen.add(x)
                stack.extend(succ[x])
        return False

    for e in sorted(edges.values(), key=lambda e: (e["status"] != "device", -e["amount"])):
        if e["from"] != e["to"] and not reaches(e["to"], e["from"]):
            succ[e["from"]].add(e["to"])
    rest: dict = {}
    for i in sorted(ids, key=str):
        stack = [i]
        while stack:            # самый длинный путь вперёд, без рекурсии
            x = stack[-1]
            todo = [y for y in succ[x] if y not in rest]
            if todo:
                stack.extend(todo)
                continue
            stack.pop()
            rest[x] = max((rest[y] + 1 for y in succ[x]), default=0)
    last = max(rest.values())
    col = {i: last - rest[i] for i in ids}

    # Пострадавшие — те, кто пожаловался на один из этих переводов.
    victims = {r["complainant_id"] for r in conn.execute(
        f"""SELECT complainant_id FROM complaints WHERE transfer_id IN ({",".join("?" * len(rows))})""",
        [t["id"] for t in rows])} if rows else set()
    nodes = {}
    for p in phones or []:      # дроп-телефон
        n = len(p["accounts"])
        nodes[f"phone-{p['n']}"] = {
            "id": f"phone-{p['n']}", "name": f"Телефон {p['n']}", "model": p["model"],
            "accounts": n, "status": "active", "kind": "device", "bank": None, "request": None, "role": "device",
            "afc": None, "age_days": None, "case_id": None, "frozen": 0, "tag": None, "col": col[f"phone-{p['n']}"],
            "weight": 10 ** 12}
    for i in ids:
        if i in nodes:
            continue
        c = _client(i)
        k = conn.execute("""SELECT id, kind, status, scope, frozen FROM cases WHERE client_id=?
                            ORDER BY (status IN ('open','under_review','kept')) DESC, created_at DESC LIMIT 1""",
                         (i,)).fetchone()
        role = "current" if i == current else "victim" if i in victims else "account"
        # Счёт в другом банке: что ответил тот банк на наш запрос.
        ask = conn.execute("""SELECT i.status, i.frozen FROM interbank i JOIN transfers t ON t.id=i.transfer_id
                              WHERE t.receiver_id=? AND i.direction='out' ORDER BY i.id DESC LIMIT 1""",
                           (i,)).fetchone() if c["kind"] == "external" else None
        nodes[i] = {"id": i, "name": c["name"], "status": c["status"], "kind": c["kind"], "role": role,
                    "bank": c["bank"], "request": dict(ask) if ask else None,
                    "afc": engine.afc_status(conn, i), "age_days": (now - datetime.fromisoformat(c["account_opened_at"])).days,
                    "case_id": k["id"] if k else None,
                    "frozen": k["frozen"] if k and k["kind"] == "block" and k["status"] in UNRESOLVED else 0,
                    "tag": tags.get(i), "col": col[i],
                    "weight": sum(e["amount"] for e in edges.values() if i in (e["from"], e["to"]))}

    # Слишком много счетов в одном столбце (например, плательщики арендованного ИП) — лишние сворачиваются.
    for c_ in sorted(set(col.values())):
        members = sorted((n for n in nodes.values() if n["col"] == c_ and n["role"] == "account" and not n["tag"]),
                         key=lambda n: -n["weight"])
        others = sum(1 for n in nodes.values() if n["col"] == c_) - len(members)   # клиент случая, пострадавший
        if others + len(members) <= GRAPH_MAX_COLUMN:
            continue
        extra = members[max(0, GRAPH_MAX_COLUMN - 1 - others):]   # одно место — под «ещё N счетов»
        gid = f"more-{c_}"
        n = len(extra)
        nodes[gid] = {"id": gid, "name": f"ещё {n} {_plural(n, 'счёт', 'счёта', 'счетов')}", "status": "active", "kind": "group",
                      "bank": None, "request": None,
                      "role": "group", "afc": None, "age_days": None, "case_id": None, "frozen": 0, "tag": None, "col": c_,
                      "weight": sum(n["weight"] for n in extra)}
        hidden = {n["id"] for n in extra}
        merged: dict[tuple, dict] = {}
        for key, e in edges.items():
            a = gid if e["from"] in hidden else e["from"]
            b = gid if e["to"] in hidden else e["to"]
            m = merged.setdefault((a, b, key[2]), {**e, "from": a, "to": b, "amount": 0, "count": 0, "returned": 0})
            for f in ("amount", "count", "returned"):
                m[f] += e[f]
            m["last_at"] = max(m["last_at"], e["last_at"])
        edges = merged
        for h in hidden:
            nodes.pop(h)
    order = sorted(nodes.values(), key=lambda n: (n["col"], n["role"] != "victim", n["role"] == "group", -n["weight"]))
    return {"nodes": [{k: v for k, v in n.items() if k != "weight"} for n in order], "edges": list(edges.values())}


@app.get("/analyst/cases/{case_id}")
def analyst_case(case_id: int, s: dict = Depends(as_analyst)):
    """Карточка случая: цепочка переводов, признаки, подсказка о роли клиента, апелляция."""
    return _analyst_case(case_id)


@app.post("/analyst/cases/{case_id}/decision")
def decide(case_id: int, body: DecisionIn, s: dict = Depends(as_analyst)):
    """Решение аналитика. Блокировка: снять или оставить. Перевод на паузе: разрешить или отменить."""
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "Решение должно быть approve или reject")
    if not _pending(case):
        raise HTTPException(409, "По этому случаю решение уже принято")
    comment = body.comment.strip()[:1000]
    if body.decision == "reject" and len(comment) < 10:
        raise HTTPException(400, "При отказе напишите клиенту причину")
    if body.decision == "approve" and engine.assess_role(conn, case["client_id"], case, _now())["verdict"] == "organizer":
        # Отпустить деньги того, кто по правилам похож на организатора, один сотрудник не может.
        what = "Снять ограничения" if case["kind"] == "block" else "Разрешить перевод"
        _ask_second_key("release", case_id, f"{what}, хотя клиент по правилам похож на организатора",
                        {"comment": comment}, s, case_id)
        return _analyst_case(case_id)
    _decide(case, body.decision, comment, s["name"])
    return _analyst_case(case_id)


def _decide(case, decision: str, comment: str, employee: str):
    """Само решение: снять или оставить ограничения, разрешить или отменить перевод."""
    case_id = case["id"]
    if case["kind"] == "block":
        status = "unblocked" if decision == "approve" else "kept"
    else:
        tr = conn.execute("SELECT * FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone()
        if decision == "approve":
            if _client(tr["sender_id"])["status"] != "active":
                raise HTTPException(409, "Счёт клиента приостановлен: перевод можно только отменить. "
                                         "Сначала разберите блокировку")
            if engine.afc_status(conn, tr["receiver_id"]) == "black" or _client(tr["receiver_id"])["status"] == "blocked":
                # Пока перевод стоял на паузе, получатель мог попасть в базу мошенников или под блокировку.
                raise HTTPException(409, "Получатель в чёрном списке Антифрод-центра или его счёт приостановлен: "
                                         "перевод можно только отменить")
            # Деньги под этот перевод были зарезервированы, пока он стоял на паузе.
            conn.execute("UPDATE transfers SET status='done' WHERE id=?", (tr["id"],))
            _move(tr["sender_id"], tr["receiver_id"], tr["amount"])
            status = "released"
        else:
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (tr["id"],))
            status = "cancelled"
    conn.execute("UPDATE cases SET status=?, decision_comment=?, decided_at=? WHERE id=?",
                 (status, comment or None, _now().isoformat(), case_id))
    # Блокировка, которую сотрудник уже оставил в силе, тоже держит счёт: одобрение другой её не снимает.
    cleared = case["kind"] == "block" and _refresh_status(case["client_id"]) == "active" and status == "unblocked"
    # Вместе с решением сохраняется подсказка, которую видел сотрудник: её потом не пересчитать задним числом.
    role = engine.assess_role(conn, case["client_id"], case, _now())
    log("analyst", "decision", case_id, case["transfer_id"], text=f"Решение: {STATUS_TEXT[status]}",
        status=status, comment=comment or None, role=role["label"], role_score=role["score"], employee=employee)
    if cleared:
        _issue_clearance(case)
    _close_approvals(("release",), case_id, "по случаю уже принято решение")
    if not _refundable(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()):
        _close_approvals(("refund",), case_id, "возвращать уже нечего")


@app.get("/quality", include_in_schema=False)
def quality_page():
    """Страница качества движка: цифры по нескольким наборам синтетических данных (python -m app.quality)."""
    return FileResponse(os.path.join(STATIC, "quality.html"))


@app.get("/quality.json", include_in_schema=False)
def quality_data():
    path = os.path.join(STATIC, "quality.json")
    if not os.path.exists(path):
        raise HTTPException(404, "Запустите python -m app.quality")
    return FileResponse(path, media_type="application/json")


# ---------- Эффективность, связи между случаями, выгрузка для Антифрод-центра ----------

@app.get("/analyst/stats", include_in_schema=False)
def stats_page():
    return FileResponse(os.path.join(STATIC, "stats.html"))


@app.get("/analyst/metrics")
def analyst_metrics(s: dict = Depends(as_analyst)):
    """Показатели за 30 дней: деньги, нагрузка на сотрудников, скорость апелляций, ошибки системы."""
    row = conn.execute("SELECT value FROM meta WHERE key='history'").fetchone()
    return metrics.compute(conn, _now()) | {"simulated_history": bool(row and row["value"] == "1")}


@app.get("/analyst/linked")
def analyst_linked(s: dict = Depends(as_analyst)):
    """Счета, связанные с несколькими независимыми случаями."""
    return metrics.linked_accounts(conn)


def _linked_for(case) -> dict | None:
    """Куда шли деньги в этом случае и встречался ли этот счёт в других случаях."""
    dest = conn.execute("SELECT receiver_id FROM transfers WHERE id=?", (case["transfer_id"],)).fetchone() \
        if case["transfer_id"] else {"receiver_id": case["client_id"]}
    found = next((a for a in metrics.linked_accounts(conn) if a["client_id"] == dest["receiver_id"]), None)
    if not found:
        return None
    others = [c for c in found["cases"] if c != case["id"]]
    rows = conn.execute(f"""SELECT k.id, k.kind, k.status, c.name FROM cases k JOIN clients c ON c.id=k.client_id
                            WHERE k.id IN ({",".join("?" * len(others))}) ORDER BY k.created_at DESC""",
                        others).fetchall() if others else []
    return found | {"is_client": dest["receiver_id"] == case["client_id"],
                    "other_cases": [dict(r) | {"kind_text": "Блокировка" if r["kind"] == "block" else "Перевод на паузе"}
                                    for r in rows]}


def _suspend(cid: int, reason: dict, text: str) -> dict:
    """Сотрудник приостанавливает счёт целиком: создаётся случай блокировки, его видит и клиент."""
    if conn.execute("""SELECT 1 FROM cases WHERE client_id=? AND kind='block'
                       AND status IN ('open','under_review')""", (cid,)).fetchone():
        raise HTTPException(409, "По этому счёту уже есть нерешённая блокировка")
    return _analyst_case(_suspend_case(cid, reason, text))


def _suspend_case(cid: int, reason: dict, text: str) -> int:
    inc = conn.execute("SELECT id FROM transfers WHERE receiver_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                       (cid,)).fetchone()
    case_id = conn.execute(
        """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,created_at)
           VALUES(?,NULL,?,'block',?,?,?)""",
        (cid, inc["id"] if inc else None, reason["points"], json.dumps([reason], ensure_ascii=False),
         _now().isoformat())).lastrowid
    _refresh_status(cid)
    log("analyst", "blocked", case_id, inc["id"] if inc else None, text=text)
    return case_id


@app.post("/analyst/accounts/{cid}/suspend")
def suspend_account(cid: int, s: dict = Depends(as_analyst)):
    """Приостановить счёт, который всплывает в нескольких случаях."""
    found = next((a for a in metrics.linked_accounts(conn) if a["client_id"] == cid), None)
    if not found:
        raise HTTPException(404, "Счёт не связан с несколькими случаями")
    return _suspend(cid, {"code": "linked", "points": 40,
                          "text": f"Счёт получал деньги от {found['sources']} разных клиентов в подозрительных случаях"},
                    f"Операции приостановлены сотрудником: счёт связан с {len(found['cases'])} случаями")


@app.get("/analyst/business")
def analyst_business(s: dict = Depends(as_analyst)):
    """Счета ИП с признаками «аренды ИП»."""
    return _rental_accounts()


@app.post("/analyst/business/{cid}/suspend")
def suspend_rental(cid: int, s: dict = Depends(as_analyst)):
    """Приостановить счёт ИП, похожий на арендованный."""
    sig = engine.rental_signal(conn, cid, _now())
    if not sig:
        raise HTTPException(404, "Признаков «аренды ИП» по этому счёту нет")
    return _suspend(cid, {"code": "ip_rental", "points": 60,
                          "text": f"Похоже на «аренду ИП»: за неделю деньги от {sig['payers']} незнакомых людей, "
                                  f"{sig['share']}% ушло частным лицам"},
                    "Операции приостановлены сотрудником: признаки «аренды ИП»")


# ---------- Правило двух ключей ----------
# Крупный возврат пострадавшему, приостановка целой сети и решение в пользу «похожего на организатора» один
# сотрудник не проводит: первый просит, второй (другой человек) подтверждает. Так сотрудник-сообщник не уведёт
# деньги и не отпустит организатора в одиночку.

def _pending_approval(kind: str, target):
    return conn.execute("SELECT * FROM approvals WHERE kind=? AND target=? AND status='pending'",
                        (kind, str(target))).fetchone()


def _approval_view(a) -> dict | None:
    if not a:
        return None
    return {k: a[k] for k in ("id", "kind", "target", "summary", "requested_by", "requested_at", "status",
                              "decided_by", "decided_at")}


def _ask_second_key(kind: str, target, summary: str, params: dict, s: dict, case_id: int | None = None) -> int:
    if _pending_approval(kind, target):
        raise HTTPException(409, "Это действие уже ждёт подтверждения второго сотрудника")
    aid = conn.execute(
        "INSERT INTO approvals(kind,target,summary,params,requested_by,requested_at) VALUES(?,?,?,?,?,?)",
        (kind, str(target), summary, json.dumps(params, ensure_ascii=False), s["name"], _now().isoformat())).lastrowid
    log("analyst", "approval_requested", case_id, None, text=f"{summary}: ждёт подтверждения второго сотрудника",
        employee=s["name"])
    return aid


def _close_approvals(kinds: tuple, target, why: str):
    """Просьба больше неактуальна (решение уже принято, возвращать нечего): закрываем её, чтобы не висела."""
    for a in conn.execute(f"""SELECT * FROM approvals WHERE status='pending' AND target=?
                              AND kind IN ({",".join("?" * len(kinds))})""", (str(target), *kinds)).fetchall():
        conn.execute("UPDATE approvals SET status='rejected', decided_by='Система', decided_at=? WHERE id=?",
                     (_now().isoformat(), a["id"]))
        log("system", "approval_rejected", int(target) if a["kind"] in ("refund", "release") else None, None,
            text=f"{a['summary']}: просьба закрыта, {why}")


def _approval(aid: int):
    a = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    if not a:
        raise HTTPException(404, "Запрос не найден")
    if a["status"] != "pending":
        raise HTTPException(409, "По этому запросу уже принято решение")
    return a


@app.get("/analyst/approvals")
def approvals(s: dict = Depends(as_analyst)):
    """Действия, которые ждут второго сотрудника."""
    return [_approval_view(a) for a in conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id")]


@app.post("/analyst/approvals/{aid}/confirm")
def approval_confirm(aid: int, s: dict = Depends(as_analyst)):
    """Второй сотрудник подтверждает действие. Тот, кто просил, подтвердить сам не может."""
    a = _approval(aid)
    if a["requested_by"] == s["name"]:
        raise HTTPException(409, "Подтвердить должен другой сотрудник: правило двух ключей")
    params = json.loads(a["params"] or "{}")
    case_id = int(a["target"]) if a["kind"] in ("refund", "release") else None
    # Сначала просьба выполнена — тогда само действие не закроет её как «неактуальную».
    conn.execute("UPDATE approvals SET status='done', decided_by=?, decided_at=? WHERE id=?",
                 (s["name"], _now().isoformat(), aid))
    try:
        if a["kind"] == "refund":
            _refund(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone(), "analyst", s["name"])
        elif a["kind"] == "release":
            case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not _pending(case):
                raise HTTPException(409, "По этому случаю решение уже принято")
            _decide(case, "approve", params.get("comment", ""), s["name"])
        else:
            _suspend_network(a["target"])
    except HTTPException as e:
        # Пока просьба ждала, действие стало невозможным. Проверки идут до изменений, поэтому откатывать
        # нечего: просьбу закрываем и сохраняем это (JSONResponse, а не исключение — иначе откатилось бы и закрытие).
        why = str(e.detail).rstrip(".")
        conn.execute("UPDATE approvals SET status='rejected', decided_by='Система' WHERE id=?", (aid,))
        log("system", "approval_rejected", case_id, None, text=f"{a['summary']}: просьба закрыта, действие уже неактуально ({why})")
        return JSONResponse({"detail": f"Действие уже неактуально: {why}. Просьба закрыта."}, status_code=409)
    log("analyst", "approval_confirmed", case_id, None, employee=s["name"], requested_by=a["requested_by"],
        text=f"{a['summary']}: подтвердил второй сотрудник ({s['name']}, запросил(а) {a['requested_by']})")
    return {"approval": _approval_view(conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()),
            "case": _analyst_case(case_id) if case_id else None}


@app.post("/analyst/approvals/{aid}/reject")
def approval_reject(aid: int, s: dict = Depends(as_analyst)):
    """Второй сотрудник отклоняет действие (или первый отменяет свою просьбу)."""
    a = _approval(aid)
    conn.execute("UPDATE approvals SET status='rejected', decided_by=?, decided_at=? WHERE id=?",
                 (s["name"], _now().isoformat(), aid))
    own = a["requested_by"] == s["name"]
    case_id = int(a["target"]) if a["kind"] in ("refund", "release") else None
    log("analyst", "approval_rejected", case_id, None, employee=s["name"],
        text=f"{a['summary']}: " + ("запрос отменён" if own else f"отклонил второй сотрудник ({s['name']})"))
    return {"approval": _approval_view(conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone())}


# ---------- Скрытые персональные данные и журнал просмотров ----------
# Сотрудник по умолчанию видит ИИН и телефон клиента скрытыми. Открыть их можно, только указав причину;
# каждый просмотр пишется в журнал, и клиент видит в приложении, когда и зачем банк открывал его данные.

class PiiIn(BaseModel):
    reason: str = Field(max_length=16)


def _pii(cid: int) -> dict:
    """ИИН и телефон клиента. В демо настоящих данных нет: они получаются из номера клиента."""
    d = str(int(hashlib.sha256(f"demo-{cid}".encode()).hexdigest()[:16], 16)).rjust(20, "0")
    iin = f"{60 + cid % 40:02d}{cid % 12 + 1:02d}{cid % 28 + 1:02d}" + d[:6]
    return {"iin": iin, "phone": f"+7 7{d[6:8]} {d[8:11]} {d[11:13]} {d[13:15]}"}


def _pii_masked(cid: int) -> dict:
    p = _pii(cid)
    return {"iin": p["iin"][:2] + "••••••" + p["iin"][-4:], "phone": p["phone"][:4] + "•• ••• •• " + p["phone"][-2:]}


def _open_pii(cid: int, employee: str, reason: str) -> dict:
    conn.execute("INSERT INTO data_access(client_id,employee,reason,at) VALUES(?,?,?,?)",
                 (cid, employee, reason, _now().isoformat()))
    log("analyst", "pii_viewed", None, None, client=cid, reason=reason, employee=employee,
        text=f"{employee} открыл(а) персональные данные клиента: {PII_REASONS[reason].lower()}")
    return _pii(cid)


def _pii_log(cid: int, limit: int = 5) -> list[dict]:
    return [dict(r) | {"reason_text": PII_REASONS[r["reason"]]} for r in conn.execute(
        "SELECT employee, reason, at FROM data_access WHERE client_id=? ORDER BY id DESC LIMIT ?", (cid, limit))]


@app.post("/analyst/clients/{cid}/pii")
def open_pii(cid: int, body: PiiIn, s: dict = Depends(as_analyst)):
    """Открыть ИИН и телефон клиента. Без причины нельзя; причина и сотрудник остаются в журнале."""
    if _client(cid)["kind"] == "external":
        raise HTTPException(404, "Это счёт в другом банке: персональных данных его владельца у нас нет")
    if body.reason not in PII_REASONS or body.reason == "afc":
        raise HTTPException(400, "Укажите причину: " + ", ".join(v.lower() for k, v in PII_REASONS.items() if k != "afc"))
    return _open_pii(cid, s["name"], body.reason) | {"log": _pii_log(cid)}


# ---------- Журнал действий: проверка целостности ----------

@app.get("/analyst/journal")
def journal_check(s: dict = Depends(as_analyst)):
    """Проверка, что записи журнала не меняли и не удаляли задним числом."""
    return journal.verify(conn) | {"checked_at": _now().isoformat()}


@app.post("/demo/journal/tamper")
def journal_tamper(s: dict = Depends(as_analyst)):
    """Демо: «сотрудник-сообщник» правит запись прямо в базе, в обход приложения. Проверка журнала это заметит.
    Вернуть журнал — «Сбросить демо»."""
    row = conn.execute("""SELECT * FROM events WHERE action IN ('refunded','decision','blocked') AND details IS NOT NULL
                          ORDER BY id DESC LIMIT 1""").fetchone() or \
        conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise HTTPException(409, "Журнал пуст")
    details = json.loads(row["details"] or "{}")
    details["text"] = (details.get("text") or EVENT_TEXT.get(row["action"], row["action"])) + " (запись изменена)"
    conn.execute("UPDATE events SET details=? WHERE id=?", (json.dumps(details, ensure_ascii=False), row["id"]))
    return {"id": row["id"], "action": row["action"], "at": row["at"], "text": details["text"]}


# ---------- «Счёт открыли без меня» ----------
# На имя клиента удалённо (по поддельному видео) открыли ещё один счёт, и через него пошли деньги незнакомцев.
# Клиент узнаёт об этом сразу на прежних устройствах и одной кнопкой «Это не я» закрывает счёт: банк фиксирует
# кражу личности, а не делает человека дроппером, и выдаёт подтверждение с датой для полиции и суда.

class ClaimIn(BaseModel):
    answer: str     # mine | not_me


def _identity_view(c) -> dict | None:
    """Для сотрудника: счёт открыт на имя другого клиента — что известно об открытии и что ответил владелец."""
    if not c["owner_id"]:
        return None
    owner = _client(c["owner_id"])
    claim = conn.execute("SELECT * FROM account_claims WHERE account_id=?", (c["id"],)).fetchone()
    dev = conn.execute("SELECT model FROM devices WHERE device_id=? LIMIT 1", (c["opened_device"],)).fetchone()
    own = conn.execute("SELECT 1 FROM devices WHERE client_id=? AND device_id=?", (owner["id"], c["opened_device"])).fetchone()
    return {"owner_id": owner["id"], "owner": owner["name"].split(" (")[0], "opened_at": c["account_opened_at"],
            "via": c["opened_via"], "device": dev["model"] if dev else None, "owner_device": bool(own),
            "answer": claim["answer"] if claim else None, "answered_at": claim["at"] if claim else None}


def _claim_number(row) -> str:
    return f"КЛ-{row['at'][:4]}-{row['account_id']:05d}"


def _own_accounts(cid: int) -> list[dict]:
    """Для клиента: счета, открытые на его имя удалённо, и что он о них ответил."""
    out = []
    mine = {d["device_id"] for d in conn.execute("SELECT device_id FROM devices WHERE client_id=?", (cid,))}
    for c in conn.execute("SELECT * FROM clients WHERE owner_id=? ORDER BY account_opened_at", (cid,)).fetchall():
        claim = conn.execute("SELECT * FROM account_claims WHERE account_id=?", (c["id"],)).fetchone()
        if claim and claim["answer"] == "mine":
            continue
        dev = conn.execute("SELECT model FROM devices WHERE device_id=? LIMIT 1", (c["opened_device"],)).fetchone()
        came = conn.execute("""SELECT COALESCE(SUM(amount),0) AS s, COUNT(DISTINCT sender_id) AS n FROM transfers
                               WHERE receiver_id=? AND status='done' AND return_of IS NULL""", (c["id"],)).fetchone()
        out.append({"id": c["id"], "opened_at": c["account_opened_at"], "via": c["opened_via"],
                    "device": dev["model"] if dev else None, "own_device": c["opened_device"] in mine,
                    "inflow": came["s"], "senders": came["n"], "status": c["status"],
                    "answer": claim["answer"] if claim else None, "answered_at": claim["at"] if claim else None,
                    "number": _claim_number(claim) if claim else None, "code": claim["code"] if claim else None,
                    "frozen": conn.execute("SELECT frozen FROM cases WHERE id=?", (claim["case_id"],)).fetchone()["frozen"]
                              if claim and claim["case_id"] else 0})
    return out


@app.post("/accounts/{aid}/claim")
def claim_account(aid: int, body: ClaimIn, s: dict = Depends(as_client)):
    """Клиент отвечает, открывал ли он счёт на своё имя. «Это не я» — счёт сразу приостанавливается."""
    cid, now = s["client_id"], _now()
    acc = conn.execute("SELECT * FROM clients WHERE id=?", (aid,)).fetchone()
    if not acc or acc["owner_id"] != cid:
        raise HTTPException(404, t("client_not_found"))
    if body.answer not in ("mine", "not_me"):
        raise HTTPException(400, t("bad_input"))
    if conn.execute("SELECT 1 FROM account_claims WHERE account_id=?", (aid,)).fetchone():
        raise HTTPException(409, t("claim.answered"))
    if body.answer == "mine":
        conn.execute("INSERT INTO account_claims(account_id,owner_id,answer,at) VALUES(?,?,?,?)",
                     (aid, cid, "mine", now.isoformat()))
        log(f"client:{cid}", "account_confirmed", None, None, account=aid)
        return {"accounts": _own_accounts(cid)}
    while True:
        code = secrets.token_hex(4).upper()
        if not conn.execute("""SELECT 1 FROM clearances WHERE code=? UNION SELECT 1 FROM self_reports WHERE code=?
                               UNION SELECT 1 FROM account_claims WHERE code=?""", (code, code, code)).fetchone():
            break
    _stop_cash(aid)
    frozen = max(0, _available(aid))
    reasons = [{"code": "identity_theft", "points": 0,
                "text": "Владелец сообщил, что не открывал этот счёт: похоже на кражу личности "
                        "(счёт открыт удалённо, с чужого устройства)"}]
    inc = conn.execute("SELECT id FROM transfers WHERE receiver_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                       (aid,)).fetchone()
    explanation = f"Владелец ({i18n.name(cid, _client(cid)['name']).split(' (')[0]}) сообщил в приложении, что не открывал счёт"
    due = (now + timedelta(days=APPEAL_SLA_DAYS)).isoformat()
    old = conn.execute(f"""SELECT * FROM cases WHERE client_id=? AND kind='block'
                           AND status IN ({",".join("?" * len(UNRESOLVED))}) ORDER BY id LIMIT 1""",
                       (aid, *UNRESOLVED)).fetchone()
    if old:
        # Счёт уже приостановлен (например, через сеть дропперов): дополняем тот же случай, а не заводим второй.
        case_id = old["id"]
        conn.execute("""UPDATE cases SET reasons=?, status='under_review', scope='full', explanation=?, submitted_at=?,
                        due_at=?, frozen=? WHERE id=?""",
                     (json.dumps(reasons + json.loads(old["reasons"]), ensure_ascii=False), explanation,
                      now.isoformat(), due, frozen, case_id))
    else:
        case_id = conn.execute(
            """INSERT INTO cases(client_id,transfer_id,incoming_transfer_id,kind,score,reasons,status,explanation,
                                 submitted_at,due_at,created_at,scope,frozen)
               VALUES(?,NULL,?,'block',0,?,'under_review',?,?,?,?,'full',?)""",
            (aid, inc["id"] if inc else None, json.dumps(reasons, ensure_ascii=False), explanation,
             now.isoformat(), due, now.isoformat(), frozen)).lastrowid
    _refresh_status(aid)
    conn.execute("INSERT INTO account_claims(account_id,owner_id,answer,code,case_id,at) VALUES(?,?,?,?,?,?)",
                 (aid, cid, "not_me", code, case_id, now.isoformat()))
    log(f"client:{cid}", "identity_theft", case_id, None, account=aid,
        text=f"Владелец сообщил, что не открывал счёт: счёт приостановлен, заморожено {_money(frozen)}")
    return {"accounts": _own_accounts(cid), "text": t("claim.done")}


@app.get("/clients/{cid}/identity-claim/{aid}", include_in_schema=False)
def identity_claim_page(cid: int, aid: int, request: Request, s: dict = Depends(session)):
    """Подтверждение: клиент сообщил банку, что не открывал счёт. Его можно приложить к заявлению в полицию или в суд."""
    c = _own_client(s, cid)
    row = conn.execute("SELECT * FROM account_claims WHERE account_id=? AND owner_id=? AND answer='not_me'",
                       (aid, cid)).fetchone()
    if not row:
        raise HTTPException(404, t("claim.none"))
    acc = _client(aid)
    dev = conn.execute("SELECT model FROM devices WHERE device_id=? LIMIT 1", (acc["opened_device"],)).fetchone()
    case = conn.execute("SELECT * FROM cases WHERE id=?", (row["case_id"],)).fetchone()
    verify = str(request.base_url) + f"verify/{row['code']}"
    d = lambda x: x[:16].replace("T", " ")  # noqa: E731
    body = f"""<h1>{escape(t("claim.title"))}</h1>
<div class="muted">№ {escape(_claim_number(row))} · {escape(d(row["at"]))}</div>
<dl><dt>{escape(t("cert.client"))}</dt><dd>{escape(i18n.name(c["id"], c["name"]).split(" (")[0])}</dd>
<dt>{escape(t("claim.opened"))}</dt><dd>{escape(d(acc["account_opened_at"]))} · {escape(t("claim.remote"))}{(" · " + escape(dev["model"])) if dev else ""}</dd>
<dt>{escape(t("claim.when"))}</dt><dd>{escape(d(row["at"]))}</dd>
<dt>{escape(t("claim.action"))}</dt><dd>{escape(t("claim.frozen", m=_money(case["frozen"] if case else 0)))}</dd></dl>
<p>{escape(t("claim.text"))}</p>
<p class="muted">{escape(t("sr.verify"))}<br><b>{escape(verify)}</b><br>{escape(t("cert.code"))}: <b>{row["code"]}</b></p>
<button onclick="print()">{escape(t("cert.print"))}</button>
<div class="demo">{escape(t("cert.demo"))}</div>"""
    return _page(t("claim.title"), body)


# ---------- Сети дропперов и дроп-фермы ----------
# Сеть — счета, связанные общим «дроп-телефоном» или переводами между подозрительными счетами (metrics.networks).
# Сотрудник видит её целиком: телефоны, счета, граф денег, и приостанавливает счета на дроп-телефонах
# (подтверждает второй сотрудник).

def _network(key: str) -> dict:
    found = next((n for n in metrics.networks(conn, _now()) if n["key"] == key), None)
    if not found:
        raise HTTPException(404, "Сеть не найдена: возможно, её счета уже разобраны")
    return found


def _network_view(n: dict, full: bool = False) -> dict:
    """Сводка по сети; full — со счетами, телефонами и графом денег для карточки сотрудника."""
    now, members = _now(), n["members"]
    q = ",".join("?" * len(members))
    since = (now - timedelta(days=metrics.NET_DAYS)).isoformat()
    clients = {c["id"]: c for c in conn.execute(f"SELECT * FROM clients WHERE id IN ({q})", members)}
    inside = conn.execute(f"""SELECT * FROM transfers WHERE sender_id IN ({q}) AND receiver_id IN ({q})
                              AND created_at >= ? AND return_of IS NULL ORDER BY created_at""",
                          members + members + [since]).fetchall()
    inflow = conn.execute(f"""SELECT COALESCE(SUM(amount),0) AS s, COUNT(DISTINCT sender_id) AS n FROM transfers
                              WHERE receiver_id IN ({q}) AND sender_id NOT IN ({q}) AND status='done'
                                AND return_of IS NULL AND created_at >= ?""", members + members + [since]).fetchone()
    partners: dict[int, set] = {i: set() for i in members}
    for t in inside:
        if t["status"] in ("done", "held", "pending") and t["sender_id"] != t["receiver_id"]:
            partners[t["sender_id"]].add(t["receiver_id"])
            partners[t["receiver_id"]].add(t["sender_id"])
    linked = {a["client_id"] for a in metrics.linked_accounts(conn)}
    # Центр сети — счёт, с которым связано больше всего других счетов сети (обычно карта организатора).
    center = max(members, key=lambda i: (len(partners[i]), i in linked, -i))
    phones: dict[int, list] = {}
    for k, f in enumerate(n["farms"], 1):
        for i in f["accounts"]:
            phones.setdefault(i, []).append(k)
    view = {
        "key": n["key"], "accounts": len(members),
        "names": [clients[i]["name"] for i in [center] + [m for m in members if m != center]][:4],
        "farms": [{"n": k, "model": f["model"], "device_id": f["device_id"], "factors": f["factors"],
                   "accounts": [{"id": i, "name": clients[i]["name"]} for i in f["accounts"]]}
                  for k, f in enumerate(n["farms"], 1)],
        "center": {"id": center, "name": clients[center]["name"], "links": len(partners[center])},
        "inflow": inflow["s"], "senders": inflow["n"],
        "blocked": sum(1 for c in clients.values() if c["status"] == "blocked"),
        "to_suspend": sum(1 for i in phones if clients[i]["status"] == "active"),
        "approval": _approval_view(_pending_approval("network", n["key"])),
    }
    if not full:
        return view
    marks = engine.dropper_marks(conn, members)
    came = {r["receiver_id"]: r for r in conn.execute(
        f"""SELECT receiver_id, SUM(amount) AS s, COUNT(DISTINCT sender_id) AS n FROM transfers
            WHERE receiver_id IN ({q}) AND sender_id NOT IN ({q}) AND status='done' AND return_of IS NULL
              AND created_at >= ? GROUP BY receiver_id""", members + members + [since])}
    cases: dict[int, int] = {}
    for k in conn.execute(f"""SELECT id, client_id FROM cases WHERE client_id IN ({q})
                              ORDER BY (status IN ('open','under_review','kept')) DESC, created_at DESC""", members):
        cases.setdefault(k["client_id"], k["id"])
    view["members"] = [{
        "id": i, "name": clients[i]["name"], "status": clients[i]["status"], "kind": clients[i]["kind"],
        "age_days": (now - datetime.fromisoformat(clients[i]["account_opened_at"])).days,
        "marks": marks[i] + (["встречается в нескольких независимых случаях"] if i in linked else []),
        "phones": phones.get(i, []), "case_id": cases.get(i), "center": i == center,
        "inflow": came[i]["s"] if i in came else 0, "senders": came[i]["n"] if i in came else 0,
    } for i in sorted(members, key=lambda i: (i != center, not phones.get(i), clients[i]["name"]))]
    # На графе — телефоны, переводы внутри сети и переводы пострадавших, которые пожаловались. Сколько денег пришло
    # извне, видно в списке счетов: иначе десятки плательщиков закрывают собой схему.
    victims = conn.execute(f"""SELECT t.* FROM complaints p JOIN transfers t ON t.id=p.transfer_id
                               WHERE t.receiver_id IN ({q}) AND t.created_at >= ?""", members + [since]).fetchall()
    tags = {i: ("телефоны " if len(p) > 1 else "телефон ") + ", ".join(map(str, p)) for i, p in phones.items()}
    tags[center] = (tags[center] + ", центр") if center in tags else "центр сети"
    view["graph"] = _graph_build(list(inside) + list(victims), set(members), None, tags, view["farms"])
    return view


@app.get("/analyst/networks")
def analyst_networks(s: dict = Depends(as_analyst)):
    """Сети дропперов: сначала те, где есть что приостановить."""
    views = [_network_view(n) for n in metrics.networks(conn, _now())]
    return sorted(views, key=lambda v: (v["to_suspend"] == 0, -len(v["farms"]), -v["accounts"]))


@app.get("/analyst/networks/{key}")
def analyst_network(key: str, s: dict = Depends(as_analyst)):
    return _network_view(_network(key), full=True)


@app.post("/analyst/networks/{key}/suspend")
def suspend_network(key: str, s: dict = Depends(as_analyst)):
    """Приостановить счета, в которые входят с дроп-телефонов этой сети: ими, похоже, управляет один человек.

    Остальные счета сети (например, невольные участники, переславшие деньги по просьбе мошенника) сотрудник
    разбирает по одному: у них может быть соразмерная блокировка.
    """
    view = _network_view(_network(key))
    if not view["to_suspend"]:
        raise HTTPException(409, "Все счета на дроп-телефонах этой сети уже приостановлены или ограничены")
    # Сразу много счетов один сотрудник не закрывает: нужен второй.
    _ask_second_key("network", key, f"Приостановить счета на дроп-телефонах сети: {view['to_suspend']}", {}, s)
    return _network_view(_network(key), full=True)


def _suspend_network(key: str) -> int:
    n = _network(key)
    done: list[int] = []
    for f in n["farms"]:
        for cid in f["accounts"]:
            if cid in done or _client(cid)["status"] != "active":
                continue
            _suspend_case(cid, {"code": "network", "points": 60,
                                "text": f"Вход в приложение с телефона, через который работают счета {len(f['accounts'])} "
                                        "разных людей (дроп-ферма)"},
                          f"Операции приостановлены сотрудником: счёт в сети дропперов, телефон {f['model']}")
            done.append(cid)
    if not done:
        raise HTTPException(409, "Все счета на дроп-телефонах этой сети уже приостановлены или ограничены")
    return len(done)


@app.post("/analyst/networks/{key}/report")
def network_report(key: str, s: dict = Depends(as_analyst)):
    """Сведения о сети для передачи в Антифрод-центр и АФМ: счета, телефоны, переводы. Формат условный."""
    v = _network_view(_network(key), full=True)
    report = {
        "format": "Сведения о сети дропперских счетов (прототип, формат условный)",
        "generated_at": _now().isoformat(), "generated_by": s["name"],
        "network": {k: v[k] for k in ("key", "accounts", "inflow", "senders", "blocked", "center")},
        "accounts": [{k: m[k] for k in ("id", "name", "status", "kind", "age_days", "marks", "phones", "case_id")}
                     | _open_pii(m["id"], s["name"], "afc") for m in v["members"]],
        "devices": [{"n": f["n"], "device_id": f["device_id"], "model": f["model"], "factors": f["factors"],
                     "accounts": [a["id"] for a in f["accounts"]]} for f in v["farms"]],
        "transfers": [{k: e[k] for k in ("from", "to", "amount", "count", "status", "returned")}
                      for e in v["graph"]["edges"]],
    }
    log("analyst", "network_report", None, None, text=f"Выгружены сведения о сети {key}: {v['accounts']} счетов")
    return RawResponse(json.dumps(report, ensure_ascii=False, indent=2), media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="network-{key}.json"', "Cache-Control": "no-store"})


# ---------- Запросы между банками через Антифрод-центр ----------
# В демо другой банк — имитация: его ответы на наши запросы и его запросы к нам создаёт сотрудник кнопками
# во вкладке «Другие банки». В банке обмен идёт через Антифрод-центр Нацбанка автоматически.

def _interbank_view(r) -> dict:
    tr = _incoming_row(r["transfer_id"])
    p = conn.execute("SELECT * FROM complaints WHERE id=?", (r["complaint_id"],)).fetchone() if r["complaint_id"] else None
    ours = _client(tr["sender_id"] if r["direction"] == "out" else tr["receiver_id"])
    owed = (_incoming_row(p["transfer_id"])["amount"] - _returned_sum(p["transfer_id"])) if p else 0
    cases = [k["id"] for k in conn.execute("SELECT id FROM cases WHERE complaint_id=? ORDER BY hop, id",
                                           (r["complaint_id"],))] if p else []
    return dict(r) | {
        "status_text": INTERBANK_STATUS[r["status"]],
        "overdue": r["status"] == "sent" and r["due_at"] < _now().isoformat(),
        "victim": _client(p["complainant_id"])["name"] if p else None,
        "ours": ours["name"], "ours_id": ours["id"], "cases": cases,
        "can_return": r["direction"] == "out" and r["status"] == "frozen" and min(r["frozen"] - r["returned"], owed) > 0,
    }


@app.get("/analyst/interbank")
def analyst_interbank(s: dict = Depends(as_analyst)):
    """Запросы в другие банки и из них. Для демо — ещё переводы из других банков, по которым можно имитировать запрос."""
    since = (_now() - timedelta(days=COMPLAINT_DAYS)).isoformat()
    demo = conn.execute(
        """SELECT t.id, t.amount, t.created_at, s.name AS sender, s.bank, r.name AS receiver, r.id AS receiver_id
           FROM transfers t JOIN clients s ON s.id=t.sender_id JOIN clients r ON r.id=t.receiver_id
           WHERE s.kind='external' AND r.kind!='external' AND t.status='done' AND t.return_of IS NULL
             AND t.created_at >= ? AND NOT EXISTS (SELECT 1 FROM complaints p WHERE p.transfer_id=t.id)
           ORDER BY t.created_at DESC""", (since,)).fetchall()
    return {"requests": [_interbank_view(r) for r in conn.execute("SELECT * FROM interbank ORDER BY sent_at DESC, id DESC")],
            "incoming_demo": [dict(d) for d in demo]}


class InterbankAnswerIn(BaseModel):
    result: str          # frozen: деньги заморожены | gone: деньги уже ушли


@app.post("/analyst/interbank/{rid}/answer")
def interbank_answer(rid: int, body: InterbankAnswerIn, s: dict = Depends(as_analyst)):
    """Демо: ответ другого банка на наш запрос."""
    r = conn.execute("SELECT * FROM interbank WHERE id=?", (rid,)).fetchone()
    if not r or r["direction"] != "out":
        raise HTTPException(404, "Запрос не найден")
    if r["status"] != "sent":
        raise HTTPException(409, "На этот запрос уже ответили")
    if body.result not in ("frozen", "gone"):
        raise HTTPException(400, "Ответ должен быть frozen или gone")
    frozen = r["amount"] if body.result == "frozen" else 0
    note = (f"{r['bank']} заморозил {_money(frozen)}" if frozen else f"{r['bank']}: деньги уже сняты или ушли дальше")
    conn.execute("UPDATE interbank SET status=?, frozen=?, answered_at=?, note=? WHERE id=?",
                 (body.result, frozen, _now().isoformat(), note, rid))
    log("system", "interbank_answer", None, r["transfer_id"], bank=r["bank"], text=note)
    return _interbank_view(conn.execute("SELECT * FROM interbank WHERE id=?", (rid,)).fetchone())


@app.post("/analyst/interbank/{rid}/return")
def interbank_return(rid: int, s: dict = Depends(as_analyst)):
    """Демо: другой банк возвращает пострадавшему деньги, которые заморозил по нашему запросу."""
    r = conn.execute("SELECT * FROM interbank WHERE id=?", (rid,)).fetchone()
    if not r or r["direction"] != "out":
        raise HTTPException(404, "Запрос не найден")
    p = conn.execute("SELECT * FROM complaints WHERE id=?", (r["complaint_id"],)).fetchone()
    owed = _incoming_row(p["transfer_id"])["amount"] - _returned_sum(p["transfer_id"])
    amount = min(r["frozen"] - r["returned"], owed) if r["status"] == "frozen" else 0
    if amount <= 0:
        raise HTTPException(409, "Возвращать нечего: деньги не заморожены или уже возвращены")
    holder = _incoming_row(r["transfer_id"])["receiver_id"]       # счёт в другом банке
    cur = conn.execute(
        "INSERT INTO transfers(sender_id,receiver_id,amount,created_at,status,return_of) VALUES(?,?,?,?,?,?)",
        (holder, p["complainant_id"], amount, _now().isoformat(), "done", p["transfer_id"]))
    _move(holder, p["complainant_id"], amount)
    conn.execute("UPDATE interbank SET returned = returned + ? WHERE id=?", (amount, rid))
    log("system", "refunded", None, cur.lastrowid, amount=amount, text=f"{r['bank']} вернул пострадавшему {_money(amount)}")
    return _interbank_view(conn.execute("SELECT * FROM interbank WHERE id=?", (rid,)).fetchone())


class InterbankInIn(BaseModel):
    transfer_id: int


@app.post("/analyst/interbank/incoming")
def interbank_incoming(body: InterbankInIn, s: dict = Depends(as_analyst)):
    """Демо: другой банк сообщает, что перевод их клиента нашему клиенту — мошенничество.

    Система сама идёт по цепочке в нашем банке, как по жалобе своего клиента, и сразу отвечает, сколько заморожено.
    """
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (body.transfer_id,)).fetchone()
    if not tr:
        raise HTTPException(404, "Перевод не найден")
    sender, receiver = _client(tr["sender_id"]), _client(tr["receiver_id"])
    if sender["kind"] != "external" or receiver["kind"] == "external" or tr["status"] != "done" or tr["return_of"]:
        raise HTTPException(400, "Это не перевод из другого банка нашему клиенту")
    if conn.execute("SELECT 1 FROM complaints WHERE transfer_id=?", (tr["id"],)).fetchone():
        raise HTTPException(409, "По этому переводу запрос уже был")
    now = _now()
    complaint_id = conn.execute(
        "INSERT INTO complaints(transfer_id,complainant_id,outcome,created_at) VALUES(?,?,?,?)",
        (tr["id"], sender["id"], "pending", now.isoformat())).lastrowid
    cases = [] if _returned(tr["id"]) else _trace_complaint(tr, complaint_id, now, sender["id"], actor=f"bank:{sender['bank']}")
    conn.execute("UPDATE complaints SET outcome=?, case_id=? WHERE id=?",
                 ("blocked" if cases else "warned", cases[0] if cases else None, complaint_id))
    frozen = sum(conn.execute("SELECT frozen FROM cases WHERE id=?", (k,)).fetchone()["frozen"] for k in cases)
    note = (f"Ответ отправлен автоматически: в нашем банке заморожено {_money(frozen)}, приостановлено счетов: {len(cases)}"
            if cases else "Ответ отправлен автоматически: получатель ещё ничего не переслал, его предупредили")
    rid = conn.execute(
        """INSERT INTO interbank(direction,bank,complaint_id,transfer_id,amount,status,frozen,sent_at,due_at,answered_at,note)
           VALUES('in',?,?,?,?,'answered',?,?,?,?,?)""",
        (sender["bank"], complaint_id, tr["id"], tr["amount"], frozen, now.isoformat(),
         (now + timedelta(hours=INTERBANK_HOURS)).isoformat(), now.isoformat(), note)).lastrowid
    log("system", "interbank_in", cases[0] if cases else None, tr["id"], bank=sender["bank"], text=note)
    return _interbank_view(conn.execute("SELECT * FROM interbank WHERE id=?", (rid,)).fetchone())


@app.post("/analyst/cases/{case_id}/escalate")
def escalate(case_id: int, s: dict = Depends(as_analyst)):
    """Сотрудник не согласен с подсказкой системы: соразмерная блокировка становится полной."""
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Случай не найден")
    if case["kind"] != "block" or case["scope"] != "partial" or case["status"] not in UNRESOLVED:
        raise HTTPException(409, "По этому случаю счёт уже приостановлен целиком или ограничения сняты")
    conn.execute("UPDATE cases SET scope='full' WHERE id=?", (case_id,))
    _refresh_status(case["client_id"])
    log("analyst", "escalated", case_id, None)
    return _analyst_case(case_id)


@app.post("/analyst/cases/{case_id}/report")
def antifraud_report(case_id: int, s: dict = Depends(as_analyst)):
    """Сведения по случаю для передачи в Антифрод-центр Нацбанка.

    Набор полей взят из требований к банкам: клиент, платёжные реквизиты, IP-адрес, геолокация, номера
    телефонов. Формат условный: точную схему обмена задаёт Антифрод-центр. Технических данных в демо нет.
    """
    card = _analyst_case(case_id)
    client = _client(card["client_id"])
    ops = [x for x in (card["incoming"], card["outgoing"]) if x]
    report = {
        "format": "Сведения о подозрительной операции (прототип, формат условный)",
        "generated_at": _now().isoformat(),
        "generated_by": s["name"],
        "case": {"id": card["case_id"], "kind": card["kind"], "created_at": card["created_at"],
                 "status": card["status"], "status_text": card["status_text"], "score": card["score"],
                 "decision_comment": card["decision_comment"], "decided_at": card["decided_at"]},
        "client": {"id": client["id"], "name": client["name"], "account_opened_at": client["account_opened_at"],
                   "status": client["status"], "role_hint": card["role"]["label"],
                   **_open_pii(client["id"], s["name"], "afc")},
        "operations": [{"id": o["id"], "created_at": o["created_at"], "amount_kzt": o["amount"], "status": o["status"],
                        "sender": o["sender"], "receiver": o["receiver"]} for o in ops],
        "reasons": [{"code": r.get("code"), "points": r.get("points"), "text": r.get("text")} for r in card["reasons"]],
        "complaint_chain": [{k: x.get(k) for k in ("hop", "name", "amount", "role", "status_text")} for x in card["chain"]],
        "money_graph": {"accounts": [{k: n[k] for k in ("id", "name", "role", "status", "kind", "afc")} for n in card["graph"]["nodes"]],
                        "transfers": [{k: e[k] for k in ("from", "to", "amount", "count", "status", "returned")}
                                      for e in card["graph"]["edges"]]},
        "linked_account": ({k: card["linked"][k] for k in ("client_id", "name", "sources", "amount")}
                           if card["linked"] else None),
        "client_explanation": card["explanation"],
        "documents": [{"filename": d["filename"], "title": d["title"], "size": d["size"]} for d in card["documents"]],
        "technical": {"ip": None, "geolocation": None, "phone_numbers": None,
                      "devices": [dict(d) for d in conn.execute(
                          "SELECT device_id, model, first_seen, last_seen FROM devices WHERE client_id=? ORDER BY last_seen DESC",
                          (client["id"],))],
                      "note": "IP-адреса, геолокации и номеров в демо-данных нет. В банке берутся из журналов мобильного приложения."},
        "history": [{"at": h["at"], "who": h["who"], "text": h["text"]} for h in card["history"]],
    }
    log("analyst", "report", case_id, None)
    body = json.dumps(report, ensure_ascii=False, indent=2)
    return RawResponse(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="antifraud-case-{case_id}.json"', "Cache-Control": "no-store"})


@app.post("/demo/reset")
def demo_reset(s: dict = Depends(as_analyst)):
    """Возвращает демо-данные в исходное состояние: даты отсчитываются от текущего момента."""
    ensure_demo(force=True)
    for token in [t for t, v in SESSIONS.items() if v["role"] == "client"]:
        SESSIONS.pop(token)
    return {"ok": True}
