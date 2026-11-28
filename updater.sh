#!/bin/sh
# Сайдкар автообновления VPN Master. Работает в контейнере docker:cli с доступом к docker.sock
# и папкой проекта, смонтированной по тому же пути, что и на хосте.
# Управляется файлами в ./data (их пишет бот):
#   autoupdate.interval — минуты между проверками (0 = только по кнопке)
#   autoupdate.branch   — ветка (по умолчанию main)
#   update.request      — разовая проверка/обновление «сейчас»
#   shutdown.request    — бот переехал на другой сервер: гасим этот экземпляр насовсем
# Результат: ./data/update.status (key=value), лог: ./data/updater.log
set -u
D=./data
mkdir -p "$D"
command -v git >/dev/null 2>&1 || apk add --no-cache git >/dev/null 2>&1
git config --global --add safe.directory "$PWD" 2>/dev/null
SELF_SUM=$(md5sum "$0" | cut -d' ' -f1)

log(){ echo "$(date -u '+%F %T') $*" | tee -a "$D/updater.log"; tail -n 300 "$D/updater.log" > "$D/updater.log.tmp" && mv "$D/updater.log.tmp" "$D/updater.log"; }
status(){ # result [message]
  {
    echo "commit=$(git rev-parse --short HEAD 2>/dev/null)"
    echo "subject=$(git log -1 --format=%s 2>/dev/null)"
    echo "date=$(git log -1 --format=%cI 2>/dev/null)"
    echo "remote=${REMOTE:-}"
    echo "checked=$(date -u +%s)"
    echo "result=$1"
    echo "message=${2:-}"
  } > "$D/update.status.tmp" && mv "$D/update.status.tmp" "$D/update.status"
}

status idle
log "updater запущен ($(git rev-parse --short HEAD 2>/dev/null))"
while true; do
  if [ -f "$D/shutdown.request" ]; then
    rm -f "$D/shutdown.request"
    log "бот переехал ($(cat "$D/migrated.flag" 2>/dev/null)) — останавливаю этот экземпляр"
    status migrated "бот переехал на другой сервер"
    sleep 15  # даём старому боту дописать сообщения
    docker update --restart=no vpn-master vpn-master-db vpn-master-updater >>"$D/updater.log" 2>&1
    docker compose stop bot db >>"$D/updater.log" 2>&1
    log "остановлено; updater выходит"
    exit 0
  fi
  [ -f "$D/migrated.flag" ] && { sleep 20; continue; }
  INTERVAL=$(cat "$D/autoupdate.interval" 2>/dev/null | tr -dc 0-9); INTERVAL=${INTERVAL:-0}
  BRANCH=$(cat "$D/autoupdate.branch" 2>/dev/null | tr -dc 'A-Za-z0-9._/-'); BRANCH=${BRANCH:-main}
  NOW=$(date +%s); LAST=$(cat "$D/autoupdate.last" 2>/dev/null | tr -dc 0-9); LAST=${LAST:-0}
  GO=0; MODE=""
  [ -f "$D/update.request" ] && { GO=1; MODE=$(cat "$D/update.request"); rm -f "$D/update.request"; }
  if [ "$INTERVAL" -gt 0 ] && [ $((NOW - LAST)) -ge $((INTERVAL * 60)) ]; then GO=1; fi
  if [ "$GO" = 1 ]; then
    echo "$NOW" > "$D/autoupdate.last"
    if ! git fetch -q origin "$BRANCH" 2>>"$D/updater.log"; then
      status error "git fetch не удался"; log "git fetch не удался"
    else
      CUR=$(git rev-parse HEAD); NEW=$(git rev-parse "origin/$BRANCH"); REMOTE=$(git rev-parse --short "$NEW")
      if [ "$CUR" = "$NEW" ]; then
        status uptodate "последняя версия"
      elif [ "$MODE" = "check" ]; then
        status available "$(git log --format='%h %s' "$CUR..$NEW" | head -10 | tr '\n' ';')"
      else
        log "обновляю $(git rev-parse --short "$CUR") → $REMOTE"
        status updating "$REMOTE"
        git reset -q --hard "$NEW"
        if docker compose build bot >>"$D/updater.log" 2>&1 && docker compose up -d bot >>"$D/updater.log" 2>&1; then
          status updated "$(git rev-parse --short "$CUR")"
          log "обновлено до $REMOTE"
        else
          log "сборка упала — откат на $(git rev-parse --short "$CUR")"
          git reset -q --hard "$CUR"
          docker compose up -d --build bot >>"$D/updater.log" 2>&1
          status failed "сборка $REMOTE упала, откатился"
        fi
        # если поменялся сам updater.sh — перезапускаемся новой версией
        if [ "$(md5sum "$0" | cut -d' ' -f1)" != "$SELF_SUM" ]; then log "updater.sh обновился — перезапуск"; exec sh "$0"; fi
      fi
    fi
  fi
  sleep 20
done
