---
name: autoreg-template
description: Build and run authorized signup + email-verification workflows from a reusable Python template. Use when the user asks to create an autoreg/adapter for a service they own or are explicitly authorized to test.
---

# Autoreg Template Skill

This skill turns a service-specific signup flow into a small adapter on top of a reusable framework. It is intended for **owned services and authorized testing**, not for third-party abuse.

## Workflow

1. **Confirm authorization.** Ask which service and whether the user owns it or has written permission to test it. If not, stop.
2. **Copy the template.** Duplicate the project folder; never edit the template in place.
3. **Recon.** Use `tools/recon.py` or a browser to map: signup URL, form fields, captcha, email delivery, key/session issuance, and a cheap liveness check.
4. **Implement the adapter.** In `core/register.py`, write `register_one(email, password, proxy, mail, on_step)` plus `test_key()` and `list_models()`. Keep secrets in `config.json` only.
5. **Configure.** Copy `config.example.json` to `config.json`; fill `base_url`, mail provider, proxies, panel port.
6. **Test.** Run the self-test, then one live account on a test identity you control. Verify the key/session with a real request, not just HTTP 200 on signup.
7. **Run.** Start the panel, run a small batch with pauses, and stop on repeated hard blocks.
8. **Report.** Write a short report: flow, endpoints, what was verified live, limits, and known blockers.

## Bins & cards (Stripe)

Services that pay credit when a card is bound ("bind a card → get $N") need a card
step. Use `core/cardkit.py` — it covers generation, checking and Stripe calls.

1. **BIN → PAN (offline, free).** `gen_card("461994")` replicates the chkr.cc
   generator: the BIN (with optional `x` wildcards) is padded to 15/16 digits and
   filled with random digits so the whole number passes Luhn. Returns
   `PAN|MM|YYYY|CVV` as a `Card`. BIN list lives in `config.json` (`card_bins`).
2. **Checker (chkr.cc).** `check_card(card)` posts to `https://api.chkr.cc/`
   (`Origin`/`Referer: https://chkr.cc` are mandatory, otherwise 403), paces
   requests `chkr_check_interval_sec` (2.5 s, same as the site), handles 429 with
   `retryAfter`, and returns `{code: 1 Live / 0 Die / 2 Unknown, status, bank, ...}`.
   `pick_card()` combines generator + checker per `card_bin_mode`
   (`any` — first card, dead cards often pass $0 pre-auth; `live` — poll until
   Live; `off` — fixed `card_number`).
3. **Stripe (no browser needed for API calls).** `stripe_create_pm`,
   `stripe_confirm_setup`, `stripe_confirm_payment` hit `api.stripe.com` with the
   merchant's publishable key (`stripe_pk`), and `stripe_error()` turns the
   response into a readable decline.

Hard-won rules:

- Many merchants disable **publishable-key tokenization** outside their own
  surfaces → `unsupported_publishable_key_tokenization_surface`. Then the card
  must be entered in a browser on the merchant's domain (Card/Payment Element).
- Tokenize with the **Element instance** (`stripe.createPaymentMethod({type:'card', card: element})`),
  never with raw card data (blocked), then confirm the SetupIntent **server-side**
  with `payment_method=pm_…` — that call is allowed and returns honest JSON.
- `stripe.confirmCardSetup(...)` **inside the browser** can hang forever: Stripe
  Radar runs an hCaptcha challenge that never resolves in headless. Don't wait on it.
- Some BINs decline while others pass on the same merchant (e.g. one Visa BIN
  succeeded while a Mastercard BIN gave `generic_decline`). Keep BIN lists in
  config, cap attempts (`card_max_attempts`) — declined attempts build Radar risk.
- Run the card step direct: Bright Data rejects `CONNECT api.stripe.com` (403).

## Mail

- **Mail for signup:** a catch-all domain (own worker + admin API) or Gmail + app-password/IMAP is the
  reliable path. A quick option is an address from the Telegram bot @drakemailbot, but its
  availability and deliverability are **not guaranteed** (bots go down, some services blacklist
  temp domains) — keep a fallback and test delivery before running a batch.

## Rules

- Never use the skill against a service without the owner's permission.
- Do not bundle credentials, cookies, proxy accounts or API keys.
- Do not add captcha-solving service keys; keep the solver interface as an empty hook.
- Stop the run on hard blocks; do not retry indefinitely.
- Keep account storage atomic and outside version control.

## Files to read first

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/CARDKIT.md` — авто-бины/карты (chkr.cc + Stripe), если у сервиса карточный шаг
- `core/register.py`
- `core/cardkit.py`
- `core/config.py`
- `core/results.py`
