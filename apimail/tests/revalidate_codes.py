#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пере-валидация истории кодов (data/codes.jsonl) новым парсером.

  env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH python3 tests/revalidate_codes.py [--fix-aliases]

Зачем: до ужесточения core/parse.py в историю попали ложные коды — годы из
служебных писем (© 2026 Z.ai, «Finished: 2026-10-07»). Скрипт чистит историю:

  * бэкап data/codes.jsonl → data/codes.jsonl.bak-<дата> (существующий не затирается);
  * записи с mail_id перечитываются с живого apimail (клиент панели, ключ из
    config.json) и код пересчитывается новым парсером:
      код совпал                  → запись остаётся;
      код не найден               → запись удаляется (ложный код);
      код другой                  → запись остаётся с исправленным кодом;
      письма нет в движке (404)   → запись удаляется;
      движок отбил ключ/не ответил → файл НЕ трогаем, выход 1;
  * записи без mail_id (коды из логов реггеров) остаются, только если тот же
    адрес+код подтверждён записью с mail_id (кросс-сверка);
  * --fix-aliases пересобирает last_code/last_code_at/codes в aliases.json по
    выжившим записям (в реестре иначе остаются те же годы).
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config as config_mod            # noqa: E402
from core import parse as parse_mod              # noqa: E402
from core.client import MailAuthError, MailError, MailClient  # noqa: E402
from core.store import atomic_write_json      # noqa: E402


def load_records(path):
    records, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return records, bad


def dump_records(path, records):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def backup(path):
    stamp = time.strftime("%Y-%m-%d")
    target = path.with_name(path.name + f".bak-{stamp}")
    seq = 2
    while target.exists():
        target = path.with_name(path.name + f".bak-{stamp}-{seq}")
        seq += 1
    shutil.copy2(path, target)
    return target


def revalidate(records, client):
    """Решения по записям: {индекс: rec|None} + список пояснений к удалённым."""
    decisions, dropped, corrected, problems = {}, [], [], []
    confirmed = set()
    for idx, rec in enumerate(records):
        mail_id = rec.get("mail_id")
        address = (rec.get("address") or "").lower()
        old_code = str(rec.get("code") or "")
        if not mail_id:
            continue  # решаем вторым проходом, после подтверждённых
        try:
            data, _mode = client.get_mail(address, mail_id)
        except MailAuthError as exc:
            problems.append((rec, f"ключ не принят: {exc}"))
            continue
        except MailError as exc:
            if getattr(exc, "status", None) == 404:
                decisions[idx] = None
                dropped.append((rec, "письма нет в движке (404)"))
            else:
                problems.append((rec, f"{exc}"))
            continue
        raw = (data or {}).get("raw") or ""
        if not raw:
            problems.append((rec, "движок вернул письмо без raw"))
            continue
        new_code = parse_mod.parse_mail(raw).get("code")
        if new_code == old_code:
            decisions[idx] = rec
            confirmed.add((address, old_code))
        elif new_code is None:
            decisions[idx] = None
            dropped.append((rec, "код не подтверждается новым парсером"))
        else:
            fixed = dict(rec, code=new_code)
            decisions[idx] = fixed
            confirmed.add((address, str(new_code)))
            corrected.append((rec, new_code))
    for idx, rec in enumerate(records):
        if rec.get("mail_id") or idx in decisions:
            continue
        key = ((rec.get("address") or "").lower(), str(rec.get("code") or ""))
        if key in confirmed:
            decisions[idx] = rec
        else:
            decisions[idx] = None
            dropped.append((rec, "лог-запись без подтверждения по mail_id"))
    return decisions, dropped, corrected, problems


def fix_aliases(data_dir, surviving):
    path = data_dir / "aliases.json"
    if not path.exists():
        return 0, []
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    best = {}
    counts = {}
    for rec in surviving:
        address = (rec.get("address") or "").lower()
        if not address or not rec.get("code"):
            continue
        counts[address] = counts.get(address, 0) + 1
        if not best.get(address) or (rec.get("ts") or 0) > (best[address].get("ts") or 0):
            best[address] = rec
    changed = []
    for address, rec in (payload.get("aliases") or {}).items():
        head = best.get(address)
        new_code = head.get("code") if head else None
        new_at = head.get("ts") if head else None
        new_count = counts.get(address, 0)
        if (rec.get("last_code"), rec.get("last_code_at"), rec.get("codes")) != \
                (new_code, new_at, new_count):
            changed.append((address, rec.get("last_code"), new_code))
            rec["last_code"] = new_code
            rec["last_code_at"] = new_at
            rec["codes"] = new_count
    if changed:
        backup(path)
        payload["updated_at"] = time.time()
        atomic_write_json(path, payload)
    return len(changed), changed


def main(argv=None):
    ap = argparse.ArgumentParser(description="пере-валидация codes.jsonl новым парсером")
    ap.add_argument("--fix-aliases", action="store_true",
                    help="пересобрать last_code/last_code_at/codes в aliases.json")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args(argv)

    cfg = config_mod.load()
    data_dir = Path(args.data_dir or cfg["data_dir"])
    codes_path = data_dir / "codes.jsonl"
    if not codes_path.exists():
        print(f"нет файла {codes_path}")
        return 1
    records, bad = load_records(codes_path)
    client = MailClient(cfg["mail_base"], cfg.get("admin_key") or "",
                        address_jwts=cfg.get("address_jwts") or {},
                        timeout=int(cfg.get("request_timeout_sec") or 25))
    decisions, dropped, corrected, problems = revalidate(records, client)
    if problems:
        print(f"движок не подтвердил {len(problems)} писем — файл НЕ тронут:")
        for rec, why in problems:
            print(f"  ? {rec.get('address')} mail_id={rec.get('mail_id')}: {why}")
        return 1

    backup_path = backup(codes_path)
    surviving = [decisions[i] for i in range(len(records)) if decisions.get(i)]
    dump_records(codes_path, surviving)
    by_mail = sum(1 for rec in records if rec.get("mail_id"))
    print(f"бэкап: {backup_path.name}")
    print(f"записей: {len(records)} (с mail_id {by_mail}, из логов {len(records) - by_mail}, "
          f"битых строк {bad})")
    print(f"перечитано с движка: {by_mail}, исправлено кодов: {len(corrected)}, "
          f"удалено: {len(dropped)}")
    for rec, new_code in corrected:
        print(f"  ~ mail_id={rec.get('mail_id')} {rec.get('code')} → {new_code} "
              f"({rec.get('subject')})")
    for rec, why in dropped:
        print(f"  - mail_id={rec.get('mail_id')} {rec.get('address')} "
              f"code={rec.get('code')!r} ({why}; тема: {rec.get('subject')})")
    print(f"осталось записей: {len(surviving)}")
    if args.fix_aliases:
        changed, items = fix_aliases(data_dir, surviving)
        print(f"aliases.json: поправлено алиасов {changed}")
        for address, old, new in items:
            print(f"  ~ {address}: last_code {old!r} → {new!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())