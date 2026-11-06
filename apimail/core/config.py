# -*- coding: utf-8 -*-
"""Конфиг панели apimail.

Приоритет: config.json в корне папки → env-оверрайды → дефолты.
Ключ админки живёт только в config.json (в git не попадает).
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    # ── почтовый движок ────────────────────────────────────
    "mail_base": "https://mail.example.com",
    "mail_domain": "example.com",
    # Мультидомен: список ВСЕХ доменов catch-all, которые обслуживает движок.
    # Если задан и непуст — приоритетнее одиночного mail_domain; панель и
    # история сканируют письма по всем доменам. Одиночный mail_domain остаётся
    # основным (совместимость).
    "mail_domains": [],
    "admin_key": "",
    # catch-all: адрес генерится на месте, префикс движка (env PREFIX воркера)
    "mail_prefix": "tmp",
    # ── опрос ──────────────────────────────────────────────
    "poll_interval_sec": 25,
    "aliases_per_cycle": 25,
    "request_timeout_sec": 25,
    "code_min_digits": 4,
    "code_max_digits": 8,
    # ── источники пополнения списка алиасов ────────────────
    "discover_unknown_mails": True,   # /admin/mails_unknow: письма на адреса вне реестра
    "unknown_scan_limit": 50,
    "history_scan": True,             # скан data/*.jsonl/json/txt/log реггеров
    "history_scan_interval_sec": 900,
    "import_log_codes": True,         # коды из логов реггеров (история не теряется)
    # ── полнотекстовый индекс писем (SQLite FTS5, data/mailindex.db) ──
    "index_enabled": True,          # фоновая доиндексация глобального списка
    "index_pages_per_cycle": 4,     # страниц глобального списка за цикл обхода
    "index_page_limit": 100,        # писем на страницу /admin/mails
    # ── доступ по адресному JWT (fallback, если админ-ключ не принят) ──
    "address_jwts": {},
    # ── второй источник: Gmail IMAP (плюс-алиасы одного ящика) ──
    # Часть сервисов принимает регистрацию только на «доверенные» домены;
    # для них используем Gmail+алиасы. Чтение — IMAP по app-password.
    "gmail": {
        "enabled": False,
        "user": "",
        "app_password": "",
        "alias_prefix": "jv",
        "host": "imap.gmail.com",
        "port": 993,
        "folder": "INBOX",
        "search_days": 7,
    },
}

ENV_MAP = {
    "APIMAIL_BASE": "mail_base",
    "APIMAIL_ADMIN_KEY": "admin_key",
    "APIMAIL_DOMAIN": "mail_domain",
    "APIMAIL_POLL_SEC": "poll_interval_sec",
}


def _env_overrides(cfg):
    for env_name, key in ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        if key in ("poll_interval_sec",):
            try:
                cfg[key] = int(raw)
            except ValueError:
                pass
        else:
            cfg[key] = raw
    return cfg


def load(path=None):
    """Собрать конфиг: дефолты < config.json < env."""
    cfg = dict(DEFAULTS)
    cfg_path = Path(path) if path else CONFIG_PATH
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if v is not None})
        except (OSError, json.JSONDecodeError) as exc:
            cfg["_config_error"] = f"config.json не прочитан: {exc}"
    _env_overrides(cfg)
    cfg["mail_base"] = str(cfg["mail_base"]).rstrip("/")
    cfg["mail_domain"] = str(cfg["mail_domain"]).strip().lower()
    # mail_domains — список всех доменов; одиночный mail_domain входит всегда.
    doms = cfg.get("mail_domains") or []
    if isinstance(doms, str):
        doms = [d.strip() for d in doms.replace(",", " ").split() if d.strip()]
    doms = [str(d).strip().lower() for d in doms if str(d).strip()]
    if cfg["mail_domain"] and cfg["mail_domain"] not in doms:
        doms.insert(0, cfg["mail_domain"])
    cfg["mail_domains"] = doms
    cfg["data_dir"] = str(DATA_DIR)
    cfg["root_dir"] = str(ROOT)
    return cfg


def masked_key(key):
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "•" * (len(key) - 8) + key[-4:]