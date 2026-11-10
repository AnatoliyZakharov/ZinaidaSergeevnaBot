# Dev shortcuts for ZinaidaSergeevnaBot (POSIX sh, GNU Make 3.81+).
# No secrets here: all tokens are read from the caller's environment.

PYTHON ?= python3
PY_SOURCES := main.py mymeet_sync.py db.py auth.py gtasks.py crypto.py

.DEFAULT_GOAL := help
.PHONY: help test lint run

help:
	@echo "Available targets:"
	@echo "  make help  - show this help (default)"
	@echo "  make test  - run smoke tests (test_smoke.py) with empty MYmeet/Google env"
	@echo "  make lint  - ruff check if installed, otherwise syntax check only"
	@echo "  make run   - start the bot locally (python main.py)"
	@echo ""
	@echo "WARNING: run 'make run' only with a separate TEST Telegram bot token,"
	@echo "         never with the production token (two instances conflict)."
	@echo "Override interpreter: make test PYTHON=python3.11"

test:
	MYMEET_TOKEN="" MYMEET_WS_ID="" MYMEET_SPREADSHEET_ID="" ORGANIZER_EMAIL="" GOOGLE_SA_KEY="{}" $(PYTHON) test_smoke.py

lint:
	@if $(PYTHON) -m ruff --version >/dev/null 2>&1; then \
		echo "$(PYTHON) -m ruff check $(PY_SOURCES)"; \
		$(PYTHON) -m ruff check $(PY_SOURCES); \
	else \
		echo "ruff not installed — syntax check only; pip install ruff"; \
		$(PYTHON) -c 'import ast, sys; [ast.parse(open(f, encoding="utf-8").read(), filename=f) for f in sys.argv[1:]]; print("syntax OK: " + " ".join(sys.argv[1:]))' $(PY_SOURCES); \
	fi

run:
	@missing=""; \
	[ -n "$${TELEGRAM_TOKEN:-}" ] || missing="$$missing TELEGRAM_TOKEN"; \
	[ -n "$${YANDEX_API_KEY:-}" ] || missing="$$missing YANDEX_API_KEY"; \
	[ -n "$${YANDEX_FOLDER_ID:-}" ] || missing="$$missing YANDEX_FOLDER_ID"; \
	if [ -n "$$missing" ]; then \
		echo "ERROR: required environment variables are not set:$$missing" >&2; \
		echo "Export them in your shell (use a TEST Telegram bot token, not production) and retry." >&2; \
		exit 1; \
	fi
	$(PYTHON) main.py
