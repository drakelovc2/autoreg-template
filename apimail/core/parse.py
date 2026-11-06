# -*- coding: utf-8 -*-
"""Разбор письма: MIME (base64/QP), тема/отправитель, извлечение кода.

Код ищем в теле (text/plain|text/html) — группа цифр с контекстом
«code/код/OTP/verification»; без контекста принимаем только 6-значные
(типовой формат сервисов). Даты/времена (`2026-10-07`, `10/07/2026`),
годы (`© 2026 Z.ai`, `7 окт. 2026 г.`) и служебные числа письма кодом
не считаем — иначе «последний код» показывает год.
Сырец письма приходит как полный MIME (`raw` в ответе админки apimail).
"""
import email
import email.header
import email.utils
import html as html_mod
import re

CODE_KEYWORDS = re.compile(
    r"(?:\bcode\b|\bкод\b|\bкода\b|otp|pin\b|verification|verif|verify|confirm|"
    r"passcode|one[\s-]?time|подтвержд|пароль|authenticat|sign[\s-]?in|"
    r"安全代码|验证码|認証コード)",
    re.IGNORECASE,
)

# маски: ссылки, адреса, длинные числа — чтобы не поймать номер заказа/трекинг
URL_RE = re.compile(r"<https?://[^>\s]+>|https?://\S+", re.IGNORECASE)
# ссылки для просмотра письма: href="…" в HTML-части (кнопки подтверждения/сброса)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
# якоря-кнопки часто оборачивают «Confirm email» текстом — берём и голые URL в HTML
BARE_URL_RE = re.compile(r"""https?://[^\s"'<>)\]]+""", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TAG_RE = re.compile(r"<[^>]+>")
STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
WS_RE = re.compile(r"[ \t\u00a0]+")
ZW_RE = re.compile(r"[\u200b-\u200d\ufeff]")
# литералы raw-unicode-escape в теле: «\u0434» (иначе их hex-хвосты — «коды»)
UNESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")

# цифры вплотную к разделителю даты — часть даты/времени (2026-10-07, 10/07/2026)
DATE_SEPS = ("-", "/")
# Код с дефисом в середине (Slack и родственные): XXX-XXX, напр. Y7L-QMK.
# Только при ключевом слове рядом (code/код/verification/…) — иначе ловит
# обычные дефисные слова вроде «e-mail»/«state-of-the-art».
DASH_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Za-z0-9])")
# 4-значное в этом диапазоне — почти всегда год (© 2026 Z.ai, «7 окт. 2026 г.»);
# принимаем его только вплотную к ключевому слову («ваш код: 2026»).
YEAR_MIN, YEAR_MAX = 1900, 2099
NEAR_WINDOW = 24  # «вплотную» для года: символов вокруг кандидата


def decode_mime_words(value):
    """Декодировать MIME-заголовок (RFC 2047) в строку."""
    if not value:
        return ""
    try:
        chunks = email.header.decode_header(str(value))
    except Exception:
        return str(value)
    out = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            for enc in (charset, "utf-8", "cp1251", "latin-1"):
                if not enc:
                    continue
                try:
                    out.append(chunk.decode(enc, "ignore"))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                out.append(chunk.decode("utf-8", "ignore"))
        else:
            out.append(str(chunk))
    return "".join(out).strip()


def html_to_text(markup):
    """Грубое HTML→текст: теги долой, сущности раскодировать (для поиска кода)."""
    if not markup:
        return ""
    text = STYLE_RE.sub(" ", markup)
    text = TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    return text


def extract_links(raw):
    """Ссылки из письма: (links, html).

    links — уникальные URL в порядке появления: сначала href из HTML-части
    (кнопки вроде «Confirm email» = активация аккаунта), затем голые URL,
    затем <url> из text/plain. html — сырой HTML-части (для вкладки HTML).
    """
    text_parts, html_parts = extract_bodies(raw)
    return _links_from_parts(text_parts, html_parts)


# служебные URL разметки: не ссылки письма (DTD/xmlns, статика, служебные схемы)
JUNK_URL_RE = re.compile(
    r"^https?://(?:www\.)?w3\.org/|\.dtd$|^https?://schemas\.|^mailto:|^tel:|"
    r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|woff2?|ttf)(?:\?|$)",
    re.IGNORECASE)


def _links_from_parts(text_parts, html_parts):
    links = []

    def _add(url):
        url = html_mod.unescape((url or "").strip())
        if not url or url in links or JUNK_URL_RE.search(url):
            return
        links.append(url)

    for part in html_parts:
        for match in HREF_RE.finditer(part):
            _add(match.group(1))
        for match in BARE_URL_RE.finditer(part):
            _add(match.group(0))
    for part in text_parts:
        for match in URL_RE.finditer(part):
            _add(match.group(0).strip("<>"))
    return links, "\n".join(html_parts)


def clean_for_code(text):
    text = unescape_artifacts(text or "")
    text = ZW_RE.sub("", text)
    text = URL_RE.sub(" ", text)
    text = EMAIL_RE.sub(" ", text)
    text = WS_RE.sub(" ", text)
    return text


def unescape_artifacts(text):
    """Развернуть литералы «\\u0434» обратно в символы.

    Письмо с не-ASCII телом, объявленное как 7bit без Transfer-Encoding,
    декодируется в raw-unicode-escape: текст выглядит как «\\u041f\\u043e…».
    Шестнадцатеричные хвосты таких литералов (0434, 043f, …) — 4 цифры
    вплотную к ключевому слову темы, то есть ложный «код».
    """
    def _sub(match):
        try:
            return chr(int(match.group(1) or match.group(2), 16))
        except (TypeError, ValueError):
            return " "

    return UNESCAPE_RE.sub(_sub, text)


def _digit_runs(text, min_digits, max_digits):
    pattern = re.compile(r"(?<!\d)(\d{%d,%d})(?!\d)" % (min_digits, max_digits))
    return list(pattern.finditer(text))


def _is_date_part(text, start, end):
    """True, если группа вплотную примыкает к - или / — это дата, не код.

    «Finished: 2026-10-07 21:01:00 UTC» → 2026 (слева пробел, справа дефис);
    «10/07/2026» → 10 и 2026. Дробные числа (1299.00) не задеваем: точка
    разделителем даты не считается.
    """
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return before in DATE_SEPS or after in DATE_SEPS


def _year_like(code):
    return len(code) == 4 and YEAR_MIN <= int(code) <= YEAR_MAX


def _keyword_near(text, match, window):
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    return bool(CODE_KEYWORDS.search(text[start:end]))


def extract_code(*texts, **opts):
    """Вернуть (code, context) — код из текста или (None, "").

    Приоритет: цифры рядом с ключевым словом («code», «код», OTP) → 6 цифр.
    Без контекста берём только 6-значные: 4/5/7/8-значные — лишь когда
    ключевое слово рядом или в теме (`hint`), иначе «последний код» ловит
    год, номер заказа или длительность.
    Контекст — фрагмент вокруг найденной группы.
    `hint` — дополнительный текст (обычно тема письма): если ключевое слово
    есть в теме, все группы считаются контекстными (письмо явно «про код»).
    """
    min_digits = int(opts.get("min_digits", 4))
    max_digits = int(opts.get("max_digits", 8))
    window = int(opts.get("window", 48))
    hint = opts.get("hint") or ""
    clean = clean_for_code("\n".join(t for t in texts if t))
    hint_is_code = bool(CODE_KEYWORDS.search(hint))

    # 0) код с дефисом (Slack XXX-XXX): принимаем только при ключевом слове рядом
    #    или в теме — «Slack confirmation code: Y7L-QMK».
    for match in DASH_CODE_RE.finditer(clean):
        start = max(0, match.start() - window)
        end = min(len(clean), match.end() + window)
        context = clean[start:end].strip()
        if hint_is_code or CODE_KEYWORDS.search(context):
            return match.group(1), context

    best = None  # (rank, length_rank, pos, code, context)
    for match in _digit_runs(clean, min_digits, max_digits):
        code = match.group(1)
        start = max(0, match.start() - window)
        end = min(len(clean), match.end() + window)
        context = clean[start:end].strip()
        contextual = hint_is_code or bool(CODE_KEYWORDS.search(context))
        is_six = len(code) == 6
        if _is_date_part(clean, match.start(), match.end()):
            continue  # часть даты: 2026 из «Finished: 2026-10-07 …»
        if not contextual and not is_six:
            continue  # без ключевого слова рядом — только 6-значные
        if _year_like(code) and not _keyword_near(clean, match, NEAR_WINDOW):
            continue  # «© 2026 Z.ai», «7 окт. 2026 г.» — год, а не код
        rank = 0 if contextual else 1
        len_rank = 0 if is_six else 1
        key = (rank, len_rank, match.start())
        if best is None or key < best[0]:
            best = (key, code, context)
    if best is None:
        return None, ""
    return best[1], best[2]


def _decode_part(part):
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "cp1251", "latin-1"):
        try:
            return payload.decode(enc, "ignore")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "ignore")


def extract_bodies(raw):
    """Вернуть (text_parts, html_parts) из MIME-сырца."""
    text_parts, html_parts = [], []
    try:
        msg = email.message_from_string(raw or "")
    except Exception:
        return text_parts, html_parts
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/plain", "text/html"):
            continue
        body = _decode_part(part)
        if not body:
            continue
        (text_parts if ctype == "text/plain" else html_parts).append(body)
    return text_parts, html_parts


def parse_mail(raw, code_min=4, code_max=8):
    """Разобрать MIME → словарь с темой/отправителем/датой/телом/кодом."""
    result = {
        "subject": "",
        "from": "",
        "to": "",
        "date_ts": None,
        "received_at": "",
        "code": None,
        "code_context": "",
        "body": "",
        "body_len": 0,
        "links": [],
        "html": "",
    }
    try:
        msg = email.message_from_string(raw or "")
    except Exception:
        return result

    result["subject"] = decode_mime_words(msg.get("Subject", ""))
    result["from"] = decode_mime_words(msg.get("From", ""))
    result["to"] = decode_mime_words(msg.get("To", ""))

    date_hdr = msg.get("Date", "")
    ts = None
    try:
        parsed = email.utils.parsedate_tz(date_hdr)
        if parsed:
            ts = int(email.utils.mktime_tz(parsed))
    except Exception:
        ts = None
    result["date_ts"] = ts
    if ts:
        result["received_at"] = _fmt_ts(ts)

    text_parts, html_parts = extract_bodies(raw)
    plain = "\n".join(text_parts)
    if not plain.strip() and html_parts:
        plain = "\n".join(html_to_text(h) for h in html_parts)
    result["body"] = plain.strip()
    result["body_len"] = len(result["body"])
    result["links"], result["html"] = _links_from_parts(text_parts, html_parts)

    code, context = extract_code(result["subject"], plain,
                                 min_digits=code_min, max_digits=code_max,
                                 hint=result["subject"])
    result["code"] = code
    result["code_context"] = context
    return result


def _fmt_ts(ts):
    import time as _time
    return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(int(ts)))


def fmt_ts(ts):
    """Unix → локальная строка (для записей без даты в письме)."""
    if not ts:
        return ""
    return _fmt_ts(ts)