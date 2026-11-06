# Examples

Two minimal adapters that show the contract. They are deliberately generic and
target `https://service.example` — replace the URLs, selectors and payloads with
the ones from your own service.

- `http_otp/` — HTTP-only flow: request a code, read it from the mailbox, submit it, fetch a key.
- `browser_turnstile/` — browser flow: fill a form, wait for a Turnstile token, submit, verify.

Both return the same record shape, so the panel and `core/results.py` can store them.

## Useful public references

- Playwright — https://playwright.dev/python/
- Camoufox — https://camoufox.com/
- The structure of this package was inspired by `Ikhdev1/autoreg-agent-skill`
  (no code copied; that repository ships no license).
