# -*- coding: utf-8 -*-
"""Фоновый обход apimail: пополнение реестра алиасов и история кодов.

Цикл: история воркспейса → catch-all (mails_unknow) → по каждому алиасу
(ротацией, порциями) выборка писем и извлечение кодов. Всё складывается в
data/codes.jsonl и data/aliases.json, поэтому история не теряется при рестарте.
"""
import threading
import time
from pathlib import Path

from . import history as history_mod
from .client import MailAuthError, MailError
from .gmail import GmailAuthError, GmailError

MAIL_PAGE_LIMIT = 20


def _id_num(value):
    """Числовая часть id письма (Gmail-id вида 'g123' → 123; int/None → как есть)."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        return int(digits) if digits else 0


class Poller:
    def __init__(self, cfg, store, client, log, gmail=None, indexer=None):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.gmail = gmail
        self.indexer = indexer
        self.log = log
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._cursor = 0
        self._running_cycle = threading.Lock()
        self._auth_blocked = False
        self.last_cycle = {"started_at": None, "finished_at": None, "aliases": 0,
                           "new_codes": 0, "new_mails": 0, "mode": "unknown",
                           "error": ""}

    # ── жизненный цикл ────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="apimail-poller",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def kick(self):
        """Разбудить цикл сейчас (POST /api/refresh)."""
        self._wake.set()

    def _run(self):
        interval = max(5, int(self.cfg.get("poll_interval_sec") or 25))
        self.log.info(f"фоновый обход запущен (интервал {interval}s)")
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # цикл не должен умирать
                self.log.bad(f"цикл обхода упал: {type(exc).__name__}: {exc}")
                self.store.set_state(last_poll=time.time(), last_poll_ok=False,
                                     last_error=f"{type(exc).__name__}: {exc}")
                self.store.save()
            self._wake.wait(interval)
            self._wake.clear()
        self.log.info("фоновый обход остановлен")

    # ── один цикл ─────────────────────────────────────────
    def cycle(self):
        if not self._running_cycle.acquire(blocking=False):
            return
        started = time.time()
        self._auth_blocked = False
        failed = False
        self.last_cycle.update(started_at=started, error="", new_codes=0,
                               new_mails=0, aliases=0)
        try:
            self._health()
            self._maybe_scan_history()
            if self.cfg.get("discover_unknown_mails") and self.client.admin_key:
                try:
                    self._scan_unknown()
                except MailAuthError as exc:
                    # ключ отбит: не долбим движок всеми алиасами, но JWT-адреса проверяем
                    self._auth_blocked = True
                    self._mark_auth_error(exc)
            self._refresh_aliases()
            self._scan_gmail()
            self._sync_index()
        except MailAuthError as exc:
            failed = True
            self._mark_auth_error(exc)
        except MailError as exc:
            failed = True
            self.log.bad(f"apimail недоступен: {exc}")
            self.store.set_state(last_poll=time.time(), last_poll_ok=False,
                                 last_error=str(exc)[:200],
                                 mail_service={"ok": False, "detail": str(exc)[:200],
                                               "mode": "unknown"})
        finally:
            self.last_cycle["finished_at"] = time.time()
            if not failed and not self._auth_blocked:
                self.store.set_state(last_error="")
            state = self.store.get_state()
            ok = not state.get("last_error")
            self.store.set_state(last_poll=time.time(), last_poll_ok=bool(ok),
                                 poll_count=int(state.get("poll_count") or 0) + 1)
            self.store.save()
            self._running_cycle.release()
            took = self.last_cycle["finished_at"] - started
            self.log.info(
                f"цикл: алиасов {self.last_cycle['aliases']}, новых писем "
                f"{self.last_cycle['new_mails']}, новых кодов {self.last_cycle['new_codes']}, "
                f"{took:.1f}s")

    def _health(self):
        try:
            ok, detail = self.client.health()
        except MailError as exc:
            ok, detail = False, str(exc)[:120]
        mode = self.client.mode()
        self.store.set_state(mail_service={
            "ok": ok,
            "detail": detail or ("движок отвечает" if ok else "нет ответа"),
            "mode": mode,
        })

    def _mark_auth_error(self, exc):
        msg = (f"ключ/токен не принят движком: {exc}. "
               "Проверь admin_key в config.json (или задай address_jwts).")
        self.log.bad(msg)
        self.store.set_state(last_error=msg[:300], last_poll_ok=False,
                             mail_service={"ok": True, "detail": str(exc)[:180],
                                           "mode": "auth_error"})

    # ── история воркспейса ────────────────────────────────
    def _maybe_scan_history(self):
        if not self.cfg.get("history_scan"):
            return
        interval = int(self.cfg.get("history_scan_interval_sec") or 900)
        last = self.store.get_state().get("history_scan_at") or 0
        if time.time() - last < interval:
            return
        root = Path(self.cfg["root_dir"]).parent
        domains = self.cfg.get("mail_domains") or [self.cfg["mail_domain"]]
        result = history_mod.scan_history(
            root, domains,
            import_log_codes=bool(self.cfg.get("import_log_codes")),
            log=self.log)
        added = 0
        for addr in result["aliases"]:
            if addr not in self.store.aliases:
                self.store.add_alias(addr, source="history")
                added += 1
        imported = 0
        if self.cfg.get("import_log_codes"):
            for rec in result["codes"]:
                if not rec.get("address"):
                    continue
                self.store.add_alias(rec["address"], source="history")
                if self.store.append_code(dict(rec, address=rec["address"])):
                    imported += 1
        self.store.set_state(history_scan_at=time.time())
        self.log.info(f"история: просмотрено файлов {result['scanned']}, "
                      f"адресов новых {added}, кодов из логов {imported}")

    # ── catch-all: письма на адреса вне реестра движка ─────
    def _scan_unknown(self):
        limit = int(self.cfg.get("unknown_scan_limit") or 50)
        try:
            rows, _count = self.client.admin_unknown_mails(limit=limit, offset=0)
        except MailAuthError:
            raise
        except MailError as exc:
            self.log.warn(f"mails_unknow недоступен: {exc}")
            return
        added = 0
        for row in rows:
            item = _row_to_item(row)
            if not item or not item["address"]:
                continue
            known = item["address"] in self.store.aliases
            rec = self.store.add_alias(item["address"], source="catch-all")
            if not known:
                added += 1
            self._absorb(item, rec)
        if rows:
            self.log.info(f"catch-all: писем просмотрено {len(rows)}, "
                          f"новых адресов {added}")

    # ── ротация по алиасам ────────────────────────────────
    def _refresh_aliases(self):
        for addr in self._jwts():
            if addr != "*" and addr not in self.store.aliases:
                # адрес, для которого есть JWT, — точно наш ящик: держим в реестре
                self.store.add_alias(addr, source="jwt", note="адресный JWT из config.json")
        aliases = self.store.list_aliases(sort="address")
        if not aliases:
            return
        batch = max(1, int(self.cfg.get("aliases_per_cycle") or 25))
        total = len(aliases)
        chunk = [aliases[(self._cursor + i) % total] for i in range(min(batch, total))]
        self._cursor = (self._cursor + len(chunk)) % total
        for rec in chunk:
            if self._stop.is_set():
                break
            has_jwt = bool(self._jwts().get(rec["address"]) or self._jwts().get("*"))
            if self._auth_blocked and not has_jwt:
                continue
            try:
                self.refresh_alias(rec["address"])
                self.last_cycle["aliases"] += 1
            except MailAuthError as exc:
                if has_jwt:
                    raise
                self._auth_blocked = True
                self._mark_auth_error(exc)
            except MailError as exc:
                self.log.warn(f"{rec['address']}: {exc}")
                self.store.update_alias(rec["address"], last_checked=time.time(),
                                        last_error=str(exc)[:120])

    def _jwts(self):
        return self.cfg.get("address_jwts") or {}

    def _is_gmail_addr(self, address):
        """Адрес относится к Gmail-источнику (плюс-алиас нашего ящика)?"""
        if not self.gmail or not self.gmail.configured():
            return False
        addr = (address or "").lower()
        if "@" not in addr:
            return False
        dom = addr.partition("@")[2]
        if dom not in ("gmail.com", "googlemail.com"):
            return False
        # только алиасы нашего ящика (user+...@), а не чужие gmail-адреса
        base = (self.gmail.user or "").lower()
        return bool(base) and addr.startswith(base.partition("@")[0] + "+")

    def refresh_alias(self, address, limit=MAIL_PAGE_LIMIT):
        """Одна выборка по адресу: новые письма → коды → активность.

        Возвращает {"items": [...], "count": N, "new_mails": N, "new_codes": N,
                    "mode": "admin|jwt|gmail"}.
        """
        rec = self.store.alias(address) or self.store.add_alias(address, source="manual")
        max_id = int(rec.get("max_mail_id") or 0)
        if self._is_gmail_addr(address):
            items, count = self.gmail.list_mails(address, limit=limit, offset=0)
            mode = "gmail"
        else:
            items, count, mode = self.client.list_mails(address, limit=limit, offset=0)
        new_mails, new_codes = 0, 0
        for item in items:
            item_id = _id_num(item.get("id"))
            if item_id <= max_id:
                continue
            new_mails += 1
            max_id = max(max_id, item_id)
            self._absorb(item, self.store.alias(address) or rec, count_new=True)
            if item.get("code"):
                new_codes += 1
        fields = {
            "last_checked": time.time(),
            "max_mail_id": max_id,
            "source": "catch-all" if rec.get("source") == "catch-all" else rec.get("source", "manual"),
        }
        if count is not None:
            fields["mails"] = int(count)
        elif new_mails:
            fields["mails"] = int(rec.get("mails") or 0) + new_mails
        if items:
            newest = max(items, key=lambda i: i.get("date_ts") or 0)
            stamp = newest.get("date_ts") or time.time()
            fields["last_mail_at"] = max(float(rec.get("last_mail_at") or 0), stamp)
        if new_codes:
            coded = [i for i in items if i.get("code")]
            if coded:
                best = max(coded, key=lambda i: i.get("date_ts") or 0)
                fields["last_code"] = best["code"]
                fields["last_code_at"] = best.get("date_ts") or time.time()
        fields.pop("last_error", None)
        self.store.update_alias(address, **fields)
        self.last_cycle["new_mails"] += new_mails
        if new_codes:
            self.log.ok(f"{address}: новых кодов {new_codes}")
        return {"items": items, "count": count, "new_mails": new_mails,
                "new_codes": new_codes, "mode": mode}

    def _absorb(self, item, rec, count_new=False):
        """Записать письмо в историю кодов и обновить активность алиаса."""
        if not item.get("code"):
            return
        address = item.get("address") or rec.get("address")
        self.store.add_alias(address, source=rec.get("source") or "manual")
        added = self.store.append_code({
            "address": address,
            "code": item["code"],
            "subject": item.get("subject") or "",
            "from": item.get("from") or "",
            "received_at": item.get("received_at") or "",
            "mail_id": item.get("id"),
            "source": item.get("source") or "apimail",
            "ts": item.get("date_ts") or time.time(),
        })
        if added:
            self.last_cycle["new_codes"] += 1
        self.store.update_alias(address,
                                last_code=item["code"],
                                last_code_at=item.get("date_ts") or time.time(),
                                last_mail_at=max(
                                    float((self.store.alias(address) or {}).get("last_mail_at") or 0),
                                    float(item.get("date_ts") or time.time())),
                                max_mail_id=max(
                                    int((self.store.alias(address) or {}).get("max_mail_id") or 0),
                                    _id_num(item.get("id"))),
                                last_checked=time.time())

    # ── полнотекстовый индекс (FTS5) ──────────────────────
    def _sync_index(self):
        """Дотянуть страницы глобального списка в FTS-индекс (data/mailindex.db)."""
        if not self.indexer or not self.client.admin_key:
            return
        if not self.cfg.get("index_enabled"):
            return
        pages = int(self.cfg.get("index_pages_per_cycle") or 4)
        limit = int(self.cfg.get("index_page_limit") or 100)
        try:
            stats = self.indexer.sync(self.client, pages=pages, limit=limit)
        except MailAuthError:
            raise
        except MailError as exc:
            self.log.warn(f"индекс: apimail недоступен ({exc})")
            return
        if stats["indexed_new"]:
            self.log.info(f"индекс: +{stats['indexed_new']} писем "
                          f"(просмотрено {stats['scanned']})")

    # ── Gmail: плюс-алиасы одного ящика (IMAP) ────────────
    def _scan_gmail(self):
        """Обход Gmail-ящика: письма на плюс-алиасы → реестр + коды."""
        if not self.gmail or not self.gmail.configured():
            return
        try:
            items, _count = self.gmail.list_mails(limit=50, offset=0)
        except GmailAuthError as exc:
            self.log.warn(f"gmail: логин не принят — {exc}")
            return
        except GmailError as exc:
            self.log.warn(f"gmail недоступен: {exc}")
            return
        added = 0
        for item in items:
            addr = (item.get("address") or "").lower()
            if not addr or "@" not in addr:
                continue
            known = addr in self.store.aliases
            rec = self.store.add_alias(addr, source="gmail")
            if not known:
                added += 1
            self._absorb(item, rec)
        if added:
            self.log.info(f"gmail: новых алиасов {added}")


def _row_to_item(row):
    """Строка raw_mails → item (без запроса одного письма)."""
    from .client import normalize_row
    address = (row.get("address") or "").strip().lower()
    if not address:
        return None
    return normalize_row(row, address)