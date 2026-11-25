"""Шаблоны профилей Xray для Remnawave (по мануалу self-steal):

- Hysteria 2 на UDP 443 (BBR через finalmask.quicParams)
- VLESS + REALITY self-steal на TCP 443, network raw (Vision: flow xtls-rprx-vision ставит панель)
- Nginx self-steal на 127.0.0.1:8443
- Один сертификат certbot с --cert-name key → /etc/letsencrypt/live/key/ (все домены ноды в SAN)
"""
import base64
import copy
import secrets

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

SELFSTEAL_TARGET = "127.0.0.1:8443"
CERT_FILE = "/etc/letsencrypt/live/key/fullchain.pem"
KEY_FILE = "/etc/letsencrypt/live/key/privkey.pem"

KINDS = {
    "both": "Reality + Hysteria2 (оба на 443)",
    "reality": "VLESS Reality self-steal (TCP 443)",
    "hy2": "Hysteria2 (UDP 443)",
}

WARP_DOMAINS = [
    "domain:openai.com", "domain:chatgpt.com", "domain:oaistatic.com", "domain:oaiusercontent.com",
    "domain:claude.ai", "domain:anthropic.com", "domain:gemini.google.com",
    "domain:aistudio.google.com", "domain:netflix.com", "domain:spotify.com",
]


def x25519() -> tuple[str, str]:
    k = X25519PrivateKey.generate()
    priv = k.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    pub = k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)

    def b64(x: bytes) -> str:
        return base64.urlsafe_b64encode(x).decode().rstrip("=")

    return b64(priv), b64(pub)


def reality_inbound(domains: list[str], tag: str = "VLESS-SELFSTEAL", port: int = 443) -> dict:
    priv, _pub = x25519()
    return {
        "tag": tag,
        "port": port,
        "listen": "0.0.0.0",
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "sniffing": {"enabled": True, "routeOnly": True, "destOverride": ["http", "tls", "quic"]},
        "streamSettings": {
            "network": "raw",
            "security": "reality",
            "realitySettings": {
                "target": SELFSTEAL_TARGET,
                "privateKey": priv,
                "shortIds": [secrets.token_hex(8)],
                "serverNames": list(domains),
            },
        },
    }


def hysteria_inbound(tag: str = "HYSTERIA-BBR", port: int = 443, bbr: bool = True) -> dict:
    stream = {
        "network": "hysteria",
        "security": "tls",
        "tlsSettings": {
            "alpn": ["h3"],
            "certificates": [{"keyFile": KEY_FILE, "certificateFile": CERT_FILE}],
        },
        "hysteriaSettings": {"version": 2},
    }
    if bbr:  # finalmask зависит от версии ноды
        stream["finalmask"] = {"quicParams": {"debug": False, "congestion": "bbr"}}
    return {
        "tag": tag,
        "port": port,
        "listen": "0.0.0.0",
        "protocol": "hysteria",
        "settings": {"clients": [], "version": 2},
        "streamSettings": stream,
    }


def build_profile(kind: str, domains: list[str] | str, prefix: str = "", bbr: bool = True) -> dict:
    if isinstance(domains, str):
        domains = [d for d in domains.split(",") if d]
    p = (prefix.upper().replace(" ", "-") + "-") if prefix else ""
    inbounds = []
    if kind in ("hy2", "both"):
        inbounds.append(hysteria_inbound(tag=f"{p}HYSTERIA-BBR", bbr=bbr))
    if kind in ("reality", "both"):
        inbounds.append(reality_inbound(domains, tag=f"{p}VLESS-SELFSTEAL"))
    return {
        "log": {"loglevel": "none"},
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "DIRECT", "protocol": "freedom"},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
        "routing": {
            "rules": [
                {"ip": ["geoip:private"], "outboundTag": "BLOCK"},
                {"domain": ["geosite:private"], "outboundTag": "BLOCK"},
                {"protocol": ["bittorrent"], "outboundTag": "BLOCK"},
            ]
        },
    }


WARP_OUT = {
    "tag": "warp-out",
    "protocol": "freedom",
    "settings": {"domainStrategy": "ForceIPv4"},
    "streamSettings": {"sockopt": {"interface": "warp", "mark": 51888, "tcpFastOpen": True}},
}


def add_warp(config: dict, domains: list[str] | None = None, everything: bool = False) -> dict:
    """Добавляет warp-out (distillium/warp-native) и правило маршрутизации."""
    cfg = copy.deepcopy(config)
    outs = cfg.setdefault("outbounds", [])
    if not any(o.get("tag") == "warp-out" for o in outs):
        outs.append(copy.deepcopy(WARP_OUT))
    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    rules[:] = [r for r in rules if r.get("outboundTag") != "warp-out"]
    if everything:
        rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "warp-out"})
    else:
        rules.append({"type": "field", "domain": domains or WARP_DOMAINS, "outboundTag": "warp-out"})
    return cfg


def remove_warp(config: dict) -> dict:
    cfg = copy.deepcopy(config)
    cfg["outbounds"] = [o for o in cfg.get("outbounds", []) if o.get("tag") != "warp-out"]
    rules = cfg.get("routing", {}).get("rules", [])
    cfg.setdefault("routing", {})["rules"] = [r for r in rules if r.get("outboundTag") != "warp-out"]
    return cfg
