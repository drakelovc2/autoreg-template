# -*- coding: utf-8 -*-
"""Полнотекстовый индекс писем: SQLite FTS5 (data/mailindex.db).

Зачем: найти «на какую почту зареган ник» — поиск по СОДЕРЖИМОМУ писем
(тема/тело/адрес), а не только по адресу и коду. Движок сам такого не умеет,
поэтому панель инкрементально вытягивает глобальный список /admin/mails
(все адреса catch-all), декодирует MIME и кладёт текст в FTS5-индекс.

Синхронизация — циклическая прокрутка offset: каждый вызов sync() берёт
несколько страниц (бюджет `pages`), дойдя до конца списка — начинает с нуля.
Уже проиндексированные письма пропускаются без разбора MIME (по mail_id),
поэтому повторные проходы дешёвые, а пропуски при сдвиге offset самолечатся.
Новые письма попадают в индекс на первых же страницах (список свежие-вперёд).
"""
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import parse as parse_mod
from .client import MailError

INDEX_FILE = "mailindex.db"
BODY_CAP = 200_000  # тело в индексе режем: письма-простыни не нужны целиком

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS mails(
  mail_id     INTEGER PRIMARY KEY,
  address     TEXT,
  subject     TEXT,
  sender      TEXT,
  date_ts     INTEGER,
  received_at TEXT,
  code        TEXT,
  body        TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS mails_fts USING fts5(
  address, subject, body,
  content='mails', content_rowid='mail_id'
);
CREATE TRIGGER IF NOT EXISTS mails_ai AFTER INSERT ON mails BEGIN
  INSERT INTO mails_fts(rowid, address, subject, body)
  VALUES (new.mail_id, new.address, new.subject, new.body);
END;
CREATE TRIGGER IF NOT EXISTS mails_ad AFTER DELETE ON mails BEGIN
  INSERT INTO mails_fts(mails_fts, rowid, address, subject, body)
  VALUES ('delete', old.mail_id, old.address, old.subject, old.body);
END;
CREATE TRIGGER IF NOT EXISTS mails_au AFTER UPDATE ON mails BEGIN
  INSERT INTO mails_fts(mails_fts, rowid, address, subject, body)
  VALUES ('delete', old.mail_id, old.address, old.subject, old.body);
  INSERT INTO mails_fts(rowid, address, subject, body)
  VALUES (new.mail_id, new.address, new.subject, new.body);
END;
"""

_WS_RE = re.compile(r"\s+")


def _id_num(value):
    """Числовой id письма (int-строка → int; мусор → 0 = не индексируем)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fts_query(query):
    """Собрать FTS5 MATCH: каждое слово — фраза в кавычках, между ними AND."""
    tokens = [t for t in _WS_RE.split((query or "").strip()) if t]
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)


class MailIndex:
    def __init__(self, data_dir, log=None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / INDEX_FILE
        self.log = log
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()
        self.last_sync = {"at": None, "indexed_new": 0, "scanned": 0,
                          "pages": 0, "error": ""}

    # ── мета-курсор ───────────────────────────────────────
    def _meta_get(self, key, default=""):
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _meta_set(self, key, value):
        self._db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value)))

    # ── запись ────────────────────────────────────────────
    def has(self, mail_id):
        row = self._db.execute("SELECT 1 FROM mails WHERE mail_id=?",
                               (mail_id,)).fetchone()
        return row is not None

    def index_mail(self, mail_id, address, parsed):
        """Записать одно письмо (parsed = parse_mail()). True — новое."""
        mail_id = _id_num(mail_id)
        if not mail_id:
            return False
        with self._lock:
            if self.has(mail_id):
                return False
            body = (parsed.get("body") or "")[:BODY_CAP]
            self._db.execute(
                "INSERT INTO mails(mail_id, address, subject, sender, date_ts,"
                "                   received_at, code, body)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (mail_id, (address or "").lower(), parsed.get("subject") or "",
                 parsed.get("from") or "", parsed.get("date_ts"),
                 parsed.get("received_at") or "", parsed.get("code") or "", body))
            self._db.commit()
            return True

    def index_row(self, row):
        """Строка /admin/mails ({id, address, raw}) → в индекс. True — новое."""
        mail_id = _id_num(row.get("id"))
        if not mail_id:
            return False
        with self._lock:
            if self.has(mail_id):
                return False
        parsed = parse_mod.parse_mail(row.get("raw") or "")
        return self.index_mail(mail_id, row.get("address"), parsed)

    # ── синхронизация с движком ───────────────────────────
    def sync(self, client, pages=4, limit=100):
        """Дотянуть страницы глобального списка /admin/mails в индекс.

        Циклический offset: дошли до конца (страница короче limit или offset
        накрыл count) — следующая синхронизация начнёт с нуля. Так новые
        письма попадают сразу (голова списка сканируется каждый круг), а
        история доиндексируется по мере кругов.
        """
        stats = {"indexed_new": 0, "scanned": 0, "pages": 0, "error": ""}
        with self._lock:
            offset = int(self._meta_get("backfill_offset", "0") or 0)
            try:
                for _ in range(max(1, int(pages))):
                    rows, count = client.admin_all_mails(limit=limit, offset=offset)
                    stats["pages"] += 1
                    if not rows:
                        offset = 0
                        break
                    for row in rows:
                        stats["scanned"] += 1
                        if self.index_row(row):
                            stats["indexed_new"] += 1
                    offset += len(rows)
                    # конец списка — короткая страница; count движок отдаёт
                    # только на первой странице (на offset>0 приходит 0) —
                    # поэтому count=0 трактуем как «неизвестно», а не «конец»
                    if len(rows) < limit or ((count or 0) > 0 and offset >= count):
                        offset = 0  # круг пройден — с головы на следующем цикле
                        self._meta_set("last_wrap_at", int(time.time()))
                        break
                self._meta_set("backfill_offset", offset)
                self._db.commit()
            except MailError as exc:
                self._db.commit()
                stats["error"] = str(exc)[:200]
                raise
            finally:
                stats["at"] = time.time()
                self.last_sync = dict(stats)
        return stats

    # ── поиск ─────────────────────────────────────────────
    def search(self, query, limit=50):
        """Полнотекстовый поиск по адресу/теме/телу. Без учёта регистра.

        Возвращает (rows, mode): mode = 'fts' | 'like'. FTS-запрос с
        экранированными фразами; при битом синтаксисе (спецсимволы) —
        fallback на подстроку instr(lower(...)).
        """
        q = (query or "").strip()
        if not q:
            return [], "fts"
        limit = max(1, min(int(limit or 50), 200))
        match = _fts_query(q)
        with self._lock:
            if match:
                try:
                    rows = self._db.execute(
                        "SELECT m.mail_id, m.address, m.subject, m.sender,"
                        "       m.date_ts, m.received_at, m.code,"
                        "       snippet(mails_fts, 2, '<b>', '</b>', ' … ', 14) AS snippet"
                        "  FROM mails_fts f JOIN mails m ON m.mail_id = f.rowid"
                        " WHERE mails_fts MATCH ?"
                        " ORDER BY m.date_ts DESC LIMIT ?", (match, limit)).fetchall()
                    return [self._view(r) for r in rows], "fts"
                except sqlite3.OperationalError:
                    pass  # экзотика в запросе — уходим в подстроку
            needle = q.lower()
            rows = self._db.execute(
                "SELECT mail_id, address, subject, sender, date_ts, received_at,"
                "       code, body FROM mails"
                " WHERE instr(lower(address), ?) OR instr(lower(subject), ?)"
                "    OR instr(lower(body), ?)"
                " ORDER BY date_ts DESC LIMIT ?",
                (needle, needle, needle, limit)).fetchall()
            return [self._view(r, needle=needle) for r in rows], "like"

    def _view(self, row, needle=None):
        body = ""
        snippet = ""
        if "snippet" in row.keys():
            snippet = row["snippet"] or ""
        else:
            body = row["body"] if "body" in row.keys() else ""
        if needle and body:
            pos = body.lower().find(needle)
            if pos >= 0:
                start = max(0, pos - 60)
                snippet = ("… " if start else "") + body[start:pos + 120] + " …"
        return {
            "id": row["mail_id"],
            "address": row["address"],
            "subject": row["subject"] or "(без темы)",
            "from": row["sender"],
            "received_at": row["received_at"],
            "date_ts": row["date_ts"],
            "code": row["code"] or None,
            "snippet": _WS_RE.sub(" ", snippet).strip(),
        }

    # ── состояние ─────────────────────────────────────────
    def stats(self):
        with self._lock:
            indexed = self._db.execute("SELECT COUNT(*) AS c FROM mails").fetchone()["c"]
            newest = self._db.execute(
                "SELECT MAX(date_ts) AS ts FROM mails").fetchone()["ts"]
            try:
                db_bytes = self.path.stat().st_size
            except OSError:
                db_bytes = 0
            offset = int(self._meta_get("backfill_offset", "0") or 0)
            return {
                "indexed": indexed,
                "offset": offset,
                "newest_ts": newest,
                "db_bytes": db_bytes,
                "last_sync": dict(self.last_sync),
                "last_wrap_at": int(self._meta_get("last_wrap_at", "0") or 0),
            }

    def reset(self):
        """Полная переиндексация: очистить таблицы и курсор."""
        with self._lock:
            self._db.execute("DELETE FROM mails")
            self._db.execute("INSERT INTO mails_fts(mails_fts) VALUES ('rebuild')")
            self._meta_set("backfill_offset", 0)
            self._meta_set("last_wrap_at", 0)
            self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
