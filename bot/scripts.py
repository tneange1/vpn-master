"""Генераторы bash-скриптов. Всё, что приходит от пользователя, экранируется через shlex.quote."""
from shlex import quote as q

BEDOLAGA_DIR = "/opt/remnawave-bedolaga-telegram-bot"
BEDOLAGA_NET = "remnawave-bedolaga-telegram-bot_bot_network"
CABINET_DIR = "/opt/bedolaga-cabinet"
CADDY_DIR = "/opt/remnawave/caddy"

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

# ---- общий Caddy (reverse proxy) в /opt/remnawave/caddy, сайты в sites/*.caddy
caddy_ours(){ [ -f CADDYDIR/.vpn-master ]; }
caddy_setup(){
  need_docker
  if [ -d CADDYDIR ] && ! caddy_ours; then
    log "Нашёл чужой CADDYDIR — переношу в CADDYDIR.bak.$(date +%s)"
    (cd CADDYDIR && docker compose down 2>/dev/null) || true
    mv CADDYDIR "CADDYDIR.bak.$(date +%s)"
  fi
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
caddy_networks(){
  for n in BEDOLAGANET; do
    if docker network inspect "$n" >/dev/null 2>&1; then docker network connect "$n" caddy 2>/dev/null || true; fi
  done
}
caddy_site(){ # имя сайта; содержимое — stdin
  local content; content=$(cat)
  if docker ps --format '{{.Names}}' | grep -qx caddy && ! caddy_ours; then
    echo "!! На сервере работает Caddy, который ставил не vpn-master. Добавь блок вручную:"; echo "$content"; return 3
  fi
  caddy_ours && docker ps --format '{{.Names}}' | grep -qx caddy || caddy_setup
  printf '%s\n' "$content" > "CADDYDIR/sites/$1.caddy"
  caddy_networks
  docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  log "Caddy: сайт $1 применён"
}
""".replace("CADDYDIR", CADDY_DIR).replace("BEDOLAGANET", BEDOLAGA_NET)


def _script(body: str) -> str:
    return PRELUDE + "\n" + body.strip() + "\n"


def _heredoc_site(name: str, content: str) -> str:
    return f"caddy_site {q(name)} <<'VMSITE'\n{content.rstrip()}\nVMSITE\n"


def _compose_restart(path: str) -> str:
    return f"cd {q(path)} && docker compose pull && docker compose down && docker compose up -d && docker compose ps"


# ===================================================================== Caddy-сайты
def panel_site(domain: str, sub_domain: str | None, cookie: str | None, link_extractor: bool) -> str:
    # route{} — директивы выполняются строго по порядку (без авто-сортировки Caddy)
    inner = []
    if cookie:
        inner += [f"@vmset query vmkey={cookie}",
                  f'header @vmset Set-Cookie "vmkey={cookie}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=2592000"',
                  "redir @vmset / 302",
                  f"@vmdeny not header Cookie *vmkey={cookie}*",
                  "abort @vmdeny"]
    if link_extractor:
        inner.append("reverse_proxy /link-extractor/* link-extractor:3020")
    inner.append("reverse_proxy remnawave:3000")
    out = f"https://{domain} {{\n\troute {{\n" + "".join(f"\t\t{x}\n" for x in inner) + "\t}\n}\n"
    if sub_domain:
        upstream = "link-extractor:3020" if link_extractor else "remnawave-subscription-page:3010"
        out += f"\nhttps://{sub_domain} {{\n\treverse_proxy {upstream}\n}}\n"
    return out


def apply_panel_site(domain, sub_domain, cookie, link_extractor) -> str:
    return _script(_heredoc_site("panel", panel_site(domain, sub_domain, cookie, link_extractor)))


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

log "Настраиваю Caddy"
""" + _heredoc_site("panel", panel_site(domain, sub_domain, None, False)) + """
log "Готово. Проверь DNS: A-записи доменов должны смотреть на этот сервер."
docker ps --format 'table {{.Names}}\\t{{.Status}}'
""")


def set_subpage_token(token: str) -> str:
    return _script(f"""
cd /opt/remnawave/subscription
set_env .env REMNAWAVE_API_TOKEN {q(token)}
docker compose up -d
""")


# ===================================================================== Нода
def node_install(secret_key: str, domain: str | None, selfsteal: bool, panel_ip: str | None) -> str:
    parts = ["need_docker"]
    if selfsteal and domain:
        parts.append(f"""
log "Ставлю self-steal Caddy для {domain}"
mkdir -p /opt/selfsteal/html /opt/selfsteal/data && cd /opt/selfsteal
if [ ! -f html/index.html ]; then
cat > html/index.html <<'VMEOF'
<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Welcome</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui;margin:0;display:grid;place-items:center;height:100vh;background:#f4f4f5;color:#27272a}}</style>
</head><body><main><h1>Under construction</h1><p>We are working on something new.</p></main></body></html>
VMEOF
fi
cat > Caddyfile <<'VMEOF'
{{
	https_port 9443
	http_port 80
}}
http://{domain} {{
	bind 0.0.0.0
	redir https://{{host}}{{uri}} permanent
}}
https://{domain}:9443 {{
	bind 127.0.0.1
	root * /var/www/html
	file_server
}}
VMEOF
cat > docker-compose.yml <<'VMEOF'
services:
  selfsteal:
    image: caddy:2
    container_name: selfsteal
    restart: always
    network_mode: host
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - ./html:/var/www/html
      - ./data:/data
VMEOF
docker compose up -d
""")
    parts.append(f"""
log "Ставлю Remnawave Node"
mkdir -p /opt/remnanode && cd /opt/remnanode
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
      - /opt/selfsteal/data:/var/lib/selfsteal:ro
VMEOF
docker compose up -d
""")
    if panel_ip:
        parts.append(f"""
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from {q(panel_ip)} to any port 2222 proto tcp
  ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 443/udp; ufw allow 8443/udp
fi
""")
    parts.append("sleep 3; docker ps --format 'table {{.Names}}\\t{{.Status}}'")
    return _script("\n".join(parts))


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
    site = f"https://{hooks_domain} {{\n\treverse_proxy remnawave_bot:8080\n}}"
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
""" + _heredoc_site("bedolaga", site) + """
sleep 5; docker ps --format 'table {{.Names}}\\t{{.Status}}'
""")


def cabinet_install(*, compose_file: str, cabinet_domain: str, hooks_domain: str | None) -> str:
    origins = f"https://{cabinet_domain}" + (f",https://{hooks_domain}" if hooks_domain else "")
    site = f"""https://{cabinet_domain} {{
	encode gzip zstd
	handle /api/* {{
		uri strip_prefix /api
		reverse_proxy remnawave_bot:8080
	}}
	handle {{
		reverse_proxy cabinet_frontend:80
	}}
}}"""
    return _script(f"""
need_docker
cd {BEDOLAGA_DIR} || {{ echo "Bedolaga не найден в {BEDOLAGA_DIR}"; exit 2; }}
set_env .env CABINET_ENABLED true
[ -n "$(get_env .env CABINET_JWT_SECRET)" ] || set_env .env CABINET_JWT_SECRET "$(openssl rand -hex 32)"
set_env .env CABINET_ALLOWED_ORIGINS {q(f"https://{cabinet_domain}")}
set_env .env WEB_API_ALLOWED_ORIGINS {q(origins)}
docker compose -f {q(compose_file)} up -d
mkdir -p {CABINET_DIR} && cd {CABINET_DIR}
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
    name: {BEDOLAGA_NET}
VMEOF
docker compose pull && docker compose up -d
""" + _heredoc_site("cabinet", site) + """
docker ps --format 'table {{.Names}}\\t{{.Status}}'
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
ufw allow {int(ssh_port)}/tcp
{allow}
ufw --force enable
ufw status verbose
""")


UFW_DISABLE = _script("command -v ufw >/dev/null && ufw --force disable; ufw status || true")
UFW_STATUS = _script("command -v ufw >/dev/null && ufw status numbered || echo 'ufw не установлен'")


def ufw_allow(rule: str) -> str:
    return _script(f"pkg ufw\nufw allow {rule}\nufw status numbered")


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


def ssh_password_auth(enabled: bool) -> str:
    val = "yes" if enabled else "no"
    return _script(f"""
mkdir -p /etc/ssh/sshd_config.d
grep -qE '^\\s*Include\\s+/etc/ssh/sshd_config.d' /etc/ssh/sshd_config || sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
cat > /etc/ssh/sshd_config.d/00-vpn-master.conf <<'VMEOF'
PasswordAuthentication {val}
KbdInteractiveAuthentication {val}
PubkeyAuthentication yes
VMEOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || service ssh reload
echo "PasswordAuthentication = $(sshd -T 2>/dev/null | grep -i '^passwordauthentication' | awk '{{print $2}}')"
""")


SSH_STATUS = _script("""
sshd -T 2>/dev/null | grep -iE '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication) ' || true
""")


# ===================================================================== Обновления
UPDATES = {
    "panel": _script(_compose_restart("/opt/remnawave") + "\ndocker image prune -f >/dev/null || true"),
    "subpage": _script(_compose_restart("/opt/remnawave/subscription")),
    "caddy": _script(_compose_restart(CADDY_DIR) + "\ncaddy_networks"),
    "node": _script(_compose_restart("/opt/remnanode") + "\ndocker image prune -f >/dev/null || true"),
    "selfsteal": _script(_compose_restart("/opt/selfsteal")),
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
