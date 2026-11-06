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

## Rules

- Never use the skill against a service without the owner's permission.
- Do not bundle credentials, cookies, proxy accounts or API keys.
- Do not add captcha-solving service keys; keep the solver interface as an empty hook.
- Stop the run on hard blocks; do not retry indefinitely.
- Keep account storage atomic and outside version control.

## Files to read first

- `README.md`
- `docs/ARCHITECTURE.md`
- `core/register.py`
- `core/config.py`
- `core/results.py`
