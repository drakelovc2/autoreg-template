# -*- coding: utf-8 -*-
"""Конфиг реггера из config.json (рядом с папкой core/).

Скопировал шаблон под новый сервис — заполни `base_url` и прочее СВОИМ.
Значения ниже — только безопасные дефолты; секретов и адресов конкретного
сервиса здесь нет."""
import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_DIR)
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")

_DEFAULTS = {
    "base_url": "https://СЕРВИС.example",
    "turnstile_sitekey": "",
    # camoufox | capsolver | auto (camoufox → capsolver fallback)
    "turnstile_solver": "auto",
    # Python executable that has camoufox installed
    "camoufox_python": "python3",
    "capsolver_api_key": None,

    "mail_base": "https://mail.example.com",
    "mail_admin_key": "",
    "mail_domain": "example.com",
    # Multi-domain catch-all: if non-empty, takes priority over mail_domain;
    # a random domain from the list is used for each address.
    "mail_domains": [],
    # Домены, к которым добавлять точку в конце (FQDN-обход блок-листа).
    # Пусто — точка не добавляется ни к кому.
    "mail_dot_domains": [],
    # Второй источник: плюс-алиасы одного Gmail-ящика (user+jv...@gmail.com),
    # чтение по IMAP (app-password). Пусто/disabled — не используется.
    "gmail": {
        "enabled": False,
        "user": "",
        "app_password": "",
        "alias_prefix": "jv",
    },
    # Только gmail-алиасы, без catch-all. Нужно, когда apimail недоступен
    # (дневной лимит Cloudflare D1, лежащий воркер) — тогда чтение идёт
    # по IMAP и подмена источника на ходу не нужна.
    "mail_only_gmail": False,
    "mail_ua": "curl/8.5.0",
    "mail_timeout_sec": 150,
    "mail_poll_sec": 5,
    "address_prefix": "",

    # Минтить капчу НАПРЯМУЮ (домашний IP), даже когда регистрация через прокси.
    # Если сервис сверяет IP минта и сабмита — расхождение ловится ошибкой
    # (напр. «a bit fast»). Правильно по умолчанию — mint через тот же прокси
    # (false), см. GUIDE §2. Сверка IP есть НЕ у всех сервисов — проверяй.
    "turnstile_mint_direct": False,

    "proxies": [],
    # Bright Data зоны с session-ротацией (GUIDE §2): свежий IP на каждый акк.
    # Пусто → используем статический data/proxies.txt.
    "brd_zones": [],
    # ISP-зоны BRD: СТАТИЧЕСКИЕ IP. Приоритетнее brd_zones. Нужны, когда сервис
    # сверяет IP минта капчи с IP сабмита (см. IspRotator, GUIDE §2).
    "isp_zones": [],
    "proxies_file": os.path.join(DATA_DIR, "proxies.txt"),
    "proxy_check_timeout": 10,
    "proxy_check_workers": 20,

    "batch_interval_sec": 25,
    "circuit_breaker_fails": 5,

    # ── cardkit: авто-бины и карты (core/cardkit.py) ────────────────────
    # Сервисы с шагом «привяжи карту → бонус»: карта генерится по BIN (Luhn,
    # алгоритм chkr.cc), проверяется через api.chkr.cc, оплата — Stripe API.
    "card_bins": [],
    # any  — брать первую сгенерированную (мёртвые карты часто проходят $0-предавторизацию)
    # live — гонять через chkr.cc до статуса Live (медленно, лимит API)
    # off  — генератор выключен, используем card_number
    "card_bin_mode": "any",
    "card_max_attempts": 6,
    "card_ttl_years": [2, 5],
    "card_zip": "10001",
    "card_number": "",
    "card_mm": "",
    "card_yyyy": "",
    "card_cvv": "",
    "chkr_api": "https://api.chkr.cc/",
    "chkr_check_interval_sec": 2.5,
    "chkr_max_429_retries": 3,
    "chkr_max_generate": 40,
    "stripe_pk": "",
    "card_step_direct": True,

    "output_jsonl": os.path.join(DATA_DIR, "accounts.jsonl"),
    "output_combo": os.path.join(DATA_DIR, "accounts.txt"),
    # Two-phase: аккаунты, зарегистрированные, но ещё не подтверждённые.
    # Лежат отдельно от готовых, чтобы `verify` взял их и дождался письма.
    "output_pending": os.path.join(DATA_DIR, "pending.jsonl"),

    # Vision-капча (сетка/выбор картинки) и STT для аудио-капчи.
    # Пусто → шаг vision в реггере честно падает, а не молчит.
    "vlm_api_base": None,
    "vlm_api_key": None,
    "vlm_model": None,
    "stt_api_base": None,
    "stt_api_key": None,
    "stt_model": "whisper-1",

    # запасной список free-моделей СВОЕГО сервиса, если /v1/models по ключу не
    # ответил. Заполни под свой сервис (напр. ["model-a:free", "model-b:free"]).
    "fallback_free_models": [],
}

_cfg = dict(_DEFAULTS)
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _cfg.update(json.load(f))

# ── OPTIONAL CENTRAL PROXY SOURCE ───────────────────────────────────────────
# If you keep a shared proxy file outside the project, point AUTOREG_PROXY_CONFIG
# at it. Nothing is hardcoded; without the env var the local config is used.
_CENTRAL_PROXY = os.path.normpath(os.environ.get(
    "AUTOREG_PROXY_CONFIG", os.path.join(ROOT, "..", "proxy", "proxies.json")))
_central_used = False


def _load_central_proxy():
    """Подтянуть зоны/креды из единого источника. True, если источник применился."""
    global _central_used
    try:
        with open(_CENTRAL_PROXY, "r", encoding="utf-8") as f:
            central = json.load(f)
    except (OSError, ValueError):
        return
    if not (central.get("autoreg") or {}).get("enabled"):
        return
    prov = (central.get("providers") or {}).get("brightdata") or {}
    isp, dc = [], []
    for z in prov.get("zones", []):
        rec = {"zone": z.get("zone"), "password": z.get("password")}
        if z.get("country"):
            rec["country"] = z["country"]
        (isp if z.get("kind") == "isp" else dc).append(rec)
    if isp:
        _cfg["isp_zones"] = isp
    if dc:
        _cfg["brd_zones"] = dc
    if prov.get("customer"):
        _cfg["brd_customer"] = prov["customer"]
    if prov.get("host"):
        _cfg["brd_host"] = prov["host"]
    _central_used = True


_load_central_proxy()

for _k, _v in _cfg.items():
    globals()[_k.upper()] = _v

CENTRAL_PROXY_PATH = _CENTRAL_PROXY
CENTRAL_PROXY_USED = _central_used

BASE_URL = _cfg["base_url"].rstrip("/")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def get(key, default=None):
    return _cfg.get(key, default)
