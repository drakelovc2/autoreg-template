# -*- coding: utf-8 -*-
"""Example adapter: HTTP-only signup with an email OTP.

Copy this file into your own `core/register.py` and replace the endpoints,
field names and the key endpoint with your service's.

This example assumes:
  * POST /api/signup  {"email": ..., "password": ...}
  * the service sends a 6-digit code;
  * POST /api/verify  {"email": ..., "code": ...}
  * POST /api/keys    (authenticated) -> {"key": "..."}
"""
import re
import time

import requests

from . import config
from .mail import MailClient, parse_code_from_raw

BASE = "https://service.example"


def _session(proxy=None):
    s = requests.Session()
    s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def register_one(email=None, password=None, proxy=None, mail=None, on_step=None):
    step = on_step or (lambda m: print(" ", m))
    mail = mail or MailClient()
    email = email or mail.new_address()
    password = password or ("Pass!" + str(int(time.time()))[-8:])
    started = time.time()
    s = _session(proxy)

    step("1/4 signup")
    r = s.post(f"{BASE}/api/signup", json={"email": email, "password": password}, timeout=30)
    if not r.ok:
        return {"email": email, "error": f"signup HTTP {r.status_code}: {r.text[:120]}"}

    step("2/4 wait for code")
    code = None
    deadline = time.time() + 180
    while time.time() < deadline and not code:
        for m in mail.list_mails(email, limit=5):
            try:
                code, _ = parse_code_from_raw(m.get("raw", ""), since_ts=started)
            except Exception:
                continue
            if code:
                break
        if not code:
            time.sleep(5)
    if not code:
        return {"email": email, "error": "code not received"}

    step("3/4 verify")
    r = s.post(f"{BASE}/api/verify", json={"email": email, "code": code}, timeout=30)
    if not r.ok:
        return {"email": email, "error": f"verify HTTP {r.status_code}: {r.text[:120]}"}

    step("4/4 create key")
    r = s.post(f"{BASE}/api/keys", json={"name": "main"}, timeout=30)
    if not r.ok:
        return {"email": email, "password": password,
                "error": f"key HTTP {r.status_code}: {r.text[:120]}"}
    key = r.json().get("key")
    if not key:
        return {"email": email, "password": password, "error": "no key in response"}

    ok, detail = test_key(key, proxy=proxy)
    return {"email": email, "password": password, "api_key": key,
            "status": "ok" if ok else "key_unverified", "verify": detail,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def test_key(api_key, proxy=None):
    s = _session(proxy)
    s.headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = s.get(f"{BASE}/api/me", timeout=30)
        return r.ok, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)[:120]


def list_models(api_key, proxy=None):
    s = _session(proxy)
    s.headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = s.get(f"{BASE}/v1/models", timeout=30)
        return [m.get("id") for m in r.json().get("data", [])] if r.ok else []
    except Exception:
        return []
