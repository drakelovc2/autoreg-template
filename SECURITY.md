# Security Policy

## Secrets

This repository contains **no credentials**. `config.example.json` uses placeholders only.

If you configure the project:

- keep `config.json` out of version control (it is in `.gitignore`);
- never commit mail keys, proxy credentials, cookies, session tokens or account banks;
- treat `data/` as private: it can contain account emails, passwords and API keys.

## Reporting

If you find a vulnerability in the template (not in a third-party service), open a private security advisory or contact the maintainer through the repository.

Please do not open a public issue containing working credentials or personal data.

## Scope

This project is a local automation skeleton. It does not run a public service and does not ship a captcha-solving backend. Reports about third-party services should go to the operator of that service.
