"""Шаблоны профилей Xray для Remnawave: VLESS TCP Reality (self-steal) и Hysteria2."""
import base64
import copy
import secrets

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# self-steal Caddy на ноде слушает 127.0.0.1:9443, его сертификаты монтируются в ноду сюда:
SELFSTEAL_PORT = 9443
CERT_ROOT = "/var/lib/selfsteal/caddy/certificates/acme-v02.api.letsencrypt.org-directory"
HY2_PORT = 8443

KINDS = {
    "reality": "VLESS TCP Reality (self-steal)",
    "hy2": "Hysteria2",
    "both": "Reality + Hysteria2",
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


def reality_inbound(domain: str, tag: str = "VLESS_REALITY", port: int = 443) -> dict:
    priv, _pub = x25519()
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "xver": 0,
                "target": f"127.0.0.1:{SELFSTEAL_PORT}",
                "spiderX": "",
                "serverNames": [domain],
                "privateKey": priv,
                "shortIds": [secrets.token_hex(8)],
            },
        },
    }


def hysteria_inbound(domain: str, tag: str = "HYSTERIA2", port: int = HY2_PORT) -> dict:
    # Hysteria2 в Xray-core (протокол "hysteria", version 2). Сертификат берём у self-steal Caddy.
    # Схема у Xray ещё развивается — если панель ругается, поправь профиль под свою версию ядра.
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "hysteria",
        "settings": {"version": 2, "clients": []},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        "streamSettings": {
            "network": "hysteria",
            "hysteriaSettings": {"version": 2},
            "security": "tls",
            "tlsSettings": {
                "serverName": domain,
                "alpn": ["h3"],
                "certificates": [{
                    "certificateFile": f"{CERT_ROOT}/{domain}/{domain}.crt",
                    "keyFile": f"{CERT_ROOT}/{domain}/{domain}.key",
                }],
            },
        },
    }


def build_profile(kind: str, domain: str, prefix: str = "") -> dict:
    p = (prefix.upper().replace("-", "_") + "_") if prefix else ""
    inbounds = []
    if kind in ("reality", "both"):
        inbounds.append(reality_inbound(domain, tag=f"{p}VLESS_REALITY"))
    if kind in ("hy2", "both"):
        inbounds.append(hysteria_inbound(domain, tag=f"{p}HYSTERIA2"))
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "DIRECT", "protocol": "freedom"},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
        "routing": {
            "rules": [
                {"ip": ["geoip:private"], "outboundTag": "BLOCK"},
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
