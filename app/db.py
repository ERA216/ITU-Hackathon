"""SQLite: схема и подключение."""
import os
import sqlite3

DB_PATH = os.environ.get("CA_DB", os.path.join(os.path.dirname(__file__), "..", "data.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    account_opened_at TEXT NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',      -- active | limited (заморожена спорная сумма) | blocked
    true_kind TEXT NOT NULL DEFAULT 'honest',   -- honest | dropper (метка синтетики, банку неизвестна)
    kind TEXT NOT NULL DEFAULT 'person',        -- person: частное лицо | ip: ИП | external: счёт в другом банке
    activity TEXT,                              -- вид деятельности ИП
    bank TEXT,                                  -- для счёта в другом банке: какой банк
    owner_id INTEGER REFERENCES clients(id),    -- ещё один счёт на имя этого клиента (в демо — открытый без него)
    opened_via TEXT,                            -- branch: в отделении | remote: удалённо, по видео
    opened_device TEXT                          -- с какого устройства открыт удалённо
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    sender_id INTEGER NOT NULL REFERENCES clients(id),
    receiver_id INTEGER NOT NULL REFERENCES clients(id),
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',        -- done | held | pending (период охлаждения) | cancelled
    return_of INTEGER REFERENCES transfers(id), -- если это безопасный возврат
    scenario TEXT,                               -- метка синтетики
    release_at TEXT                              -- когда уйдёт перевод в периоде охлаждения
);
CREATE INDEX IF NOT EXISTS ix_tr_sender ON transfers(sender_id, created_at);
CREATE INDEX IF NOT EXISTS ix_tr_receiver ON transfers(receiver_id, created_at);
CREATE INDEX IF NOT EXISTS ix_tr_pending ON transfers(release_at) WHERE status='pending';
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    transfer_id INTEGER REFERENCES transfers(id),          -- остановленный перевод
    incoming_transfer_id INTEGER REFERENCES transfers(id), -- "ошибочный" входящий
    kind TEXT NOT NULL,                 -- intercept | block
    score INTEGER NOT NULL,
    reasons TEXT NOT NULL,              -- JSON-список сработавших признаков
    status TEXT NOT NULL DEFAULT 'open',-- open | returned | under_review | unblocked | kept | released | cancelled
    explanation TEXT,                   -- объяснение клиента при апелляции
    decision_comment TEXT,              -- комментарий аналитика к решению
    decided_at TEXT,
    complaint_id INTEGER,               -- жалоба, по которой возник случай (для всей цепочки)
    hop INTEGER,                        -- звено цепочки: 1 — получил деньги от пострадавшего
    submitted_at TEXT,                  -- когда клиент отправил апелляцию
    due_at TEXT,                        -- до какого момента банк обещал ответить
    guardian_id INTEGER REFERENCES clients(id),  -- доверенное лицо, которому сообщили об остановке
    guardian_state TEXT,                -- notified | asked (клиент настаивает) | confirmed | cancelled
    scope TEXT NOT NULL DEFAULT 'full', -- блокировка: full — счёт целиком | partial — заморожена только спорная сумма
    frozen INTEGER NOT NULL DEFAULT 0,  -- сколько денег из цепочки осталось на счёте и заморожено по этому случаю
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY,
    transfer_id INTEGER NOT NULL UNIQUE REFERENCES transfers(id),  -- перевод, на который пожаловались
    complainant_id INTEGER NOT NULL REFERENCES clients(id),
    outcome TEXT NOT NULL,              -- blocked | warned | already_returned
    case_id INTEGER REFERENCES cases(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incoming_marks (
    transfer_id INTEGER PRIMARY KEY REFERENCES transfers(id),   -- клиент сказал, что знает отправителя
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,                -- client:<id> | analyst | system
    action TEXT NOT NULL,
    case_id INTEGER REFERENCES cases(id),
    transfer_id INTEGER REFERENCES transfers(id),
    details TEXT,                       -- JSON
    hash TEXT                           -- отпечаток записи вместе с отпечатком предыдущей (app/journal.py)
);
CREATE INDEX IF NOT EXISTS ix_events_case ON events(case_id, id);
CREATE TABLE IF NOT EXISTS afc_list (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- счёт в базе Антифрод-центра (в демо — имитация)
    list TEXT NOT NULL,                 -- grey: подозрительный | black: подтверждённый мошенник
    added_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS call_signals (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- сигнал оператора: клиенту звонят с подозрительного номера
    number TEXT NOT NULL,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardians (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- кого защищают
    guardian_id INTEGER NOT NULL REFERENCES clients(id),   -- доверенное лицо
    added_at TEXT NOT NULL,
    remove_at TEXT                      -- отключение вступает в силу не сразу: мошенник не уговорит снять защиту
);
CREATE TABLE IF NOT EXISTS clearances (
    id INTEGER PRIMARY KEY,             -- справка о снятии ограничений
    case_id INTEGER NOT NULL REFERENCES cases(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    code TEXT NOT NULL UNIQUE,          -- код проверки подлинности
    issued_at TEXT NOT NULL,
    revoked_at TEXT                     -- если счёт снова приостановили
);
CREATE TABLE IF NOT EXISTS recruit_marks (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- клиент ответил на предупреждение о вербовке
    action TEXT NOT NULL,               -- dismissed: «это мои покупатели или знакомые»
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_checks (
    id INTEGER PRIMARY KEY,             -- проверка подозрительного сообщения; сам текст не хранится
    client_id INTEGER NOT NULL REFERENCES clients(id),
    scheme TEXT,                        -- mistaken | safe_account | investment | job | fake_letter | fake_alert | fake_invoice | NULL
    verdict TEXT NOT NULL,              -- danger | caution | none
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS self_reports (
    id INTEGER PRIMARY KEY,             -- клиент сам сообщил банку, что его втянули в схему
    client_id INTEGER NOT NULL REFERENCES clients(id),
    kind TEXT NOT NULL,                 -- card: отдал карту или доступ | transit: принимал и пересылал | offer: только предложили
    details TEXT,                       -- что клиент рассказал своими словами
    case_id INTEGER REFERENCES cases(id),
    code TEXT NOT NULL UNIQUE,          -- код проверки подтверждения
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY,             -- кредит наличными: деньги банка зачисляются на счёт клиента
    client_id INTEGER NOT NULL REFERENCES clients(id),
    amount INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    repaid_at TEXT                      -- клиент вернул кредит банку (досрочное погашение)
);
CREATE TABLE IF NOT EXISTS interbank (
    id INTEGER PRIMARY KEY,             -- запрос между банками через Антифрод-центр (в демо — имитация)
    direction TEXT NOT NULL,            -- out: деньги ушли в другой банк, просим заморозить | in: другой банк просит нас
    bank TEXT NOT NULL,
    complaint_id INTEGER REFERENCES complaints(id),
    transfer_id INTEGER NOT NULL REFERENCES transfers(id),  -- out: перевод на счёт в другом банке | in: перевод оттуда
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,               -- out: sent | frozen | gone | in: answered
    frozen INTEGER NOT NULL DEFAULT 0,  -- out: сколько заморозил другой банк | in: сколько заморозили мы
    returned INTEGER NOT NULL DEFAULT 0,-- out: сколько другой банк вернул пострадавшему
    sent_at TEXT NOT NULL,
    due_at TEXT NOT NULL,               -- до какого момента ждём (или обещали) ответ
    answered_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS safes (
    client_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- «Сейф»: деньги, которые нельзя перевести через приложение
    amount INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    unlock_amount INTEGER,              -- клиент попросил открыть сейф: сколько
    unlock_at TEXT,                     -- когда эти деньги станут доступны (через сутки)
    unlock_requested_at TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT NOT NULL,            -- устройство, с которого входили в приложение (в демо — синтетика)
    client_id INTEGER NOT NULL REFERENCES clients(id),
    model TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (device_id, client_id)
);
CREATE INDEX IF NOT EXISTS ix_devices_client ON devices(client_id);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,             -- правило двух ключей: действие ждёт подтверждения второго сотрудника
    kind TEXT NOT NULL,                 -- refund: возврат пострадавшему | network: приостановка сети | release: снять
                                        -- ограничения или разрешить перевод «похожему на организатора»
    target TEXT NOT NULL,               -- номер случая или ключ сети
    summary TEXT NOT NULL,              -- что будет сделано, словами
    params TEXT,                        -- JSON
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | rejected
    decided_by TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS data_access (
    id INTEGER PRIMARY KEY,             -- сотрудник открыл скрытые персональные данные клиента
    client_id INTEGER NOT NULL REFERENCES clients(id),
    employee TEXT NOT NULL,
    reason TEXT NOT NULL,               -- contact | appeal | police | afc
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_data_access_client ON data_access(client_id);
CREATE TABLE IF NOT EXISTS account_claims (
    account_id INTEGER PRIMARY KEY REFERENCES clients(id),  -- счёт, открытый на имя клиента: что ответил владелец
    owner_id INTEGER NOT NULL REFERENCES clients(id),
    answer TEXT NOT NULL,               -- mine: открывал сам | not_me: не открывал
    code TEXT UNIQUE,                   -- код проверки подтверждения «я не открывал»
    case_id INTEGER REFERENCES cases(id),
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_withdrawals (
    id INTEGER PRIMARY KEY,             -- снятие наличных по QR-коду банкомата, без карты
    client_id INTEGER NOT NULL REFERENCES clients(id),
    amount INTEGER NOT NULL,
    foreign_amount INTEGER NOT NULL DEFAULT 0,  -- сколько в сумме денег незнакомцев, пришедших меньше суток назад
    status TEXT NOT NULL,               -- done | ready: ждёт у банкомата | pending: отложено на час | cancelled | expired
    created_at TEXT NOT NULL,
    release_at TEXT,                    -- pending: с какого момента можно снять (звонок с подозрительного номера)
    expires_at TEXT,                    -- ready: до какого момента банкомат ждёт клиента
    done_at TEXT,
    face INTEGER NOT NULL DEFAULT 0     -- снятие подтвердили по лицу в банкомате
);
CREATE INDEX IF NOT EXISTS ix_cash_client ON cash_withdrawals(client_id);
CREATE TABLE IF NOT EXISTS qr_checks (
    id INTEGER PRIMARY KEY,             -- проверка платёжного QR-кода перед оплатой
    client_id INTEGER NOT NULL REFERENCES clients(id),
    receiver_id INTEGER NOT NULL REFERENCES clients(id),
    verdict TEXT NOT NULL,              -- ok | caution | danger
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trainer_results (
    id INTEGER PRIMARY KEY,             -- тренажёр против мошенников: результат одного прохождения
    client_id INTEGER NOT NULL REFERENCES clients(id),
    score INTEGER NOT NULL,
    total INTEGER NOT NULL,
    wrong TEXT,                         -- JSON: номера ситуаций, где клиент ошибся
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    uploaded_at TEXT NOT NULL
);
"""


SCHEMA_VERSION = 15


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        # Демо-база от старой версии кода: проще пересоздать, данные заполнятся заново.
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn
