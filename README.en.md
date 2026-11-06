# Autoreg Template

A reusable Python skeleton for building **signup and email-verification workflows** for services you own or are explicitly authorized to test.

It is not a ready-made bot for any particular website. It gives you the boring parts — a registration contract, mail readers, proxy rotation, a local panel, atomic account storage and self-tests — so you can implement a service-specific adapter in one file.

> **Responsible use.** Use this only on services you own or have written permission to test. Running it against third-party services may violate their terms and local law. See [TERMS.md](TERMS.md).

## What is inside

- **One adapter contract** — `core/register.py` exposes `register_one()`, `test_key()` and `list_models()`.
- **Engine helpers** — HTTP (`requests`) and browser automation (`Camoufox` / Playwright) with humanized input.
- **Mail adapters** — catch-all mailbox over an admin API, Gmail plus-aliases over IMAP, or your own provider.
- **Proxy helpers** — static lists, rotating sessions, ISP zones, per-account exit IP.
- **Local panel** — start/stop runs, progress, account list, logs.
- **Atomic results** — JSONL bank + combo file, safe against partial writes and parallel workers.
- **Self-tests and a mock site** — no external service required.

## What is NOT included

- No credentials, API keys, proxy accounts or mailboxes.
- No service-specific adapters.
- No captcha-solving service keys.
- No accounts, cookies or sessions.

Everything sensitive is read from `config.json`, which is git-ignored.

## Quick start

```bash
cp config.example.json config.json
# edit config.json: base_url, mail_*, proxies
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 tests/selftest_browser.py     # optional, needs a browser
bash start.sh                          # local panel on 127.0.0.1:9900
```

Implement your service in `core/register.py`:

```python
def register_one(email=None, password=None, proxy=None, mail=None, on_step=None):
    ...
    return {"email": email, "api_key": "...", "status": "ok"}
```

Then call it from the panel or from your own CLI.

## Layout

```
core/          engine + adapter contract (start here: core/register.py)
panel/         local web panel
bridge/        optional key-pool gateway helpers
tests/         self-tests
tools/         recon and mock-site utilities
docs/          architecture and adapter notes
config.example.json
```

## Design notes

- **Idempotent writes.** `core/results.py` writes with a temp file + `os.replace` and an inter-process lock, so parallel workers do not lose rows.
- **Two-phase signup.** Some services create the account first and verify the email later; `output_pending` keeps those separate.
- **No secrets in code.** Proxy/mail credentials live in `config.json`; the example file contains placeholders only.
- **Bounded retries.** Network/5xx retries are capped; a hard block stops the run instead of burning accounts.

## License

MIT — see [LICENSE](LICENSE). No warranty; use at your own risk.
