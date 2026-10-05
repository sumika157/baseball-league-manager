#!/bin/sh
# 起動のたびに未適用のマイグレーションを反映してから、渡されたコマンド（gunicorn）に切り替える。
set -e
python manage.py migrate --noinput
exec "$@"
