#!/usr/bin/env bash
# Сборка Docker-образа и smoke-test импортов внутри него (issue #29).
# Используется job «Docker packaging» (smoke-tests.yml) и preflight в deploy.yml.
# Без сети внутри контейнера, только фиктивные значения env — секреты не нужны.
set -euo pipefail

IMAGE="zinochka-ci:${GITHUB_SHA:-local}"

echo "::group::Static packaging check"
python ci/check_docker_packaging.py
MODULES="$(python ci/check_docker_packaging.py --list-modules)"
ENTRY="$(python ci/check_docker_packaging.py --print-entrypoint)"
echo "modules: ${MODULES}"
echo "entrypoint: ${ENTRY}"
echo "::endgroup::"

echo "::group::docker build"
docker build -t "${IMAGE}" .
echo "::endgroup::"

echo "::group::Import smoke inside image"
# shellcheck disable=SC2086  # MODULES — список имён через пробел
docker run --rm --network none \
  -e TELEGRAM_TOKEN=ci-dummy \
  -e YANDEX_API_KEY=ci-dummy \
  -e YANDEX_FOLDER_ID=ci-dummy \
  -e GOOGLE_CLIENT_ID=ci-dummy \
  -e GOOGLE_CLIENT_SECRET=ci-dummy \
  -e OAUTH_REDIRECT_URI=http://localhost/cb \
  -e DB_PATH=/tmp/ci.db \
  --entrypoint python "${IMAGE}" \
  -c "import importlib,sys; [importlib.import_module(m) for m in sys.argv[1:]]; print('imports OK:', ' '.join(sys.argv[1:]))" \
  ${MODULES}
echo "::endgroup::"

echo "::group::Deploy Monitor import check (exact production command, no env at all)"
# Та же фиксированная команда, что Deploy Monitor выполняет через flyctl ssh console.
# Контейнер без env и без сети: фиктивные значения подставляются только через setdefault.
# После импорта БД не должна появиться ни в /data, ни в /app — бот не запускается.
CHECK_CMD="$(python ci/deploy_monitor.py module-check-command)"
docker run --rm --network none --entrypoint sh "${IMAGE}" -c \
  "${CHECK_CMD} && test ! -e /data/bot.db && test ! -e /app/bot.db && echo 'no database files created'"
echo "::endgroup::"

echo "::group::Entrypoint file present in image"
docker run --rm --network none --entrypoint test "${IMAGE}" -f "${ENTRY}"
echo "entrypoint OK: ${ENTRY}"
echo "::endgroup::"
