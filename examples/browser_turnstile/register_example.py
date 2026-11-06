# -*- coding: utf-8 -*-
"""Example adapter: browser signup with a Turnstile-style widget.

Replace the URL, selectors and success condition with your service's.
The captcha hook is deliberately empty: provide your own solver if the
service requires one, and keep its key in config.json.
"""
import time

from . import config


def _token(page, timeout=90):
    """Wait for a hidden input named cf-turnstile-response to get a value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tok = page.evaluate(
            "() => { const e = document.querySelector("
            "'input[name=\"cf-turnstile-response\"]'); return e ? e.value : null; }")
        if tok:
            return tok
        time.sleep(2)
    return None


def register_one(email=None, password=None, proxy=None, mail=None, on_step=None):
    from camoufox.sync_api import Camoufox

    step = on_step or (lambda m: print(" ", m))
    step("1/4 open form")

    opts = {"headless": True, "humanize": True, "locale": "en-US"}
    if proxy:
        opts["proxy"] = {"server": proxy}  # or split username/password

    with Camoufox(**opts) as browser:
        page = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
        page.goto("https://service.example/signup", wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)

        step("2/4 fill")
        page.fill("input[name=email]", email or "user@example.com")
        page.fill("input[name=password]", password or "ChangeMe!2026")
        page.click("button[role=checkbox]", timeout=10000)  # terms, if present

        step("3/4 captcha")
        tok = _token(page)
        if not tok:
            return {"error": "captcha token not found"}

        step("4/4 submit")
        page.click("button[type=submit]", timeout=15000)
        time.sleep(8)
        body = page.inner_text("body") or ""
        if "check your inbox" not in body.lower():
            return {"error": f"unexpected page: {body[:160]}"}

    return {"email": email, "password": password, "status": "pending_verification",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def test_key(api_key, proxy=None):
    return False, "not implemented in this example"


def list_models(api_key, proxy=None):
    return []
