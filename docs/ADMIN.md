# Руководство администратора — Зинаида Сергеевна

Документ для человека, который обслуживает бота в production:
добавляет пользователей, ротирует секреты, следит за здоровьем сервиса.

---

## 1. Добавление нового пользователя

Бот использует Google OAuth. Пока приложение в **режиме тестирования**
(Publishing status: Testing в Google Cloud Console), к нему могут подключиться
только явно добавленные тестовые пользователи.

### Шаг 1 — Добавить Google-аккаунт в список тестовых пользователей

1. Открой [Google Cloud Console → OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent).
2. Выбери проект бота (убедись, что выбран правильный — смотри название в заголовке).
3. Прокрути вниз до раздела **Test users**.
4. Нажми **+ Add users**.
5. Введи Gmail-адрес нового пользователя, нажми **Save**.

После добавления пользователь может сразу авторизоваться через /connect
в Telegram-боте. Добавление вступает в силу немедленно.

### Если нужно открыть доступ широкой аудитории

Перевести приложение в **Production** (кнопка **Publish App** на экране consent).
Google потребует пройти верификацию (обычно несколько дней).

---

## 2. Управление секретами (Fly.io)

### Просмотр списка секретов
```bash
fly secrets list --app calendar-bot-anatolii
```

### Установка или обновление секрета
```bash
fly secrets set ИМЯ=значение --app calendar-bot-anatolii
```

### Ротация ENCRYPTION_KEY

Перед ротацией проверь, что зашифрованных данных нет:
```bash
fly ssh console --app calendar-bot-anatolii
sqlite3 /data/bot.db "SELECT COUNT(*) FROM users WHERE mymeet_token IS NOT NULL OR mymeet_pending_token IS NOT NULL;"
```

Если 0 — безопасно ротировать:
```bash
fly secrets set ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") --app calendar-bot-anatolii
```

### Текущие секреты (имена)

| Имя | Описание |
|-----|----------|
| TELEGRAM_TOKEN | Токен Telegram-бота |
| ENCRYPTION_KEY | Fernet-ключ для MYmeet-токенов |
| GOOGLE_CLIENT_ID | OAuth Client ID |
| GOOGLE_CLIENT_SECRET | OAuth Client Secret |
| GOOGLE_SA_KEY | Service Account JSON |
| YANDEX_API_KEY | Yandex Cloud API key |
| YANDEX_FOLDER_ID | Yandex Cloud folder ID |
| YANDEX_TELEMOST_TOKEN | Яндекс Телемост токен |
| MYMEET_TOKEN | MYmeet API токен |
| OAUTH_REDIRECT_URI | Redirect URI для Google OAuth |
| DB_PATH | Путь к SQLite: /data/bot.db |
| ADMIN_TELEGRAM_ID | Telegram ID администратора |

---

## 3. Проверка состояния

```bash
# Логи
fly logs --app calendar-bot-anatolii

# Только ошибки
fly logs --app calendar-bot-anatolii | grep ERROR

# Статус машины
fly status --app calendar-bot-anatolii

# Доступ к SQLite
fly ssh console --app calendar-bot-anatolii
sqlite3 /data/bot.db
SELECT telegram_id, telegram_username, mymeet_setup_done FROM users;
```

---

## 4. Перезапуск бота

```bash
fly machine restart --app calendar-bot-anatolii
```

---

## 5. Деплой

Деплой происходит **автоматически** при merge в main через GitHub Actions.
Ручной деплой не нужен.

---

## 6. Частые проблемы

| Симптом | Причина | Решение |
|---------|---------|---------|
| «Error 403: access_denied» при Google OAuth | Не добавлен в Test users | Google Cloud Console → OAuth consent screen → Test users → Add users |
| Бот не отвечает | Машина упала | fly status, fly logs, fly machine restart |
| /setup_mymeet — «Не удалось сохранить данные» | ENCRYPTION_KEY не задан | fly secrets set ENCRYPTION_KEY=... |
| Токены не расшифровываются | Ключ изменился | Пользователю пересетапиться через /setup_mymeet |
