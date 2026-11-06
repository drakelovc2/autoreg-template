# -*- coding: utf-8 -*-
"""Хранилище панели: реестр алиасов (aliases.json) и история кодов (codes.jsonl).

Всё пишется атомарно (.tmp + os.replace), история не теряется при рестарте.
"""
import json
import os
import threading
import time
from pathlib import Path

ALIASES_FILE = "aliases.json"
CODES_FILE = "codes.jsonl"
CODES_MEMORY = 2000


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_append_line(path, line):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class Store:
    def __init__(self, data_dir, log=None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.aliases_path = self.dir / ALIASES_FILE
        self.codes_path = self.dir / CODES_FILE
        self.log = log
        self._lock = threading.RLock()
        self.aliases = {}
        self.state = {
            "created_at": time.time(),
            "last_poll": None,
            "last_poll_ok": None,
            "last_error": "",
            "poll_count": 0,
            "mail_service": {"ok": None, "detail": "не проверялся", "mode": "unknown"},
            "history_scan_at": None,
        }
        self.codes = []          # новые сверху
        self._code_keys = set()  # (address, mail_id) для дедупа
        self._dirty = False
        self.load()

    # ── загрузка/сохранение ───────────────────────────────
    def load(self):
        with self._lock:
            if self.aliases_path.exists():
                try:
                    with open(self.aliases_path, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    self.aliases = data.get("aliases") or {}
                    state = data.get("state") or {}
                    self.state.update(state)
                except (OSError, json.JSONDecodeError) as exc:
                    if self.log:
                        self.log.warn(f"aliases.json не прочитан: {exc}")
            if self.codes_path.exists():
                try:
                    with open(self.codes_path, "r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            key = code_key(rec)
                            if key in self._code_keys:
                                continue
                            self._code_keys.add(key)
                            self.codes.append(rec)
                    self.codes.sort(key=lambda r: r.get("ts") or 0, reverse=True)
                    self.codes = self.codes[:CODES_MEMORY]
                except OSError as exc:
                    if self.log:
                        self.log.warn(f"codes.jsonl не прочитан: {exc}")
            if self.log:
                self.log.info(f"хранилище: алиасов {len(self.aliases)}, кодов {len(self.codes)}")

    def save(self, force=False):
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "version": 1,
                "updated_at": time.time(),
                "state": self.state,
                "aliases": self.aliases,
            }
            atomic_write_json(self.aliases_path, payload)
            self._dirty = False

    def touch(self):
        with self._lock:
            self._dirty = True

    # ── алиасы ────────────────────────────────────────────
    def add_alias(self, address, source="manual", note=""):
        address = (address or "").strip().lower()
        if not address or "@" not in address:
            raise ValueError("нужен адрес вида name@example.com")
        with self._lock:
            rec = self.aliases.get(address)
            if rec is None:
                local, _, domain = address.partition("@")
                rec = {
                    "address": address,
                    "prefix": local[:6],
                    "domain": domain,
                    "subdomain": domain.split(".")[0] if domain.count(".") >= 2 else "",
                    "source": source,
                    "added_at": time.time(),
                    "mails": 0,
                    "codes": 0,
                    "max_mail_id": 0,
                    "last_mail_at": None,
                    "last_code": None,
                    "last_code_at": None,
                    "last_checked": None,
                    "note": note or "",
                }
                self.aliases[address] = rec
                self._dirty = True
            elif note and not rec.get("note"):
                rec["note"] = note
                self._dirty = True
            if source and rec.get("source") == "history" and source != "history":
                rec["source"] = source
                self._dirty = True
            return rec

    def remove_alias(self, address):
        address = (address or "").strip().lower()
        with self._lock:
            if address in self.aliases:
                self.aliases.pop(address)
                self._dirty = True
                return True
            return False

    def list_aliases(self, sort="recent"):
        with self._lock:
            items = [dict(rec) for rec in self.aliases.values()]
        if sort == "recent":
            items.sort(key=lambda r: (r.get("last_mail_at") or 0,
                                     r.get("added_at") or 0), reverse=True)
        else:
            items.sort(key=lambda r: r.get("address") or "")
        return items

    def alias(self, address):
        with self._lock:
            rec = self.aliases.get((address or "").strip().lower())
            return dict(rec) if rec else None

    def update_alias(self, address, **fields):
        with self._lock:
            rec = self.aliases.get((address or "").strip().lower())
            if rec is None:
                return None
            rec.update(fields)
            self._dirty = True
            return dict(rec)

    def set_state(self, **fields):
        with self._lock:
            self.state.update(fields)
            self._dirty = True

    def get_state(self):
        with self._lock:
            return dict(self.state)

    # ── коды ──────────────────────────────────────────────
    def append_code(self, rec):
        """Добавить код в историю (дедуп по address+mail_id). True — новый."""
        rec = dict(rec)
        rec.setdefault("ts", time.time())
        key = code_key(rec)
        with self._lock:
            if key in self._code_keys:
                return False
            self._code_keys.add(key)
            self.codes.append(rec)
            self.codes.sort(key=lambda r: r.get("ts") or 0, reverse=True)
            del self.codes[CODES_MEMORY:]
        atomic_append_line(self.codes_path, json.dumps(rec, ensure_ascii=False))
        return True

    def last_code(self, address=None):
        address = (address or "").strip().lower() or None
        with self._lock:
            for rec in self.codes:
                if not rec.get("code"):
                    continue
                if address and (rec.get("address") or "").lower() != address:
                    continue
                return dict(rec)
        return None

    def feed(self, address=None, only_codes=False, limit=50, offset=0):
        address = (address or "").strip().lower() or None
        with self._lock:
            rows = list(self.codes)
        if address:
            rows = [r for r in rows if (r.get("address") or "").lower() == address]
        if only_codes:
            rows = [r for r in rows if r.get("code")]
        total = len(rows)
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            offset = 0
        return [dict(r) for r in rows[offset:offset + limit]], total

    def search(self, query):
        """Поиск по адресу/префиксу/коду: совпадения в реестре и в истории."""
        q = (query or "").strip().lower()
        if not q:
            return []
        with self._lock:
            aliases = [dict(r) for r in self.aliases.values()]
            codes = list(self.codes)
        found = {}
        for rec in aliases:
            addr = (rec.get("address") or "").lower()
            if q in addr:
                item = found.setdefault(addr, _search_item(rec["address"]))
                item["alias"] = True
                item.update({k: rec.get(k) for k in
                             ("mails", "codes", "last_mail_at", "last_code",
                              "last_code_at", "source", "added_at", "note")})
        for rec in codes:
            addr = (rec.get("address") or "").lower()
            code = str(rec.get("code") or "")
            if q in addr or (q.isdigit() and q and q in code):
                item = found.setdefault(addr or "(без адреса)", _search_item(rec.get("address") or ""))
                item["codes"] = (item.get("codes") or 0) + 1
                if rec.get("ts") and (not item.get("last_code_at") or rec["ts"] > item["last_code_at"]):
                    item["last_code_at"] = rec["ts"]
                    item["last_code"] = rec.get("code")
                    item["last_mail_at"] = item.get("last_mail_at") or rec.get("ts")
                item["matched_codes"] = (item.get("matched_codes") or [])
                if code and q.isdigit() and q in code and len(item["matched_codes"]) < 5:
                    item["matched_codes"].append(code)
        out = list(found.values())
        out.sort(key=lambda r: (r.get("last_mail_at") or 0, r.get("alias") or False), reverse=True)
        return out

    # ── сводка ────────────────────────────────────────────
    def summary_counts(self):
        with self._lock:
            aliases = list(self.aliases.values())
            codes = list(self.codes)
        live = [a for a in aliases if (a.get("mails") or 0) > 0]
        total_mails = sum(int(a.get("mails") or 0) for a in aliases)
        total_codes = sum(1 for c in codes if c.get("code"))
        return {
            "aliases": len(aliases),
            "aliases_live": len(live),
            "aliases_empty": len(aliases) - len(live),
            "mails": total_mails,
            "codes": total_codes,
            "codes_seen": len(codes),
        }


def _search_item(address):
    return {
        "address": address,
        "alias": False,
        "mails": 0,
        "codes": 0,
        "last_mail_at": None,
        "last_code": None,
        "last_code_at": None,
        "matched_codes": [],
    }


def code_key(rec):
    address = (rec.get("address") or "").lower()
    mail_id = rec.get("mail_id")
    if mail_id not in (None, "", 0):
        return f"{address}|m{mail_id}"
    return f"{address}|c{rec.get('code')}|{int(rec.get('ts') or 0) // 300}"