# -*- coding: utf-8 -*-
"""Авто-пополнение реестра: скан истории воркспейса (data/*.jsonl, логи реггеров).

Catch-all не отдаёт список адресов через API, поэтому реестр алиасов ведём сами:
руками + из истории + из фонового обхода. Здесь — шаг «из истории»: находим
адреса @<домен> в файлах реггеров и (по желанию) коды из их логов.
"""
import json
import re
import time
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
CODE_LOG_RE = re.compile(r"код\s+из\s+письма\s*[:\-]\s*(\d{4,8})(?:\s*\(тема:\s*([^)]*)\))?", re.IGNORECASE)
ADDR_HINT_RE = re.compile(r"(?:адрес|email|почта)\s*[:\-]\s*([A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]+)", re.IGNORECASE)
MIME_HINT = "MIME-Version"

SKIP_DIRS = {"node_modules", ".git", "__pycache__", "legacy", "bak", "archive",
             ".venv", "venv", "logs", "apimail-panel", ".port-migration"}
GLOBS = ("*/data/*.jsonl", "*/data/*.json", "*/data/*.txt", "*/data/*.log",
         "*/data/runs/*.json", "*/data/runs/*.txt", "*/*.txt", "*/*.jsonl")


def _iter_files(root, max_files=600, max_bytes=3 * 1024 * 1024):
    root = Path(root)
    seen = set()
    for pattern in GLOBS:
        for path in sorted(root.glob(pattern)):
            if len(seen) >= max_files:
                return
            if not path.is_file() or str(path) in seen:
                continue
            parts = set(path.relative_to(root).parts)
            if parts & SKIP_DIRS:
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            seen.add(str(path))
            yield path


def scan_history(root, domains, import_log_codes=True, max_files=600, log=None,
                 max_codes=200):
    """Найти адреса (и коды из логов) в истории реггеров.

    Возвращает {"aliases": {addr: "файл"}, "codes": [rec, ...], "scanned": N}.
    """
    domains = {d.lower() for d in domains if d}
    aliases, codes, scanned = {}, [], 0
    for path in _iter_files(root, max_files=max_files):
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = _rel(path, root)
        for match in EMAIL_RE.finditer(text):
            addr = match.group(0).strip(".").lower()
            if addr.split("@", 1)[1] in domains or _subdomain_of(addr, domains):
                aliases.setdefault(addr, rel)
        if import_log_codes and len(codes) < max_codes:
            codes.extend(_codes_from_text(text, rel, domains))
    codes.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"aliases": aliases, "codes": codes[:max_codes], "scanned": scanned}


def _codes_from_text(text, rel, domains):
    """Коды из логов панелей реггеров («код из письма: 123456 (тема: ...)»)."""
    out = []
    if "код из письма" not in text.lower() and "code from mail" not in text.lower():
        return out
    last_addr = None
    for line in text.splitlines():
        hint = ADDR_HINT_RE.search(line)
        if hint:
            addr = hint.group(1).lower()
            if _addr_in_domains(addr, domains):
                last_addr = addr
            elif EMAIL_RE.fullmatch(addr):
                last_addr = None
        match = CODE_LOG_RE.search(line)
        if not match:
            continue
        code = match.group(1)
        subject = (match.group(2) or "").strip()
        ts_str = ""
        ts_match = TS_RE.search(line)
        ts = None
        if ts_match:
            ts_str = ts_match.group(1)
            try:
                ts = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                ts = None
        out.append({
            "address": last_addr or "",
            "code": code,
            "subject": subject,
            "from": "",
            "received_at": ts_str,
            "mail_id": None,
            "source": f"log:{rel}",
            "ts": ts or time.time(),
        })
    return out


def _addr_in_domains(addr, domains):
    if "@" not in addr:
        return False
    dom = addr.split("@", 1)[1]
    return dom in domains or _subdomain_of(addr, domains)


def _subdomain_of(addr, domains):
    if "@" not in addr:
        return False
    dom = addr.split("@", 1)[1]
    return any(dom.endswith("." + d) for d in domains)


def _rel(path, root):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def json_codes(path, domains, max_items=200):
    """Коды из jsonl/json реггеров, если они там сохранены (email+code)."""
    out = []
    path = Path(path)
    try:
        if path.suffix == ".jsonl":
            records = []
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        else:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                data = json.load(fh)
            records = data if isinstance(data, list) else [data]
    except (OSError, json.JSONDecodeError):
        return out
    for rec in records[:max_items]:
        if not isinstance(rec, dict):
            continue
        addr = str(rec.get("email") or rec.get("address") or "").lower()
        code = rec.get("code") or rec.get("mail_code")
        if not addr or not code:
            continue
        if not _addr_in_domains(addr, domains):
            continue
        out.append({
            "address": addr,
            "code": str(code),
            "subject": str(rec.get("code_subject") or rec.get("subject") or ""),
            "from": "",
            "received_at": "",
            "mail_id": None,
            "source": f"json:{path.name}",
            "ts": float(rec.get("ts") or rec.get("started_at") or time.time()),
        })
    return out