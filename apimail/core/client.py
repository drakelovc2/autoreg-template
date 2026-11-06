# -*- coding: utf-8 -*-
"""Клиент apimail (example.com) на голом stdlib.

Два режима доступа:
  1) admin  — GET /admin/mails?address=<addr>&limit=N&offset=0
              заголовки: x-admin-auth: <ключ>, User-Agent: curl/8.5.0
              (дефолтный UA python сервер отбивает);
  2) jwt    — GET /api/mails?limit=N&offset=0
              заголовок: Authorization: Bearer <адресный JWT>.
              JWT выдаёт POST /api/new_address; работает только для адресов,
              зарегистрированных в движке (реестр `address`).

Админ-режим видит любой catch-all адрес — он основной. JWT-режим — резерв,
когда админ-ключ не принят (ротация ключа) или админ-пароль выключен.
"""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import parse as parse_mod

UA = "curl/8.5.0"  # apimail (Cloudflare) отбивает дефолтный UA питона


class MailError(Exception):
    def __init__(self, message, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


class MailAuthError(MailError):
    """Ключ/токен не принят (401/403 от движка)."""


def _mk_ssl_context():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


class MailClient:
    def __init__(self, base, admin_key="", address_jwts=None, timeout=25, log=None):
        self.base = (base or "").rstrip("/")
        self.admin_key = admin_key or ""
        self.address_jwts = dict(address_jwts or {})
        self.timeout = timeout
        self.log = log
        self._ctx = _mk_ssl_context()
        self.stats = {"requests": 0, "errors": 0, "auth_errors": 0}

    # ── низкий уровень ────────────────────────────────────
    def _request(self, path, params=None, headers=None, method="GET", payload=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        hdrs = {"User-Agent": UA, "Accept": "application/json",
                "Accept-Encoding": "identity"}
        hdrs.update(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        self.stats["requests"] += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                body = resp.read().decode("utf-8", "ignore")
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read().decode("utf-8", "ignore")
            except Exception:
                body = ""
        except Exception as exc:  # сеть/SSL/таймаут
            self.stats["errors"] += 1
            raise MailError(f"{type(exc).__name__}: {exc}") from exc
        return status, body

    def _json(self, path, params=None, headers=None, method="GET", payload=None,
              allow_status=(200,)):
        status, body = self._request(path, params, headers, method, payload)
        if status not in allow_status:
            if status in (401, 403):
                self.stats["auth_errors"] += 1
                raise MailAuthError(_short_auth_msg(body) or f"auth {status}", status, body)
            self.stats["errors"] += 1
            raise MailError(f"HTTP {status}: {body[:180]}", status, body)
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            self.stats["errors"] += 1
            raise MailError(f"ответ не JSON: {body[:180]}") from exc

    def _admin_headers(self):
        if not self.admin_key:
            raise MailAuthError("admin_key не задан в config.json")
        return {"x-admin-auth": self.admin_key}

    def _jwt_headers(self, address=None):
        jwt = self.address_jwts.get(address) if address else None
        if not jwt:
            jwt = self.address_jwts.get("*")
        if not jwt:
            raise MailAuthError(f"нет адресного JWT для {address or 'адреса'}")
        return {"Authorization": f"Bearer {jwt}"}

    # ── admin: список писем ───────────────────────────────
    def admin_list_mails(self, address, limit=20, offset=0):
        data = self._json("/admin/mails",
                          {"address": address, "limit": int(limit), "offset": int(offset)},
                          self._admin_headers())
        return data.get("results") or [], _as_int(data.get("count"))

    def admin_all_mails(self, limit=50, offset=0):
        """ВСЕ письма движка (без фильтра по адресу) — catch-all целиком.

        GET /admin/mails без параметра address отдаёт письма всех адресов
        (проверено 10.10: count = mailCount из /admin/statistics, пагинация
        offset работает).
        """
        data = self._json("/admin/mails",
                          {"limit": int(limit), "offset": int(offset)},
                          self._admin_headers())
        return data.get("results") or [], _as_int(data.get("count"))

    def admin_unknown_mails(self, limit=50, offset=0):
        """Письма на адреса, которых нет в реестре движка (catch-all)."""
        data = self._json("/admin/mails_unknow",
                          {"limit": int(limit), "offset": int(offset)},
                          self._admin_headers())
        return data.get("results") or [], _as_int(data.get("count"))

    def admin_statistics(self):
        return self._json("/admin/statistics", None, self._admin_headers())

    def admin_list_addresses(self, limit=20, offset=0, query=None):
        params = {"limit": int(limit), "offset": int(offset)}
        if query:
            params["query"] = query
        data = self._json("/admin/address", params, self._admin_headers())
        return data.get("results") or [], _as_int(data.get("count"))

    def admin_mail(self, mail_id):
        data = self._json(f"/admin/mails/{int(mail_id)}", None, self._admin_headers())
        return data or {}

    # ── jwt: список писем адреса ──────────────────────────
    def jwt_list_mails(self, address, limit=20, offset=0):
        data = self._json("/api/mails",
                          {"limit": int(limit), "offset": int(offset)},
                          self._jwt_headers(address))
        return data.get("results") or [], _as_int(data.get("count"))

    def jwt_mail(self, address, mail_id):
        data = self._json(f"/api/mail/{int(mail_id)}", None, self._jwt_headers(address))
        return data or {}

    # ── публичное ─────────────────────────────────────────
    def health(self):
        status, body = self._request("/health_check")
        return status == 200, body.strip()[:80]

    def create_address(self, name, domain=None):
        """POST /api/new_address → {address, jwt, address_id} (префикс движка)."""
        payload = {"name": name}
        if domain:
            payload["domain"] = domain
        return self._json("/api/new_address", None, None, "POST", payload)

    # ── высокий уровень ───────────────────────────────────
    def mode(self, address=None):
        """Какой режим будет использован: 'admin' | 'jwt' | 'none'."""
        if self.admin_key:
            return "admin"
        if address and (self.address_jwts.get(address) or self.address_jwts.get("*")):
            return "jwt"
        return "none"

    def list_mails(self, address, limit=20, offset=0, with_raw=False):
        """Лента писем адреса: admin → jwt (fallback при отказе ключа).

        Возвращает (items, count, mode); item — нормализованный словарь.
        """
        order = []
        if self.admin_key:
            order.append("admin")
        if self.address_jwts.get(address) or self.address_jwts.get("*"):
            order.append("jwt")
        if not order:
            raise MailAuthError("нет ни admin_key, ни адресного JWT")

        last_exc = None
        for mode in order:
            try:
                if mode == "admin":
                    rows, count = self.admin_list_mails(address, limit, offset)
                else:
                    rows, count = self.jwt_list_mails(address, limit, offset)
            except MailAuthError as exc:
                last_exc = exc
                if mode == "admin" and self.log:
                    self.log.warn(f"админ-ключ не принят ({exc}); пробую JWT-режим")
                continue
            items = [normalize_row(r, address, with_raw=with_raw) for r in rows]
            return items, count, mode
        raise last_exc or MailAuthError("движок не пустил")

    def get_mail(self, address, mail_id, raw_only=False):
        """Одно письмо целиком (для просмотра сырца)."""
        if self.admin_key:
            try:
                data = self.admin_mail(mail_id)
                return data, "admin"
            except MailAuthError:
                pass
        data = self.jwt_mail(address, mail_id)
        return data, "jwt"

    def list_all_mails(self, limit=50, offset=0, with_raw=False):
        """Лента ВСЕХ писем движка (все адреса catch-all) — admin-режим.

        Возвращает (items, count, mode). Только admin: у JWT нет глобального
        списка (он привязан к одному адресу).
        """
        if not self.admin_key:
            raise MailAuthError("нет admin_key — глобальный список недоступен")
        rows, count = self.admin_all_mails(limit, offset)
        items = [normalize_row(r, r.get("address") or "", with_raw=with_raw)
                 for r in rows]
        return items, count, "admin"


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _short_auth_msg(body):
    """Вытащить короткое пояснение из 401/403 движка."""
    try:
        data = json.loads(body)
        msg = data.get("message") or data.get("error")
        if msg:
            return str(msg)[:120]
    except Exception:
        pass
    return (body or "").strip()[:120]


def normalize_row(row, address, with_raw=False):
    """Строка raw_mails → запись для API панели (тема/код/время/сырец)."""
    raw = row.get("raw") or ""
    parsed = parse_mod.parse_mail(raw)
    item = {
        "id": row.get("id"),
        "address": row.get("address") or address,
        "subject": parsed["subject"] or "(без темы)",
        "from": parsed["from"],
        "received_at": parsed["received_at"],
        "date_ts": parsed["date_ts"],
        "code": parsed["code"],
        "code_context": parsed["code_context"],
        "body_len": parsed["body_len"],
        "preview": _preview(parsed["body"]),
    }
    if with_raw:
        item["raw"] = raw
        item["body"] = parsed["body"]
    return item


def _preview(body, limit=240):
    text = " ".join((body or "").split())
    return text[:limit]