# Telegram Referral Wheel Bot

Готовий Telegram-бот на Python 3.11 + aiogram 3.x з SQLite, рефералами, перевіркою підписки, серверною рулеткою, Telegram Web App, виплатами та адмін-панеллю.

## Структура

```text
.
├── bot.py
├── requirements.txt
├── render.yaml
├── README.md
└── web/
    └── index.html
```

## 1. Створення бота

У Telegram відкрийте @BotFather → `/newbot` → створіть бота. Скопіюйте token.

`BOT_USERNAME` — username бота без `@`, наприклад `my_wheel_bot`.

## 2. Додайте бота адміністратором каналу

Канал: https://t.me/+CGRBOztPG64wMWMy

Додайте бота адміністратором каналу. Для `getChatMember` бот повинен мати право бачити учасників/бути адміністратором.

## 3. CHANNEL_ID

Для приватного каналу потрібен Telegram numeric ID на кшталт `-1001234567890`. Його можна отримати через службового бота/власні інструменти Telegram API або з логів/оновлень після взаємодії з каналом. У Render задайте `CHANNEL_ID` саме цим значенням.

## 4. Telegram ID адміністратора

Надішліть повідомлення боту на кшталт `/start`, а Telegram ID можна отримати через бота для показу ID або з логів/інструментів Telegram. У `ADMIN_IDS` можна вказати декілька ID через кому:

```text
123456789,987654321
```

## 5. Render

Створіть **Web Service** з GitHub-репозиторію.

Blueprint `render.yaml` вже містить build/start команди, health check та persistent disk для SQLite. Диск потрібен, якщо база не повинна зникати після перезапуску/деплою.

Команди:

```bash
pip install -r requirements.txt
python bot.py
```

Render автоматично передає `PORT`; сервер слухає `0.0.0.0`.

## 6. Environment Variables

Обов'язково:

```text
BOT_TOKEN=токен_від_BotFather
BOT_USERNAME=username_бота_без_@
CHANNEL_ID=-100xxxxxxxxxx
CHANNEL_URL=https://t.me/+CGRBOztPG64wMWMy
ADMIN_IDS=123456789
RENDER_EXTERNAL_URL=https://your-service.onrender.com
DB_PATH=/var/data/bot.db
```

`RENDER_EXTERNAL_URL` — стабільний HTTPS URL вашого Render Web Service, без `/` в кінці.

## 7. Webhook

Після старту застосунок сам встановлює:

```text
https://your-service.onrender.com/telegram/webhook
```

Випадковий URL не використовується. При перезапуску webhook встановлюється знову на той самий endpoint.

## 8. Запуск локально

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN='...'
export BOT_USERNAME='...'
export CHANNEL_ID='-100...'
export CHANNEL_URL='https://t.me/+CGRBOztPG64wMWMy'
export ADMIN_IDS='123456789'
export RENDER_EXTERNAL_URL='https://example.com'
export DB_PATH='./bot.db'
python bot.py
```

Для локального Telegram Web App потрібен HTTPS URL; для Render це вирішується автоматично.

## 9. Адмін-панель

Адміністратор надсилає:

```text
/admin
```

Доступ перевіряється через `ADMIN_IDS`.

Панель має:

- 📊 Статистика
- 👥 Користувачі
- 💰 Виплати
- 🎡 Налаштування рулетки
- 📢 Налаштування каналу
- ✏️ Змінити тексти
- 🔎 Знайти користувача
- 🚫 Заблоковані
- 📣 Розсилка

## 10. Налаштування рулетки

У панелі натисніть `🎡 Налаштування рулетки` і надішліть:

```text
10=50 20=35 30=12 50=3 refs=5 min=50
```

Сума чотирьох шансів повинна бути рівно 100. Негативні значення та `refs < 1` не приймаються.

## 11. Тексти

Усі основні тексти зберігаються в SQLite. Через `✏️ Змінити тексти` можна змінювати привітання, підписку, меню, реферали, рулетку, виграш, правила, вивід та `Заробити більше` без редагування Python-коду.

Плейсхолдери:

- `{link}`, `{refs}`, `{spins}` — реферали
- `{reward}` — виграш
- `{refs_per_spin}`, `{min_withdraw}` — правила/заробіток
- `{balance}`, `{min_withdraw}` — вивід

## 12. Реферальна система

Посилання:

```text
https://t.me/BOT_USERNAME?start=ref_USER_ID
```

Реферал стає підтвердженим лише після успішної перевірки підписки. Самореферал заборонений. Один Telegram user ID може бути зарахований лише один раз.

За кожні N підтверджених рефералів нараховується 1 spin. N зберігається в SQLite та змінюється з адмін-панелі.

## 13. Рулетка та захист

Виграш визначається Python backend через криптографічно-незалежний серверний random (`random.random`) до повернення відповіді фронтенду. Frontend не передає суму виграшу.

Spin списується атомарним SQL-запитом `spins=spins-1 WHERE spins>0`.

Для Web App використовується перевірка Telegram `initData` HMAC на backend.

Кожен spin має `request_id`, який зберігається в SQLite. Повторний HTTP-запит з тим самим request ID повертає вже створений результат і не списує другий spin.

## 14. Виплати

Мінімум за замовчуванням — 50 грн.

Коли користувач створює заявку, весь доступний баланс блокується до рішення адміністратора. Статуси:

- `pending`
- `paid`
- `rejected`

При `rejected` сума повертається на баланс.

## 15. База

SQLite створюється автоматично. Таблиці:

- `users`
- `referrals`
- `withdrawals`
- `spins_history`
- `spin_requests`
- `settings`

Є індекси за Telegram ID, referrer та статусом виплат.

## 16. Перевірка /start

Підтримуються:

```text
/start
/start ref_123456789
```

При першому вході створюється користувач. Якщо є валідний `ref_...`, referrer зберігається, але реферал не зараховується до моменту успішної перевірки підписки.

## 17. Health check

Render може перевіряти:

```text
GET /health
```

Відповідь:

```json
{"ok": true}
```

## Важливо

SQLite на Render потребує persistent disk. `render.yaml` використовує Starter + 1 GB disk, оскільки без persistent disk файл SQLite може бути втрачений під час заміни інстансу.
