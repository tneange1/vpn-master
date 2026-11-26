"""Reverse proxy: одно описание сайтов → Caddyfile или nginx.conf.

Секретный путь (как Caddy with custom path, docs.rw/security) — один ключ KEY:
  • браузер: https://панель/KEY — ставит cookie и пускает в панель;
  • API (Bedolaga, vpn-master и т.п.): заголовок X-Api-Key: KEY;
  • остальным соединение рвётся (Caddy: abort, Nginx: 444).
Link Extractor: домен подписки → экстрактор (подписки и API). Если le_on_panel — как задумано в самом
экстракторе: HTML панели идёт через него (он встраивает пункт «Экстрактор ссылок» в меню), а страница
/link-extractor/ открывается на домене панели (за тем же секретным путём). Иначе на домене панели его нет.
"""
import re
from dataclasses import dataclass, field
from shlex import quote as q

from . import crypto, db, scripts

KINDS = {"caddy": "Caddy (сертификаты сам)", "nginx": "Nginx + certbot"}
LE_UP = "link-extractor:3020"
COOKIE = "vm_auth"
KEY_RE = r"^[A-Za-z0-9_-]{8,128}$"


@dataclass
class Route:
    prefix: str  # "/api/" (префикс) или точный путь при exact=True
    upstream: str  # "host:port" (внутри docker-сети) или "https://host"
    strip: bool = False
    exact: bool = False


@dataclass
class Site:
    domain: str
    upstream: str
    routes: list = field(default_factory=list)
    cookie: str | None = None
    html_upstream: str | None = None  # Accept: text/html → сюда (экстрактор для меню панели)
    redirects: list = field(default_factory=list)  # [(prefix, url)] — 302


def _https(up: str) -> str | None:
    return up[len("https://"):] if up.startswith("https://") else None


# ------------------------------------------------------------------ Caddy
def _caddy_proxy(up: str, matcher: str = "") -> str:
    m = f" {matcher}" if matcher else ""
    host = _https(up)
    return f"reverse_proxy{m} {up} {{\n\t\t\theader_up Host {host}\n\t\t}}" if host else f"reverse_proxy{m} {up}"


def render_caddy(sites: list[Site]) -> str:
    out = []
    for s in sites:
        lines = []
        if s.cookie:
            c = s.cookie
            lines += [f"@vmlogin path /{c} /{c}/",
                      f'header @vmlogin Set-Cookie "{COOKIE}={c}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000"',
                      "redir @vmlogin / 302",
                      f"@vmdeny {{\n\t\t\tnot header Cookie *{COOKIE}={c}*\n\t\t\tnot header X-Api-Key {c}\n\t\t}}",
                      "abort @vmdeny"]
        for r in s.routes:
            m = r.prefix if r.exact else r.prefix.rstrip("/") + "/*"
            if r.strip:
                lines.append(f"route {m} {{\n\t\t\turi strip_prefix {r.prefix.rstrip('/')}\n\t\t\t"
                             f"{_caddy_proxy(r.upstream)}\n\t\t}}")
            elif _https(r.upstream):
                lines.append(f"route {m} {{\n\t\t\t{_caddy_proxy(r.upstream)}\n\t\t}}")
            else:
                lines.append(f"reverse_proxy {m} {r.upstream}")
        for prefix, url in s.redirects:
            lines.append(f"redir {prefix.rstrip('/')}* {url} 302")
        if s.html_upstream:
            lines += ["@vmhtml header Accept *text/html*", _caddy_proxy(s.html_upstream, "@vmhtml")]
        lines.append(_caddy_proxy(s.upstream))
        out.append(f"https://{s.domain} {{\n\tencode gzip zstd\n\troute {{\n" +
                   "".join(f"\t\t{x}\n" for x in lines) + "\t}\n}\n")
    return "\n".join(out)


# ------------------------------------------------------------------ Nginx
def _ident(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _nginx_location(path: str, up: str, strip: bool, auth: str, html_up: str | None = None,
                    exact: bool = False) -> str:
    host = _https(up)
    target = up if host else f"http://{up}"
    rewrite = f"        rewrite ^{re.escape(path)}(.*)$ /$1 break;\n" if strip else ""
    html = f'        if ($http_accept ~* "text/html") {{ set $vm_up http://{html_up}; }}\n' if html_up else ""
    host_hdr = host if host else "$host"
    ssl = "        proxy_ssl_server_name on;\n" if host else ""
    return (f"    location {'= ' if exact else ''}{path} {{\n{auth}"
            f"        set $vm_up {target};\n{html}{rewrite}"
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
            cvar = f"$vm_c_{_ident(cert_name)}_{i}"
            # cookie ИЛИ заголовок X-Api-Key (так ходят Bedolaga и API-клиенты)
            head = (f'map $http_cookie {cvar} {{ default 0; "~(^|;\\s*){COOKIE}={c}(;|$)" 1; }}\n'
                    f'map $http_x_api_key {var} {{ default {cvar}; "{c}" 1; }}\n')
            auth = f"        if ({var} = 0) {{ return 444; }}\n"
        locs = ""
        if s.cookie:
            login = (f'        add_header Set-Cookie "{COOKIE}={s.cookie}; Path=/; HttpOnly; Secure; SameSite=Lax; '
                     'Max-Age=2592000" always;\n        return 302 /;\n')
            locs += f"    location = /{s.cookie} {{\n{login}    }}\n    location = /{s.cookie}/ {{\n{login}    }}\n"
        locs += "".join(_nginx_location(r.prefix, r.upstream, r.strip, auth, exact=r.exact) for r in s.routes)
        for prefix, url in s.redirects:
            locs += f"    location {prefix.rstrip('/')} {{\n{auth}        return 302 {url};\n    }}\n"
        locs += _nginx_location("/", s.upstream, False, auth, html_up=s.html_upstream)
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
def sname(kind: str, obj_id) -> str:
    """Имя файла сайта: panel5, bedolaga2, cabinet2 (раньше были panel/bedolaga/cabinet без номера)."""
    return f"{kind}{obj_id}"


def _legacy(name: str) -> str:
    m = re.match(r"^(panel|bedolaga|cabinet)\d+$", name)
    return f"vm_rm_site {m.group(1)}\n" if m else ""


def apply_fragment(kind: str, name: str, sites: list[Site], email: str | None = None) -> str:
    """bash-фрагмент (без PRELUDE): применить сайт name на сервере."""
    pre = _legacy(name)
    if kind == "nginx":
        domains = " ".join(s.domain for s in sites)
        mail = f"VM_EMAIL={q(email)} " if email else ""
        return pre + f"{mail}nginx_site {q(name)} {q(domains)} <<'VMSITE'\n{render_nginx(name, sites).rstrip()}\nVMSITE\n"
    return pre + f"caddy_site {q(name)} <<'VMSITE'\n{render_caddy(sites).rstrip()}\nVMSITE\n"


def apply_script(kind: str, name: str, sites: list[Site], email: str | None = None) -> str:
    return scripts.PRELUDE + "\n" + apply_fragment(kind, name, sites, email)


def rebuild_fragment(kind: str, groups: list[tuple[str, list[Site]]], email: str | None = None) -> str:
    """Пересобрать ВСЕ сайты прокси сервера разом: бэкап → запись → проверка → reload, при ошибке — возврат."""
    if kind == "nginx":
        d, ext, keep = scripts.NGINX_DIR + "/conf.d", "conf", "00-default.conf"
        ensure = 'if ! (nginx_ours && running nginx); then nginx_setup || exit $?; fi'
        certs = "".join(f"nginx_cert {q(n)} {q(' '.join(x.domain for x in ss))} || exit 4\n" for n, ss in groups)
        if email:
            certs = f"export VM_EMAIL={q(email)}\n" + certs
        check, reload_ = "docker exec nginx nginx -t", "docker exec nginx nginx -s reload"
        render = lambda n, ss: render_nginx(n, ss)  # noqa: E731
        net = "proxy_networks nginx"
    else:
        d, ext, keep = scripts.CADDY_DIR + "/sites", "caddy", "_default.caddy"
        ensure = ('if running caddy && ! caddy_ours; then echo "!! Работает чужой Caddy"; exit 3; fi\n'
                  'if ! (caddy_ours && running caddy); then caddy_setup || exit $?; fi')
        certs = ""
        check = "docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
        reload_ = "docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile"
        render = lambda n, ss: render_caddy(ss)  # noqa: E731
        net = "caddy_networks"
    files = "".join(f"cat > {d}/{n}.{ext} <<'VMSITE'\n{render(n, ss).rstrip()}\nVMSITE\n" for n, ss in groups)
    return f"""
{ensure}
{certs}BK=$(mktemp -d)
cp -a {d}/. "$BK/"
find {d} -maxdepth 1 -type f -name '*.{ext}' ! -name '{keep}' -delete
{files}{net}
if {check}; then
  {reload_}
  log "Прокси пересобран: {", ".join(n for n, _ in groups) or "—"}"
  rm -rf "$BK"
else
  echo "!! Новый конфиг не прошёл проверку — возвращаю прежний"
  find {d} -maxdepth 1 -type f -name '*.{ext}' -delete; cp -a "$BK/." {d}/; rm -rf "$BK"
  {reload_} || true
  exit 1
fi
"""


def rebuild_script(kind: str, groups: list[tuple[str, list[Site]]], email: str | None = None) -> str:
    return scripts.PRELUDE + rebuild_fragment(kind, groups, email)


# ------------------------------------------------------------------ готовые наборы сайтов
def panel_sites(p: db.Panel, cookie: str | None = None, link_extractor: bool | None = None,
                ct: dict | None = None) -> list[Site]:
    """Панель (+секретный путь) и домен подписки (через Link Extractor, если он стоит)."""
    ct = ct or {}
    le = p.link_extractor if link_extractor is None else link_extractor
    site = Site(p.domain, f"{ct.get('panel', 'remnawave')}:3000", cookie=cookie)
    if le and p.le_on_panel is not False:
        le_up = f"{ct.get('le', 'link-extractor')}:3020"
        site.html_upstream = le_up  # экстрактор встраивает пункт меню в панель
        site.routes = [Route("/link-extractor", le_up, exact=True), Route("/link-extractor/", le_up)]
    sites = [site]
    if p.sub_domain and p.sub_local:
        up = f"{ct.get('le', 'link-extractor')}:3020" if le else f"{ct.get('subpage', 'remnawave-subscription-page')}:3010"
        sites.append(Site(p.sub_domain, up))
    return sites


def panel_cookie(p: db.Panel) -> str | None:
    """Ключ секретного пути (X-Api-Key) или None."""
    return crypto.dec(p.cookie_enc) if p.protected else None


def login_url(domain: str, key: str | None) -> str:
    return f"https://{domain}/{key}" if key else f"https://{domain}"


def bedolaga_sites(hooks_domain: str, bot_ct: str = "remnawave_bot") -> list[Site]:
    return [Site(hooks_domain, f"{bot_ct}:8080")]


def cabinet_sites(cabinet_domain: str, api_upstream: str, front_ct: str = "cabinet_frontend") -> list[Site]:
    return [Site(cabinet_domain, f"{front_ct}:80", [Route("/api/", api_upstream, strip=True)])]


async def containers(server: db.Server) -> dict:
    """Имена контейнеров на сервере (из последнего обнаружения): panel, subpage, le, bot, cabinet."""
    import json
    raw = await db.setting(f"ct:{server.id}")
    return json.loads(raw) if raw else {}


async def server_groups(server: db.Server, cookie_override: dict | None = None) -> list[tuple[str, list[Site]]]:
    """Все сайты, которые прокси этого сервера должен обслуживать: панели, боты Bedolaga, кабинеты."""
    ct = await containers(server)
    bot_ct = ct.get("bot", "remnawave_bot")
    groups = []
    for p in await db.all_(db.Panel, db.Panel.server_id == server.id):
        cookie = (cookie_override or {}).get(p.id, panel_cookie(p))
        groups.append((sname("panel", p.id), panel_sites(p, cookie, ct=ct)))
    for b in await db.all_(db.Bedolaga):
        if b.server_id == server.id and b.hooks_domain:
            groups.append((sname("bedolaga", b.id), bedolaga_sites(b.hooks_domain, bot_ct)))
        if b.cabinet and b.cabinet_domain:
            cab_srv = int(await db.setting(f"cabinet_srv:{b.id}") or b.server_id or 0)
            if cab_srv == server.id:
                api = f"{bot_ct}:8080" if cab_srv == b.server_id else f"https://{b.hooks_domain}"
                groups.append((sname("cabinet", b.id),
                               cabinet_sites(b.cabinet_domain, api, ct.get("cabinet", "cabinet_frontend"))))
    return groups


# ------------------------------------------------------------------ подключение существующей панели
def takeover_script(kind: str, groups: list[tuple[str, list[Site]]],
                    check_domain: str, cookie: str | None, email: str | None = None) -> str:
    """Бэкапит и останавливает текущий прокси (80/443), ставит наш со всеми сайтами сервера,
    проверяет панель, при ошибке — откат."""
    applies = "(\n" + rebuild_fragment(kind, groups, email) + "\n)"
    cookie_hdr = f'-H "X-Api-Key: {cookie}"' if cookie else ""
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


def bedolaga_auth_env(cookie: str | None) -> dict:
    """Bedolaga за секретным путём: AUTH_TYPE=caddy →
    Authorization: Bearer <API-ключ панели>, X-Api-Key: <ключ секретного пути>."""
    if cookie:
        return {"REMNAWAVE_AUTH_TYPE": "caddy", "REMNAWAVE_CADDY_TOKEN": cookie, "REMNAWAVE_SECRET_KEY": ""}
    return {"REMNAWAVE_AUTH_TYPE": "api_key", "REMNAWAVE_CADDY_TOKEN": "", "REMNAWAVE_SECRET_KEY": ""}


async def sync_bedolaga_token(bot, chat_id: int, panel: db.Panel) -> None:
    """Всем ботам Bedolaga этой панели — актуальные настройки доступа через секретный путь."""
    from .runner import run_task
    env = bedolaga_auth_env(panel_cookie(panel))
    for b in await db.all_(db.Bedolaga, db.Bedolaga.panel_id == panel.id):
        srv = await db.get(db.Server, b.server_id)
        if srv:
            await run_task(bot, chat_id, srv, f"Bedolaga {b.name}: доступ к панели", scripts.bedolaga_env(b.compose_file, env, b.install_dir))
