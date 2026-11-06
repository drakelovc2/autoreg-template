# Почта через apimail / @apimaildrakebot

`MailClient` в `core/mail.py` — это catch-all admin API. Один и тот же клиент
работает с apimail: бот **[@apimaildrakebot](https://t.me/apimaildrakebot)**
выдаёт адрес и показывает входящие, а для автоматизации используется admin API.

## Что заполнить

`config.json` (значения — свои, в репозитории их нет):

```json
{
  "mail_base": "https://<ваш-apimail-хост>",
  "mail_admin_key": "<admin-key>",
  "mail_domain": "example.com",
  "mail_domains": ["example.com"]
}
```

`mail_domains` — список catch-all доменов; `new_address()` выбирает случайный.
Если домен заблокирован по точному имени, добавьте его в `mail_dot_domains` —
клиент отправит FQDN-форму с точкой в конце.

## Как это работает

- `new_address()` генерирует адрес на catch-all домене — ящик создавать не нужно.
- `list_mails(address)` читает письма через
  `GET {mail_base}/admin/mails?address=...&limit=...&offset=0`
  с заголовком `x-admin-auth`.
- `wait_code()` / `wait_link()` вытаскивают 6-значный код или verify-ссылку.
- `baseline()` фиксирует «сколько писем уже было» — для двухфазного `pending`.

## Своя панель (исходники в репо)

В `apimail/` лежит панель для **своего** catch-all движка: последний код, лента
писем, реестр алиасов, поиск. Зависимостей нет — только stdlib Python.

```bash
cd apimail
cp config.example.json config.json   # mail_base, mail_domain, admin_key
bash start.sh                        # http://127.0.0.1:9971
```

Панель — это UI, а не сам движок: письма она читает через
`GET {mail_base}/admin/mails` с заголовком `x-admin-auth`. Движок (catch-all
worker / SMTP-приёмник) остаётся ваш.

## Чего в репозитории нет

- реальных доменов, адресов, admin-ключей и ящиков;
- токена бота. Бот — UI для человека; для автоматизации используйте admin API
  или свой мост, а секреты держите в `config.json` (он в `.gitignore`).

## Ручной сценарий

1. Открыть [@apimaildrakebot](https://t.me/apimaildrakebot).
2. Получить адрес.
3. Вставить его в форму регистрации.
4. Забрать код или ссылку из чата.

Для потока без человека настройте `mail_base`/`mail_admin_key` и используйте
`MailClient` как обычно — реггеру не важно, откуда пришло письмо.
