#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оффлайн-проверки панели apimail (без сети).

  env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH python3 tests/selfcheck.py

Проверяем: парсер MIME/кода на фикстурах, хранилище (атомарность/дедуп/рестарт),
скан истории, fallback admin→JWT в клиенте, сборку панели и её API-логику.
exit 0 — всё зелёное.
"""
import base64
import email.utils
import json
import quopri
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config as config_mod          # noqa: E402
from core import history as history_mod        # noqa: E402
from core import parse as parse_mod            # noqa: E402
from core.client import (MailAuthError, MailClient,  # noqa: E402
                         MailError, normalize_row)
from core.indexer import MailIndex             # noqa: E402
from core.logbus import LogBus                 # noqa: E402
from core.store import Store                   # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(f"{name} {detail}")


def fixture_mime(subject, body, ctype="text/plain", cte="7bit", from_="Test <no-reply@service.tld>"):
    headers = [
        f"From: {from_}",
        "To: probe@example.com",
        "Subject: " + subject,
        "Date: Tue, 07 Oct 2026 22:20:10 +0000",
        "MIME-Version: 1.0",
        f"Content-Type: {ctype}; charset=UTF-8",
    ]
    if cte == "base64":
        headers.append("Content-Transfer-Encoding: base64")
        payload = base64.b64encode(body.encode("utf-8")).decode()
    elif cte == "quoted-printable":
        headers.append("Content-Transfer-Encoding: quoted-printable")
        payload = quopri.encodestring(body.encode("utf-8")).decode("ascii")
    else:
        payload = body
    return "\r\n".join(headers) + "\r\n\r\n" + payload + "\r\n"


def test_parse():
    print("── парсер MIME и кода ──")
    raw = fixture_mime("Your verification code for DeepSeek",
                       "Hello!\n\nYour verification code: 483920\n\nDeepSeek", cte="base64")
    parsed = parse_mod.parse_mail(raw)
    check("тема разобрана", parsed["subject"] == "Your verification code for DeepSeek",
          parsed["subject"])
    check("отправитель разобран", "no-reply@service.tld" in parsed["from"], parsed["from"])
    check("код из base64-тела", parsed["code"] == "483920", str(parsed["code"]))
    expected_ts = int(email.utils.mktime_tz(
        email.utils.parsedate_tz("Tue, 07 Oct 2026 22:20:10 +0000")))
    check("время письма разобрано (Date → локальное)",
          parsed["date_ts"] == expected_ts and parsed["received_at"] != "",
          f"{parsed['date_ts']} != {expected_ts} ({parsed['received_at']})")

    body_html = "<html><body><div>Код подтверждения: <b>8421</b></div></body></html>"
    raw_qp = fixture_mime("Подтверждение адреса", body_html,
                          ctype="text/html", cte="quoted-printable")
    parsed_qp = parse_mod.parse_mail(raw_qp)
    check("код из QP-html (4 цифры, utf-8)", parsed_qp["code"] == "8421", str(parsed_qp["code"]))
    check("html→текст: теги убраны", "<b>" not in parsed_qp["body"], parsed_qp["body"][:60])

    trap = ("Invoice #5544332211, total 1299.00 USD.\n"
            "Your verification code is 553311. Ref 998877")
    code, _ctx = parse_mod.extract_code("", trap)
    check("контекстный код побеждает номер счёта", code == "553311", str(code))

    zw = "Ваш код: 12\u200b34\u200b56 — действует 10 минут"
    check("нулевые пробелы в коде вычищены", parse_mod.extract_code("", zw)[0] == "123456",
          str(parse_mod.extract_code("", zw)[0]))

    check("кода нет — None", parse_mod.extract_code("", "Welcome aboard, no digits here")[0] is None)
    check("URL-цифры не код",
          parse_mod.extract_code("", "Reset: https://svc.tld/verify?a=123456&b=2")[0] is None)

    subj_hint = parse_mod.extract_code("код", "834721 и больше ничего")
    check("ключевое слово в теме помогает", subj_hint[0] == "834721", str(subj_hint[0]))

    eight = fixture_mime("verification code", "verification code: 12345678")
    check("8-значный код поддержан", parse_mod.parse_mail(eight)["code"] == "12345678",
          str(parse_mod.parse_mail(eight)["code"]))

    # новое правило: даты/годы — не код, fallback без контекста — только 6 цифр
    gh_body = ("[kokosasksos/vigilant-happiness] suite workflow run\r\n\r\n"
               "Repository: kokosasksos/vigilant-happiness\r\n"
               "Workflow: suite\r\n"
               "Duration: 17.0 seconds\r\n"
               "Finished: 2026-10-07 21:01:00 UTC\r\n\r\n"
               "View results: https://github.com/kokosasksos/vigilant-happiness"
               "/actions/runs/37686312741\r\n")
    gh = parse_mod.parse_mail(fixture_mime(
        "[kokosasksos/vigilant-happiness] Run failed: suite - main (2a1e3db)", gh_body))
    check("Actions-письмо: год из «Finished: 2026-10-07» не код",
          gh["code"] is None, str(gh["code"]))

    confirm = fixture_mime("Подтверждение адреса", "Подтверждение адреса: 8421")
    check("4 цифры с контекстом в теме найдены",
          parse_mod.parse_mail(confirm)["code"] == "8421",
          str(parse_mod.parse_mail(confirm)["code"]))

    escaped = parse_mod.extract_code(
        "Подтверждение адреса",
        "\\u041f\\u043e\\u0434\\u0442\\u0432\\u0435\\u0440\\u0436\\u0434\\u0435\\u043d\\u0438\\u0435: 8421",
        hint="Подтверждение адреса")
    check("escape-артефакты \\uXXXX не дают ложный код",
          escaped[0] == "8421", str(escaped[0]))

    check("6 цифр без контекста найдены",
          parse_mod.extract_code("", "Случайное число 834721 и больше ничего")[0] == "834721",
          str(parse_mod.extract_code("", "Случайное число 834721 и больше ничего")[0]))
    check("4 цифры без контекста не код",
          parse_mod.extract_code("", "Случайное число 8421 и больше ничего")[0] is None,
          str(parse_mod.extract_code("", "Случайное число 8421 и больше ничего")[0]))
    check("дата 10/07/2026 не код",
          parse_mod.extract_code("", "Заявка от 10/07/2026 принята")[0] is None,
          str(parse_mod.extract_code("", "Заявка от 10/07/2026 принята")[0]))

    zai = fixture_mime("Verify Your Email",
                       "This verification link will expire in 24 hours.\r\n"
                       "© 2026 Z.ai. All rights reserved.\r\n")
    check("год из копирайта не код (даже при «verify» в теме)",
          parse_mod.parse_mail(zai)["code"] is None,
          str(parse_mod.parse_mail(zai)["code"]))
    check("год вплотную к слову «код» остаётся кодом",
          parse_mod.extract_code("", "Ваш код: 2026")[0] == "2026",
          str(parse_mod.extract_code("", "Ваш код: 2026")[0]))

    decoded = parse_mod.decode_mime_words("=?UTF-8?B?0JrQvtC0INC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGP?=")
    check("RFC2047 тема раскодирована", decoded == "Код подтверждения", decoded)

    empty = parse_mod.parse_mail("")
    check("пустой MIME не падает", empty["code"] is None and empty["subject"] == "")

    # извлечение ссылок (письма-активации без кода: CapSolver и родственные)
    activation_html = (
        '<html><body><p>Hi,</p>'
        '<a href="https://svc.example.com/passport/activation?token=abc123def456">'
        'Confirm email</a>'
        '<p>Questions? https://www.example.com/</p></body></html>')
    act = parse_mod.parse_mail(fixture_mime("Your email verification", activation_html,
                                            ctype="text/html", cte="base64"))
    check("ссылка активации извлечена из HTML",
          any("/passport/activation?token=" in u for u in act["links"]), str(act["links"]))
    check("битая ссылка подтверждения не съедена как код",
          act["code"] is None, str(act["code"]))
    check("HTML-часть сохранена для просмотра", "<a href=" in (act.get("html") or ""))
    check("голый URL в HTML тоже пойман",
          any("www.example.com" in u for u in act["links"]), str(act["links"]))

    plain_link = parse_mod.parse_mail(fixture_mime(
        "Reset your password", "Open: https://svc.example.com/reset?token=zzz999 - 10 min"))
    check("ссылка из text/plain извлечена",
          any("reset?token=zzz999" in u for u in plain_link["links"]), str(plain_link["links"]))
    check("дубликаты ссылок схлопнуты",
          len(parse_mod.extract_links(fixture_mime(
              "s", '<a href="https://a.example.com/x">a</a>'
                   '<a href="https://a.example.com/x">a</a>', ctype="text/html"))[0]) == 1)
    check("письмо без ссылок - пустой список",
          parse_mod.extract_links(fixture_mime("s", "нет ссылок тут"))[0] == [])
    junk_html = ('<html><body><a href="https://x.example.com/go?t=1">go</a>'
                 '<img src="https://cdn.example.com/logo.png">'
                 '<a href="mailto:no-reply@example.com">mail</a></body></html>')
    junk = parse_mod.extract_links(fixture_mime("s", junk_html, ctype="text/html"))[0]
    check("служебные URL отфильтрованы (статика/mailto)",
          junk == ["https://x.example.com/go?t=1"], str(junk))


def test_store():
    print("── хранилище (aliases.json / codes.jsonl) ──")
    with tempfile.TemporaryDirectory() as tmp:
        log = LogBus()
        store = Store(tmp, log=log)
        store.add_alias("Dsglbysp2n@example.com", source="manual")
        rec = store.alias("dsglbysp2n@example.com")
        check("алиас нормализован (lower)", rec is not None and rec["address"] == "dsglbysp2n@example.com")
        added = store.append_code({
            "address": "dsglbysp2n@example.com", "code": "076915",
            "subject": "Your verification code for DeepSeek", "from": "noreply@deepseek.com",
            "received_at": "2026-10-07 22:20:16", "mail_id": 17, "source": "apimail",
            "ts": 1791402016.0,
        })
        dup = store.append_code({
            "address": "dsglbysp2n@example.com", "code": "076915",
            "mail_id": 17, "source": "apimail", "ts": 1791402016.0,
        })
        check("код записан", added is True)
        check("дубль по mail_id отброшен", dup is False)
        store.update_alias("dsglbysp2n@example.com", mails=3, last_code="076915")
        store.set_state(last_poll=time.time(), last_poll_ok=True)
        store.save(force=True)

        aliases_file = Path(tmp) / "aliases.json"
        codes_file = Path(tmp) / "codes.jsonl"
        check("aliases.json создан", aliases_file.exists())
        check("codes.jsonl создан", codes_file.exists())
        payload = json.loads(aliases_file.read_text(encoding="utf-8"))
        check("aliases.json — валидный JSON с записью",
              "dsglbysp2n@example.com" in payload.get("aliases", {}))
        lines = [json.loads(l) for l in codes_file.read_text(encoding="utf-8").splitlines() if l]
        check("codes.jsonl — одна строка на код", len(lines) == 1, str(len(lines)))
        check("нет .tmp-остатков", not list(Path(tmp).glob("*.tmp")))

        store2 = Store(tmp, log=log)
        check("история пережила рестарт", len(store2.codes) == 1 and store2.aliases)
        check("last_code читается", (store2.last_code() or {}).get("code") == "076915")
        feed, total = store2.feed(address="dsglbysp2n@example.com", limit=10)
        check("лента фильтрует по адресу", total == 1 and feed[0]["code"] == "076915")
        check("поиск по префиксу находит алиас",
              any(r["address"] == "dsglbysp2n@example.com"
                  for r in store2.search("dsgl")))
        check("поиск по коду находит запись",
              any("076915" in (r.get("matched_codes") or []) for r in store2.search("076915")))
        removed = store2.remove_alias("dsglbysp2n@example.com")
        check("алиас удаляется", removed and store2.alias("dsglbysp2n@example.com") is None)

        try:
            store2.add_alias("мусор")
            check("кривой адрес не проходит", False, "исключения не было")
        except ValueError:
            check("кривой адрес не проходит", True)


def test_history():
    print("── авто-пополнение из истории воркспейса ──")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "regger" / "data").mkdir(parents=True)
        (root / "regger" / "data" / "panel.log").write_text(
            "2026-10-07 22:19:47      адрес: dsglbysp2n@example.com\n"
            "2026-10-07 22:20:16      код из письма: 076915 (тема: Your verification code for DeepSeek)\n",
            encoding="utf-8")
        (root / "regger" / "data" / "accounts.jsonl").write_text(
            json.dumps({"email": "conol1397490503@example.com", "status": "ok"}) + "\n"
            + json.dumps({"email": "someone@example.com"}) + "\n", encoding="utf-8")
        result = history_mod.scan_history(root, ["example.com"])
        check("адреса из истории найдены",
              "dsglbysp2n@example.com" in result["aliases"]
              and "conol1397490503@example.com" in result["aliases"])
        check("чужой домен отброшен", "someone@example.com" not in result["aliases"])
        check("код из лога импортирован",
              any(c["code"] == "076915" for c in result["codes"]))
        rec = [c for c in result["codes"] if c["code"] == "076915"][0]
        check("адрес кода из лога восстановлен",
              rec["address"] == "dsglbysp2n@example.com", rec["address"])
        check("тема кода из лога разобрана",
              "DeepSeek" in rec["subject"], rec["subject"])
        check("время кода из лога разобрано", rec["received_at"].startswith("2026-10-07"),
              rec["received_at"])


def test_client():
    print("── клиент apimail (без сети) ──")
    raw = fixture_mime("Your verification code for DeepSeek",
                       "Your verification code: 076915")
    row = normalize_row({"id": 17, "address": "dsglbysp2n@example.com", "raw": raw},
                        "dsglbysp2n@example.com")
    check("normalize_row: код извлечён", row["code"] == "076915", str(row["code"]))
    check("normalize_row: тема на месте", "DeepSeek" in row["subject"])
    check("normalize_row: адрес проставлен", row["address"] == "dsglbysp2n@example.com")
    check("normalize_row: id сохранён", row["id"] == 17)

    client = MailClient("https://apimail.invalid", admin_key="stale-key",
                        address_jwts={"probe@example.com": "jwt-token"})
    calls = []

    def fake_request(path, params=None, headers=None, method="GET", payload=None):
        calls.append((path, dict(headers or {})))
        if path.startswith("/admin/"):
            return 401, '{"code":"AUTH_ADMIN_CREDENTIAL_INVALID","message":"admin password required"}'
        return 200, json.dumps({"results": [{"id": 5, "address": "probe@example.com",
                                             "raw": raw}], "count": 1})

    client._request = fake_request  # type: ignore[assignment]
    items, count, mode = client.list_mails("probe@example.com", limit=5, offset=0)
    check("админ-ключ не принят → ошибка распознана",
          calls[0][0] == "/admin/mails" and len(calls) == 2, str(calls))
    check("fallback на JWT-режим сработал", mode == "jwt", mode)
    check("JWT ушёл в Authorization",
          calls[1][1].get("Authorization") == "Bearer jwt-token", str(calls[1][1]))
    check("лента в JWT-режиме разобрана", count == 1 and items[0]["code"] == "076915")

    client_nokey = MailClient("https://apimail.invalid", admin_key="")
    try:
        client_nokey.list_mails("nope@example.com")
        check("без ключа и JWT — MailAuthError", False, "исключения не было")
    except MailAuthError:
        check("без ключа и JWT — MailAuthError", True)

    jwt_only = MailClient("https://apimail.invalid", admin_key="",
                          address_jwts={"*": "any-jwt"})
    check("wildcard-JWT даёт режим jwt", jwt_only.mode("x@example.com") == "jwt")

    bad = MailClient("https://apimail.invalid", admin_key="k")
    bad._request = lambda *a, **kw: (500, "boom")  # type: ignore[assignment]
    try:
        bad.list_mails("x@example.com")
        check("5xx → MailError", False, "исключения не было")
    except MailError as exc:
        check("5xx → MailError", not isinstance(exc, MailAuthError), str(exc))


def test_index():
    print("── полнотекстовый индекс (SQLite FTS5) ──")
    with tempfile.TemporaryDirectory() as tmp:
        idx = MailIndex(tmp, log=LogBus())
        raw_a = fixture_mime("Welcome to GameService",
                             "Hi lfodoskdkd!\n\nConfirm your account, код: 553311")
        raw_b = fixture_mime("Your verification code",
                             "Hello kroshka2007, your verification code: 876543")
        raw_c = fixture_mime("Новости сервиса", "Вечерний дайджест без ников")
        check("письмо A проиндексировано",
              idx.index_row({"id": 11, "address": "nk1@example.com", "raw": raw_a}))
        idx.index_row({"id": 12, "address": "kroshka2007@gh.example.com", "raw": raw_b})
        idx.index_row({"id": 13, "address": "news@example.com", "raw": raw_c})
        check("дубль по mail_id не индексируется",
              idx.index_row({"id": 11, "address": "nk1@example.com", "raw": raw_a}) is False)

        rows, mode = idx.search("lfodoskdkd")
        check("ник из тела найден", len(rows) == 1, str([r["address"] for r in rows]))
        check("найден правильный адрес", rows and rows[0]["address"] == "nk1@example.com")
        check("сниппет с подсветкой отдан", rows and "<b>" in (rows[0]["snippet"] or ""),
              rows[0]["snippet"] if rows else "")
        check("режим FTS", mode == "fts", mode)

        rows, _ = idx.search("LFODOSKDKD")
        check("регистр не важен", len(rows) == 1)
        rows, _ = idx.search("kroshka2007")
        check("ник из приветствия найден на своём адресе",
              len(rows) >= 1 and rows[0]["address"] == "kroshka2007@gh.example.com",
              str([r["address"] for r in rows]))
        check("код письма доступен в выдаче", rows[0]["code"] == "876543", str(rows[0]["code"]))
        rows, _ = idx.search("несуществующийник")
        check("промах — пусто, без падения", rows == [])
        rows, mode = idx.search('ник " с кавычкой')
        check("спецсимволы: fallback на подстроку не падает", isinstance(rows, list), mode)

        st = idx.stats()
        check("stats: 3 письма в индексе", st["indexed"] == 3, str(st["indexed"]))

        # рестарт: база на диске, поиск живёт без переиндексации
        idx.close()
        idx2 = MailIndex(tmp, log=LogBus())
        rows, _ = idx2.search("lfodoskdkd")
        check("индекс пережил рестарт", len(rows) == 1 and rows[0]["address"] == "nk1@example.com")

        # sync с фейковым клиентом: циклический offset и дедуп.
        # Движок отдаёт count только на первой странице, дальше — 0
        # (проверено на живом 17.10): count=0 — «неизвестно», а не конец.
        pages = {
            0: ([{"id": 5, "address": "a@example.com", "raw": raw_a},
                 {"id": 4, "address": "b@example.com", "raw": raw_b}], 5),
            2: ([{"id": 3, "address": "c@example.com", "raw": raw_c},
                 {"id": 2, "address": "d@example.com", "raw": raw_a}], 0),
            4: ([{"id": 1, "address": "e@example.com", "raw": raw_b}], 0),
        }

        class FakeClient:
            def admin_all_mails(self, limit=100, offset=0):
                rows, count = pages.get(offset, ([], 0))
                return rows, count

        idx2.reset()
        stats = idx2.sync(FakeClient(), pages=1, limit=2)
        check("sync: первая страница проиндексирована",
              stats["indexed_new"] == 2 and stats["pages"] == 1, json.dumps(stats))
        check("sync: курсор сдвинут", idx2.stats()["offset"] == 2, str(idx2.stats()["offset"]))
        stats = idx2.sync(FakeClient(), pages=1, limit=2)
        check("sync: полная страница с count=0 — НЕ конец списка",
              idx2.stats()["offset"] == 4 and stats["indexed_new"] == 2,
              json.dumps(stats) + " offset=" + str(idx2.stats()["offset"]))
        stats = idx2.sync(FakeClient(), pages=1, limit=2)
        check("sync: круг замкнулся (короткая страница → offset 0)",
              idx2.stats()["offset"] == 0 and stats["indexed_new"] == 1, json.dumps(stats))
        stats = idx2.sync(FakeClient(), pages=3, limit=2)
        check("sync: повторный круг ничего не дублирует",
              stats["indexed_new"] == 0 and idx2.stats()["indexed"] == 5, json.dumps(stats))
        rows, _ = idx2.search("lfodoskdkd")
        check("sync: ник находится после циклической прокрутки",
              len(rows) == 2 and {r["address"] for r in rows} ==
              {"a@example.com", "d@example.com"}, str([r["address"] for r in rows]))
        idx2.close()


def test_server():
    print("── панель: сборка и API-логика ──")
    import importlib
    server = importlib.import_module("panel.server")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = config_mod.load(Path(tmp) / "нет-такого-config.json")
        cfg["data_dir"] = tmp
        cfg["mail_base"] = "https://apimail.invalid"
        cfg["admin_key"] = "test-key-abcdefghijkl"
        cfg["poll_interval_sec"] = 3600
        app = server.build_app(cfg)
        app.port = 9971
        s = app.summary()
        check("summary: сервис/порт/версия", s["service"] == "apimail-panel" and s["port"] == 9971,
              json.dumps({k: s[k] for k in ("service", "port", "version")}))
        check("summary: counts на месте",
              set(["aliases", "mails", "codes"]).issubset(s["counts"].keys()))
        check("summary: last_code = null при пустой истории", s["last_code"] is None)
        check("summary: режим доступа в mail", "mode" in s["mail"] and "interval_sec" in s["mail"])
        masked = s["registry"]["admin_key_masked"]
        check("summary: маска ключа без утечки",
              "•" in masked and "abcdefghijkl" not in masked and masked.startswith("test"),
              masked)

        # живого apimail нет — mails() должен отдать историю с предупреждением
        app.client.list_mails = lambda *a, **kw: (_ for _ in ()).throw(
            MailAuthError("ключ не принят (тест)"))  # type: ignore[assignment]
        app.store.add_alias("dsglbysp2n@example.com", source="history")
        app.store.append_code({"address": "dsglbysp2n@example.com", "code": "076915",
                               "subject": "Your verification code for DeepSeek",
                               "from": "noreply@deepseek.com",
                               "received_at": "2026-10-07 22:20:16", "mail_id": 17,
                               "source": "apimail", "ts": 1791402016.0})
        data = app.mails("dsglbysp2n@example.com", limit=10, offset=0)
        check("mails: при отказе apimail отдаёт историю", data["source"] == "history")
        check("mails: предупреждение заполнено", "не отдал" in (data["warning"] or ""),
              data.get("warning"))
        check("mails: код в ленте", data["results"][0]["code"] == "076915")

        last = app.store.last_code()
        check("last_code: формат ответа",
              set(["code", "address", "subject", "received_at", "mail_id"]).issubset(last.keys()))

        # mail_raw: отдаёт тело, ссылки активации и HTML-часть
        act_raw = fixture_mime(
            "Your email verification",
            '<html><body><a href="https://svc.example.com/passport/'
            'activation?token=tok123">Confirm email</a></body></html>',
            ctype="text/html", cte="base64")
        app.client.get_mail = lambda *a, **kw: (
            {"id": 1010, "address": "drake@example.com", "raw": act_raw}, "admin")
        mail = app.mail_raw("drake@example.com", 1010)
        check("mail_raw: тело разобрано", mail["subject"] == "Your email verification",
              mail["subject"])
        check("mail_raw: ссылки активации отданы API",
              any("activation?token=tok123" in u for u in mail["links"]), str(mail["links"]))
        check("mail_raw: HTML отдан API", "<a href=" in (mail["html"] or ""))
        check("mail_raw: размер письма", mail["raw_len"] == len(act_raw))

        alias_data = app.aliases()
        check("aliases: реестр + счётчики",
              alias_data["count"] == 1 and alias_data["counts"]["codes"] == 1)
        check("aliases: человекочитаемое время",
              bool(alias_data["aliases"][0]["last_mail_str"] is None
                   or isinstance(alias_data["aliases"][0]["last_mail_str"], str)))

        search = app.search("dsgl")
        check("search: находит адрес", any(r["address"].startswith("dsgl") for r in search["results"]))

        # полнотекстовый поиск по содержимому писем (FTS5)
        app.index.index_mail(4242, "niknick@example.com", parse_mod.parse_mail(
            fixture_mime("Welcome", "Hi niknick9000, подтверди регистрацию")))
        body_search = app.search_body("niknick9000")
        check("search_body: ник из письма найден",
              body_search["count"] == 1
              and body_search["results"][0]["address"] == "niknick@example.com",
              json.dumps(body_search["results"]))
        check("search_body: статистика индекса в ответе",
              body_search["index"]["indexed"] >= 1)
        check("summary: блок index на месте", "indexed" in (s := app.summary()).get("index", {})
              and s["index"]["indexed"] >= 1)
        reset = app.index_reset()
        check("index_reset: индекс очищен", reset["index"]["indexed"] == 0)
        added = app.add_alias("probe@example.com")
        check("POST /api/aliases логика", added["added"] is True)
        removed = app.remove_alias("probe@example.com")
        check("DELETE /api/aliases логика", removed["removed"] is True)
        try:
            app.remove_alias("нет@example.com")
            check("DELETE неизвестного → KeyError", False)
        except KeyError:
            check("DELETE неизвестного → KeyError", True)

        handler_ok = server.Handler.app is app and hasattr(server.Handler, "do_DELETE")
        check("Handler привязан к app и умеет DELETE", handler_ok)
        server_src = (Path(ROOT) / "panel" / "server.py").read_text(encoding="utf-8")
        check("порт по умолчанию 9971 и только 127.0.0.1",
              server.DEFAULT_PORT == 9971 and 'default="127.0.0.1"' in server_src
              and "панель слушает только 127.0.0.1" in server_src)

        html = (Path(ROOT) / "panel" / "static" / "index.html").read_text(encoding="utf-8")
        check("index.html: поиск по содержимому на месте",
              'id="body-q"' in html and "/search_body?q=" in html and "/index/sync" in html)
        check("index.html: APP_CONFIG с портом 9971 и API_BASE",
              'SERVICE_NAME: "apimail-panel"' in html and "DEFAULT_PORT: 9971" in html
              and 'API_BASE: "/api"' in html)
        check("index.html: MOCK_FALLBACK выключен", "MOCK_FALLBACK: false" in html)
        check("index.html: без внешних URL",
              "http://" not in html.replace("http://127.0.0.1", "")
              and "https://" not in html
              and "cdn" not in html.lower())
        app.stop()


def main():
    print("apimail-panel selfcheck (оффлайн)\n")
    test_parse()
    test_store()
    test_history()
    test_client()
    test_index()
    test_server()
    print(f"\nпроверок: {CHECKS[0]}, провалов: {len(FAILS)}")
    if FAILS:
        for fail in FAILS:
            print("  ✗", fail)
        return 1
    print("все проверки пройдены ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())