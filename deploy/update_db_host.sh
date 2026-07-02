#!/bin/sh
# Перед запуском сервиса: обновить DB_HOST в .env актуальным IP контейнера БД сайта.
# IP контейнера oasis-db-1 меняется после ребута сервера — та же грабля, что у
# StockBot (там лечится обёрткой run_daily.sh). Контейнера нет/докер не поднялся —
# ничего не трогаем (сервис упадёт на коннекте с понятной ошибкой в journalctl).
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' oasis-db-1 2>/dev/null)
if [ -n "$IP" ]; then
  sed -i "s/^DB_HOST=.*/DB_HOST=$IP/" /opt/content-factory/.env
fi
exit 0
