# -*- coding: utf-8 -*-
"""Gmail IMAP как второй источник почты для панели apimail.

Зачем: часть сервисов (и JEV в их числе) принимают регистрацию только на
«доверенные» домены. У нас есть Gmail-ящик, а Gmail умеет плюс-алиасы:
`user+тег@gmail.com` приходит в тот же INBOX. Здесь читаем письма ящика
по IMAP (app-password) и отдаём их в том же формате, что apimail-движок,
чтобы панель и poller работали единообразно.

Конфиг (config.json):
    "gmail": {
      "enabled": true,
      "user": "drafc27super@gmail.com",
      "app_password": "REPLACE_APP_PASSWORD",   # без пробелов
      "alias_prefix": "jv",                  # user+jvXXXX@gmail.com
      "host": "imap.gmail.com",
      "port": 993,
      "folder": "INBOX",                     # + [Gmail]/Spam при желании
      "search_days": 7                       # сколько дней назад смотреть
    }

Алиасы (плюс-адреса) — это НЕ отдельные ящики: всё падает в один INBOX,
различаем по заголовку To/Delivered-To. Реестр алиасов панель ведёт сама.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import re
import ssl
import time
from email.header import decode_header, make_header

from . import parse as parse_mod

# Плюс-алиас Gmail: local+тег@googlemail|gmail.com
GMAIL_ALIAS_RE = re.compile(r"([A-Za-z0-9._%+-]+@(?:gmail|googlemail)\.com)", re.I)


class GmailError(Exception):
    pass


class GmailAuthError(GmailError):
    """app-password/логин не принят (IMAP AUTHENTICATIONFAILED)."""


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


class GmailClient:
    """Тонкая обёртка над imaplib: список писем и одно письмо (RFC822).

    Соединение открывается на каждый вызов (как в реггерах) — это надёжнее
    долгоживущей сессии, которую Gmail рвёт по таймауту. Для панели с
    интервалом опроса 25с накладные расходы незаметны.
    """

    def __init__(self, cfg, log=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled"))
        self.user = (cfg.get("user") or "").strip()
        self.app_password = (cfg.get("app_password") or "").replace(" ", "").strip()
        self.alias_prefix = (cfg.get("alias_prefix") or "jv").strip()
        self.host = cfg.get("host") or "imap.gmail.com"
        self.port = int(cfg.get("port") or 993)
        self.folder = cfg.get("folder") or "INBOX"
        self.search_days = int(cfg.get("search_days") or 7)
        self.log = log

    # ── служебное ─────────────────────────────────────────
    def configured(self):
        return bool(self.enabled and self.user and self.app_password)

    def make_address(self, tag=None):
        """Собрать плюс-алиас: user+<prefix><tag>@gmail.com."""
        if not self.user or "@" not in self.user:
            raise GmailError("gmail.user не задан в config.json")
        local, _, dom = self.user.partition("@")
        tag = tag or str(int(time.time()))
        return f"{local}+{self.alias_prefix}{tag}@{dom}"

    def _connect(self):
        if not self.configured():
            raise GmailAuthError("Gmail не настроен (enabled/user/app_password)")
        try:
            m = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=_ctx())
        except Exception as exc:  # noqa: BLE001
            raise GmailError(f"IMAP connect: {type(exc).__name__}: {exc}") from exc
        try:
            m.login(self.user, self.app_password)
        except imaplib.IMAP4.error as exc:
            raise GmailAuthError(f"IMAP login: {exc}") from exc
        return m

    # ── чтение ────────────────────────────────────────────
    def _search_uids(self, m, address=None, limit=25):
        """UID писем: по адресу (To) или последние за search_days."""
        if address:
            try:
                st, data = m.uid("search", None, "TO", f'"{address}"')
            except Exception as exc:  # noqa: BLE001
                raise GmailError(f"IMAP search: {exc}") from exc
            if st == "OK" and data and data[0]:
                return data[0].split()[-limit:]
            # нет писем на этот алиас — пустой результат, не падаем в общий поиск
            return []
        since = time.strftime("%d-%b-%Y",
                              time.localtime(time.time() - self.search_days * 86400))
        try:
            st, data = m.uid("search", None, "SINCE", since)
        except Exception as exc:  # noqa: BLE001
            raise GmailError(f"IMAP search: {exc}") from exc
        if st != "OK":
            return []
        uids = (data[0].split() if data and data[0] else [])
        return uids[-limit:]

    def _fetch_one(self, m, uid):
        try:
            st, md = m.uid("fetch", uid, "(RFC822)")
        except Exception:
            return None
        if st != "OK" or not md or not isinstance(md[0], tuple):
            return None
        return md[0][1].decode("utf-8", "replace") if isinstance(md[0][1], bytes) else str(md[0][1])

    def list_mails(self, address=None, limit=25, offset=0, with_raw=False):
        """Письма ящика (при address — только на этот плюс-алиас).

        Возвращает (items, count) — items в формате панели (normalize-совместим).
        """
        m = self._connect()
        try:
            m.select(self.folder, readonly=True)
            uids = self._search_uids(m, address=address, limit=max(limit + offset, limit))
            items = []
            for uid in reversed(uids):  # новые первыми
                raw = self._fetch_one(m, uid)
                if not raw:
                    continue
                msg = email.message_from_string(raw)
                to_hdr = " ".join(filter(None, [
                    _decode(msg.get("To", "")),
                    _decode(msg.get("Delivered-To", "")),
                    _decode(msg.get("X-Original-To", "")),
                ]))
                # фильтр по конкретному алиасу (если запрошен)
                if address and address.lower() not in to_hdr.lower():
                    continue
                item = self._normalize(uid, raw, address or self._address_of(to_hdr))
                items.append(item)
            total = len(items)
            page = items[offset:offset + limit]
            return page, total
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def get_mail(self, address, mail_id):
        """Одно письмо целиком по UID."""
        m = self._connect()
        try:
            m.select(self.folder, readonly=True)
            raw = self._fetch_one(m, str(mail_id).encode())
            if not raw:
                raise GmailError(f"письмо {mail_id} не найдено")
            return {"id": mail_id, "address": address, "raw": raw}, "gmail"
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def health(self):
        try:
            m = self._connect()
            m.logout()
            return True, f"IMAP ok ({self.user})"
        except GmailAuthError as exc:
            return False, str(exc)[:120]
        except GmailError as exc:
            return False, str(exc)[:120]

    # ── нормализация под формат панели ────────────────────
    @staticmethod
    def _address_of(to_hdr):
        match = GMAIL_ALIAS_RE.search(to_hdr or "")
        return match.group(1).lower() if match else ""

    def _normalize(self, uid, raw, address):
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        parsed = parse_mod.parse_mail(raw)
        to_hdr = ""
        try:
            msg = email.message_from_string(raw)
            to_hdr = _decode(msg.get("To", ""))
        except Exception:
            pass
        return {
            "id": f"g{uid_str}",
            "uid": uid_str,
            "address": address or self._address_of(to_hdr),
            "subject": parsed["subject"] or "(без темы)",
            "from": parsed["from"],
            "received_at": parsed["received_at"],
            "date_ts": parsed["date_ts"],
            "code": parsed["code"],
            "code_context": parsed["code_context"],
            "body_len": parsed["body_len"],
            "preview": " ".join((parsed["body"] or "").split())[:240],
            "links": parsed["links"],
            "source": "gmail",
        }
