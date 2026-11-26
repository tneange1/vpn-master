"""Генераторы bash-скриптов. Всё, что приходит от пользователя, экранируется через shlex.quote."""
from shlex import quote as q

BEDOLAGA_DIR = "/opt/remnawave-bedolaga-telegram-bot"
BEDOLAGA_NET = "remnawave-bedolaga-telegram-bot_bot_network"
CABINET_DIR = "/opt/bedolaga-cabinet"
CADDY_DIR = "/opt/remnawave/caddy"
NGINX_DIR = "/opt/remnawave/nginx"
CERTBOT_DIR = "/opt/certbot"

PRELUDE = r"""#!/usr/bin/env bash
set -eo pipefail
export DEBIAN_FRONTEND=noninteractive
log(){ echo "==> $*"; }
mark(){ echo "##VM $1=$2"; }
pkg(){ if command -v apt-get >/dev/null 2>&1; then apt-get update -qq >/dev/null 2>&1 || true; apt-get install -y -qq "$@" >/dev/null; fi; }
command -v curl >/dev/null 2>&1 || pkg curl ca-certificates
need_docker(){
  if ! command -v docker >/dev/null 2>&1; then log "Устанавливаю Docker"; curl -fsSL https://get.docker.com | sh; fi
  systemctl enable --now docker >/dev/null 2>&1 || true
}
set_env(){ # файл ключ значение
  local f="$1" k="$2" v="$3" e
  touch "$f"
  e=$(printf '%s' "$v" | sed -e 's/[\\/&|]/\\&/g')
  if grep -q "^$k=" "$f"; then sed -i "s|^$k=.*|$k=$e|" "$f"; else printf '%s=%s\n' "$k" "$v" >> "$f"; fi
}
get_env(){ grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

# ---- reverse proxy: Caddy (/opt/remnawave/caddy) или Nginx (/opt/remnawave/nginx), сайты — отдельными файлами
# Что перенесли/остановили — пишем в /tmp/vm_moved, чтобы уметь откатиться.
proxy_networks(){ # контейнер прокси
  for n in remnawave-network BEDOLAGANET; do
    if docker network inspect "$n" >/dev/null 2>&1; then docker network connect "$n" "$1" 2>/dev/null || true; fi
  done
}
running(){ docker ps --format '{{.Names}}' | grep -qx "$1"; }
move_foreign(){ # каталог
  local d="$1" bak
  bak="$d.bak.$(date +%s)"
  log "Нашёл чужой $d — останавливаю и переношу в $bak"
  (cd "$d" && docker compose down 2>/dev/null) || true
  mv "$d" "$bak"; echo "$d|$bak" >> /tmp/vm_moved
}
certbot_compose(){
  mkdir -p CERTBOTDIR/certs CERTBOTDIR/var-lib-letsencrypt CERTBOTDIR/logs CERTBOTDIR/www
  cat > CERTBOTDIR/docker-compose.yml <<'VMEOF'
services:
  certbot:
    container_name: certbot
    image: certbot/certbot
    network_mode: host
    volumes:
      - ./certs:/etc/letsencrypt
      - ./var-lib-letsencrypt:/var/lib/letsencrypt
      - ./logs:/var/log/letsencrypt
      - ./www:/var/www/certbot
VMEOF
}

caddy_ours(){ [ -f CADDYDIR/.vpn-master ]; }
caddy_setup(){
  need_docker
  if [ -d CADDYDIR ] && ! caddy_ours; then move_foreign CADDYDIR; fi
  if running nginx && [ -f NGINXDIR/.vpn-master ]; then echo "!! На сервере уже стоит Nginx от vpn-master — Caddy не ставлю"; return 5; fi
  mkdir -p CADDYDIR/sites && cd CADDYDIR && touch .vpn-master
  [ -f sites/_default.caddy ] || echo "# vpn-master" > sites/_default.caddy
  cat > Caddyfile <<'VMEOF'
{
	servers {
		trusted_proxies static private_ranges
	}
}
import /etc/caddy/sites/*.caddy
VMEOF
  docker network inspect remnawave-network >/dev/null 2>&1 || docker network create remnawave-network >/dev/null
  cat > docker-compose.yml <<'VMEOF'
services:
  caddy:
    image: caddy:2
    container_name: caddy
    hostname: caddy
    restart: always
    ports:
      - "0.0.0.0:80:80"
      - "0.0.0.0:443:443"
      - "0.0.0.0:443:443/udp"
    networks:
      - remnawave-network
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - ./sites:/etc/caddy/sites
      - caddy-data:/data
      - caddy-config:/config
networks:
  remnawave-network:
    name: remnawave-network
    external: true
volumes:
  caddy-data:
  caddy-config:
VMEOF
  docker compose up -d
}
caddy_networks(){ proxy_networks caddy; }
caddy_site(){ # имя; содержимое — stdin
  local content; content=$(cat)
  if running caddy && ! caddy_ours; then
    echo "!! На сервере работает Caddy, который ставил не vpn-master. Добавь блок вручную:"; echo "$content"; return 3
  fi
  if ! (caddy_ours && running caddy); then caddy_setup || return $?; fi
  printf '%s\n' "$content" > "CADDYDIR/sites/$1.caddy"
  caddy_networks
  if ! docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null; then
    rm -f "CADDYDIR/sites/$1.caddy"; echo "!! Конфиг Caddy не прошёл проверку — сайт $1 не применён"; return 1
  fi
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  log "Caddy: сайт $1 применён"
}

nginx_ours(){ [ -f NGINXDIR/.vpn-master ]; }
nginx_setup(){
  need_docker
  if [ -d NGINXDIR ] && ! nginx_ours; then move_foreign NGINXDIR; fi
  if running caddy && caddy_ours; then echo "!! На сервере уже стоит Caddy от vpn-master — Nginx не ставлю"; return 5; fi
  mkdir -p NGINXDIR/conf.d && cd NGINXDIR && touch .vpn-master
  certbot_compose
  cat > conf.d/00-default.conf <<'VMEOF'
map $http_upgrade $vm_conn { default upgrade; '' close; }
server {
    listen 80 default_server;
    server_name _;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
}
VMEOF
  docker network inspect remnawave-network >/dev/null 2>&1 || docker network create remnawave-network >/dev/null
  cat > docker-compose.yml <<'VMEOF'
services:
  nginx:
    image: nginx:stable-alpine
    container_name: nginx
    hostname: nginx
    restart: always
    ports:
      - "0.0.0.0:80:80"
      - "0.0.0.0:443:443"
    networks:
      - remnawave-network
    volumes:
      - ./conf.d:/etc/nginx/conf.d:ro
      - CERTBOTDIR/certs:/etc/letsencrypt:ro
      - CERTBOTDIR/www:/var/www/certbot:ro
networks:
  remnawave-network:
    name: remnawave-network
    external: true
VMEOF
  docker compose up -d
  cat > /etc/cron.d/vpn-master-nginx-certs <<'VMEOF'
0 3 * * 1 root cd CERTBOTDIR && docker compose run --rm certbot renew --webroot -w /var/www/certbot >> /var/log/vpn-master-certbot.log 2>&1 && docker exec nginx nginx -s reload
VMEOF
  chmod 644 /etc/cron.d/vpn-master-nginx-certs
}
nginx_site(){ # имя "домены через пробел"; содержимое — stdin
  local name="$1" domains="$2" content args="" d
  content=$(cat)
  if running nginx && ! nginx_ours; then
    echo "!! На сервере работает Nginx, который ставил не vpn-master. Добавь блок вручную:"; echo "$content"; return 3
  fi
  if ! (nginx_ours && running nginx); then nginx_setup || return $?; fi
  certbot_compose
  for d in $domains; do args="$args -d $d"; done
  log "Сертификат $name: $domains"
  if [ -n "${VM_EMAIL:-}" ]; then mail="--email $VM_EMAIL"; else mail="--register-unsafely-without-email"; fi
  # shellcheck disable=SC2086
  (cd CERTBOTDIR && docker compose run --rm certbot certonly --webroot -w /var/www/certbot \
     --non-interactive --agree-tos $mail --cert-name "$name" --expand $args) || { echo "!! certbot не выдал сертификат"; return 4; }
  printf '%s\n' "$content" > "NGINXDIR/conf.d/$name.conf"
  proxy_networks nginx
  if ! docker exec nginx nginx -t; then
    rm -f "NGINXDIR/conf.d/$name.conf"; echo "!! Конфиг Nginx не прошёл проверку — сайт $name не применён"; return 1
  fi
  docker exec nginx nginx -s reload
  log "Nginx: сайт $name применён"
}
""".replace("CADDYDIR", CADDY_DIR).replace("NGINXDIR", NGINX_DIR).replace("CERTBOTDIR", CERTBOT_DIR) \
    .replace("BEDOLAGANET", BEDOLAGA_NET)


def _script(body: str) -> str:
    return PRELUDE + "\n" + body.strip() + "\n"


def _heredoc_site(name: str, content: str) -> str:
    return f"caddy_site {q(name)} <<'VMSITE'\n{content.rstrip()}\nVMSITE\n"


def _compose_restart(path: str) -> str:
    return f"cd {q(path)} && docker compose pull && docker compose down && docker compose up -d && docker compose ps"


# ===================================================================== Remnawave
def remnawave_install(domain: str, sub_domain: str, admin_user: str, admin_pass: str) -> str:
    raw = "https://raw.githubusercontent.com/remnawave/backend/refs/heads/main"
    return _script(f"""
need_docker
pkg jq openssl
mkdir -p /opt/remnawave && cd /opt/remnawave
if docker ps --format '{{{{.Names}}}}' | grep -qx remnawave; then
  log "Панель уже запущена — файлы не трогаю"
else
  log "Скачиваю docker-compose и .env"
  curl -fsSLo docker-compose.yml {raw}/docker-compose-prod.yml
  curl -fsSLo .env {raw}/.env.sample
  for k in APP_SECRET JWT_AUTH_SECRET JWT_API_TOKENS_SECRET METRICS_PASS WEBHOOK_SECRET_HEADER; do
    if grep -q "^$k=" .env; then set_env .env "$k" "$(openssl rand -hex 64)"; fi
  done
  pw=$(openssl rand -hex 24)
  set_env .env POSTGRES_PASSWORD "$pw"
  sed -i "s|^\\(DATABASE_URL=\\"postgresql://postgres:\\)[^\\@]*\\(@.*\\)|\\1$pw\\2|" .env
  set_env .env FRONT_END_DOMAIN {q(domain)}
  set_env .env SUB_PUBLIC_DOMAIN {q(sub_domain)}
  log "Запускаю панель"
  docker compose up -d
fi

H=(-H "Content-Type: application/json" -H "X-Forwarded-For: 127.0.0.1" -H "X-Forwarded-Proto: https")
log "Жду, пока API поднимется"
for i in $(seq 1 100); do
  code=$(curl -s -o /dev/null -w '%{{http_code}}' "${{H[@]}}" http://127.0.0.1:3000/api/auth/status || true)
  [ "$code" = "200" ] && break; sleep 3
done
log "Регистрирую супер-админа"
BODY=$(jq -nc --arg u {q(admin_user)} --arg p {q(admin_pass)} '{{username:$u,password:$p}}')
RESP=$(curl -s "${{H[@]}}" -X POST http://127.0.0.1:3000/api/auth/register -d "$BODY" || true)
JWT=$(echo "$RESP" | jq -r '.response.accessToken // empty' 2>/dev/null || true)
TOK=""
if [ -n "$JWT" ]; then
  TOK=$(curl -s "${{H[@]}}" -H "Authorization: Bearer $JWT" -X POST http://127.0.0.1:3000/api/tokens \\
        -d '{{"tokenName":"vpn-master"}}' | jq -r '.response.token // empty' || true)
  [ -n "$TOK" ] && log "API-токен создан" && mark RW_TOKEN "$TOK"
else
  log "Админ не создан (возможно, уже есть): $(echo "$RESP" | head -c 300)"
fi

log "Ставлю страницу подписки"
mkdir -p /opt/remnawave/subscription && cd /opt/remnawave/subscription
cat > docker-compose.yml <<'VMEOF'
services:
  remnawave-subscription-page:
    image: remnawave/subscription-page:latest
    container_name: remnawave-subscription-page
    hostname: remnawave-subscription-page
    restart: always
    env_file: .env
    ports:
      - "127.0.0.1:3010:3010"
    networks:
      - remnawave-network
networks:
  remnawave-network:
    name: remnawave-network
    external: true
VMEOF
set_env .env APP_PORT 3010
set_env .env REMNAWAVE_PANEL_URL http://remnawave:3000
if [ -n "$TOK" ]; then set_env .env REMNAWAVE_API_TOKEN "$TOK"; fi
docker compose up -d

""" + """
log "Готово. Проверь DNS: A-записи доменов должны смотреть на этот сервер."
docker ps --format 'table {{.Names}}\\t{{.Status}}'
""")


def set_subpage_token(token: str) -> str:
    return _script(f"""
cd /opt/remnawave/subscription
set_env .env REMNAWAVE_API_TOKEN {q(token)}
docker compose up -d
""")


# ===================================================================== Нода (self-steal по мануалу)
NGINX_SS_DIR = "/opt/nginx-selfsteal"

SELFSTEAL_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Сайт в разработке</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;background:#f4f4f5;color:#27272a}main{text-align:center;padding:24px}h1{font-weight:600}</style>
</head><body><main><h1>Сайт в разработке</h1><p>Скоро здесь что-то появится.</p></main></body></html>"""

NGINX_SS_CONF = """server {
    listen 8443 ssl default_server;
    server_name _;

    ssl_certificate     /etc/letsencrypt/live/key/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/key/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;

    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }
}"""


def _certbot_issue(domains: list[str], email: str | None, expand: bool = False) -> str:
    ds = " ".join(f"-d {q(d)}" for d in domains)
    mail = f"--email {q(email)}" if email else "--register-unsafely-without-email"
    return f"""
log "Выпускаю сертификат (cert-name key): {' '.join(domains)}"
certbot_compose && cd {CERTBOT_DIR}
if ss -ltnH 2>/dev/null | awk '{{print $4}}' | grep -qE '(^|:)80$'; then
  echo "!! Порт 80 занят — certbot --standalone не сможет пройти проверку:"; ss -ltnp | grep -E ':80\\b' || true; exit 4
fi
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then ufw allow 80/tcp >/dev/null; fi
docker compose run --rm certbot certonly --standalone --non-interactive --agree-tos {mail} \\
  --cert-name key {'--expand ' if expand else ''}{ds}
ls -l {CERTBOT_DIR}/certs/live/key/
"""


CERT_RENEW_CRON = f"""
cat > /etc/cron.d/vpn-master-certbot <<'VMEOF'
0 0 28 * * root cd {CERTBOT_DIR} && docker compose run --rm certbot renew >> /var/log/vpn-master-certbot.log 2>&1 && docker exec nginx-selfsteal nginx -s reload && docker restart remnanode
VMEOF
chmod 644 /etc/cron.d/vpn-master-certbot
log "Автопродление: /etc/cron.d/vpn-master-certbot (28 числа каждого месяца)"
"""


def node_install(secret_key: str, domains: list[str], selfsteal: bool, panel_ip: str | None,
                 email: str | None = None) -> str:
    parts = ["need_docker", "pkg cron iproute2 || true",
             # старый вариант self-steal на Caddy (ранние версии бота) — убираем
             "docker rm -f selfsteal >/dev/null 2>&1 || true"]
    if selfsteal and domains:
        parts.append(_certbot_issue(domains, email))
        parts.append(f"""
log "Ставлю Nginx self-steal на 127.0.0.1:8443"
mkdir -p {NGINX_SS_DIR}/html && cd {NGINX_SS_DIR}
[ -f html/index.html ] || cat > html/index.html <<'VMEOF'
{SELFSTEAL_HTML}
VMEOF
cat > default.conf <<'VMEOF'
{NGINX_SS_CONF}
VMEOF
cat > docker-compose.yml <<'VMEOF'
services:
  nginx-selfsteal:
    image: nginx:stable-alpine
    container_name: nginx-selfsteal
    restart: unless-stopped
    ports:
      - "127.0.0.1:8443:8443"
    volumes:
      - ./default.conf:/etc/nginx/conf.d/default.conf:ro
      - ./html:/usr/share/nginx/html:ro
      - {CERTBOT_DIR}/certs:/etc/letsencrypt:ro
VMEOF
docker compose up -d
sleep 2
docker exec nginx-selfsteal nginx -t
code=$(curl -sk -o /dev/null -w '%{{http_code}}' --max-time 5 --resolve {q(domains[0])}:8443:127.0.0.1 https://{domains[0]}:8443/ || true)
log "Self-steal отвечает: HTTP $code"
""")
        parts.append(CERT_RENEW_CRON)
    parts.append(f"""
log "Ставлю Remnawave Node"
mkdir -p /opt/remnanode {CERTBOT_DIR}/certs && cd /opt/remnanode
cat > docker-compose.yml <<'VMEOF'
services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: remnawave/node:latest
    restart: always
    network_mode: host
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    environment:
      NODE_PORT: "2222"
      SECRET_KEY: "{secret_key}"
    volumes:
      - {CERTBOT_DIR}/certs:/etc/letsencrypt:ro
VMEOF
docker compose up -d --force-recreate
""")
    if panel_ip:
        parts.append(f"""
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from {q(panel_ip)} to any port 2222 proto tcp
  ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 443/udp
fi
""")
    parts.append("sleep 3; docker ps --format 'table {{.Names}}\\t{{.Status}}'\n"
                 "ss -lntup 2>/dev/null | grep -E ':(443|8443|2222)\\b' || true")
    return _script("\n".join(parts))


def cert_issue(domains: list[str], email: str | None, expand: bool = True) -> str:
    """Перевыпуск/расширение сертификата key (добавить домены) + перезапуск nginx и ноды."""
    return _script(_certbot_issue(domains, email, expand=expand) + """
docker exec nginx-selfsteal nginx -s reload 2>/dev/null || true
docker restart remnanode >/dev/null && log "remnanode перезапущен"
""" + CERT_RENEW_CRON)


CERT_RENEW = _script(f"""
cd {CERTBOT_DIR} && docker compose run --rm certbot renew
docker exec nginx-selfsteal nginx -s reload 2>/dev/null || true
docker restart remnanode >/dev/null && log "remnanode перезапущен"
""")
CERT_DRYRUN = _script(f"cd {CERTBOT_DIR} && docker compose run --rm certbot renew --dry-run")
CERT_STATUS = _script(f"""
cd {CERTBOT_DIR} 2>/dev/null && docker compose run --rm certbot certificates || echo "certbot не установлен"
cat /etc/cron.d/vpn-master-certbot 2>/dev/null || echo "крон автопродления не найден"
""")
NODE_CHECK = _script("""
docker ps --format 'table {{.Names}}\\t{{.Status}}'
echo; ss -lntup 2>/dev/null | grep -E ':(443|8443|2222)\\b' || true
echo; docker logs --tail 15 remnanode 2>&1 || true
""")


# ===================================================================== Bedolaga
def bedolaga_install(*, bot_token: str, admin_ids: str, hooks_domain: str, rw_url: str, rw_key: str,
                     local: bool, web_api_token: str, webhook_secret: str, pg_pass: str,
                     rw_cookie: str | None) -> str:
    compose = "docker-compose.local.yml" if local else "docker-compose.yml"
    env = {
        "BOT_TOKEN": bot_token, "ADMIN_IDS": admin_ids,
        "BOT_RUN_MODE": "webhook", "WEBHOOK_URL": f"https://{hooks_domain}", "WEBHOOK_PATH": "/webhook",
        "WEBHOOK_SECRET_TOKEN": webhook_secret,
        "WEB_API_ENABLED": "true", "WEB_API_PORT": "8080", "WEB_API_DEFAULT_TOKEN": web_api_token,
        "WEB_API_ALLOWED_ORIGINS": f"https://{hooks_domain}",
        "REMNAWAVE_API_URL": rw_url, "REMNAWAVE_API_KEY": rw_key, "REMNAWAVE_AUTH_TYPE": "api_key",
        "POSTGRES_DB": "remnawave_bot", "POSTGRES_USER": "remnawave_user", "POSTGRES_PASSWORD": pg_pass,
    }
    if rw_cookie:
        env["REMNAWAVE_SECRET_KEY"] = f"vmkey:{rw_cookie}"
    sets = "\n".join(f"set_env .env {k} {q(v)}" for k, v in env.items())
    return _script(f"""
need_docker
pkg git
D={BEDOLAGA_DIR}
if [ -d "$D/.git" ]; then cd "$D" && git pull --ff-only || true
else git clone https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot "$D"; cd "$D"; fi
[ -f .env ] || cp .env.example .env
{sets}
mkdir -p data logs
F={compose}; [ -f "$F" ] || F=docker-compose.yml
mark BEDOLAGA_COMPOSE "$F"
log "Собираю и запускаю бота ($F)"
docker compose -f "$F" up -d --build
sleep 5; docker ps --format 'table {{.Names}}\\t{{.Status}}'
""")


def cabinet_bot_env(*, compose_file: str, cabinet_domain: str, hooks_domain: str | None) -> str:
    """На сервере бота: включить кабинет в .env Bedolaga и перезапустить бота."""
    origins = f"https://{cabinet_domain}" + (f",https://{hooks_domain}" if hooks_domain else "")
    return _script(f"""
cd {BEDOLAGA_DIR} || {{ echo "Bedolaga не найден в {BEDOLAGA_DIR}"; exit 2; }}
set_env .env CABINET_ENABLED true
[ -n "$(get_env .env CABINET_JWT_SECRET)" ] || set_env .env CABINET_JWT_SECRET "$(openssl rand -hex 32)"
set_env .env CABINET_ALLOWED_ORIGINS {q(f"https://{cabinet_domain}")}
set_env .env WEB_API_ALLOWED_ORIGINS {q(origins)}
docker compose -f {q(compose_file)} up -d
log "Кабинет включён в .env бота"
""")


def cabinet_front(*, local: bool) -> str:
    """Фронтенд кабинета. local — в сети бота; иначе в remnawave-network на отдельном сервере."""
    net = BEDOLAGA_NET if local else "remnawave-network"
    pre = "" if local else "docker network inspect remnawave-network >/dev/null 2>&1 || docker network create remnawave-network >/dev/null\n"
    return _script(f"""
need_docker
{pre}mkdir -p {CABINET_DIR} && cd {CABINET_DIR}
cat > docker-compose.yml <<'VMEOF'
services:
  cabinet-frontend:
    image: ghcr.io/bedolaga-dev/bedolaga-cabinet:latest
    container_name: cabinet_frontend
    restart: unless-stopped
    networks:
      - bot_network
networks:
  bot_network:
    external: true
    name: {net}
VMEOF
docker compose pull && docker compose up -d
docker ps --format 'table {{{{.Names}}}}\\t{{{{.Status}}}}' | grep -i cabinet || true
""")


# ===================================================================== Модификации
def link_extractor_install(bot_token: str, admin_ids: str) -> str:
    return _script(f"""
need_docker
pkg git
D=/opt/link-extractor
if [ -d "$D/.git" ]; then cd "$D" && git pull --ff-only || true
else git clone https://github.com/tneange1/link-extractor "$D"; cd "$D"; fi
[ -f .env ] || cp .env.example .env
set_env .env COMPOSE_PROFILES extractor,bot
set_env .env TG_BOT_TOKEN {q(bot_token)}
set_env .env TG_ADMIN_IDS {q(admin_ids)}
mkdir -p data/bot && chown -R 1000:1000 data
docker compose up -d --build
sleep 8
S=$(docker exec link-extractor node -e 'try{{console.log(JSON.stringify(require("/app/data/store.json").secrets))}}catch(e){{}}' 2>/dev/null || true)
[ -n "$S" ] && mark LE_SECRETS "$S"
docker ps --format 'table {{{{.Names}}}}\\t{{{{.Status}}}}' | grep -i link || true
""")


LINK_EXTRACTOR_SECRETS = _script("""
S=$(docker exec link-extractor node -e 'try{console.log(JSON.stringify(require("/app/data/store.json").secrets))}catch(e){}' 2>/dev/null || true)
[ -n "$S" ] && mark LE_SECRETS "$S" && echo "секреты получены" || echo "контейнер link-extractor не отвечает"
""")

LINK_EXTRACTOR_REMOVE = _script("""
cd /opt/link-extractor && docker compose down || true
echo "Контейнеры остановлены, папка /opt/link-extractor оставлена (там .env и data/)."
""")

BACKUP_RESTORE_INSTALL_CMD = ("curl -fsSLo ~/backup-restore.sh "
                              "https://raw.githubusercontent.com/distillium/remnawave-backup-restore/main/backup-restore.sh"
                              " && chmod +x ~/backup-restore.sh && ~/backup-restore.sh")
BACKUP_RESTORE_MENU_CMD = "rw-backup"
BACKUP_RESTORE_NOW = _script("rw-backup backup")
BACKUP_RESTORE_UPDATE = _script("rw-backup update")

WARP_INSTALL_CMD = "bash <(curl -fsSL https://raw.githubusercontent.com/distillium/warp-native/main/install.sh)"
WARP_UNINSTALL_CMD = "bash <(curl -fsSL https://raw.githubusercontent.com/distillium/warp-native/main/uninstall.sh)"
WARP_STATUS = _script("""
systemctl is-active wg-quick@warp || true
command -v wg >/dev/null && wg show warp 2>/dev/null | head -20 || echo "интерфейс warp не найден"
curl -s --interface warp --max-time 8 https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|warp)=' || true
""")


# ===================================================================== Безопасность
def ufw_enable(ssh_port: int, rules: list[str]) -> str:
    allow = "\n".join(f"ufw allow {r}" for r in rules)
    return _script(f"""
pkg ufw
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow {int(ssh_port)}/tcp
{allow}
ufw --force enable
ufw status verbose
""")


UFW_DISABLE = _script("command -v ufw >/dev/null && ufw --force disable; ufw status || true")
UFW_STATUS = _script("""
command -v ufw >/dev/null || { echo 'ufw не установлен'; exit 0; }
ufw status verbose | head -4
echo "IPv6 в UFW: $(grep -E '^IPV6=' /etc/default/ufw | cut -d= -f2)"
echo "IPv6 в системе: $([ "$(sysctl -n net.ipv6.conf.all.disable_ipv6 2>/dev/null)" = 1 ] && echo выключен || echo включен)"
echo "Ping: $(grep -qE '^-A ufw-before-input -p icmp --icmp-type echo-request -j DROP' /etc/ufw/before.rules && echo блокируется || echo разрешён)"
echo; ufw status numbered | tail -n +4
""")
# только список правил: «[ 1] 22/tcp ALLOW IN Anywhere» → маркеры для кнопок
UFW_RULES = _script("""
command -v ufw >/dev/null || exit 0
ufw status numbered | grep -E '^\\[ *[0-9]+\\]' | sed -E 's/^\\[ *([0-9]+)\\] +/\\1|/' | while IFS='|' read -r n rest; do
  mark "R$n" "$(echo "$rest" | tr -s ' ')"
done
""")
UFW_RESET = _script("""
pkg ufw
ufw --force reset
echo "Правила сброшены, UFW выключен. Включи заново кнопкой «Включить UFW»."
""")


def ufw_allow(rule: str) -> str:
    return _script(f"pkg ufw\nufw allow {rule}\nufw status numbered")


def ufw_delete(num: int) -> str:
    return _script(f"ufw --force delete {int(num)}\nufw status numbered")


def ufw_ipv6(enabled: bool) -> str:
    v = "yes" if enabled else "no"
    return _script(f"""
pkg ufw
sed -i 's/^IPV6=.*/IPV6={v}/' /etc/default/ufw
grep -q '^IPV6=' /etc/default/ufw || echo 'IPV6={v}' >> /etc/default/ufw
ufw status | grep -q "Status: active" && ufw reload
echo "IPv6 в UFW: {v}"
""")


def sys_ipv6(enabled: bool) -> str:
    if enabled:
        return _script("""
rm -f /etc/sysctl.d/99-vpn-master-ipv6.conf
sysctl -w net.ipv6.conf.all.disable_ipv6=0 net.ipv6.conf.default.disable_ipv6=0 net.ipv6.conf.lo.disable_ipv6=0
echo "IPv6 в системе включён"; ip -6 addr | grep inet6 | head -5 || true
""")
    return _script("""
cat > /etc/sysctl.d/99-vpn-master-ipv6.conf <<'VMEOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
VMEOF
sysctl -p /etc/sysctl.d/99-vpn-master-ipv6.conf
echo "IPv6 в системе выключен (переживёт перезагрузку)"
""")


def ufw_ping(allow: bool) -> str:
    src, dst = ("DROP", "ACCEPT") if allow else ("ACCEPT", "DROP")
    return _script(f"""
pkg ufw
F=/etc/ufw/before.rules
cp "$F" "$F.vm.bak"
sed -i -E 's/^(-A ufw-before-input -p icmp --icmp-type echo-request) -j {src}$/\\1 -j {dst}/' "$F"
ufw status | grep -q "Status: active" && ufw reload
echo "Ping: {'разрешён' if allow else 'блокируется'}"
""")


def ssh_add_key(username: str, pubkey: str) -> str:
    return _script(f"""
U={q(username)}
HOME_DIR=$(getent passwd "$U" | cut -d: -f6)
mkdir -p "$HOME_DIR/.ssh" && chmod 700 "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
grep -qxF {q(pubkey)} "$HOME_DIR/.ssh/authorized_keys" || echo {q(pubkey)} >> "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"; chown -R "$U":"$U" "$HOME_DIR/.ssh" 2>/dev/null || true
echo "Ключ добавлен в $HOME_DIR/.ssh/authorized_keys"
""")


def ssh_remove_key(username: str, pubkey: str) -> str:
    # сравниваем по типу+телу ключа, комментарий не важен
    body = " ".join(pubkey.split()[:2])
    return _script(f"""
U={q(username)}
HOME_DIR=$(getent passwd "$U" | cut -d: -f6)
F="$HOME_DIR/.ssh/authorized_keys"
[ -f "$F" ] || {{ echo "authorized_keys нет"; exit 0; }}
cp "$F" "$F.vm.bak"
grep -vF {q(body)} "$F.vm.bak" > "$F" || true
chmod 600 "$F"
echo "Ключ убран из $F (копия: $F.vm.bak)"
""")


SSHD_RELOAD = r"""
sshd -t
if systemctl is-active --quiet ssh.socket 2>/dev/null; then
  systemctl daemon-reload; systemctl restart ssh.socket; systemctl restart ssh 2>/dev/null || true
else
  systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart
fi
"""
SSHD_INCLUDE = r"""
mkdir -p /etc/ssh/sshd_config.d
grep -qE '^\s*Include\s+/etc/ssh/sshd_config.d' /etc/ssh/sshd_config || sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
"""


def ssh_password_auth(enabled: bool) -> str:
    val = "yes" if enabled else "no"
    return _script(SSHD_INCLUDE + f"""
cat > /etc/ssh/sshd_config.d/00-vpn-master.conf <<'VMEOF'
PasswordAuthentication {val}
KbdInteractiveAuthentication {val}
PubkeyAuthentication yes
VMEOF
""" + SSHD_RELOAD + """
echo "PasswordAuthentication = $(sshd -T 2>/dev/null | grep -i '^passwordauthentication' | awk '{print $2}')"
""")


def ssh_root_login(mode: str) -> str:
    assert mode in ("yes", "prohibit-password", "no")
    return _script(SSHD_INCLUDE + f"""
echo 'PermitRootLogin {mode}' > /etc/ssh/sshd_config.d/02-vpn-master-root.conf
""" + SSHD_RELOAD + """
echo "PermitRootLogin = $(sshd -T 2>/dev/null | grep -i '^permitrootlogin' | awk '{print $2}')"
""")


def ssh_ports(ports: list[int], close_port: int | None = None) -> str:
    """Шаг смены порта: слушать на ports (сначала старый+новый, потом только новый)."""
    lines = "\n".join(f"Port {int(p)}" for p in ports)
    ufw = "\n".join(f"ufw allow {int(p)}/tcp >/dev/null" for p in ports)
    close = (f"ufw delete allow {int(close_port)}/tcp >/dev/null 2>&1 || true\n"
             f"echo 'Порт {int(close_port)} закрыт в UFW'") if close_port else ""
    return _script(SSHD_INCLUDE + f"""
# Port из основного конфига мешает — комментируем
sed -i -E 's/^\\s*Port\\s+/#&/' /etc/ssh/sshd_config
cat > /etc/ssh/sshd_config.d/01-vpn-master-port.conf <<'VMEOF'
{lines}
VMEOF
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
{ufw}
{close}
fi
""" + SSHD_RELOAD + """
sleep 1
echo "sshd слушает:"; ss -ltnp 2>/dev/null | grep -E 'sshd|systemd' | awk '{print $4}' | sort -u
""")


BBR_CONF = """net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_notsent_lowat = 16384
net.core.netdev_max_backlog = 250000
net.core.somaxconn = 8192
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.ipv4.tcp_rmem = 4096 87380 67108864
net.ipv4.tcp_wmem = 4096 65536 67108864
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384"""

BBR_STATUS = _script("""
echo "congestion: $(sysctl -n net.ipv4.tcp_congestion_control) (доступно: $(sysctl -n net.ipv4.tcp_available_congestion_control))"
echo "qdisc: $(sysctl -n net.core.default_qdisc)"
echo "ядро: $(uname -r)"
[ -f /etc/sysctl.d/99-vpn-master-bbr.conf ] && echo "профиль vpn-master: включён" || echo "профиль vpn-master: нет"
""")


def bbr(enabled: bool) -> str:
    if enabled:
        return _script(f"""
modprobe tcp_bbr 2>/dev/null || true
grep -qx tcp_bbr /etc/modules-load.d/vpn-master-bbr.conf 2>/dev/null || echo tcp_bbr > /etc/modules-load.d/vpn-master-bbr.conf
cat > /etc/sysctl.d/99-vpn-master-bbr.conf <<'VMEOF'
{BBR_CONF}
VMEOF
sysctl -p /etc/sysctl.d/99-vpn-master-bbr.conf
""") + BBR_STATUS.split("\n", 1)[1]
    return _script("""
rm -f /etc/sysctl.d/99-vpn-master-bbr.conf /etc/modules-load.d/vpn-master-bbr.conf
sysctl -w net.ipv4.tcp_congestion_control=cubic net.core.default_qdisc=fq_codel
""") + BBR_STATUS.split("\n", 1)[1]


SERVER_STATE = _script("""
echo "$(hostname) · $(. /etc/os-release && echo $PRETTY_NAME) · ядро $(uname -r)"
echo "uptime: $(uptime -p 2>/dev/null || uptime)"
echo "load: $(cut -d' ' -f1-3 /proc/loadavg) · CPU: $(nproc)"
free -h | awk 'NR<=2'
df -h / | awk 'NR<=2'
echo; echo "IP: $(curl -s4 --max-time 5 https://ifconfig.me || echo ?)"
echo; command -v docker >/dev/null && docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || echo "docker не установлен"
""")
SERVER_REBOOT = _script("""
echo "Перезагружаюсь через 3 секунды…"
nohup sh -c 'sleep 3; reboot' >/dev/null 2>&1 &
""")
DOCKER_PRUNE = _script("docker system prune -af --filter 'until=168h' && df -h / | awk 'NR<=2'")
APT_UPGRADE = _script("""
apt-get update -qq
apt-get -y -qq -o Dpkg::Options::=--force-confold upgrade
[ -f /var/run/reboot-required ] && echo "⚠️ нужна перезагрузка" || echo "перезагрузка не нужна"
""")


SSH_STATUS = _script("""
sshd -T 2>/dev/null | grep -iE '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication) ' || true
""")


# ===================================================================== Обновления
UPDATES = {
    "panel": _script(_compose_restart("/opt/remnawave") + "\ndocker image prune -f >/dev/null || true"),
    "subpage": _script(_compose_restart("/opt/remnawave/subscription")),
    "caddy": _script(_compose_restart(CADDY_DIR) + "\ncaddy_networks"),
    "nginx": _script(_compose_restart(NGINX_DIR) + "\nproxy_networks nginx"),
    "node": _script(_compose_restart("/opt/remnanode") + "\ndocker image prune -f >/dev/null || true"),
    "selfsteal": _script(_compose_restart(NGINX_SS_DIR)),
    "cabinet": _script(_compose_restart(CABINET_DIR)),
    "link_extractor": _script("cd /opt/link-extractor && git pull --ff-only && docker compose up -d --build"),
    "backup_restore": BACKUP_RESTORE_UPDATE,
}


def bedolaga_update(compose_file: str) -> str:
    return _script(f"""
cd {BEDOLAGA_DIR}
git pull --ff-only
docker compose -f {q(compose_file)} up -d --build
docker image prune -f >/dev/null || true
docker compose -f {q(compose_file)} ps
""")


def custom(cmd: str) -> str:
    return "#!/usr/bin/env bash\n" + cmd + "\n"
