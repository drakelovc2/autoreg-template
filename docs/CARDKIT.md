# cardkit — авто-бины и карты (chkr.cc + Stripe)

Модуль `core/cardkit.py` закрывает карточный шаг реггеров: генерирует карты по BIN
(валидные по Luhn), при необходимости чекает их через `api.chkr.cc` и умеет
подтверждать Stripe `SetupIntent`/`PaymentIntent` картой.

Живьём проверено 08.11.2026 на CodeGateway (`autoreg/codegateway-register`):
бин `461994` → привязка карты в Stripe → бонус $2.

## 1. Генератор (офлайн, $0)

Алгоритм чкр.cc повторён на сервере: BIN с `x`-масками, недостающие позиции —
случайные цифры, последняя `x` — контрольная цифра Luhn (если последняя цифра
фиксирована — перебор до 500 попыток).

```python
from core.cardkit import gen_card

gen_card("461994").line()            # 461994xxxxxxxxxx|MM|YYYY|CVV
gen_card("52792622911").line()       # BIN 11 цифр → хвост добирается
gen_card("461994xxxxxxxxxxxx")       # своя маска, длина 16
```

Срок действия: текущий год + `card_ttl_years` (по умолчанию 2..5), CVV 3 цифры
(4 для Amex `3xxx`).

## 2. Чекер chkr.cc

Тот же API, что у сайта: `POST https://api.chkr.cc/` с `{"data": "PAN|MM|YYYY|CVV"}`.
Ответ: `code` 1 = Live, 0 = Die, 2 = Unknown.

```python
from core.cardkit import gen_card, check_card, is_live

res = check_card(gen_card("461994"))
is_live(res)          # True/False
res["status"], res["bank"], res["country"]
```

Нюансы (проверено):
- без браузерного `User-Agent` + `Origin/Referer: https://chkr.cc` → **403**;
- после нескольких запросов подряд → **429**; модуль ждёт `retryAfter`/`Retry-After`;
- пауза между чеками `chkr_check_interval_sec` (2.5 с, как в вебе).

## 3. Stripe (вызовы API)

```python
from core.cardkit import (gen_card, stripe_create_pm,
                          stripe_confirm_setup, stripe_confirm_payment, stripe_error)

card = gen_card("461994")
pm = stripe_create_pm(pk_live, card)                  # /v1/payment_methods
stripe_confirm_setup(client_secret, pk_live, card)    # /v1/setup_intents/<id>/confirm
stripe_confirm_payment(client_secret, pk_live, card)  # /v1/payment_intents/<id>/confirm
stripe_error(resp)                                    # читаемый текст ошибки
```

**Ограничение мерчанта**: многие включают в Stripe Dashboard запрет
«publishable-key tokenization» — тогда прямой POST с `card[number]` и даже
`stripe.createPaymentMethod` с сырыми данными из JS получают
`unsupported_publishable_key_tokenization_surface`. Единственный путь — карту
вводить в **браузере на домене мерчанта** (Payment/Card Element), см.
`codegateway-register/core/cardbind.py`: токенизация Элементом в headless-Chromium
→ `pm_…`, затем **серверный** `confirm` по `payment_method=pm_…` (эта схема
ограничением не режется и отдаёт честный JSON).

Зависание `confirmCardSetup` из браузера: Radar гоняет hCaptcha, в headless она
не решается → промис не резолвится. Поэтому confirm делаем серверно.

## 4. Конфиг (config.json)

```json
"card_bins": ["461994"],
"card_bin_mode": "any",
"card_max_attempts": 6,
"card_ttl_years": [2, 5],
"card_zip": "10001",
"card_number": "", "card_mm": "", "card_yyyy": "", "card_cvv": "",
"chkr_api": "https://api.chkr.cc/",
"chkr_check_interval_sec": 2.5,
"chkr_max_429_retries": 3,
"chkr_max_generate": 40,
"stripe_pk": "pk_live_…",
"card_step_direct": true
```

`card_bin_mode`:
- `any` — первая сгенерированная карта (мёртвые часто проходят $0-предавторизацию);
- `live` — гонять через chkr до Live (медленно и упирается в лимит API);
- `off` — генератор выключен, берём `card_number`.

`pick_card()` — высокоуровневый подбор с учётом конфига (принимает `fixed`).

## 5. CLI

```bash
python3 -m core.cardkit gen 5 --bin 461994
python3 -m core.cardkit check "4619940000000000|12|2029|123"
python3 -m core.cardkit pick --bin 461994 --mode any
```

## 6. Что важно помнить

- Карты — одноразовые по смыслу: бин и срок живут, пока платёжка их принимает.
- `52792622911` (Mastercard, JP) у CodeGateway даёт `generic_decline`, `461994`
  (Visa, NP) проходит — бин-лист держи в конфиге, а не в коде.
- Не долби declined-картами один и тот же IP/аккаунт десятки раз: Radar
  накапливает риск; `card_max_attempts` ограничивает перебор.
