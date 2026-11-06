#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apimail-panel — catch-all mailbox panel (apimail, catch-all example.com).

python stdlib: http.server + threading. Только 127.0.0.1, свой порт (9971).

API (JSON):
  GET    /api/summary                 — жив ли apimail, сколько алиасов/писем, последний код
  GET    /api/last-code               — {code, address, subject, received_at, mail_id}
  GET    /api/mails?address=&limit=&offset=  — лента писем (с извлечённым кодом)
  GET    /api/mail?address=&id=       — одно письмо целиком (MIME/тело)
  GET    /api/search?q=               — поиск по адресу/префиксу/коду
  GET    /api/search_body?q=&limit=   — полнотекстовый поиск по содержимому писем (FTS5)
  GET    /api/index/stats             — состояние FTS-индекса (сколько писем проиндексировано)
  POST   /api/index/sync {pages}      — доиндексировать сейчас (синхронно, N страниц)
  POST   /api/index/reset             — очистить индекс (переиндексация с нуля)
  GET    /api/aliases                 — реестр с активностью
  POST   /api/aliases {address, note} — добавить алиас
  DELETE /api/aliases?address=        — убрать алиас
  POST   /api/refresh                 — принудительный обход (фоновый поток)
  GET    /api/logs?tail=N             — хвост лога панели
"""
import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import __version__, config as config_mod      # noqa: E402
from core.client import MailAuthError, MailError, MailClient  # noqa: E402
from core.logbus import LogBus                          # noqa: E402
from core.poller import Poller                          # noqa: E402
from core.indexer import MailIndex                      # noqa: E402
from core.store import Store                            # noqa: E402
from core.gmail import GmailClient, GmailError, GmailAuthError  # noqa: E402

STATIC_DIR = ROOT / "panel" / "static"
DEFAULT_PORT = 9971


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = LogBus(Path(cfg["data_dir"]) / "panel.log")
        self.store = Store(cfg["data_dir"], log=self.log)
        self.client = MailClient(
            cfg["mail_base"], cfg.get("admin_key") or "",
            address_jwts=cfg.get("address_jwts") or {},
            timeout=int(cfg.get("request_timeout_sec") or 25),
            log=self.log)
        self.gmail = GmailClient(cfg.get("gmail") or {}, log=self.log)
        self.index = MailIndex(cfg["data_dir"], log=self.log)
        self.poller = Poller(cfg, self.store, self.client, self.log, gmail=self.gmail,
                             indexer=self.index)
        self.started_at = time.time()
        self.port = None

    def start(self):
        if self.cfg.get("_config_error"):
            self.log.bad(self.cfg["_config_error"])
        self.log.info(f"панель запущена: {self.cfg['mail_base']} "
                      f"(домены {', '.join(self.cfg.get('mail_domains') or [self.cfg['mail_domain']])}, ключ "
                      f"{config_mod.masked_key(self.cfg.get('admin_key') or '') or 'нет'})")
        if not self.cfg.get("admin_key"):
            self.log.warn("admin_key не задан — работает только JWT-режим адресов")
        self.poller.start()

    def stop(self):
        self.poller.stop()
        self.index.close()

    # ── сводки ────────────────────────────────────────────
    def summary(self):
        state = self.store.get_state()
        counts = self.store.summary_counts()
        last = self.store.last_code()
        service = dict(state.get("mail_service") or {})
        gmail_cfg = self.cfg.get("gmail") or {}
        service.update({
            "base": self.cfg["mail_base"],
            "domain": self.cfg["mail_domain"],
            "domains": self.cfg.get("mail_domains") or [self.cfg["mail_domain"]],
            "last_poll": state.get("last_poll"),
            "last_poll_str": _fmt(state.get("last_poll")),
            "poll_count": state.get("poll_count") or 0,
            "interval_sec": self.cfg.get("poll_interval_sec"),
            "last_error": state.get("last_error") or "",
            "poller_running": self.poller.running(),
            "last_cycle": dict(self.poller.last_cycle),
            "gmail": {
                "enabled": bool(gmail_cfg.get("enabled")),
                "configured": self.gmail.configured(),
                "user": gmail_cfg.get("user") or "",
                "alias_prefix": gmail_cfg.get("alias_prefix") or "",
            },
        })
        return {
            "service": "apimail-panel",
            "version": __version__,
            "running": True,
            "port": self.port,
            "uptime_sec": int(time.time() - self.started_at),
            "mail": service,
            "counts": counts,
            "last_code": _code_view(last),
            "registry": {
                "history_scan_at": state.get("history_scan_at"),
                "history_scan_str": _fmt(state.get("history_scan_at")),
                "discover_unknown_mails": bool(self.cfg.get("discover_unknown_mails")),
                "history_scan": bool(self.cfg.get("history_scan")),
                "admin_key_set": bool(self.cfg.get("admin_key")),
                "admin_key_masked": config_mod.masked_key(self.cfg.get("admin_key") or ""),
                "jwts": len(self.cfg.get("address_jwts") or {}),
            },
            "server_time": _fmt(time.time()),
            "index": self.index.stats(),
        }

    def mails(self, address, limit, offset, with_raw=False, register=False):
        """Лента писем: без адреса — ВСЕ письма движка (catch-all целиком);
        с адресом — письма одного адреса; при отказе движка — история кодов."""
        # Без адреса: живой глобальный список всех писем (если есть admin_key)
        if not address:
            try:
                items, count, mode = self.client.list_all_mails(
                    limit=limit, offset=offset, with_raw=with_raw)
                for item in items:
                    item["source"] = "apimail"
                    item["mode"] = mode
                return {"address": None, "source": "apimail", "results": items,
                        "count": count if count is not None else len(items),
                        "limit": limit, "offset": offset, "mode": mode,
                        "warning": ""}
            except (MailAuthError, MailError) as exc:
                history_items, history_total = self.store.feed(
                    address=None, limit=limit, offset=offset)
                return {"address": None, "source": "history",
                        "results": [_history_view(r) for r in history_items],
                        "count": history_total, "limit": limit, "offset": offset,
                        "warning": f"глобальный список недоступен: {exc}"}
        if register:
            self.store.add_alias(address, source="manual", note="из ленты")
        # Gmail-адрес (плюс-алиас) — читаем из Gmail, а не из apimail-движка
        if self.poller._is_gmail_addr(address):
            try:
                items, count = self.gmail.list_mails(address, limit=limit, offset=offset,
                                                     with_raw=with_raw)
                for item in items:
                    item["mode"] = "gmail"
                return {"address": address, "source": "gmail", "results": items,
                        "count": count if count is not None else len(items),
                        "limit": limit, "offset": offset, "mode": "gmail", "warning": ""}
            except (GmailAuthError, GmailError) as exc:
                history_items, history_total = self.store.feed(address=address,
                                                              limit=limit, offset=offset)
                return {"address": address, "source": "history",
                        "results": [_history_view(r) for r in history_items],
                        "count": history_total, "limit": limit, "offset": offset,
                        "warning": f"gmail недоступен: {exc}"}
        try:
            items, count, mode = self.client.list_mails(address, limit=limit,
                                                        offset=offset,
                                                        with_raw=with_raw)
        except (MailAuthError, MailError) as exc:
            history_items, history_total = self.store.feed(address=address,
                                                          limit=limit, offset=offset)
            return {"address": address, "source": "history",
                    "results": [_history_view(r) for r in history_items],
                    "count": history_total, "limit": limit, "offset": offset,
                    "warning": f"apimail не отдал письма: {exc}"}
        for item in items:
            item["source"] = "apimail"
            item["mode"] = mode
        return {"address": address, "source": "apimail", "results": items,
                "count": count if count is not None else len(items),
                "limit": limit, "offset": offset, "mode": mode, "warning": ""}

    def mail_raw(self, address, mail_id):
        data, mode = self.client.get_mail(address, mail_id, raw_only=True)
        if not data:
            raise MailError("письмо не найдено", 404)
        raw = data.get("raw") or ""
        from core import parse as parse_mod
        parsed = parse_mod.parse_mail(raw)
        return {
            "id": data.get("id") or mail_id,
            "address": data.get("address") or address,
            "mode": mode,
            "subject": parsed["subject"],
            "from": parsed["from"],
            "to": parsed["to"],
            "received_at": parsed["received_at"],
            "code": parsed["code"],
            "code_context": parsed["code_context"],
            "body": parsed["body"],
            "links": parsed.get("links") or [],
            "html": parsed.get("html") or "",
            "raw": raw,
            "raw_len": len(raw),
        }

    def search(self, query):
        results = self.store.search(query)
        q = (query or "").strip().lower()
        if "@" in q and "." in q and not any(r.get("address") == q for r in results):
            try:
                items, count, _mode = self.client.list_mails(q, limit=5, offset=0)
                codes = [i for i in items if i.get("code")]
                results.insert(0, {
                    "address": q, "alias": q in self.store.aliases, "mails": count or 0,
                    "codes": len(codes), "last_mail_at": max(
                        [i.get("date_ts") or 0 for i in items] or [0]) or None,
                    "last_code": codes[0]["code"] if codes else None,
                    "last_code_at": codes[0].get("date_ts") if codes else None,
                    "matched_codes": [i["code"] for i in codes[:5]],
                    "live": True,
                })
            except (MailAuthError, MailError) as exc:
                self.log.warn(f"поиск {q}: живых данных нет ({exc})")
        return {"query": query, "results": results, "count": len(results)}

    # ── полнотекстовый индекс ─────────────────────────────
    def search_body(self, query, limit=50):
        """Поиск по содержимому писем (ник/фраза → на каком адресе зареган)."""
        rows, mode = self.index.search(query, limit=limit)
        return {"query": query, "results": rows, "count": len(rows),
                "mode": mode, "index": self.index.stats()}

    def index_stats(self):
        return self.index.stats()

    def index_sync(self, pages=None):
        pages = int(pages or self.cfg.get("index_pages_per_cycle") or 4)
        pages = max(1, min(pages, 200))
        limit = int(self.cfg.get("index_page_limit") or 100)
        stats = self.index.sync(self.client, pages=pages, limit=limit)
        self.log.info(f"индекс: ручная синхронизация, +{stats['indexed_new']} писем "
                      f"(страниц {stats['pages']})")
        return {"synced": True, "sync": stats, "index": self.index.stats()}

    def index_reset(self):
        self.index.reset()
        self.log.warn("индекс писем очищен — переиндексация с нуля")
        return {"reset": True, "index": self.index.stats()}

    # ── алиасы ────────────────────────────────────────────
    def aliases(self):
        items = self.store.list_aliases()
        for item in items:
            item["last_mail_str"] = _fmt(item.get("last_mail_at"))
            item["last_code_str"] = _fmt(item.get("last_code_at"))
            item["last_checked_str"] = _fmt(item.get("last_checked"))
            item["has_jwt"] = bool((self.cfg.get("address_jwts") or {}).get(item["address"]))
        counts = self.store.summary_counts()
        return {"aliases": items, "count": len(items), "counts": counts}

    def add_alias(self, address, note=""):
        rec = self.store.add_alias(address, source="manual", note=note)
        self.store.save(force=True)
        self.log.ok(f"алиас добавлен: {rec['address']}")
        return {"added": True, "alias": rec}

    def remove_alias(self, address):
        if not address:
            raise ValueError("нужен параметр address")
        removed = self.store.remove_alias(address)
        self.store.save(force=True)
        if not removed:
            raise KeyError(address)
        self.log.info(f"алиас убран: {address}")
        return {"removed": True, "address": address}

    def refresh(self):
        self.log.info("принудительный обход (POST /api/refresh)")
        self.poller.kick()
        return {"started": True, "poller_running": self.poller.running(),
                "interval_sec": self.cfg.get("poll_interval_sec")}


def _fmt(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _code_view(rec):
    if not rec:
        return None
    return {
        "code": rec.get("code"),
        "address": rec.get("address"),
        "subject": rec.get("subject"),
        "from": rec.get("from"),
        "received_at": rec.get("received_at") or _fmt(rec.get("ts")),
        "mail_id": rec.get("mail_id"),
        "source": rec.get("source"),
        "ts": rec.get("ts"),
        "ago": _ago(rec.get("ts")),
    }


def _history_view(rec):
    return {
        "id": rec.get("mail_id"),
        "address": rec.get("address"),
        "subject": rec.get("subject") or "(без темы)",
        "from": rec.get("from") or "",
        "received_at": rec.get("received_at") or _fmt(rec.get("ts")),
        "date_ts": rec.get("ts"),
        "code": rec.get("code"),
        "preview": "",
        "source": rec.get("source") or "history",
    }


def _ago(ts):
    if not ts:
        return ""
    delta = max(0, int(time.time() - float(ts)))
    if delta < 60:
        return f"{delta}с назад"
    if delta < 3600:
        return f"{delta // 60}м назад"
    if delta < 86400:
        return f"{delta // 3600}ч назад"
    return f"{delta // 86400}д назад"


class Handler(BaseHTTPRequestHandler):
    server_version = "apimail-panel/" + __version__
    app = None

    # ── утилиты ───────────────────────────────────────────
    def log_message(self, fmt, *args):  # тихий лог (свой LogBus)
        return

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._send_json({"error": str(message)}, status)

    def _q(self, params, name, default=None):
        values = params.get(name)
        if not values:
            return default
        return values[0]

    def _int(self, params, name, default):
        raw = self._q(params, name)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    # ── маршруты ──────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        app = self.app
        try:
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/summary":
                return self._send_json(app.summary())
            if path == "/api/last-code":
                last = app.store.last_code(self._q(params, "address"))
                return self._send_json({"last_code": _code_view(last),
                                        "empty": last is None})
            if path == "/api/mails":
                address = (self._q(params, "address") or "").strip().lower() or None
                limit = max(1, min(self._int(params, "limit", 25), 200))
                offset = max(0, self._int(params, "offset", 0))
                with_raw = self._q(params, "full") in ("1", "true", "yes")
                return self._send_json(app.mails(address, limit, offset, with_raw))
            if path == "/api/mail":
                address = (self._q(params, "address") or "").strip().lower()
                mail_id = self._int(params, "id", None)
                if not mail_id:
                    return self._error("нужен параметр id", 400)
                return self._send_json(app.mail_raw(address, mail_id))
            if path == "/api/search":
                q = (self._q(params, "q") or "").strip()
                if not q:
                    return self._error("нужен параметр q", 400)
                return self._send_json(app.search(q))
            if path == "/api/search_body":
                q = (self._q(params, "q") or "").strip()
                if not q:
                    return self._error("нужен параметр q", 400)
                limit = max(1, min(self._int(params, "limit", 50), 200))
                return self._send_json(app.search_body(q, limit=limit))
            if path == "/api/index/stats":
                return self._send_json(app.index_stats())
            if path == "/api/aliases":
                return self._send_json(app.aliases())
            if path == "/api/logs":
                tail = self._int(params, "tail", 200)
                return self._send_text("\n".join(app.log.tail(tail)) + "\n")
            return self._error(f"неизвестный путь: {path}", 404)
        except MailAuthError as exc:
            self._error(f"apimail: ключ/токен не принят — {exc}", 502)
        except MailError as exc:
            self._error(f"apimail: {exc}", 502)
        except KeyError as exc:
            self._error(f"не найдено: {exc}", 404)
        except ValueError as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            app.log.bad(f"{path}: {type(exc).__name__}: {exc}")
            self._error(f"внутренняя ошибка: {type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        app = self.app
        body = self._read_json()
        try:
            if path == "/api/aliases":
                address = (body.get("address") or "").strip().lower()
                if not address:
                    return self._error("нужен адрес в теле {address}", 400)
                return self._send_json(app.add_alias(address, body.get("note") or ""))
            if path == "/api/refresh":
                return self._send_json(app.refresh())
            if path == "/api/index/sync":
                return self._send_json(app.index_sync(pages=body.get("pages")))
            if path == "/api/index/reset":
                return self._send_json(app.index_reset())
            return self._error(f"неизвестный путь: {path}", 404)
        except MailAuthError as exc:
            return self._error(f"apimail: ключ/токен не принят — {exc}", 502)
        except MailError as exc:
            return self._error(f"apimail: {exc}", 502)
        except ValueError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            app.log.bad(f"POST {path}: {type(exc).__name__}: {exc}")
            return self._error(f"внутренняя ошибка: {type(exc).__name__}: {exc}", 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        app = self.app
        try:
            if path == "/api/aliases":
                address = (self._q(params, "address") or "").strip().lower()
                return self._send_json(app.remove_alias(address))
            return self._error(f"неизвестный путь: {path}", 404)
        except KeyError as exc:
            return self._error(f"алиас не найден: {exc}", 404)
        except ValueError as exc:
            return self._error(str(exc), 400)

    # ── вспомогательное ───────────────────────────────────
    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1024 * 1024))
        try:
            data = json.loads(raw.decode("utf-8", "ignore") or "{}")
        except json.JSONDecodeError:
            raise ValueError("тело запроса — не JSON")
        return data if isinstance(data, dict) else {}

    def _serve_static(self, name):
        path = STATIC_DIR / name
        if not path.exists() or not path.is_file():
            return self._error("index.html не найден", 500)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_app(cfg):
    app = App(cfg)
    Handler.app = app
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="apimail-panel (catch-all mailbox panel)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    if args.host != "127.0.0.1":
        print("панель слушает только 127.0.0.1 (PANEL-SPEC §2)", file=sys.stderr)
        return 2
    cfg = config_mod.load(args.config)
    app = build_app(cfg)
    app.port = args.port
    app.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"apimail-panel {__version__}: http://127.0.0.1:{args.port} "
          f"(данные: {cfg['data_dir']})")

    stop_flag = threading.Event()

    def _shutdown(_signum, _frame):
        stop_flag.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    try:
        while not stop_flag.is_set():
            time.sleep(0.3)
    finally:
        app.log.info("панель остановлена")
        app.stop()
        httpd.shutdown()
        app.store.save(force=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())