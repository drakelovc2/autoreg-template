# apimail panel (sources)

A small stdlib-only panel for a **catch-all mailbox engine** that exposes an
admin API. It shows the latest verification code, the mail feed for any address,
the alias registry and a search over stored codes.

It does **not** include the mail engine itself (the catch-all worker / SMTP
receiver). You point it at your own engine with `mail_base` and `admin_key`.

## What it expects from the engine

| Method | Path | Meaning |
|---|---|---|
| GET | `/admin/mails?address=<addr>&limit=N&offset=0` | list mail for one address |
| GET | `/admin/mails_unknow?limit=N` | mail for addresses outside the registry (optional) |

Auth: header `x-admin-auth: <admin_key>`. Some engines also require
`User-Agent: curl/8.5.0`; it is set by the client.

## Run

```bash
cp config.example.json config.json
# fill mail_base, mail_domain, admin_key
bash start.sh              # http://127.0.0.1:9971
bash start.sh --no-browser # headless
```

No third-party Python packages are needed.

## Config

| Field | Meaning |
|---|---|
| `mail_base` | base URL of your mail engine |
| `mail_domain` | catch-all domain; subdomains are picked up too |
| `admin_key` | admin API key (sent as `x-admin-auth`) |
| `poll_interval_sec` | background poll period |
| `aliases_per_cycle` | how many aliases are checked per cycle |
| `discover_unknown_mails` | use `/admin/mails_unknow` to discover new addresses |
| `gmail` | optional second source: Gmail plus-aliases over IMAP |

## Security

- The panel binds `127.0.0.1` only.
- `config.json` and `data/` are git-ignored.
- The admin key is a secret: do not commit it, do not expose the panel publicly.
- No accounts, cookies or mail data are shipped with these sources.
