"""Генераторы bash-скриптов. Всё, что приходит от пользователя, экранируется через shlex.quote."""
from dataclasses import asdict, dataclass, field
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
proxy_networks(){ # контейнер прокси: подключаем ко всем сетям панели/бота/кабинета/экстрактора
  local px="$1" n c
  running "$px" || return 0
  for n in remnawave-network BEDOLAGANET bedolaga-cabinet_default; do
    if docker network inspect "$n" >/dev/null 2>&1; then docker network connect "$n" "$px" 2>/dev/null || true; fi
  done
  for c in remnawave remnawave-subscription-page remnawave_bot cabinet_frontend link-extractor; do
    running "$c" || continue
    for n in $(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$c"); do
      case "$n" in host|none|bridge) continue;; esac
      docker network connect "$n" "$px" 2>/dev/null || true
    done
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
nginx_cert(){ # имя "домены" — сертификат certbot webroot (nginx должен работать)
  local name="$1" domains="$2" args="" d mail
  certbot_compose
  for d in $domains; do args="$args -d $d"; done
  log "Сертификат $name: $domains"
  if [ -n "${VM_EMAIL:-}" ]; then mail="--email $VM_EMAIL"; else mail="--register-unsafely-without-email"; fi
  # shellcheck disable=SC2086
  (cd CERTBOTDIR && docker compose run --rm -T certbot certonly --webroot -w /var/www/certbot \
     --non-interactive --agree-tos $mail --cert-name "$name" --expand $args </dev/null) || { echo "!! certbot не выдал сертификат для $domains"; return 4; }
}
vm_rm_site(){ rm -f "CADDYDIR/sites/$1.caddy" "NGINXDIR/conf.d/$1.conf"; }
nginx_site(){ # имя "домены через пробел"; содержимое — stdin
  local name="$1" domains="$2" content args="" d
  content=$(cat)
  if running nginx && ! nginx_ours; then
    echo "!! На сервере работает Nginx, который ставил не vpn-master. Добавь блок вручную:"; echo "$content"; return 3
  fi
  if ! (nginx_ours && running nginx); then nginx_setup || return $?; fi
  nginx_cert "$name" "$domains" || return 4
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


# ===================================================================== Нода (self-steal по мануалу, несколько нод на сервер)
NGINX_SS_DIR = "/opt/nginx-selfsteal"

SELFSTEAL_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Сайт в разработке</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;background:#f4f4f5;color:#27272a}main{text-align:center;padding:24px}h1{font-weight:600}</style>
</head><body><main><h1>Сайт в разработке</h1><p>Скоро здесь что-то появится.</p></main></body></html>"""


@dataclass
class NodeSpec:
    """Где и как живёт нода на сервере. Значения по умолчанию = мануал (одна нода на сервер)."""
    ct: str = "remnanode"  # имя контейнера remnanode
    node_port: int = 2222  # NODE_PORT — порт для связи панели с нодой
    ss_port: int = 8443  # локальный порт Nginx self-steal (target Reality)
    reality_port: int | None = 443  # TCP
    hy2_port: int | None = 443  # UDP
    domains: list = field(default_factory=list)
    selfsteal: bool = True

    @property
    def default(self) -> bool:
        return self.ct == "remnanode"

    @property
    def dir(self) -> str:
        return f"/opt/{self.ct}"

    @property
    def ss_ct(self) -> str:
        return "nginx-selfsteal" if self.default else f"{self.ct}-selfsteal"

    @property
    def ss_dir(self) -> str:
        return NGINX_SS_DIR if self.default else f"/opt/{self.ct}-selfsteal"

    @property
    def cert(self) -> str:
        return "key" if self.default else self.ct

    @property
    def cron(self) -> str:
        return "/etc/cron.d/vpn-master-certbot" + ("" if self.default else f"-{self.ct}")

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict | None) -> "NodeSpec":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _ss_conf(spec: NodeSpec) -> str:
    return f"""server {{
    listen {spec.ss_port} ssl default_server;
    server_name _;

    ssl_certificate     /etc/letsencrypt/live/{spec.cert}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{spec.cert}/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;

    root /usr/share/nginx/html;
    index index.html;

    location / {{
        try_files $uri $uri/ =404;
    }}
}}"""


def _certbot_issue(domains: list[str], email: str | None, expand: bool = False, cert: str = "key") -> str:
    ds = " ".join(f"-d {q(d)}" for d in domains)
    mail = f"--email {q(email)}" if email else "--register-unsafely-without-email"
    return f"""
log "Выпускаю сертификат (cert-name {cert}): {' '.join(domains)}"
certbot_compose && cd {CERTBOT_DIR}
if ss -ltnH 2>/dev/null | awk '{{print $4}}' | grep -qE '(^|:)80$'; then
  echo "!! Порт 80 занят — certbot --standalone не сможет пройти проверку:"; ss -ltnp | grep -E ':80\\b' || true; exit 4
fi
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then ufw allow 80/tcp >/dev/null; fi
docker compose run --rm -T certbot certonly --standalone --non-interactive --agree-tos {mail} \\
  --cert-name {q(cert)} {'--expand ' if expand else ''}{ds} </dev/null
ls -l {CERTBOT_DIR}/certs/live/{cert}/
"""


def _renew_cron(spec: NodeSpec) -> str:
    return f"""
cat > {spec.cron} <<'VMEOF'
0 0 28 * * root cd {CERTBOT_DIR} && docker compose run --rm -T certbot renew >> /var/log/vpn-master-certbot.log 2>&1 </dev/null; docker exec {spec.ss_ct} nginx -s reload; docker restart {spec.ct}
VMEOF
chmod 644 {spec.cron}
log "Автопродление: {spec.cron} (28 числа каждого месяца)"
"""


def _port_checks(spec: NodeSpec) -> str:
    """Порты должны быть свободны (кроме случая, когда переустанавливаем ту же ноду)."""
    checks = [("tcp", spec.node_port, "NODE_PORT")]
    if spec.selfsteal:
        checks.append(("tcp", spec.ss_port, "self-steal"))
    if spec.reality_port:
        checks.append(("tcp", spec.reality_port, "Reality"))
    if spec.hy2_port:
        checks.append(("udp", spec.hy2_port, "Hysteria2"))
    lines = "\n".join(f"busy {proto} {port} {q(label)}" for proto, port, label in checks)
    return f"""
busy(){{ # proto port label
  local flag=-ltnH; [ "$1" = udp ] && flag=-lunH
  if ss $flag 2>/dev/null | awk '{{print $4}}' | grep -qE "(^|:)$2$"; then
    echo "!! $1-порт $2 ($3) уже занят:"; ss $flag -p 2>/dev/null | grep -E ":$2\\b" || true; BUSY=1
  fi
}}
BUSY=0
if docker ps -a --format '{{{{.Names}}}}' | grep -qxE {q(spec.ct + '|' + spec.ss_ct)}; then
  log "Нода {spec.ct} уже есть — переустанавливаю (проверку портов пропускаю)"
else
{lines}
fi
[ "$BUSY" = 1 ] && {{ echo "!! Выбери другие порты (или имя контейнера) — установка остановлена"; exit 5; }}
"""


def node_install(secret_key: str, spec: NodeSpec, panel_ip: str | None, email: str | None = None) -> str:
    parts = ["need_docker", "pkg cron iproute2 || true", _port_checks(spec)]
    if spec.default:
        parts.append("docker rm -f selfsteal >/dev/null 2>&1 || true")  # Caddy self-steal ранних версий
    if spec.selfsteal and spec.domains:
        d0 = spec.domains[0]
        parts.append(_certbot_issue(spec.domains, email, cert=spec.cert))
        parts.append(f"""
log "Ставлю Nginx self-steal ({spec.ss_ct}) на 127.0.0.1:{spec.ss_port}"
mkdir -p {spec.ss_dir}/html && cd {spec.ss_dir}
[ -f html/index.html ] || cat > html/index.html <<'VMEOF'
{SELFSTEAL_HTML}
VMEOF
cat > default.conf <<'VMEOF'
{_ss_conf(spec)}
VMEOF
cat > docker-compose.yml <<'VMEOF'
services:
  {spec.ss_ct}:
    image: nginx:stable-alpine
    container_name: {spec.ss_ct}
    restart: unless-stopped
    ports:
      - "127.0.0.1:{spec.ss_port}:{spec.ss_port}"
    volumes:
      - ./default.conf:/etc/nginx/conf.d/default.conf:ro
      - ./html:/usr/share/nginx/html:ro
      - {CERTBOT_DIR}/certs:/etc/letsencrypt:ro
VMEOF
docker compose up -d
sleep 2
docker exec {spec.ss_ct} nginx -t
code=$(curl -sk -o /dev/null -w '%{{http_code}}' --max-time 5 --resolve {q(d0)}:{spec.ss_port}:127.0.0.1 https://{d0}:{spec.ss_port}/ || true)
log "Self-steal отвечает: HTTP $code"
""")
        parts.append(_renew_cron(spec))
    parts.append(f"""
log "Ставлю Remnawave Node: контейнер {spec.ct}, NODE_PORT {spec.node_port}"
mkdir -p {spec.dir} {CERTBOT_DIR}/certs && cd {spec.dir}
cat > docker-compose.yml <<'VMEOF'
services:
  {spec.ct}:
    container_name: {spec.ct}
    hostname: {spec.ct}
    image: remnawave/node:latest
    restart: always
    network_mode: host
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    environment:
      NODE_PORT: "{spec.node_port}"
      SECRET_KEY: "{secret_key}"
    volumes:
      - {CERTBOT_DIR}/certs:/etc/letsencrypt:ro
VMEOF
docker compose up -d --force-recreate
""")
    rules = ["80/tcp"] + ([f"{spec.reality_port}/tcp"] if spec.reality_port else []) + \
            ([f"{spec.hy2_port}/udp"] if spec.hy2_port else [])
    allow_panel = f"ufw allow from {q(panel_ip)} to any port {spec.node_port} proto tcp" if panel_ip else \
        f"ufw allow {spec.node_port}/tcp"
    parts.append(f"""
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  {allow_panel}
  {'; '.join('ufw allow ' + r for r in rules)}
fi
""")
    ports = "|".join(str(x) for x in (spec.node_port, spec.ss_port, spec.reality_port, spec.hy2_port) if x)
    parts.append(f"sleep 3; docker ps --filter name={q(spec.ct)} --format 'table {{{{.Names}}}}\\t{{{{.Status}}}}'\n"
                 f"ss -lntup 2>/dev/null | grep -E ':({ports})\\b' || true")
    return _script("\n".join(parts))


def cert_issue(spec: NodeSpec, domains: list[str], email: str | None, expand: bool = True) -> str:
    """Перевыпуск/расширение сертификата ноды + перезапуск её nginx и контейнера."""
    return _script(_certbot_issue(domains, email, expand=expand, cert=spec.cert) + f"""
docker exec {spec.ss_ct} nginx -s reload 2>/dev/null || true
docker restart {spec.ct} >/dev/null && log "{spec.ct} перезапущен"
""" + _renew_cron(spec))


def cert_renew(spec: NodeSpec) -> str:
    return _script(f"""
cd {CERTBOT_DIR} && docker compose run --rm -T certbot renew </dev/null
docker exec {spec.ss_ct} nginx -s reload 2>/dev/null || true
docker restart {spec.ct} >/dev/null && log "{spec.ct} перезапущен"
""")


CERT_DRYRUN = _script(f"cd {CERTBOT_DIR} && docker compose run --rm -T certbot renew --dry-run </dev/null")


def cert_status(spec: NodeSpec) -> str:
    return _script(f"""
cd {CERTBOT_DIR} 2>/dev/null && docker compose run --rm -T certbot certificates --cert-name {q(spec.cert)} </dev/null || echo "certbot не установлен"
cat {spec.cron} 2>/dev/null || echo "крон автопродления не найден"
""")


def node_check(spec: NodeSpec) -> str:
    ports = "|".join(str(x) for x in (spec.node_port, spec.ss_port, spec.reality_port, spec.hy2_port) if x)
    return _script(f"""
docker ps --format 'table {{{{.Names}}}}\\t{{{{.Status}}}}' | grep -E 'NAMES|{spec.ct}' || true
echo; ss -lntup 2>/dev/null | grep -E ':({ports})\\b' || true
echo; docker logs --tail 15 {q(spec.ct)} 2>&1 || true
""")


def node_update(spec: NodeSpec) -> str:
    return _script(_compose_restart(spec.dir) + "\ndocker image prune -f >/dev/null || true")


def selfsteal_update(spec: NodeSpec) -> str:
    return _script(_compose_restart(spec.ss_dir))


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
    if rw_cookie:  # секретный путь панели: AUTH_TYPE=caddy → X-Api-Key = CADDY_TOKEN, плюс cookie
        env.update({"REMNAWAVE_AUTH_TYPE": "caddy", "REMNAWAVE_CADDY_TOKEN": rw_cookie, "REMNAWAVE_SECRET_KEY": ""})
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
def link_extractor_install(*, panel_name: str, panel_domain: str, sub_domain: str | None, rw_token: str,
                           tg_token: str, tg_chat: int, proxy_auto: bool) -> str:
    """Только экстрактор (без отдельного бота) через его install.sh. Управление — из vpn-master,
    уведомления экстрактора идут через нашего бота владельцу."""
    args = ["--mode", "extractor", "--yes", "--panel-name", panel_name, "--auto-update", "1",
            "--proxy", "auto" if proxy_auto else "none", "--tg-token", tg_token, "--tg-chat", str(tg_chat),
            "--api-token", rw_token, "--panel-domain", panel_domain]
    if sub_domain:
        args += ["--sub-domain", sub_domain]
    return _script(f"""
need_docker
curl -fsSL https://raw.githubusercontent.com/tneange1/link-extractor/main/install.sh -o /tmp/le-install.sh
# LE_RESULT содержит API-токен — превращаем в скрытый маркер
bash /tmp/le-install.sh {" ".join(q(a) for a in args)} </dev/null 2>&1 | sed -E 's/^LE_RESULT (.*)$/##VM LE_RESULT=\\1/; s/^LE_ERROR (.*)$/!! \\1/'
rm -f /tmp/le-install.sh
cd /opt/link-extractor
# отдельный бот экстрактора не нужен — всё в vpn-master
sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=extractor/' .env
set_env .env TG_BOT_TOKEN {q(tg_token)}
set_env .env TG_ADMIN_IDS {q(str(tg_chat))}
docker rm -f link-extractor-bot >/dev/null 2>&1 || true
docker compose up -d --remove-orphans >/dev/null 2>&1 || docker compose up -d
log "Link Extractor работает без отдельного бота"
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
    "cabinet": _script(_compose_restart(CABINET_DIR)),
    "link_extractor": _script("cd /opt/link-extractor && git pull --ff-only && docker compose up -d --build"),
    "backup_restore": BACKUP_RESTORE_UPDATE,
}


def bedolaga_update(compose_file: str, bot_dir: str = BEDOLAGA_DIR) -> str:
    return _script(f"""
cd {q(bot_dir)}
git pull --ff-only
docker compose -f {q(compose_file)} up -d --build
docker image prune -f >/dev/null || true
docker compose -f {q(compose_file)} ps
""")


def bedolaga_env(compose_file: str, env: dict, bot_dir: str = BEDOLAGA_DIR) -> str:
    """Поменять переменные .env Bedolaga и перезапустить бота. Значение None — удалить переменную."""
    lines = []
    for k, v in env.items():
        lines.append(f"sed -i '/^{k}=/d' .env" if v is None else f"set_env .env {k} {q(v)}")
    return _script(f"""
cd {q(bot_dir)} || {{ echo "Bedolaga не найден в {bot_dir}"; exit 2; }}
{chr(10).join(lines)}
docker compose -f {q(compose_file)} up -d
log "Bedolaga перезапущен с новыми настройками"
""")


FIX_PROXY_NETS = _script("""
for px in caddy nginx; do
  if running "$px"; then proxy_networks "$px"; log "$px подключён к сетям:"; docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$px"; fi
done
""")


# ===================================================================== Обнаружение (фикс прокси, Bedolaga)
DISCOVER = _script(r"""
envv(){ grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true; }
lbl(){ docker inspect -f "{{ index .Config.Labels \"$2\" }}" "$1" 2>/dev/null | sed 's/<no value>//' || true; }
command -v docker >/dev/null 2>&1 || { echo "docker не установлен"; exit 0; }
i=0
for c in $(docker ps --format '{{.Names}}'); do
  i=$((i+1))
  img=$(docker inspect -f '{{.Config.Image}}' "$c")
  nets=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}},{{end}}' "$c")
  ports=$(docker port "$c" 2>/dev/null | tr '\n' ',' || true)
  mark "CT$i" "$c|$img|$(lbl "$c" com.docker.compose.project.working_dir)|$(lbl "$c" com.docker.compose.project.config_files)|$nets|$ports"
  echo "контейнер: $c ($img)"
done
RWD=$(lbl remnawave com.docker.compose.project.working_dir); RWD=${RWD:-/opt/remnawave}
for k in FRONT_END_DOMAIN SUB_PUBLIC_DOMAIN; do mark "RW_$k" "$(envv "$RWD/.env" $k)"; done
SPD=$(lbl remnawave-subscription-page com.docker.compose.project.working_dir)
[ -n "$SPD" ] && mark SP_APP_PORT "$(envv "$SPD/.env" APP_PORT)"
BOTC=$(docker ps --format '{{.Names}}' | grep -xE 'remnawave_bot|bedolaga_bot|remnawave-bedolaga-telegram-bot' | head -1 || true)
if [ -n "$BOTC" ]; then
  BD=$(lbl "$BOTC" com.docker.compose.project.working_dir)
  mark BD_CT "$BOTC"; mark BD_DIR "$BD"; mark BD_COMPOSE "$(basename "$(lbl "$BOTC" com.docker.compose.project.config_files | cut -d, -f1)")"
  for k in BOT_TOKEN ADMIN_IDS WEBHOOK_URL BOT_RUN_MODE CABINET_ENABLED CABINET_ALLOWED_ORIGINS REMNAWAVE_API_URL REMNAWAVE_AUTH_TYPE WEB_API_DEFAULT_TOKEN; do
    mark "BD_$k" "$(envv "$BD/.env" $k)"
  done
  echo "Bedolaga: $BOTC ($BD)"
fi
# ключ X-Api-Key из текущего Caddyfile (если прокси ставили вручную)
for c in $(docker ps --format '{{.Names}}|{{.Ports}}' | grep -E ':443->' | cut -d'|' -f1); do
  wd=$(lbl "$c" com.docker.compose.project.working_dir); mark PROXY "$c|$(docker inspect -f '{{.Config.Image}}' "$c")|$wd"
  for f in "$wd/Caddyfile" "$wd"/*.caddy "$wd"/sites/*.caddy; do
    [ -f "$f" ] || continue
    k=$(grep -oE 'X-Api-Key[[:space:]]+[^[:space:]}]+' "$f" 2>/dev/null | head -1 | awk '{print $2}' || true)
    case "$k" in \{\$*) v=${k#\{\$}; v=${v%\}}; k=$(envv "$wd/.env" "$v");; esac
    if [ -n "$k" ]; then mark FOREIGN_KEY "$k"; echo "в $f найден ключ X-Api-Key"; break; fi
  done
done
for px in nginx caddy apache2; do systemctl is-active --quiet "$px" 2>/dev/null && mark HOST_PROXY "$px"; done
true
""")

BEDOLAGA_STATUS = _script(r"""
docker ps -a --format 'table {{.Names}}\t{{.Status}}' | grep -iE 'NAMES|bot|bedolaga|cabinet|postgres|redis' || true
echo; curl -s --max-time 5 http://127.0.0.1:8080/health 2>/dev/null | head -c 400 || true
""")


def bedolaga_cmd(bot_dir: str, compose_file: str, action: str) -> str:
    f = q(compose_file)
    cmds = {
        "restart": f"docker compose -f {f} restart && docker compose -f {f} ps",
        "logs": f"docker compose -f {f} logs --tail 120 --no-color 2>&1 | tail -120",
        "up": f"docker compose -f {f} up -d && docker compose -f {f} ps",
        "env": "grep -vE '^\\s*(#|$)' .env | sed -E 's/^([A-Z0-9_]*(TOKEN|KEY|SECRET|PASSWORD)[A-Z0-9_]*=).+/\\1***/'",
    }
    return _script(f"cd {q(bot_dir)} || {{ echo 'нет каталога {bot_dir}'; exit 2; }}\n{cmds[action]}")


def cabinet_cmd(cab_ct: str, action: str) -> str:
    c = q(cab_ct)
    cmds = {
        "logs": f"docker logs --tail 80 {c} 2>&1",
        "restart": f"docker restart {c} && docker ps --filter name={c}",
        "update": f"wd=$(docker inspect -f '{{{{ index .Config.Labels \"com.docker.compose.project.working_dir\" }}}}' {c}); "
                  f"cd \"$wd\" && docker compose pull && docker compose up -d && docker ps --filter name={c}",
    }
    return _script(cmds[action])


# ===================================================================== Умный переезд бота
VM_REPO = "https://github.com/tneange1/vpn-master"
VM_DIR = "/opt/vpn-master"
MIGRATE_UPLOAD = "/tmp/vpn-master-migrate.tar.gz"


def migrate_prepare(bot_token: str, owner_id: int, branch: str = "main") -> str:
    """Новый сервер: Docker, код бота, .env, сборка образа, запуск только БД. Бот пока НЕ запускаем."""
    return _script(f"""
need_docker
pkg git openssl
D={VM_DIR}
if [ -d "$D/.git" ]; then
  log "Код уже есть — обновляю до {branch}"
  git -C "$D" fetch -q origin {q(branch)} && git -C "$D" reset -q --hard origin/{q(branch)}
else
  log "Клонирую {VM_REPO}"
  git clone -q -b {q(branch)} {VM_REPO} "$D"
fi
cd "$D"
PW=$(get_env .env POSTGRES_PASSWORD)   # повторная попытка — пароль БД тот же
[ -n "$PW" ] || PW=$(openssl rand -hex 16)
umask 077
printf 'BOT_TOKEN=%s\\nOWNER_ID=%s\\nPOSTGRES_PASSWORD=%s\\n' {q(bot_token)} {q(str(owner_id))} "$PW" > .env
mkdir -p data
docker compose stop bot updater >/dev/null 2>&1 || true
log "Собираю образ бота"
docker compose build bot
log "Запускаю PostgreSQL"
docker compose up -d db
for i in $(seq 1 60); do
  docker compose exec -T db pg_isready -U vpnmaster -d vpnmaster >/dev/null 2>&1 && break; sleep 2
done
docker compose exec -T db pg_isready -U vpnmaster -d vpnmaster
""")


MIGRATE_RESTORE = _script(f"""
cd {VM_DIR}
mv -f {MIGRATE_UPLOAD} data/migrate.tar.gz
rm -f data/migrated.flag data/shutdown.request
log "Восстанавливаю базу и ключ шифрования из бэкапа"
docker compose run --rm --no-deps -T bot python -m bot.restore_cli /app/data/migrate.tar.gz </dev/null
rm -f data/migrate.tar.gz
""")

MIGRATE_START = _script(f"""
cd {VM_DIR}
log "Запускаю бота и автообновление"
docker compose up -d
for i in $(seq 1 60); do
  if docker logs vpn-master 2>&1 | grep -q "Start polling"; then mark STARTED 1; log "Новый бот принимает обновления"; break; fi
  if ! docker ps --format '{{{{.Names}}}}' | grep -qx vpn-master; then echo "!! контейнер vpn-master не запущен"; break; fi
  sleep 3
done
docker compose ps
docker logs --tail 20 vpn-master 2>&1 | grep -v -i token || true
""")

MIGRATE_STOP = _script(f"cd {VM_DIR} && docker compose stop bot updater && docker compose ps")


def custom(cmd: str) -> str:
    return "#!/usr/bin/env bash\n" + cmd + "\n"
