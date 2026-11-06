# -*- coding: utf-8 -*-
"""ПРИМЕР флоу реггера — HTTP-only, Next.js Server Actions.

Это ОБРАЗЕЦ под конкретный тип сервиса; замени своим флоу (core/<сервис>.py).
Демонстрирует типовую цепочку HTTP-регистрации без браузера.

Шаги:
  1. discover: GET /login?mode=signup и /login → deployment id + action id
     из server-rendered скрытого инпута $ACTION_1:0 (не «первый hex в HTML»).
  2. Turnstile (camoufox бесплатно → capsolver fallback).
  3. signup server action → verify-ссылка из apimail → GET verify → login action.
  4. POST /api/keys → POST /api/me/privacy (free models) → /v1/models по ключу.

Проверка живости — отдельный шаг: логин + чтение ключей, а не «200 на signup».
"""
import base64
import html
import json
import re
import secrets
import string
import time
import uuid

import requests

from . import config
from .mail import MailClient

# Капча сервис-специфична и в шаблоне НЕ поставляется (см. docs/СТАНДАРТ.md §4).
# Если у твоего сервиса Turnstile — положи свой core/turnstile.py; до тех пор
# solve-обёртка ниже вернёт None, и флоу пойдёт без токена (решит сервер).
try:
    from . import turnstile as _turnstile
except ImportError:
    _turnstile = None


def solve_turnstile(url, proxy=None):
    """Токен Turnstile или None. Нет модуля капчи — None (флоу без токена)."""
    if _turnstile is None:
        return None
    return _turnstile.solve(url, proxy=proxy)


def _gen_password(length=20):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    # гарантируем по символу каждого класса — у сервисов бывают требования
    pw = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
          secrets.choice(string.digits), secrets.choice("!@#$%^&*")]
    pw += [secrets.choice(chars) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)

# $ACTION_1:0 value="{&quot;id&quot;:&quot;<hex40>&quot;,&quot;bound&quot;:&quot;$@1&quot;}"
_ACTION_INPUT_RE = re.compile(r'name="\$ACTION_1:0" value="([^"]+)"')
_DPL_RE = re.compile(r"dpl_[A-Za-z0-9]{8,}")


class ServiceError(Exception):
    pass


class ServiceClient:
    def __init__(self, proxy=None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.UA,
            "Origin": config.BASE_URL,
        })
        self.proxy = proxy
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._dpl = None
        self._actions = {}          # mode → action id ("signup" / "signin")
        self.fingerprint = str(uuid.uuid4())
        self.timezone = "Europe/Berlin"

    # ── discover: deployment id + action ids ────────────────────────────
    def _discover(self, mode):
        """action id из скрытого инпута формы — единственный надёжный источник."""
        url = f"{config.BASE_URL}/login" + ("?mode=signup" if mode == "signup" else "")
        r = self.session.get(url, timeout=20)
        r.raise_for_status()
        if not self._dpl:
            m = _DPL_RE.search(r.headers.get("Link", "") or "") or _DPL_RE.search(r.text)
            if m:
                self._dpl = m.group(0)
        m = _ACTION_INPUT_RE.search(r.text)
        if not m:
            raise ServiceError(f"action id не найден на {url} — верстка сменилась?")
        try:
            payload = json.loads(html.unescape(m.group(1)))
            action_id = payload["id"]
        except (ValueError, KeyError) as exc:
            raise ServiceError(f"action id не распарсился на {url}: {exc}")
        self._actions[mode] = action_id
        return action_id

    def _action_id(self, mode):
        return self._actions.get(mode) or self._discover(mode)

    # ── Next.js server action multipart ─────────────────────────────────
    def _build_multipart(self, action_id, fields):
        boundary = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
        sep = f"--{boundary}"
        parts = [
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_REF_1"\r\n\r\n\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_1:0"\r\n\r\n'
            f'{{"id":"{action_id}","bound":"$@1"}}\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_1:1"\r\n\r\n["$undefined"]\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_KEY"\r\n\r\n'
            f'k{uuid.uuid4().hex[:31]}\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_device_fingerprint"\r\n\r\n{self.fingerprint}\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_timezone"\r\n\r\n{self.timezone}\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_next"\r\n\r\n\r\n',
        ]
        for name, value in fields:
            parts.append(f'{sep}\r\nContent-Disposition: form-data; name="1_{name}"\r\n\r\n{value}\r\n')
        parts.append(f'{sep}\r\nContent-Disposition: form-data; name="0"\r\n\r\n["$undefined","$K1"]\r\n')
        parts.append(f'{sep}--\r\n')
        return "".join(parts).encode("utf-8"), boundary

    def _call_action(self, mode, fields):
        action_id = self._action_id(mode)
        body, boundary = self._build_multipart(action_id, fields)
        page = f"{config.BASE_URL}/login" + ("?mode=signup" if mode == "signup" else "")
        r = self.session.post(
            page,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "next-action": action_id,
                "Accept": "text/x-component",
                "Referer": page,
                "x-deployment-id": self._dpl or "",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            data=body,
            timeout=30,
        )
        if r.status_code == 429:
            return {"ok": False, "error": "rate-limited (429)", "retryable": True}
        r.encoding = "utf-8"  # RSC отдаёт UTF-8 без charset — requests гадает и портит
        # RSC-ответ: ищем настоящую ошибку (React-refs вида $d — не ошибки)
        for line in r.text.strip().split("\n"):
            try:
                _, data = line.split(":", 1)
                parsed = json.loads(data)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and "error" in parsed:
                err = parsed["error"]
                if isinstance(err, str) and not err.startswith("$"):
                    low = err.lower()
                    # «too many sign-ups from this network» — IP сгорел на час;
                    # ретраить бессмысленно, надо менять прокси (уровень batch)
                    if "too many sign-ups" in low:
                        return {"ok": False, "error": err, "retryable": False, "ip_burned": True}
                    retryable = any(w in low for w in ("fast", "rate", "later"))
                    return {"ok": False, "error": err, "retryable": retryable}
        if r.status_code in (200, 303):
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}

    # ── публичные шаги ──────────────────────────────────────────────────
    @staticmethod
    def _needs_captcha(result):
        err = (result.get("error") or "").lower()
        return any(w in err for w in ("turnstile", "captcha", "challenge", "bot", "human"))

    def signup(self, email, password):
        self.fingerprint = str(uuid.uuid4())
        self._discover("signup")
        fields = [("email", email), ("password", password), ("invite_code", "")]
        # Turnstile на 09.10 не рендерится — идём без токена, решаем только
        # если сервер явно попросил (виджет появляется под подозрением).
        result = self._call_action("signup", fields)
        if result.get("ok") or not self._needs_captcha(result):
            return result
        token = solve_turnstile(f"{config.BASE_URL}/login?mode=signup", proxy=self.proxy)
        if not token:
            return {"ok": False, "error": "turnstile: токен не получен", "retryable": True}
        return self._call_action("signup", [("cf-turnstile-response", token)] + fields)

    def verify_email(self, url):
        r = self.session.get(url, allow_redirects=True, timeout=20)
        ok = r.status_code == 200 and ("success" in r.url.lower() or "verif" in r.text.lower())
        return ok

    def login(self, email, password):
        self._discover("signin")
        fields = [("email", email), ("password", password)]
        result = self._call_action("signin", fields)
        if result.get("ok") or not self._needs_captcha(result):
            return result
        token = solve_turnstile(f"{config.BASE_URL}/login", proxy=self.proxy)
        if not token:
            return {"ok": False, "error": "turnstile: токен не получен", "retryable": True}
        return self._call_action("signin", [("cf-turnstile-response", token)] + fields)

    def access_token(self):
        """Supabase access_token из chunked-cookies (для проверки живости)."""
        chunked = {}
        for c in self.session.cookies:
            if c.name.startswith("sb-") and "auth-token." in c.name:
                chunked[c.name.split(".")[-1]] = c.value
        if not chunked:
            return None
        combined = "".join(chunked[k] for k in sorted(chunked))
        if combined.startswith("base64-"):
            combined = combined[7:]
        combined += "=" * (-len(combined) % 4)
        try:
            return json.loads(base64.b64decode(combined).decode()).get("access_token")
        except Exception:
            return None

    def create_api_key(self, label="<сервис>-reg"):
        r = self.session.post(f"{config.BASE_URL}/api/keys",
                              json={"label": label}, timeout=20)
        if not r.ok:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()

    def list_api_keys(self):
        r = self.session.get(f"{config.BASE_URL}/api/keys", timeout=20)
        return r.json().get("keys", []) if r.ok else []

    def enable_free_models(self):
        r = self.session.post(f"{config.BASE_URL}/api/me/privacy",
                              json={"consent_version": 2, "free_models_enabled": True},
                              timeout=20)
        if not r.ok:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        data.setdefault("ok", True)
        return data

    def get_free_tier_status(self):
        r = self.session.get(f"{config.BASE_URL}/api/me/free-tier", timeout=20)
        return r.json() if r.ok else {"error": f"HTTP {r.status_code}"}

    def session_valid(self):
        """Verify-ссылка часто сама ставит сессию — тогда логин не нужен."""
        try:
            r = self.session.get(f"{config.BASE_URL}/api/me/free-tier", timeout=15)
            return r.status_code == 200
        except Exception:
            return False


# ── API по ключу (без сессии) ────────────────────────────────────────────

def list_models(api_key, proxy=None):
    """Список моделей по ключу → [id, ...]. Пусто при ошибке."""
    try:
        r = requests.get(f"{config.BASE_URL}/v1/models",
                         headers={"Authorization": f"Bearer {api_key}",
                                  "User-Agent": config.UA},
                         proxies={"http": proxy, "https": proxy} if proxy else None,
                         timeout=20)
        if r.ok:
            return [m.get("id", "") for m in r.json().get("data", [])]
    except Exception:
        pass
    return []


def free_models(api_key, proxy=None):
    """Фри-модели по ключу; fallback — список из конфига.

    Голый алиас `:free` числится в /v1/models, но у апстрима отдаёт 404
    («free route is not available») — ставим его в конец, чтобы дефолтом
    всегда была рабочая именованная модель.
    """
    ids = [m for m in list_models(api_key, proxy) if m.endswith(":free")]
    ids.sort(key=lambda m: (m == ":free", m))
    return ids or list(config.FALLBACK_FREE_MODELS)


def test_key(api_key, model=None, proxy=None):
    """Один chat-запрос по ключу → (ok, detail)."""
    model = model or free_models(api_key, proxy)[0]
    try:
        r = requests.post(
            f"{config.BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": config.UA},
            json={"model": model,
                  "messages": [{"role": "user", "content": "Say hi in 3 words"}],
                  "max_tokens": 20},
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=30)
        if r.ok:
            return True, model, r.json()["choices"][0]["message"]["content"]
        return False, model, f"HTTP {r.status_code}: {r.text[:150]}"
    except Exception as exc:
        return False, model, str(exc)[:150]


# ── полный флоу ──────────────────────────────────────────────────────────

def register_one(email=None, password=None, proxy=None, mail=None, on_step=None):
    """Регистрация одного аккаунта. Возвращает dict с результатом или {"error": ...}.

    on_step(str) — колбэк прогресса для CLI.
    """
    def step(msg):
        if on_step:
            on_step(msg)
        else:
            print(f"  {msg}")

    mail = mail or MailClient()
    email = email or mail.new_address()
    # Адрес для ОТПРАВКИ: для доменов из mail_dot_domains добавит точку в конце
    # (FQDN-обход блок-листа сервиса). Читаем всё равно по email (catch-all один).
    email_send = mail.send_address(email)
    password = password or _gen_password()
    started = time.time()
    client = ServiceClient(proxy=proxy)

    # 1. signup (ретраим retryable: капча/сеть/рейт-лимит; фатальные — сразу наружу)
    step(f"step 1/6: signup {email}")
    result, err = None, None
    for attempt in range(1, 4):
        try:
            res = client.signup(email_send, password)
        except (requests.RequestException, ServiceError) as exc:
            err = str(exc)
            step(f"step 1/6: signup, попытка {attempt}: {err}")
            time.sleep(15)
            continue
        if res.get("ok"):
            result = res
            break
        err = res.get("error", "signup failed")
        step(f"step 1/6: signup, попытка {attempt}: {err}")
        if not res.get("retryable"):
            break
        time.sleep(30)  # рейт-лимит отпускает не сразу
    if not result:
        return {"email": email, "password": password, "error": f"signup: {err}"}

    # 2. verify-ссылка из письма
    step("step 2/6: жду письмо с verify-ссылкой...")
    link, subj = mail.wait_link(email, sender_hint="<сервис>", since_ts=started)
    if not link:
        return {"email": email, "password": password, "error": "verify-письмо не пришло"}
    step("step 3/6: открываю verify-ссылку")
    try:
        client.verify_email(link)
    except Exception as exc:
        return {"email": email, "password": password, "error": f"verify GET: {exc}"}

    # 3. login — проверка живости аккаунта (verify-ссылка может уже залогинить)
    if client.session_valid():
        step("step 4/6: verify-ссылка уже залогинила — login не нужен")
    else:
        step("step 4/6: login...")
        login_result = client.login(email, password)
        if not login_result.get("ok"):
            return {"email": email, "password": password,
                    "error": f"login: {login_result.get('error')}"}

    # 4. API key
    step("step 5/6: создаю API key...")
    key_result = client.create_api_key()
    api_key = key_result.get("plaintext") or (key_result.get("key") or {}).get("plaintext")
    if not api_key:
        return {"email": email, "password": password,
                "error": f"api key: {key_result.get('error', key_result)}"}

    # 5. включить фри-модели
    step("step 6/6: включаю free models...")
    free = client.enable_free_models()
    free_ok = bool(free.get("ok") or free.get("free_models_enabled"))

    return {
        "email": email,
        "password": password,
        "api_key": api_key,
        "free_tier_enabled": free_ok,
        "access_token": client.access_token(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
