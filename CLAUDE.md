# CLAUDE.md — Developer Guide

Инструкции для разработки, тестирования и работы с репозиторием.  
**Не менять бизнес-логику бота** без Issue + APPROVAL от Technical Lead.
---

## AI-команда разработки

### Роли и системы

| Роль | Система | Когда активируется |
|------|---------|--------------------|
| **Product Owner** | Человек (ты) | Всегда — финальное слово |
| **Tech Lead / Code Reviewer** | ChatGPT | После открытия PR; при конфликте агентов |
| **architect** | Claude Code subagent | Перед разработкой — анализ и план |
| **developer** | Claude Code subagent | После одобрения архитектуры |
| **qa-engineer** | Claude Code subagent | После PR — независимая проверка |
| **devops** | Claude Code subagent | После QA/Security — готовность к деплою |

Агенты находятся в `.claude/agents/`. Claude Code выбирает агента автоматически по описанию задачи.

### Правила командной работы

1. **Product Owner — единственный, кто разрешает релиз.** Без его явного одобрения нет merge и нет deploy.
2. **Tech Lead и внешний Code Reviewer — ChatGPT.** Ты передаёшь туда diff PR и критерии приёмки вручную.
3. **Claude Code — исполнитель и координатор внутренних subagents** (architect, developer, qa-engineer, devops).
4. **Разработчик не проверяет собственную работу.** `developer` передаёт отчёт `qa-engineer`, не выносит вердикт сам.
5. **QA анализирует реальный код, не только отчёт разработчика.** `qa-engineer` читает diff напрямую.
6. **Один цикл возражений.** Блокировать работу только при подтверждённых существенных дефектах. Гипотезы и улучшения — в бэклог.
7. **Запрещено без разрешения Product Owner:** merge, production deploy, изменение fly secrets, удаление пользовательских данных и `/data/bot.db`.
8. **Минимальные изменения.** Не трогать рабочий функционал без явного требования.

---


## Зависимость от PR #1

> **CI-статус:** Smoke-тесты (`smoke-tests.yml`) и сканер секретов (`secret-scan.yml`)
> появятся в ветках, основанных на `main`, **после** того, как
> **PR #1 (`ci/security-review`)** будет смержен.  
> До этого в PR, основанных на текущем `main`, работает `validate-docs.yml`
> и базовый `deploy.yml` (только на push в main).

---

## Структура проекта

```
├── main.py              # Telegram-бот: хендлеры, команды
├── mymeet_sync.py       # Интеграция с MYmeet API + Google Sheets
├── db.py                # SQLite: пользователи, конфиги
├── auth.py              # Авторизация пользователей
├── gtasks.py            # Google Tasks интеграция
├── requirements.txt     # Python-зависимости
├── Dockerfile           # Контейнер для Fly.io
├── Procfile             # Команда запуска
├── fly.toml             # Конфигурация деплоя (не менять без TL)
├── test_smoke.py        # Smoke-тесты (из PR #1; запускать перед каждым PR)
└── .github/
    ├── workflows/
    │   ├── deploy.yml            # CD: деплой на Fly.io (только push → main)
    │   ├── validate-docs.yml     # CI: проверка инфра-файлов (PR #2)
    │   ├── smoke-tests.yml       # CI: smoke-тесты (появится после мержа PR #1)
    │   └── secret-scan.yml       # Security: Gitleaks (появится после мержа PR #1)
    ├── ISSUE_TEMPLATE/           # Шаблоны Issues
    └── pull_request_template.md
```

---

## Локальный запуск

### Требования
- Python 3.11+
- Переменные окружения (см. раздел «Секреты»)

### Установка зависимостей
```bash
pip install -r requirements.txt
```

### Запуск бота
```bash
python main.py
```

### Запуск smoke-тестов
```bash
# Обязательно перед созданием PR (доступно после мержа PR #1)
python test_smoke.py
```

Или через pytest:
```bash
pip install pytest
pytest test_smoke.py -v
```

Ожидаемый вывод:
```
✅ Все проверки прошли
```

---

## Секреты и переменные окружения

### Правило: никаких секретов в коде

❌ Запрещено:
```python
TOKEN = "mm_RealToken123..."          # прямой литерал  # gitleaks:allow
TOKEN = os.environ.get("TOKEN", "mm_RealToken123...")  # default-значение  # gitleaks:allow
```

✅ Правильно:
```python
TOKEN = os.environ.get("MYMEET_TOKEN", "")
```

### Список переменных окружения

| Переменная              | Описание                        | Где задаётся      |
|-------------------------|----------------------------------|-------------------|
| `TELEGRAM_BOT_TOKEN`    | Telegram Bot API token           | Fly Secret        |
| `MYMEET_TOKEN`          | MYmeet API token (глобальный)    | Fly Secret        |
| `MYMEET_WS_ID`          | MYmeet Workspace ID              | Fly Secret        |
| `MYMEET_SPREADSHEET_ID` | Google Sheets ID                 | Fly Secret        |
| `ORGANIZER_EMAIL`       | Email организатора               | Fly Secret        |
| `GOOGLE_SA_KEY`         | Google Service Account JSON      | Fly Secret        |

### Установка секретов (Fly.io)
```bash
fly secrets set VARIABLE_NAME=value -a calendar-bot-anatolii
```

Никогда не передавать значения секретов в переменных окружения CI (`.github/workflows/*.yml`).  
В workflow допустимы только пустые значения для тестов: `MYMEET_TOKEN: ""`.

---

## Работа с ветками

### Именование веток

| Тип задачи       | Формат                          | Пример                        |
|------------------|---------------------------------|-------------------------------|
| Новая функция    | `feat/<issue-num>-<slug>`       | `feat/42-google-tasks-sync`   |
| Исправление бага | `fix/<issue-num>-<slug>`        | `fix/38-latin1-token-error`   |
| Инфраструктура   | `infra/<slug>`                  | `infra/ai-software-factory`   |
| CI/Security      | `ci/<slug>`                     | `ci/security-review`          |
| Технический долг | `debt/<issue-num>-<slug>`       | `debt/15-refactor-db-layer`   |

### Создание ветки
```bash
git checkout main
git pull origin main
git checkout -b feat/42-google-tasks-sync
```

### Пуш и PR
```bash
git push origin feat/42-google-tasks-sync
# Затем открыть PR на GitHub через веб-интерфейс
```

**Запрещено:**
- `git push --force` в `main`
- Прямые коммиты в `main`
- Merge PR без approve Technical Lead **и** финального одобрения Product Owner

---

## Pull Request

1. Заполни шаблон `.github/pull_request_template.md` полностью.
2. Привяжи Issue: `Closes #<номер>` в описании PR.
3. Убедись, что все CI-проверки зелёные.
4. Запроси Review у Technical Lead.
5. **Дождись approve TL** (технический review и рекомендация о merge).
6. **Дождись финального одобрения Product Owner** (решение о релизе).
7. Не мержи самостоятельно — merge выполняет TL после явного одобрения PO.

---

## Деплой

Деплой происходит **автоматически** при мерже в `main` через `.github/workflows/deploy.yml`.

Ручной деплой (только при необходимости):
```bash
fly deploy --remote-only -a calendar-bot-anatolii
```

**Не менять** `fly.toml`, `Dockerfile`, `Procfile` без согласования с Technical Lead.

---

## CI/CD Checklist перед PR

- [ ] `python test_smoke.py` — все тесты проходят локально
- [ ] Нет захардкоженных секретов (`grep -r "mm_" --include="*.py" .`)
- [ ] Ветка актуальна относительно `main` (`git rebase origin/main`)
- [ ] Описание PR заполнено по шаблону
- [ ] Issue привязан к PR
