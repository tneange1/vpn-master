#!/usr/bin/env bash
# Установка VPN Master: bash <(curl -fsSL https://raw.githubusercontent.com/tneange1/vpn-master/main/install.sh)
set -euo pipefail
DIR=/opt/vpn-master
[ "$(id -u)" = 0 ] || { echo "Запусти от root"; exit 1; }
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }
command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone https://github.com/tneange1/vpn-master "$DIR"; fi
cd "$DIR"

if [ ! -f .env ]; then
  read -rp "Токен бота от @BotFather: " BOT_TOKEN
  read -rp "Твой Telegram ID (владелец): " OWNER_ID
  printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' "$BOT_TOKEN" "$OWNER_ID" > .env
  # пароль внутренней БД (наружу не публикуется)
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env
  chmod 600 .env
fi
mkdir -p data
docker compose up -d --build
echo
echo "✅ VPN Master запущен. Напиши боту /start"
echo "   Логи: cd $DIR && docker compose logs -f bot"
echo "   Обновление: cd $DIR && git pull && docker compose up -d --build"
