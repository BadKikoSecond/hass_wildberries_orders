# Wildberries Orders — Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Неофициальная интеграция **личного кабинета покупателя Wildberries** для Home Assistant.

Отслеживает активные доставки: статус, срок, готовность в ПВЗ, срок хранения (если есть в ответе API), оплату и срок жизни cookies-сессии.

> **Важно:** Wildberries не предоставляет публичный API для покупателей. Интеграция использует тот же внутренний `webapi/lk`, что и сайт `wildberries.ru`, с cookies вашей браузерной сессии. Это может перестать работать без предупреждения.

<p align="center">
  <img src="custom_components/wildberries_orders/brand/icon.png" alt="Wildberries" width="120" />
</p>

---

## Возможности

| Сущность | Назначение |
|----------|------------|
| `sensor.*_active_orders` | Сколько активных доставок |
| `sensor.*_at_pickup` | Сколько в ПВЗ |
| `sensor.*_in_transit` | Сколько в пути / в доставке |
| `sensor.*_past_purchases` | Сколько завершённых покупок (user-grade API) |
| `sensor.*_session_expires` | Когда истекает OAuth-токен / cookies |
| `binary_sensor.*_session_valid` | Сессия жива / ошибка опроса |
| `sensor.*_order_*_status` | Статус конкретной позиции + атрибуты |
| `binary_sensor.*_order_*_at_pickup` | Удобно для автоматизаций «забери посылку» |
| `binary_sensor.*_order_*_in_transit` | Удобно для «скоро приедет» |

### Атрибуты сенсора статуса заказа

- `order_number` — rId заказа
- `nm_id` — артикул товара на WB
- `eta` — ожидаемая дата доставки
- `delivery_type` — тип доставки / адрес ПВЗ
- `storage_until` — текст срока хранения (если распознан)
- `products_count` — число товаров
- `payment_status` — статус оплаты (если есть)
- `detail_url` — ссылка на раздел доставок
- `is_at_pickup_point`, `is_in_transit`

---

## Установка

### HACS (рекомендуется)

1. HACS → **Интеграции** → три точки → **Пользовательские репозитории**
2. URL репозитория + категория **Integration**
3. Установить **Wildberries Orders**
4. Перезагрузить Home Assistant

### Вручную

Скопируйте папку `custom_components/wildberries_orders` в `/config/custom_components/` и перезагрузите HA.

---

## Настройка

### Способ A — вставить cookies JSON (рекомендуется для Home Assistant)

1. Залогиньтесь на [wildberries.ru](https://www.wildberries.ru) в браузере на ПК
2. Экспортируйте cookies (Cookie-Editor) **или** `cookie.json` из `wb_phone_login.py` (см. способ B)
3. В мастере HA: **Вставить cookies JSON**

Обязательны **`x_wbaas_token`** (antibot) и marketplace-токен (`WBTokenV3` / `wbx__tokenData` в storage_state).

> На ARM / Docker / Python 3.14 в контейнере HA **Playwright не ставится** — это нормально. Опрос заказов идёт через **curl_cffi** (как в интеграции Ozon), браузер для работы не нужен.

### Способ B — телефон + код (только если Playwright доступен на хосте HA)

В мастере: **Телефон + код PUSH/SMS** → номер → код из приложения WB.

Playwright **не** входит в зависимости интеграции — HA не пытается ставить его при открытии мастера. Пункт появится только если Playwright уже установлен в окружении HA вручную.

На хосте HA при первом входе один раз скачается Chromium (~300 МБ):

```bash
# внутри окружения HA / SSH add-on (если pip install playwright прошёл)
python -m playwright install chromium
```

**Проще с ПК** — экспорт сессии без Playwright в HA:

```bash
cd hass_wildberries_orders
pip install -r requirements.txt playwright
playwright install chromium
python scripts/wb_phone_login.py 79117108265 -i -o cookie.json
```

Затем вставьте содержимое `cookie.json` в мастере HA (способ A).

После настройки опрос заказов идёт через **curl_cffi**, браузер больше не нужен.

### Повторный вход

Если сессия истекла, HA предложит **Reconfigure** / reauth — снова телефон или новый JSON cookies.

---

## Служба

```yaml
service: wildberries_orders.refresh
```

Принудительно обновить данные со всех настроенных аккаунтов.

---

## Примеры автоматизаций

### Уведомление, когда посылка в ПВЗ

```yaml
alias: WB — можно забирать
trigger:
  - platform: state
    entity_id: binary_sensor.wildberries_order_12345_0_at_pickup
    to: "on"
action:
  - service: notify.mobile_app_phone
    data:
      title: "Wildberries"
      message: >
        {{ state_attr('sensor.wildberries_order_12345_0_status', 'delivery_type') }}
        — {{ states('sensor.wildberries_order_12345_0_status') }}
```

### Сессия скоро истечёт

```yaml
alias: WB — обнови cookies
trigger:
  - platform: numeric_state
    entity_id: sensor.wildberries_session_expires
    attribute: days_remaining
    below: 7
action:
  - service: notify.persistent_notification
    data:
      title: "Wildberries Orders"
      message: "Осталось меньше недели до истечения cookies. Пересоздайте интеграцию с новым JSON."
```

---

## Нюансы и ограничения

### Не Seller API

Интеграция для **покупателя**, не для продавца. `marketplace-api.wildberries.ru` и токены из кабинета продавца здесь не используются.

### Cookies и antibot

- Запросы идут через **curl_cffi** с TLS-отпечатком Chrome — иначе Wildberries (wbaas) режет контейнер HA.
- Иногда всё равно нужна проверка antibot — тогда обновите cookies из браузера, где вы уже прошли проверку.
- С другого IP / VPN сессия может умереть раньше.

### API

- Активные доставки: `POST /webapi/v2/lk/myorders/delivery/active` + `Authorization: Bearer`
- Счётчик покупок: `GET user-grade.wildberries.ru/api/v6/grade?curr=rub`
- Карточки товаров: `card.wb.ru/cards/v2/detail`

Названия товаров подтягиваются из публичного card API по артикулу (если antibot пропускает).

### Зависимости

`curl_cffi` — ставится автоматически при установке интеграции (HACS pip-install из `manifest.json`).

---

Файл cookies = полный доступ к аккаунту Wildberries. Храните только в `/config`, не коммитьте в git, ограничьте бэкапы.

---

## CLI (для отладки вне HA)

В корне репозитория есть `cli.py` и пакет `wildberries_orders/`:

```bash
python cli.py --cookies cookies.json
```

---

## Иконка

Используется favicon Wildberries (`wildberries.ru`).

---

## Лицензия

MIT. Не аффилировано с Wildberries. Только для личного использования.
