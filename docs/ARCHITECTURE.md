# Architecture

## Layers

```
panel/          local web UI (start/stop, progress, accounts, logs)
core/register.py   service adapter: register_one / test_key / list_models
core/engines.py    HTTP + browser helpers
core/mail.py       mail providers (catch-all admin API, Gmail IMAP)
core/proxy.py      proxy rotation / static lists
core/results.py    atomic account storage
core/limits.py     optional quota/limits reader
core/retry.py      bounded retry helpers
```

## The adapter contract

Every service adapter implements:

```python
def register_one(email=None, password=None, proxy=None, mail=None, on_step=None):
    """Return a dict with at least email + status; add api_key/session on success."""

def test_key(api_key, proxy=None):
    """Return (ok, detail) after a real liveness request."""

def list_models(api_key, proxy=None):
    """Return a list of model ids, or []."""
```

`on_step(str)` is the progress callback used by the panel. Keep messages short and do not include secrets.

## Run lifecycle

1. **Prepare** — generate or accept an email, password and proxy.
2. **Signup** — submit the form over HTTP or a browser.
3. **Verify** — read the code/link from the mail provider and confirm.
4. **Key/session** — obtain the API key or session cookie.
5. **Liveness** — call a real endpoint; HTTP 200 on signup alone is not proof.
6. **Store** — write the account atomically; keep failures separate.

## Storage

`core/results.py` writes JSONL with a temporary file and `os.replace`, guarded by an inter-process lock. Each record is merged idempotently by email, so retries do not duplicate rows.

`output_pending` is for two-phase flows where the account exists but the email is not confirmed yet.

## Browser engines

`core/engines.py` exposes helpers for Camoufox and Playwright. Browser automation is optional: HTTP-only adapters are preferred when the service allows them.

## Captcha hooks

Captcha handling is intentionally a hook, not a bundled solver. If a service needs one, implement `solve_turnstile()` / a vision hook and keep any third-party solver key in `config.json`.

## Failure policy

- Retry network errors and 5xx a bounded number of times.
- On a hard block (403/429 with an anti-bot page), rotate the proxy once if configured, then stop.
- Never retry indefinitely; never create more accounts to "get past" a block.
