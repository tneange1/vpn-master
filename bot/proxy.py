"""Reverse proxy: одно описание сайтов → Caddyfile или nginx.conf.

Секретный путь (docs.rw/install/panel-security): по ссылке https://домен/?vmkey=TOKEN ставится cookie,
без cookie соединение рвётся (Caddy: abort, Nginx: 444). Работает одинаково на обоих прокси.
"""
import re
from dataclasses import dataclass, field
from shlex import quote as q

from . import crypto, db, scripts

KINDS = {"caddy": "Caddy (сертификаты сам)", "nginx": "Nginx + certbot"}


@dataclass
class Route:
    prefix: str  # "/api/" или "/link-extractor/"
    upstream: str  # "host:port" (внутри docker-сети) или "https://host"
    strip: bool = False


@dataclass
class Site:
    domain: str
    upstream: str
    routes: list = field(default_factory=list)
    cookie: str | None = None


def _https(up: str) -> str | None:
    return up[len("https://"):] if up.startswith("https://") else None


# ------------------------------------------------------------------ Caddy
def _caddy_proxy(up: str) -> str:
    host = _https(up)
    return f"reverse_proxy {up} {{\n\t\t\theader_up Host {host}\n\t\t}}" if host else f"reverse_proxy {up}"


def render_caddy(sites: list[Site]) -> str:
    out = []
    for s in sites:
        lines = []
        if s.cookie:
            c = s.cookie
            lines += [f"@vmset query vmkey={c}",
                      f'header @vmset Set-Cookie "vmkey={c}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=2592000"',
                      "redir @vmset / 302",
                      f"@vmdeny not header Cookie *vmkey={c}*",
                      "abort @vmdeny"]
        for r in s.routes:
            m = r.prefix.rstrip("/") + "/*"
            if r.strip:
                lines.append(f"route {m} {{\n\t\t\turi strip_prefix {r.prefix.rstrip('/')}\n\t\t\t"
                             f"{_caddy_proxy(r.upstream)}\n\t\t}}")
            elif _https(r.upstream):
                lines.append(f"route {m} {{\n\t\t\t{_caddy_proxy(r.upstream)}\n\t\t}}")
            else:
                lines.append(f"reverse_proxy {m} {r.upstream}")
        lines.append(_caddy_proxy(s.upstream))
        out.append(f"https://{s.domain} {{\n\tencode gzip zstd\n\troute {{\n" +
                   "".join(f"\t\t{x}\n" for x in lines) + "\t}\n}\n")
    return "\n".join(out)


# ------------------------------------------------------------------ Nginx
def _ident(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _nginx_location(path: str, up: str, strip: bool, auth: str) -> str:
    host = _https(up)
    target = up if host else f"http://{up}"
    rewrite = f"        rewrite ^{re.escape(path)}(.*)$ /$1 break;\n" if strip else ""
    host_hdr = host if host else "$host"
    ssl = "        proxy_ssl_server_name on;\n" if host else ""
    return (f"    location {path} {{\n{auth}"
            f"        set $vm_up {target};\n{rewrite}"
            f"        proxy_pass $vm_up;\n{ssl}"
            "        proxy_http_version 1.1;\n"
            f"        proxy_set_header Host {host_hdr};\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection $vm_conn;\n"
            "    }\n")


def render_nginx(cert_name: str, sites: list[Site]) -> str:
    out = []
    for i, s in enumerate(sites):
        var = f"$vm_ok_{_ident(cert_name)}_{i}"
        head, auth = "", ""
        if s.cookie:
            c = s.cookie
            head = f'map $http_cookie {var} {{ default 0; "~(^|;\\s*)vmkey={c}(;|$)" 1; }}\n'
            auth = (f'        if ($arg_vmkey = "{c}") {{\n'
                    f'            add_header Set-Cookie "vmkey={c}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=2592000" always;\n'
                    "            return 302 /;\n        }\n"
                    f"        if ({var} = 0) {{ return 444; }}\n")
        locs = "".join(_nginx_location(r.prefix, r.upstream, r.strip, auth) for r in s.routes)
        locs += _nginx_location("/", s.upstream, False, auth)
        out.append(head + "server {\n"
                    "    listen 443 ssl;\n    http2 on;\n"
                    f"    server_name {s.domain};\n"
                    f"    ssl_certificate /etc/letsencrypt/live/{cert_name}/fullchain.pem;\n"
                    f"    ssl_certificate_key /etc/letsencrypt/live/{cert_name}/privkey.pem;\n"
                    "    ssl_protocols TLSv1.2 TLSv1.3;\n"
                    "    server_tokens off;\n"
                    "    client_max_body_size 50m;\n"
                    "    resolver 127.0.0.11 valid=30s ipv6=off;\n"
                    f"{locs}}}\n")
    return "\n".join(out)


# ------------------------------------------------------------------ применение
def apply_fragment(kind: str, name: str, sites: list[Site], email: str | None = None) -> str:
    """bash-фрагмент (без PRELUDE): применить сайт name на сервере."""
    if kind == "nginx":
        domains = " ".join(s.domain for s in sites)
        mail = f"VM_EMAIL={q(email)} " if email else ""
        return f"{mail}nginx_site {q(name)} {q(domains)} <<'VMSITE'\n{render_nginx(name, sites).rstrip()}\nVMSITE\n"
    return f"caddy_site {q(name)} <<'VMSITE'\n{render_caddy(sites).rstrip()}\nVMSITE\n"


def apply_script(kind: str, name: str, sites: list[Site], email: str | None = None) -> str:
    return scripts.PRELUDE + "\n" + apply_fragment(kind, name, sites, email)


# ------------------------------------------------------------------ готовые наборы сайтов
def panel_sites(p: db.Panel, cookie: str | None = None, link_extractor: bool | None = None) -> list[Site]:
    le = p.link_extractor if link_extractor is None else link_extractor
    routes = [Route("/link-extractor/", "link-extractor:3020")] if le else []
    sites = [Site(p.domain, "remnawave:3000", routes, cookie)]
    if p.sub_domain and p.sub_local:
        sites.append(Site(p.sub_domain, "link-extractor:3020" if le else "remnawave-subscription-page:3010"))
    return sites


def panel_cookie(p: db.Panel) -> str | None:
    return crypto.dec(p.cookie_enc) if p.protected else None


def bedolaga_sites(hooks_domain: str) -> list[Site]:
    return [Site(hooks_domain, "remnawave_bot:8080")]


def cabinet_sites(cabinet_domain: str, api_upstream: str) -> list[Site]:
    return [Site(cabinet_domain, "cabinet_frontend:80", [Route("/api/", api_upstream, strip=True)])]


# ------------------------------------------------------------------ подключение существующей панели
def takeover_script(kind: str, panel_sites_: list[Site], extra: list[tuple[str, list[Site]]],
                    check_domain: str, cookie: str | None, email: str | None = None) -> str:
    """Бэкапит и останавливает текущий прокси (80/443), ставит наш, проверяет панель, при ошибке — откат."""
    applies = apply_fragment(kind, "panel", panel_sites_, email) + "".join(
        apply_fragment(kind, n, s, email) for n, s in extra)
    cookie_hdr = f'-H "Cookie: vmkey={cookie}"' if cookie else ""
    return scripts.PRELUDE + f"""
need_docker
: > /tmp/vm_moved
BK=/opt/vpn-master-backup/proxy-$(date +%Y%m%d-%H%M%S)
mkdir -p "$BK"
log "Бэкап текущего прокси → $BK"
OURS="$(docker ps --format '{{{{.Names}}}}' | grep -xE 'caddy|nginx' || true)"
for c in $(docker ps --format '{{{{.Names}}}}|{{{{.Ports}}}}' | grep -E ':(80|443)->' | cut -d'|' -f1); do
  wd=$(docker inspect -f '{{{{ index .Config.Labels "com.docker.compose.project.working_dir" }}}}' "$c" 2>/dev/null || true)
  if [ -n "$wd" ] && [ -f "$wd/.vpn-master" ]; then continue; fi
  echo "$c|$wd" >> "$BK/containers"
  [ -n "$wd" ] && [ -d "$wd" ] && cp -a "$wd" "$BK/$(basename "$wd")-$c"
  log "Останавливаю $c ($wd)"
  docker stop "$c" >/dev/null
done
for s in nginx caddy apache2 angie haproxy; do
  if systemctl is-active --quiet "$s" 2>/dev/null; then
    echo "$s" >> "$BK/services"; [ -d "/etc/$s" ] && cp -a "/etc/$s" "$BK/etc-$s"
    log "Останавливаю сервис $s"; systemctl stop "$s"
  fi
done

vm_rollback(){{
  log "ОТКАТ: возвращаю прежний прокси"
  for d in {scripts.CADDY_DIR} {scripts.NGINX_DIR}; do
    if [ -f "$d/.vpn-master" ] && ! echo "$OURS" | grep -qx "$(basename "$d")"; then (cd "$d" && docker compose down) || true; fi
  done
  while IFS='|' read -r orig bak; do [ -n "$bak" ] && rm -rf "$orig" && mv "$bak" "$orig"; done < /tmp/vm_moved
  [ -f "$BK/containers" ] && while IFS='|' read -r c wd; do docker start "$c" >/dev/null 2>&1 || true; done < "$BK/containers"
  [ -f "$BK/services" ] && while read -r s; do systemctl start "$s" || true; done < "$BK/services"
  log "Откат завершён, бэкап: $BK"
}}

vm_apply(){{
{applies}
}}
vm_health(){{
  for i in $(seq 1 18); do
    code=$(curl -sk -o /dev/null -w '%{{http_code}}' --max-time 8 --resolve {q(check_domain)}:443:127.0.0.1 {cookie_hdr} https://{check_domain}/ || true)
    case "$code" in 2*|3*|401|404) log "Панель через прокси отвечает: HTTP $code"; return 0;; esac
    sleep 5
  done
  echo "!! Панель через новый прокси не отвечает (последний код: $code)"; return 1
}}

if vm_apply && vm_health; then
  log "Готово. Бэкап старого прокси: $BK"
  mark BACKUP "$BK"
else
  vm_rollback; exit 1
fi
"""


def kind_of(server: db.Server | None) -> str:
    return server.proxy if server and server.proxy else "caddy"
