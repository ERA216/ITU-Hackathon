"""Журнал действий, который нельзя незаметно подделать задним числом.

Каждая запись хранит отпечаток (SHA-256) своего содержимого вместе с отпечатком предыдущей записи.
Если кто-то изменит или удалит запись прямо в базе, отпечатки всех следующих записей перестанут сходиться,
и проверка покажет первую испорченную запись. Блокчейн для этого не нужен: хватает одной цепочки в той же базе.
Чтобы нельзя было пересчитать всю цепочку целиком, банк может раз в сутки передавать последний отпечаток
наружу (например, в Антифрод-центр или нотариусу); в демо этого нет.
"""
import hashlib
import json

START = "0" * 64        # отпечаток «перед первой записью»


def _digest(prev: str, row) -> str:
    body = json.dumps([row["id"], row["at"], row["actor"], row["action"], row["case_id"], row["transfer_id"],
                       row["details"]], ensure_ascii=False)
    return hashlib.sha256((prev + body).encode()).hexdigest()


def seal(conn) -> int:
    """Досчитывает отпечатки новых записей по порядку номеров. Возвращает, сколько записей запечатано."""
    last = conn.execute("SELECT hash FROM events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else START
    rows = conn.execute("SELECT * FROM events WHERE hash IS NULL ORDER BY id").fetchall()
    for r in rows:
        prev = _digest(prev, r)
        conn.execute("UPDATE events SET hash=? WHERE id=?", (prev, r["id"]))
    return len(rows)


def verify(conn) -> dict:
    """Проверяет всю цепочку. Если запись изменили или удалили, называет первую запись, где цепочка рвётся."""
    prev, count, broken = START, 0, None
    for r in conn.execute("SELECT * FROM events ORDER BY id"):
        count += 1
        if broken is None and r["hash"] != _digest(prev, r):
            broken = {"id": r["id"], "at": r["at"], "action": r["action"], "case_id": r["case_id"]}
        prev = r["hash"] or prev
    return {"ok": broken is None, "count": count, "broken": broken, "last": prev[:16]}
