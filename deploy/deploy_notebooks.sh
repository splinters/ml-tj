#!/usr/bin/env bash
# Развёртывание блокнотов ML TJ на ВМ заказчика.
#
# Что делает:
#   1. создаёт виртуальное окружение .venv-nb и ставит зависимости
#      (из интернета или из локального каталога колёс: WHEELHOUSE=/path);
#   2. проверяет наличие выгрузки GeoPackage;
#   3. выполняет блокноты 01-04 и экспортирует их в автономный HTML (без CDN);
#   4. создаёт файл паролей nginx (если заданы NB_USER и NB_PASSWORD);
#   5. по INSTALL_NGINX=1 копирует HTML и фрагмент конфигурации и перезагружает nginx.
#
# Запуск из корня проекта:
#   NB_USER=viewer NB_PASSWORD='...' INSTALL_NGINX=1 sudo -E bash deploy/deploy_notebooks.sh
# Без sudo (шаги 1-3):
#   bash deploy/deploy_notebooks.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv-nb"
GPKG="${ML_TJ_GPKG:-$ROOT/notebooks/data/tajikistan_soil_ml_export.gpkg}"
OUT="${OUT_DIR:-$ROOT/notebooks_html}"
WEB_DIR="${WEB_DIR:-/opt/ml_tj/notebooks_html}"
HTPASSWD="${HTPASSWD_FILE:-/etc/nginx/ml_tj_notebooks.htpasswd}"

echo "== 1. окружение"
python3 -m venv "$VENV"
if [ -n "${WHEELHOUSE:-}" ]; then
  "$VENV/bin/pip" install --no-index --find-links "$WHEELHOUSE" -r "$ROOT/notebooks/requirements-notebooks.txt"
else
  "$VENV/bin/pip" install -r "$ROOT/notebooks/requirements-notebooks.txt"
fi
"$VENV/bin/pip" freeze > "$ROOT/notebooks/requirements-lock.txt"
"$VENV/bin/python" -m ipykernel install --prefix "$VENV" --name python3 >/dev/null

echo "== 2. данные"
if [ ! -f "$GPKG" ]; then
  echo "Нет файла $GPKG. Скопируйте tajikistan_soil_ml_export.gpkg в notebooks/data/"
  echo "или создайте его: python db/export_geopackage.py" >&2
  exit 1
fi
export ML_TJ_GPKG="$GPKG"

echo "== 3. выполнение и экспорт блокнотов (несколько минут)"
(cd "$ROOT/notebooks" && JUPYTER_PATH="$VENV/share/jupyter" "$VENV/bin/python" build_html.py --out "$OUT" --execute)

if [ -n "${NB_USER:-}" ] && [ -n "${NB_PASSWORD:-}" ]; then
  echo "== 4. пароль для nginx"
  HASH="$(openssl passwd -apr1 "$NB_PASSWORD")"
  if [ "${INSTALL_NGINX:-0}" = "1" ]; then
    printf '%s:%s\n' "$NB_USER" "$HASH" > "$HTPASSWD"
    chmod 640 "$HTPASSWD"
    chgrp www-data "$HTPASSWD" 2>/dev/null || true
  else
    echo "Строка для $HTPASSWD: $NB_USER:$HASH"
  fi
fi

if [ "${INSTALL_NGINX:-0}" = "1" ]; then
  echo "== 5. nginx"
  mkdir -p "$WEB_DIR"
  cp -r "$OUT"/. "$WEB_DIR"/
  echo "Страницы скопированы в $WEB_DIR."
  echo "Подключите $ROOT/deploy/nginx_notebooks.conf в server{} (до location /testmap/), затем:"
  echo "  nginx -t && systemctl reload nginx"
else
  echo "== 5. nginx не трогаю (INSTALL_NGINX не задан). HTML: $OUT"
fi
echo "Готово."
