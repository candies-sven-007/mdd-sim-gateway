#!/usr/bin/env python3
"""Host-side country egress orchestrator.

The manager writes ``data/orchestrator/desired.json``.  This process owns the host network
namespace, the sing-box (and optional Xray) process and the ePDG host routes. SIM readers are
plain PC/SC devices served by pcscd; this process does not touch them.
"""
from __future__ import annotations

import argparse
import base64
import collections
from copy import deepcopy
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - installer provides PyYAML
    yaml = None


def load_yaml_text(text: str) -> dict:
    """Parse with libyaml when PyYAML has it: the same safe schema, far less CPU. The country
    egress re-reads a subscription of hundreds of nodes every few seconds."""
    loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
    return yaml.load(text, Loader=loader) or {}

MANAGED_ROUTE_PROTO = "186"
CLASH_API = os.environ.get("MDD_CLASH_API", "127.0.0.1:19090")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict, limit: int = 500):
    """Append one bounded diagnostic record, trimming to the newest ``limit`` lines.

    Rewriting the file keeps it from growing without bound on a Pi's SD card. These records
    exist to correlate a line's failure window with exit-node changes, so only the recent
    tail has any value.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lines = []
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append(json.dumps(record, sort_keys=True))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        # Diagnostics must never take the orchestrator's reconcile loop down.
        pass


def run(args, *, check=False, capture=True, timeout=None):
    return subprocess.run(args, check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,
                          **({"timeout": timeout} if timeout is not None else {}))


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", value).strip("-") or "modem"


def tun_name(country: str) -> str:
    return f"mdd-{country.lower()}"[:15]


def parse_proxy_url(url: str, tag: str) -> dict:
    from urllib.parse import unquote, urlsplit
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("socks", "socks5") or not parsed.hostname:
        raise ValueError("VoWiFi requires UDP; manual proxy must be a UDP-capable socks5:// URL")
    out = {"type": "socks", "tag": tag, "version": "5",
           "server": parsed.hostname, "server_port": parsed.port or 1080}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


def b64_padded(value: str) -> str:
    """Decode base64 that share links emit without padding, in either alphabet."""
    text = value.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="replace")


# Transports this converter can actually render into a working outbound. A link naming
# anything else (grpc, h2, httpupgrade, quic, splithttp) must be refused rather than
# silently downgraded to plain TCP.
SUPPORTED_LINK_TRANSPORTS = {"", "tcp", "ws", "xhttp"}


def parse_share_link(url: str) -> dict:
    """Convert one protocol share link into the Clash-style node dict clash_outbound() takes.

    Reusing that converter — rather than emitting sing-box outbounds directly — keeps pasted
    nodes on exactly the same code path as subscription nodes, including the UDP admission
    rules that decide whether a node may carry IKE at all.
    """
    from urllib.parse import parse_qs, unquote, urlsplit
    text = url.strip()
    scheme = text.split("://", 1)[0].lower() if "://" in text else ""

    if scheme == "vmess":
        # vmess links are a base64 JSON blob rather than a URL.
        payload = json.loads(b64_padded(text.split("://", 1)[1].split("#", 1)[0]))
        node = {"type": "vmess", "server": payload.get("add"), "port": payload.get("port"),
                "uuid": payload.get("id"), "alterId": payload.get("aid") or 0,
                "cipher": payload.get("scy") or "auto",
                "tls": str(payload.get("tls") or "").lower() in ("tls", "true"),
                "servername": payload.get("sni") or payload.get("host") or "",
                "network": str(payload.get("net") or "tcp")}
        if node["network"] == "ws":
            host = payload.get("host")
            node["ws-opts"] = {"path": payload.get("path") or "/",
                               "headers": {"Host": host} if host else {}}
        return node

    if scheme == "ss":
        # Either ss://base64(method:password)@host:port or ss://base64(method:password@host:port)
        body = text.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0]
        if "@" in body:
            credentials, _, hostport = body.rpartition("@")
            credentials = b64_padded(credentials)
        else:
            credentials, _, hostport = b64_padded(body).rpartition("@")
        method, _, password = credentials.partition(":")
        host, _, port = hostport.rpartition(":")
        return {"type": "ss", "server": host, "port": port,
                "cipher": method, "password": password}

    parsed = urlsplit(text)
    if not parsed.hostname or not parsed.port:
        raise ValueError("node link is missing a host or port")
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    # Hysteria2 carries its whole auth string in userinfo, and that string is allowed to
    # contain a colon ("user:pass"). Reading only .username silently truncated it, so the
    # server rejected an authentication the operator had pasted correctly.
    userinfo = unquote(parsed.username or "")
    if parsed.password is not None:
        userinfo = userinfo + ":" + unquote(parsed.password)
    node = {"server": parsed.hostname, "port": parsed.port,
            "network": query.get("type") or query.get("network") or "tcp",
            "servername": query.get("sni") or query.get("peer") or query.get("host") or "",
            "skip-cert-verify": str(query.get("allowInsecure")
                                    or query.get("insecure") or "").lower() in ("1", "true")}
    if scheme == "vless":
        node.update({"type": "vless", "uuid": userinfo, "flow": query.get("flow") or "",
                     # VLESS Encryption (Xray 26.7+): the server declares a `decryption` and
                     # the client must echo the matching `encryption`. Dropping it produced a
                     # client that connected and could not be understood — every request
                     # timed out with nothing logged.
                     "encryption": unquote(query.get("encryption") or "") or "none",
                     "tls": str(query.get("security") or "").lower()
                     in ("tls", "reality", "xtls")})
        if str(query.get("security") or "").lower() == "reality":
            node["reality-opts"] = {"public-key": query.get("pbk") or query.get("publicKey") or "",
                                    "short-id": query.get("sid") or query.get("shortId") or "",
                                    "spider-x": unquote(query.get("spx") or "")}
            node["client-fingerprint"] = query.get("fp") or "chrome"
        if node["network"] == "xhttp":
            try:
                extra = json.loads(unquote(query.get("extra") or "{}"))
            except (TypeError, ValueError):
                extra = {}
            node["xhttp-opts"] = {"host": query.get("host") or "",
                                  "path": unquote(query.get("path") or "/"),
                                  "mode": query.get("mode") or "auto",
                                  "extra": extra if isinstance(extra, dict) else {}}
            node["packet-encoding"] = query.get("packetEncoding") or "xudp"
    elif scheme == "trojan":
        node.update({"type": "trojan", "password": userinfo})
    elif scheme in ("hysteria2", "hy2"):
        # QUIC-based, so it has no stream transport to describe.
        node.update({"type": "hysteria2", "password": userinfo, "network": "tcp"})
        # A server expecting salamander discards every unobfuscated packet without a word,
        # so a link whose obfs parameters are dropped here produces an exit that looks
        # configured and never carries a byte.
        if query.get("obfs"):
            node["obfs"] = query["obfs"]
            node["obfs-password"] = unquote(
                query.get("obfs-password") or query.get("obfs_password")
                or query.get("obfsParam") or "")
    else:
        raise ValueError(f"unsupported node link scheme {scheme or text[:12]!r}")
    # alpn belongs to the TLS layer every one of these protocols shares; it used to be read
    # for VLESS only, which quietly dropped the h3 an hysteria2 node may require.
    if query.get("alpn"):
        node["alpn"] = [x for x in unquote(query["alpn"]).split(",") if x]
    if node["network"] == "ws":
        host = query.get("host")
        node["ws-opts"] = {"path": unquote(query.get("path") or "/"),
                           "headers": {"Host": host} if host else {}}
    elif node["network"] not in SUPPORTED_LINK_TRANSPORTS:
        # Anything else was previously dropped on the floor: the outbound came out as a
        # plain TCP one, passed every check, and then never completed a handshake. Name the
        # transport instead, so the operator knows the gateway cannot carry this node.
        raise ValueError(
            f"node transport {node['network']!r} is not supported for VoWiFi exits "
            "(supported: tcp, ws, xhttp)")
    return node


def parse_manual_outbound(value, tag: str) -> dict:
    """Build one exit outbound from whatever an operator pasted.

    Three accepted forms: a socks5:// URL, a protocol share link (the format nodes are
    normally handed out in), or a raw sing-box outbound object. All three are checked for UDP
    capability here, because a TCP-only exit otherwise stays invisible until IKE times out.
    """
    if isinstance(value, dict):
        outbound = dict(value)
        outbound["tag"] = tag
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("no node link or proxy URL provided")
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"node configuration is not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("node configuration must be a single outbound object")
            outbound = {**parsed, "tag": tag}
        elif text.split("://", 1)[0].lower() in ("socks", "socks5"):
            outbound = parse_proxy_url(text, tag)
        else:
            outbound = clash_outbound(parse_share_link(text), tag)
    if not outbound_supports_udp(outbound):
        raise ValueError(f"node type {outbound.get('type') or 'unknown'!r} cannot carry the "
                         "UDP traffic VoWiFi IKE requires")
    return outbound


UDP_PROXY_TYPES = {"ss", "shadowsocks", "trojan", "vless", "vmess", "hysteria2", "hy2"}
# Carrier ePDG names rotate their A record every ~30 seconds. Routing only the newest answer
# pulls the route out from under a tunnel that is still talking to the previous address, which
# drops that traffic onto the host default route — the wrong country, and the carrier drops the
# IKE SA. Addresses are therefore retained well beyond one DNS answer.
EPDG_ADDRESS_TTL = float(os.environ.get("MDD_EPDG_ADDRESS_TTL", "21600"))
EPDG_ADDRESS_MAX = int(os.environ.get("MDD_EPDG_ADDRESS_MAX", "64"))
# How long a node that just failed a line is kept out of the candidate pool.
EXIT_RESELECT_COOLDOWN = float(os.environ.get("MDD_EXIT_RESELECT_COOLDOWN", "900"))
EXIT_RESELECT_MAX_AGE = float(os.environ.get("MDD_EXIT_RESELECT_MAX_AGE", "600"))
EXIT_RESELECT_RETRY_SECONDS = float(os.environ.get("MDD_EXIT_RESELECT_RETRY", "60"))
EXIT_RESELECT_MAX_ATTEMPTS = int(os.environ.get("MDD_EXIT_RESELECT_ATTEMPTS", "3"))
EXIT_PROBE_URL = os.environ.get("MDD_EXIT_PROBE_URL", "https://www.gstatic.com/generate_204")
EXIT_PROBE_TIMEOUT_MS = int(os.environ.get("MDD_EXIT_PROBE_TIMEOUT_MS", "3000"))
# Probes of a pinned node before giving up on it, so one spike cannot void the preference.
EXIT_PREFERRED_PROBE_ATTEMPTS = int(os.environ.get("MDD_EXIT_PREFERRED_PROBE_ATTEMPTS", "2"))
# Ranking a sing-box that has only just started measures its cold start rather than the nodes:
# a QUIC outbound must complete a fresh handshake and loses to TCP candidates that are slower
# in steady state. Wait for the process to settle before believing any measurement.
EXIT_RANK_WARMUP_SECONDS = float(os.environ.get("MDD_EXIT_RANK_WARMUP", "25"))
# A full reconcile shells out to ip and systemctl several times. At the base interval that
# was the largest source of process creation on the box, and almost all of it re-derived a
# state that had not changed. When a cycle finds nothing to do the loop backs off, while still
# waking on the base interval to stat the input documents so an operator action is never
# delayed by more than one base tick.
# sing-tun makes every tun it creates the host's catch-all resolver whenever resolvectl is
# present (see release_tun_dns). It does so once, shortly after start; this bounds how long
# the orchestrator keeps looking for that registration after sing-box (re)starts.
TUN_DNS_WATCH_SECONDS = float(os.environ.get("MDD_TUN_DNS_WATCH", "60"))
IDLE_INTERVAL_SECONDS = float(os.environ.get("MDD_IDLE_INTERVAL", "15"))

COUNTRY_PROXY_LISTEN = os.environ.get("MDD_COUNTRY_PROXY_LISTEN", "172.17.0.1")
COUNTRY_PROXY_PORT_BASE = int(os.environ.get("MDD_COUNTRY_PROXY_PORT_BASE", "22000"))


def country_proxy_port(country: str) -> int:
    """Stable, collision-free port for two-letter ISO codes (22000..22675 by default)."""
    code = str(country).lower()
    if not re.fullmatch(r"[a-z]{2}", code):
        raise ValueError("country must be a two-letter ISO code")
    return COUNTRY_PROXY_PORT_BASE + (ord(code[0]) - 97) * 26 + ord(code[1]) - 97


def node_keyword_matches(name: str, keyword: str) -> bool:
    name, keyword = str(name).lower(), str(keyword).lower().strip()
    if not keyword:
        return False
    # ISO/country abbreviations must be standalone tokens: GB must not match a quota such as
    # "58.5GB", and US must not match an unrelated longer word.
    if re.fullmatch(r"[a-z0-9]{2,3}", keyword):
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", name) is not None
    return keyword in name


def clash_node_supports_udp(node: dict) -> bool:
    """Conservative subscription admission check for IKE UDP 500/4500.

    Clash's `udp` flag is optional for protocols that inherently support UDP, so only an
    explicit false rejects them. Shadowsocks SIP003 plugins are TCP-only in this converter.
    Unsupported node/transport types are rejected before they can poison the whole pool.
    """
    kind = str(node.get("type") or "").lower()
    if kind not in UDP_PROXY_TYPES or node.get("udp") is False:
        return False
    if kind in {"ss", "shadowsocks"} and node.get("plugin"):
        return False
    network = str(node.get("network") or "").lower()
    return network in {"", "tcp", "ws", "xhttp"}


def node_needs_xray(node: dict) -> bool:
    """True when this node is better served by Xray-core than by sing-box.

    REALITY is an Xray protocol and its wire details move with Xray. When a server runs a
    build newer than the one sing-box's implementation targets, sing-box fails the handshake
    with "reality verification failed" while Xray clients on the same server connect — a
    difference that reads to an operator as a broken gateway. Handing REALITY to the engine
    that defines it removes a whole class of version skew; XHTTP already went this way.
    """
    if str(node.get("type") or "").lower() != "vless":
        return False
    if str(node.get("network") or "").lower() == "xhttp":
        return True
    if str(node.get("encryption") or "none").lower() not in ("", "none"):
        return True
    return bool((node.get("reality-opts") or {}).get("public-key"))


def xray_outbound(node: dict, tag: str) -> dict:
    """Convert a VLESS node to Xray-core's native outbound form (raw/ws/xhttp)."""
    if str(node.get("type") or "").lower() != "vless":
        raise ValueError("the Xray path currently requires a VLESS node")
    network = str(node.get("network") or "tcp").lower() or "tcp"
    reality = node.get("reality-opts") or {}
    if network == "xhttp" and not reality.get("public-key"):
        raise ValueError("Reality XHTTP node is missing its public key (pbk)")
    user = {"id": str(node.get("uuid") or ""),
            "encryption": str(node.get("encryption") or "none"),
            "flow": str(node.get("flow") or "")}
    # XUDP is how Xray clients carry UDP inside VLESS, and UDP is the whole point of these
    # exits — IKE cannot run without it. The XHTTP path already defaulted to it while the
    # raw/ws path sent nothing, so identical nodes were built two different ways.
    user["packetEncoding"] = str(node.get("packet-encoding") or "xudp")
    server_name = node.get("servername") or node.get("server")
    fingerprint = str(node.get("client-fingerprint") or "") or "chrome"
    # Xray names the plain TCP transport "raw"; "tcp" remains accepted as its alias.
    stream = {"network": "raw" if network == "tcp" else network}
    if reality.get("public-key"):
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName": server_name,
            "fingerprint": fingerprint,
            "publicKey": reality.get("public-key"),
            "shortId": reality.get("short-id") or "",
        }
        if reality.get("spider-x"):
            stream["realitySettings"]["spiderX"] = str(reality["spider-x"])
    elif node.get("tls"):
        stream["security"] = "tls"
        stream["tlsSettings"] = {"serverName": server_name,
                                 "allowInsecure": bool(node.get("skip-cert-verify", False)),
                                 "fingerprint": fingerprint}
        if node.get("alpn"):
            stream["tlsSettings"]["alpn"] = list(node["alpn"])
    else:
        stream["security"] = "none"
    if network == "xhttp":
        xhttp = node.get("xhttp-opts") or {}
        stream["xhttpSettings"] = {"host": xhttp.get("host") or "",
                                   "path": xhttp.get("path") or "/",
                                   "mode": xhttp.get("mode") or "auto"}
        if isinstance(xhttp.get("extra"), dict) and xhttp["extra"]:
            stream["xhttpSettings"]["extra"] = xhttp["extra"]
    elif network == "ws":
        ws = node.get("ws-opts") or {}
        stream["wsSettings"] = {"path": ws.get("path") or "/",
                                "headers": ws.get("headers") or {}}
    return {"protocol": "vless", "tag": tag,
            "settings": {"vnext": [{"address": node.get("server"),
                                     "port": int(node.get("port") or 0), "users": [user]}]},
            "streamSettings": stream}



def outbound_supports_udp(outbound: dict) -> bool:
    kind = str(outbound.get("type") or "").lower()
    if outbound.get("network") == "tcp":
        return False
    if kind == "socks":
        return str(outbound.get("version") or "5") == "5"
    return kind in {"shadowsocks", "trojan", "vless", "vmess", "hysteria", "hysteria2",
                    "tuic", "wireguard"}


def clash_outbound(node: dict, tag: str) -> dict:
    """Convert the common Clash subscription node types used by the gateway."""
    kind = str(node.get("type", "")).lower()
    base = {"type": kind, "tag": tag, "server": node.get("server"),
            "server_port": int(node.get("port") or 0)}
    if not base["server"] or not base["server_port"]:
        raise ValueError("subscription node lacks server/port")
    if kind == "trojan":
        base["password"] = node.get("password", "")
    elif kind == "vless":
        if str(node.get("encryption") or "none").lower() not in ("", "none"):
            raise ValueError(
                "this node uses VLESS Encryption, which only Xray-core carries — "
                "install Xray so the gateway can run it")
        base["uuid"] = node.get("uuid", "")
        base["flow"] = node.get("flow", "")
    elif kind == "vmess":
        base["uuid"] = node.get("uuid", "")
        base["security"] = node.get("cipher") or "auto"
        base["alter_id"] = int(node.get("alterId") or 0)
    elif kind in ("ss", "shadowsocks"):
        base["type"] = "shadowsocks"
        base["method"] = node.get("cipher", "")
        base["password"] = node.get("password", "")
    elif kind in ("hysteria2", "hy2"):
        base["type"] = "hysteria2"
        base["password"] = node.get("password") or node.get("auth", "")
    else:
        raise ValueError(f"unsupported subscription node type {kind!r}")
    if node.get("tls") is True or kind in ("trojan", "hysteria2", "hy2"):
        tls = {"enabled": True,
               "server_name": node.get("servername") or node.get("sni") or node["server"],
               "insecure": bool(node.get("skip-cert-verify", False))}
        if node.get("alpn"):
            tls["alpn"] = list(node["alpn"])
        fingerprint = str(node.get("client-fingerprint") or "")
        reality = node.get("reality-opts") or {}
        if reality:
            # Dropping these produced an outbound that looked valid and simply never
            # completed a handshake — the server cannot answer a client that omits them.
            tls["reality"] = {"enabled": True,
                              "public_key": reality.get("public-key", ""),
                              "short_id": reality.get("short-id", "")}
            # REALITY authenticates the server through its own key exchange, and sing-box
            # requires uTLS alongside it.
            tls["insecure"] = False
            fingerprint = fingerprint or "chrome"
        if fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        base["tls"] = tls
    if kind in ("hysteria2", "hy2") and node.get("obfs"):
        # A server expecting salamander silently discards unobfuscated packets, so the client
        # only ever sees "no recent network activity".
        base["obfs"] = {"type": str(node["obfs"]),
                        "password": str(node.get("obfs-password") or "")}
    network = node.get("network")
    if network == "ws":
        ws = node.get("ws-opts") or {}
        base["transport"] = {"type": "ws", "path": ws.get("path", "/"),
                             "headers": ws.get("headers") or {}}
    return {k: v for k, v in base.items() if v not in (None, "")}


class Orchestrator:
    def __init__(self, data: Path, repo: Path, interval: float = 3.0, dry_run=False):
        self.data, self.repo, self.interval, self.dry_run = data, repo, interval, dry_run
        self.root = data / "orchestrator"
        self.desired_path = self.root / "desired.json"
        self.status_path = self.root / "proxy-status.json"
        self.generated = self.root / "sing-box.json"
        self.xray_generated = self.root / "xray.json"
        self.cache = self.root / "subscription.yaml"
        # The selector process is our child, so a service restart also restarts sing-box.
        # Recover the last observed choices before rendering its new defaults; otherwise a
        # preferred pin that just failed wins again merely because the orchestrator restarted.
        prior_exits = (read_json(self.status_path).get("exits") or {})
        if not isinstance(prior_exits, dict):
            prior_exits = {}
        # "<country>:<epdg host>" -> {address: time it may stop being routed}
        self.epdg_seen: dict[str, dict[str, float]] = {}
        # Last node change per country, so the UI can say why the exit is not the pinned one.
        self.exit_last_change: dict[str, dict] = {
            str(country): dict(state["last_change"])
            for country, state in prior_exits.items()
            if isinstance(state, dict) and isinstance(state.get("last_change"), dict)
        }
        # When sing-box last (re)started; measurements before it settles are cold-start
        # numbers, not node quality.
        self.singbox_started_at = 0.0
        # Country tuns whose systemd-resolved registration has not been undone yet.
        self.tun_dns_pending: set[str] = set()
        self.exit_node_history = self.root / "exit-node-history.jsonl"
        self.reselect_path = self.root / "exit-reselect.json"
        self.reselect_handled_path = self.root / "exit-reselect-handled.json"
        # Reports that a country's exit is carrying connections the control plane has proven
        # dead. Separate from a reselect: the node is not being blamed, its sessions are.
        self.stalled_path = self.root / "exit-stalled.json"
        self.stalled_handled_path = self.root / "exit-stalled-handled.json"
        self.last_exit_node: dict[str, str] = {
            str(country): str(state.get("node") or "")
            for country, state in prior_exits.items()
            if isinstance(state, dict) and state.get("ready") and state.get("node")
        }
        # country -> {node name: time it may be chosen again}
        self.exit_cooldown: dict[str, dict[str, float]] = {}
        # country -> timestamp of the newest reselect request already served. This must survive
        # an orchestrator restart: exit-reselect.json is deliberately written by another process
        # and retained for diagnostics, so an in-memory watermark would replay old failures.
        handled = (read_json(self.reselect_handled_path).get("countries") or {})
        self.handled_reselect: dict[str, float] = {}
        if isinstance(handled, dict):
            for country, requested_at in handled.items():
                try:
                    self.handled_reselect[str(country)] = float(requested_at)
                except (TypeError, ValueError):
                    pass
        # Same watermark discipline for stalled-connection reports: the file is written by the
        # control plane and kept for diagnostics, so a restart must not re-close sessions for a
        # failure that was already dealt with.
        stalled = (read_json(self.stalled_handled_path).get("countries") or {})
        self.handled_stalled: dict[str, float] = {}
        if isinstance(stalled, dict):
            for country, requested_at in stalled.items():
                try:
                    self.handled_stalled[str(country)] = float(requested_at)
                except (TypeError, ValueError):
                    pass
        # country -> retry state for the current request. Ranking is synchronous and each
        # unreachable member can consume five seconds, so retrying on every reconcile cycle would
        # starve the rest of the loop. This state is intentionally ephemeral; the persistent handled
        # watermark still prevents a completed/abandoned request replay after restart.
        self.reselect_retries: dict[str, dict] = {}
        # Countries whose selector has not been ranked since sing-box last (re)started.
        self.exit_unranked: set[str] = set()
        # Countries whose freshly rendered config already defaults to the node
        # currently carrying their tunnels, so a restart needs no re-ranking.
        self.exit_resume: dict[str, str] = {}
        self.singbox = None
        self.xray = None
        self.last_xray_fingerprint = ""
        self.next_xray_config = None
        self._xray_inbounds: list[dict] = []
        self._xray_outbounds: list[dict] = []
        self._xray_rules: list[dict] = []
        self._xray_ports: dict[str, int] = {}
        self.last_proxy_fingerprint = ""
        self.last_proxy_config: dict | None = None
        self.applied_timezone = ""
        self.stop = False
        # What the previous cycle concluded, for deciding whether this one changed anything.
        self._last_conclusion = ""
        # ---------------------------------------------------------- support diagnostics
        # Our own recent output, for the redacted support bundle.
        self._log_ring: collections.deque[str] = collections.deque(maxlen=200)
    @staticmethod
    def service_active(name: str) -> bool:
        return run(["systemctl", "is-active", "--quiet", name]).returncode == 0

    def process_service_restart_request(self):
        """Restart the gateway's own services, or the host, when the control plane asks.

        The control plane is unprivileged and is itself restarted in every scope, so it can
        only state the intent.  ``control`` is safe to run inline — it does not touch this
        process, so the SIM bridges and engine containers stay up — while ``services``
        restarts this very process and has to outlive it in a transient unit, the way
        self-updates do.
        """
        request_path = self.root / "service-restart-request.json"
        request = read_json(request_path)
        if not request:
            return
        try:
            request_path.unlink()
        except OSError:
            pass
        status_path = self.root / "service-restart-status.json"
        scope = str(request.get("scope") or "")

        def publish(state: str, **fields):
            atomic_json(status_path, {"state": state, "scope": scope,
                                      "updated_at": int(time.time()), **fields})

        if scope not in {"control", "services", "host"}:
            publish("failed", error_code="restart.error.invalid_scope")
            return
        if self.dry_run:
            publish("failed", error_code="restart.error.dry_run")
            return
        publish("running")
        self.log(f"restarting on request: scope={scope}")
        if scope == "control":
            result = run(["systemctl", "restart", "mdd-sim-gateway-control"])
            if result.returncode:
                publish("failed", error_code="restart.error.failed",
                        error=(result.stderr or result.stdout or "").strip()[:400])
            else:
                publish("success")
            return
        if scope == "host":
            # systemd owns the shutdown from here; this process is torn down with everything
            # else, so there is no completion to publish.
            result = run(["systemctl", "reboot"])
            if result.returncode:
                publish("failed", error_code="restart.error.failed",
                        error=(result.stderr or result.stdout or "").strip()[:400])
            return
        run(["systemctl", "reset-failed", "mdd-sim-gateway-restart.service"])
        result = run(["systemd-run", "--unit", "mdd-sim-gateway-restart", "--collect",
                      "--description", "MDD Sim Gateway service restart",
                      "sh", str(self.repo / "install.sh"), "restart"])
        if result.returncode:
            publish("failed", error_code="restart.error.launch",
                    error=(result.stderr or result.stdout or "").strip()[:400])

    def settle_service_restart(self):
        """Close out a restart that could not report its own completion.

        ``services`` restarts this process and ``host`` takes the machine down, so neither can
        publish a result — this process running again *is* the result. Without this the
        document stays "running" forever, which is the very thing this release removes from
        the update path.
        """
        status_path = self.root / "service-restart-status.json"
        status = read_json(status_path)
        if status.get("state") == "running" and status.get("scope") in {"services", "host"}:
            atomic_json(status_path, {**status, "state": "success",
                                      "updated_at": int(time.time())})

    def log(self, message):
        line = f"{time.strftime('%F %T')} {message}"
        # Retained as well as printed: journalctl is out of reach from the control-plane
        # container, so the support bundle would otherwise show no reason for a stall.
        self._log_ring.append(line)
        print(line, flush=True)

    def reconcile_timezone(self):
        """Apply the validated WebUI timezone to the host without changing its hostname."""
        try:
            document = load_yaml_text((self.data / "config.yaml").read_text())
            timezone = str((document.get("settings") or {}).get("timezone") or "").strip()
        except Exception:
            return
        if not timezone or timezone == self.applied_timezone:
            return
        zone = Path("/usr/share/zoneinfo") / timezone
        try:
            if not zone.resolve().is_relative_to(Path("/usr/share/zoneinfo").resolve()) or not zone.is_file():
                raise ValueError("invalid timezone")
        except (OSError, ValueError):
            self.log(f"ignored invalid timezone {timezone!r}")
            return
        if not self.dry_run:
            result = run(["timedatectl", "set-timezone", timezone])
            if result.returncode:
                self.log(f"could not apply timezone: {(result.stderr or result.stdout).strip()}")
                return
        self.applied_timezone = timezone

    def subscription(self, url: str, refresh_minutes: int, profile_id: str = "legacy") -> dict:
        safe_id = slug(profile_id)[:64]
        cache = self.root / "subscriptions" / f"{safe_id}.yaml"
        # Preserve the pre-profile cache for a seamless first restart after upgrade.
        if profile_id == "legacy" and self.cache.exists() and not cache.exists():
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(self.cache, cache)
        stale = not cache.exists() or time.time() - cache.stat().st_mtime > max(1, refresh_minutes) * 60
        if stale:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "mdd-sim-gateway/1"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    body = response.read(8 * 1024 * 1024)
                cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cache.write_bytes(body)
                os.chmod(cache, 0o600)
            except Exception:
                # A refresh outage must not discard the last known-good subscription. The
                # urltest pool keeps probing its members while we retry the feed next cycle.
                if not cache.exists():
                    raise
        if yaml is None:
            raise RuntimeError("PyYAML is required for subscription mode")
        # The cache only changes on a refresh (every refresh_minutes), but this runs on every
        # reconcile pass. Keep the parsed document until the file changes; hand out a copy so
        # the proxy builders can never edit the cached one.
        stat = cache.stat()
        key = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        parsed = getattr(self, "_subscription_docs", None)
        if parsed is None:
            parsed = self._subscription_docs = {}
        entry = parsed.get(str(cache))
        if entry is None or entry[0] != key:
            entry = parsed[str(cache)] = (key, load_yaml_text(cache.read_text(encoding="utf-8")))
        return deepcopy(entry[1])

    def xray_bridge_outbound(self, node: dict, sing_tag: str, runtime_id: str) -> dict:
        """Register one loopback-only Xray endpoint and return its sing-box detour."""
        if runtime_id not in self._xray_ports:
            # Stable allocation with collision probing. Ports never leave loopback.
            port = 24000 + int(hashlib.sha256(runtime_id.encode()).hexdigest()[:6], 16) % 1000
            used = set(self._xray_ports.values())
            while port in used:
                port = 24000 + ((port - 23999) % 1000)
            self._xray_ports[runtime_id] = port
            inbound_tag, outbound_tag = f"in-{slug(runtime_id)}", f"out-{slug(runtime_id)}"
            self._xray_inbounds.append({"listen": "127.0.0.1", "port": port,
                                        "protocol": "socks", "tag": inbound_tag,
                                        "settings": {"auth": "noauth", "udp": True,
                                                     "ip": "127.0.0.1"}})
            self._xray_outbounds.append(xray_outbound(node, outbound_tag))
            self._xray_rules.append({"type": "field", "inboundTag": [inbound_tag],
                                     "outboundTag": outbound_tag})
        return {"type": "socks", "tag": sing_tag, "version": "5",
                "server": "127.0.0.1", "server_port": self._xray_ports[runtime_id]}

    def node_outbound(self, node: dict, tag: str, runtime_id: str) -> dict:
        if node_needs_xray(node):
            return self.xray_bridge_outbound(node, tag, runtime_id)
        return clash_outbound(node, tag)

    def build_proxy_config(self, proxy: dict) -> tuple[dict, dict]:
        inbounds, outbounds, rules, state = [], [], [], {}
        self._xray_inbounds, self._xray_outbounds, self._xray_rules, self._xray_ports = [], [], [], {}
        # Which countries are carried by Xray, so that Xray failing takes only those down.
        self._xray_countries = set()
        tun_index = 0
        existing_path = str(proxy.get("existing_singbox_config") or "").strip()
        existing = read_json(Path(existing_path)) if existing_path else {}
        existing_by_tag = {x.get("tag"): x for x in existing.get("outbounds", []) if x.get("tag")}
        subscriptions: dict[str, dict] = {}
        profiles = proxy.get("profiles") or {}
        for country, exit_cfg in sorted((proxy.get("exits") or {}).items()):
            country = str(country).lower()
            if not re.fullmatch(r"[a-z]{2}", country) or not exit_cfg.get("enabled"):
                continue
            mode = str(exit_cfg.get("mode") or "subscription").lower()
            profile_id = str(exit_cfg.get("profile_id") or "").strip()
            profile = profiles.get(profile_id) if profile_id else None
            if isinstance(profile, dict):
                profile_type = str(profile.get("type") or "").lower()
                mode = "subscription" if profile_type == "subscription" else "existing" \
                    if profile_type == "existing" else "manual"
            tag = f"exit-{country}"
            if mode == "direct":
                state[country] = {"ready": True, "mode": mode, "interface": ""}
                continue
            try:
                if mode == "manual":
                    value = (profile or {}).get("value") if profile else None
                    if profile and profile.get("type") == "socks5" and not value:
                        host = str(profile.get("server") or "").strip()
                        port = int(profile.get("port") or 1080)
                        outbound = {"type": "socks", "tag": tag, "version": "5",
                                    "server": host, "server_port": port}
                        if profile.get("username"):
                            outbound["username"] = str(profile["username"])
                        if profile.get("password"):
                            outbound["password"] = str(profile["password"])
                        if not host:
                            raise ValueError("SOCKS5 server is empty")
                    else:
                        value = value or exit_cfg.get("outbound_json") or exit_cfg.get("proxy_url") or ""
                        text = str(value or "").strip()
                        if text.lower().startswith("vless://"):
                            node = parse_share_link(text)
                            if node_needs_xray(node):
                                self._xray_countries.add(country)
                            outbound = self.node_outbound(node, tag, profile_id or f"country-{country}")
                        else:
                            outbound = parse_manual_outbound(value, tag)
                elif mode == "existing":
                    source_tag = str((profile or {}).get("outbound_tag")
                                     or exit_cfg.get("outbound_tag") or "").strip()
                    if source_tag not in existing_by_tag:
                        raise ValueError(f"outbound tag {source_tag!r} not found in existing config")
                    outbound = dict(existing_by_tag[source_tag]); outbound["tag"] = tag
                    if not outbound_supports_udp(outbound):
                        raise ValueError(f"outbound {source_tag!r} is not UDP-capable")
                elif mode == "subscription":
                    subscription_id = profile_id or "legacy"
                    if subscription_id not in subscriptions:
                        url = str((profile or {}).get("url") or proxy.get("subscription_url") or "").strip()
                        if not url:
                            raise ValueError("subscription URL is empty")
                        refresh = int((profile or {}).get("refresh_minutes")
                                      or proxy.get("refresh_minutes") or 30)
                        subscriptions[subscription_id] = self.subscription(url, refresh, subscription_id)
                    words = [str(x).lower() for x in (exit_cfg.get("keywords") or []) if str(x).strip()]
                    nodes = subscriptions[subscription_id].get("proxies") or []
                    named = [n for n in nodes if not words or any(
                        node_keyword_matches(n.get("name", ""), w) for w in words)]
                    matches = [n for n in named if clash_node_supports_udp(n)]
                    if not matches:
                        if named:
                            raise ValueError("matching nodes exist, but none are UDP-capable for VoWiFi IKE")
                        raise ValueError("no subscription node matched country keywords")
                    matches.sort(key=lambda n: str(n.get("name", "")))
                    member_tags = []
                    member_names = {}
                    for index, node in enumerate(matches[:32]):
                        member_tag = f"{tag}-{index}"
                        if node_needs_xray(node):
                            self._xray_countries.add(country)
                        outbounds.append(self.node_outbound(
                            node, member_tag, f"{subscription_id}-{hashlib.sha256(str(node).encode()).hexdigest()[:12]}"))
                        member_tags.append(member_tag)
                        member_names[member_tag] = str(node.get("name") or member_tag)
                    # An operator can pin one node by its subscription name. Pinning matches on
                    # the name because member tags are positional and shift whenever the feed
                    # adds or drops a node.
                    pinned_name = str(exit_cfg.get("pinned_node") or "").strip()
                    pinned_tag = next((member for member, name in member_names.items()
                                       if name == pinned_name), "") if pinned_name else ""
                    # "lock" keeps the exit on this node whatever happens, which is what an
                    # operator running a controlled comparison needs. "prefer" gives up that
                    # guarantee in exchange for failover.
                    pin_mode = str(exit_cfg.get("pin_mode") or "lock").lower()
                    # Always a selector: it never switches on its own. A urltest re-ranks the
                    # pool on a timer and switches whenever another node measures faster, which
                    # changes the outer source address underneath an established IKE SA. For a
                    # VoWiFi tunnel that trade is pure loss — latency does not matter once the
                    # tunnel is up, and the switch costs a re-registration. Node changes are
                    # driven instead by reselect requests, i.e. by a line actually failing.
                    # A restart resets the selector to whatever this config names as its
                    # default. Naming the node that is already carrying tunnels makes the
                    # restart land back where it was, instead of ranking by latency and moving
                    # a healthy line — which is how a config rewrite flipped a country between
                    # two nodes four times in an hour without a single line ever failing.
                    running = self.last_exit_node.get(country) or ""
                    resumed = next((member for member, name in member_names.items()
                                    if name == running), "") if running else ""
                    # A lock is a standing instruction and therefore still wins on every
                    # restart.  A preference is different: it is consulted when an exit has
                    # to be selected, but must not undo a failure-driven fallback merely
                    # because an unrelated config change restarts sing-box.
                    if pinned_tag and pin_mode == "lock":
                        default_tag = pinned_tag
                    else:
                        default_tag = resumed or pinned_tag or member_tags[0]
                    self.exit_resume[country] = (
                        default_tag if running and member_names.get(default_tag) == running
                        else "")
                    outbound = {"type": "selector", "tag": tag, "outbounds": member_tags,
                                "default": default_tag}
                else:
                    raise ValueError(f"unknown exit mode {mode!r}")
                iface = tun_name(country)
                proxy_port = country_proxy_port(country)
                inbounds.append({"type": "tun", "tag": f"tun-{country}", "interface_name": iface,
                                 "address": [f"172.29.{20 + tun_index}.1/30"], "auto_route": False,
                                 "strict_route": True})
                tun_index += 1
                # TCP/UDP SOCKS entry reachable only from Docker's host bridge. The control
                # container uses host.docker.internal, while LAN clients cannot reach it.
                inbounds.append({"type": "socks", "tag": f"proxy-{country}",
                                 "listen": COUNTRY_PROXY_LISTEN, "listen_port": proxy_port})
                outbounds.append(outbound)
                rules.append({"inbound": [f"tun-{country}", f"proxy-{country}"], "outbound": tag})
                state[country] = {"ready": True, "mode": mode, "interface": iface,
                                  "proxy_host": COUNTRY_PROXY_LISTEN,
                                  "proxy_port": proxy_port,
                                  "node": str((profile or {}).get("name")
                                              or outbound.get("server") or outbound.get("type"))}
                if mode == "subscription":
                    # Kept only in memory until the Clash API reports which urltest member is
                    # active. proxy-status.json publishes the original subscription node name,
                    # never the internal exit-gb-1 tag or the generic word "urltest".
                    state[country]["node"] = ""
                    state[country]["_member_names"] = member_names
                    state[country]["candidate_count"] = len(member_names)
                    state[country]["udp_rejected_count"] = len(named) - len(matches)
                    state[country]["udp_required"] = True
                    # The WebUI builds its node picker from this list, so it must carry the
                    # UDP-capable candidates only — offering a node the pool rejected would
                    # produce a pin that silently falls back to automatic.
                    state[country]["candidates"] = [member_names[member] for member in member_tags]
                    state[country]["pinned_node"] = pinned_name
                    state[country]["pin_mode"] = pin_mode if pinned_tag else ""
                    state[country]["selection"] = (
                        ("preferred" if pin_mode == "prefer" else "manual")
                        if pinned_tag else "managed")
                    # A pinned node disappears whenever the feed renames or drops it. Automatic
                    # selection still respects this country's keywords, so falling back cannot
                    # leak into another geography; surface it instead of failing the exit.
                    state[country]["pinned_missing"] = bool(pinned_name and not pinned_tag)
            except Exception as exc:
                state[country] = {"ready": False, "mode": mode, "error": str(exc), "terminal": True}
        config = {"log": {"level": "info"}, "inbounds": inbounds,
                  "outbounds": outbounds, "route": {"rules": rules, "auto_detect_interface": True}}
        self.next_xray_config = ({"log": {"loglevel": "warning"},
                                  "inbounds": self._xray_inbounds,
                                  "outbounds": self._xray_outbounds,
                                  "routing": {"domainStrategy": "AsIs", "rules": self._xray_rules}}
                                 if self._xray_inbounds else None)
        if any(value.get("mode") == "subscription" and value.get("ready")
               for value in state.values()):
            # Loopback-only: used to resolve urltest's current member tag to the human-readable
            # node name. It is not exposed on LAN and does not proxy user traffic.
            config["experimental"] = {"clash_api": {"external_controller": CLASH_API}}
        return config, state

    def update_selected_nodes(self, exits_state: dict):
        """Replace each subscription urltest placeholder with its selected node's real name."""
        now = time.time()
        for country, state in exits_state.items():
            # Why the exit is where it is. Without this the UI can only say the running node
            # disagrees with the pinned one, which reads as an unexplained override.
            change = self.exit_last_change.get(country)
            if change:
                state["last_change"] = change
            pinned = str(state.get("pinned_node") or "")
            cooling = (self.exit_cooldown.get(country) or {}).get(pinned) if pinned else None
            if cooling and cooling > now:
                state["pinned_cooldown_seconds"] = int(cooling - now)
            members = state.pop("_member_names", None)
            if not members or not state.get("ready"):
                continue
            tag = f"exit-{country}"
            try:
                url = f"http://{CLASH_API}/proxies/{urllib.parse.quote(tag, safe='')}"
                with urllib.request.urlopen(url, timeout=1.5) as response:
                    selected = str((json.load(response) or {}).get("now") or "")
                if selected:
                    state["node_tag"] = selected
                    state["node"] = members.get(selected, selected)
                    self.record_exit_node(country, state)
            except Exception:
                # Immediately after a restart the first health check may not have selected a
                # member yet. The next 3-second reconcile updates it; never show "urltest".
                pass

    def measure_member(self, tag: str) -> int | None:
        """Latency of one pool member, measured on demand. None when it cannot be reached.

        Measuring only at selection time replaces the timer that used to probe every member
        every three minutes. The probe still runs over TCP while the exit carries UDP, so it
        is a liveness signal for ranking candidates, never a reason to move a working line.
        """
        query = urllib.parse.urlencode({"url": EXIT_PROBE_URL, "timeout": EXIT_PROBE_TIMEOUT_MS})
        url = f"http://{CLASH_API}/proxies/{urllib.parse.quote(tag, safe='')}/delay?{query}"
        try:
            with urllib.request.urlopen(url, timeout=EXIT_PROBE_TIMEOUT_MS / 1000 + 2) as response:
                return int((json.load(response) or {}).get("delay"))
        except Exception:
            # 503 for an unusable member, or the API is briefly unavailable. Either way this
            # candidate is not a safe choice right now.
            return None

    def select_member(self, country: str, tag: str) -> bool:
        """Point a country's selector at ``tag`` through the Clash API."""
        url = f"http://{CLASH_API}/proxies/{urllib.parse.quote(f'exit-{country}', safe='')}"
        request = urllib.request.Request(
            url, data=json.dumps({"name": tag}).encode(), method="PUT",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3):
                return True
        except Exception as exc:
            print(f"exit reselect: cannot select {tag} for {country}: {exc}", flush=True)
            return False

    def drop_exit_connections(self, country: str) -> int:
        """Close the connections currently pinned to a country's exit. Returns how many.

        sing-box keys a UDP session on its 5-tuple and retires it on an IDLE timeout. A line
        rebuilding its tunnel retransmits IKE every few seconds, and every retransmit refreshes
        that timer — so a session whose outbound died (a dial that lost the race with the
        network coming back up, say) is held open by the very retries meant to recover it.
        Each later packet is handed to the same dead connection, no NEW session is ever
        created, and the failure becomes permanent while sing-box logs nothing at all.

        Closing the session is the whole remedy: the next packet has to route and dial afresh.
        This changes no node and touches no selector — the exit the operator chose stays
        exactly where it is.
        """
        try:
            with urllib.request.urlopen(f"http://{CLASH_API}/connections", timeout=3) as response:
                payload = json.load(response) or {}
        except Exception as exc:
            print(f"exit cleanup: cannot list {country.upper()} connections: {exc}", flush=True)
            return 0
        prefix = f"exit-{country}"
        dropped = 0
        for conn in (payload.get("connections") or []):
            # Match the country's selector and its members ("exit-gb", "exit-gb-0"), never
            # another country whose tag merely starts the same way.
            chains = [str(item) for item in (conn.get("chains") or [])]
            if not any(tag == prefix or tag.startswith(prefix + "-") for tag in chains):
                continue
            identifier = str(conn.get("id") or "")
            if not identifier:
                continue
            request = urllib.request.Request(
                f"http://{CLASH_API}/connections/{urllib.parse.quote(identifier, safe='')}",
                method="DELETE")
            try:
                with urllib.request.urlopen(request, timeout=3):
                    dropped += 1
            except Exception:
                # Already gone, or the API blinked: the next report retries.
                pass
        return dropped

    def process_stalled_reports(self, exits_state: dict):
        """Clear a country exit's sessions once the control plane proves they carry nothing.

        The control plane reports this only after attributing a line's failure to the exit AND
        finding no sibling line registered over it, so nothing that works is torn down. It is
        deliberately the weaker sibling of a reselect: a reselect says "this node is bad, move";
        this says "the node may be fine, but the sessions on it are dead".
        """
        report = read_json(self.stalled_path) or {}
        countries = report.get("countries") or {}
        handled_changed = False
        for country, entry in countries.items():
            country = str(country)
            requested_at = float((entry or {}).get("ts") or 0)
            if requested_at <= self.handled_stalled.get(country, 0):
                continue
            # Stale evidence is no evidence: a report only describes the moment it was made.
            if time.time() - requested_at > EXIT_RESELECT_MAX_AGE:
                self.handled_stalled[country] = requested_at
                handled_changed = True
                continue
            state = exits_state.get(country) or {}
            if not state.get("ready") or state.get("mode") != "subscription":
                continue
            self.handled_stalled[country] = requested_at
            handled_changed = True
            dropped = self.drop_exit_connections(country)
            if dropped:
                print(f"exit cleanup: closed {dropped} stalled {country.upper()} connection(s) "
                      f"on {state.get('node') or 'the current node'} "
                      f"(line {entry.get('line') or '?'}: {entry.get('reason') or 'exit blamed'})",
                      flush=True)
        if handled_changed:
            atomic_json(self.stalled_handled_path,
                        {"version": 1, "countries": self.handled_stalled})

    def rank_and_select(self, country: str, state: dict, avoid: str = "", prefer: str = "") -> str:
        """Measure the candidates and move the selector to the best usable one.

        ``avoid`` is the node that just failed a line; it enters a cooldown so the next
        attempt tries something else. When every candidate is cooling down the cooldown is
        cleared rather than leaving the country with no exit at all.

        ``prefer`` wins over latency whenever it is usable and not cooling down. It is applied
        only here, at a moment the exit is being changed anyway — returning to a preferred node
        while a line is established would tear down a working tunnel, which is the disruption
        this whole design exists to avoid.
        """
        members = state.get("_member_names") or {}
        if not members:
            return ""
        now = time.time()
        cooldown = self.exit_cooldown.setdefault(country, {})
        if avoid:
            cooldown[avoid] = now + EXIT_RESELECT_COOLDOWN
        for name, until in list(cooldown.items()):
            if until <= now:
                cooldown.pop(name, None)
        allowed = [tag for tag, name in members.items() if name not in cooldown]
        if not allowed:
            print(f"exit reselect: every {country.upper()} candidate is cooling down; "
                  "clearing the cooldown and retrying the whole pool", flush=True)
            cooldown.clear()
            allowed = list(members)
        preferred_tag = next((tag for tag in allowed if members.get(tag) == prefer), "") if prefer else ""
        if preferred_tag:
            # A pin is an explicit operator choice, so it gets more evidence than one probe
            # before being passed over. A single sample on a path with occasional latency
            # spikes would otherwise make the preference silently ineffective.
            delay = None
            for _ in range(EXIT_PREFERRED_PROBE_ATTEMPTS):
                delay = self.measure_member(preferred_tag)
                if delay is not None:
                    break
            if delay is not None and self.select_member(country, preferred_tag):
                print(f"exit reselect: {country.upper()} -> {prefer} ({delay}ms, preferred)",
                      flush=True)
                return preferred_tag
            print(f"exit reselect: preferred {country.upper()} node {prefer} is unusable; "
                  "falling back to the rest of the pool", flush=True)
        measured = [(delay, tag) for tag, delay in
                    ((tag, self.measure_member(tag)) for tag in allowed) if delay is not None]
        if not measured:
            print(f"exit reselect: no {country.upper()} candidate answered its probe", flush=True)
            return ""
        delay, best = min(measured)
        if not self.select_member(country, best):
            return ""
        print(f"exit reselect: {country.upper()} -> {members.get(best, best)} "
              f"({delay}ms, {len(measured)}/{len(allowed)} candidates usable)", flush=True)
        return best

    def process_reselect_requests(self, exits_state: dict):
        """Move a country's exit after the control plane gave up on one of its lines.

        This is the only automatic node change. A line that cannot register is evidence the
        current exit is unusable for VoWiFi in a way no latency probe detects — the tunnel
        can be established while SIP inside it goes unanswered.

        Nothing is measured until sing-box has settled. Requests are left pending rather than
        served with cold-start numbers, so a restart cannot quietly demote a pinned node.
        """
        if time.time() - self.singbox_started_at < EXIT_RANK_WARMUP_SECONDS:
            return
        requests = (read_json(self.reselect_path) or {}).get("countries") or {}
        handled_changed = False
        for country, state in exits_state.items():
            if not state.get("ready") or state.get("mode") != "subscription":
                continue
            if state.get("selection") == "manual":
                # Locked: an operator wants this exact node, including while it is failing.
                continue
            # Preferred: the pin still steers the choice, but a failing line may move away
            # from it and come back on a later reselect once its cooldown has expired.
            prefer = (str(state.get("pinned_node") or "")
                      if state.get("selection") == "preferred" else "")
            request = requests.get(country) or {}
            requested_at = float(request.get("ts") or 0)
            pending = requested_at > self.handled_reselect.get(country, 0)
            if not pending and country not in self.exit_unranked:
                continue
            if pending and time.time() - requested_at > EXIT_RESELECT_MAX_AGE:
                # A line failure is evidence only while it is current. Replaying a days-old
                # request after a service restart would move a healthy live tunnel based on an
                # obsolete event (and on today's latency ranking). Persist the rejection so the
                # retained diagnostic document stays harmless across every later restart.
                self.handled_reselect[country] = requested_at
                self.reselect_retries.pop(country, None)
                handled_changed = True
                continue
            # Both entry points rank synchronously and every unreachable member costs seconds,
            # so a failed ranking must never be retried on the very next reconcile cycle. The
            # zero key stands for initial selection: it has no request to abandon, so it keeps
            # retrying until the pool answers — just at the same slow cadence.
            attempt_key = requested_at if pending else 0.0
            retry = self.reselect_retries.get(country) or {}
            if not retry or float(retry.get("requested_at") or 0) != attempt_key:
                retry = {"requested_at": attempt_key, "attempts": 0, "next_at": 0.0}
                self.reselect_retries[country] = retry
            if time.monotonic() < float(retry.get("next_at") or 0):
                continue
            chosen = self.rank_and_select(country, state, prefer=prefer,
                                          # The first attempt establishes the failed node's full
                                          # cooldown. Retrying must not extend that cooldown.
                                          avoid=(str(request.get("node") or "")
                                                 if pending and not retry.get("attempts") else ""))
            if chosen:
                self.reselect_retries.pop(country, None)
                if pending:
                    # A failed measurement/selection remains pending until its short TTL expires;
                    # only a successfully applied selector change consumes the request.
                    self.handled_reselect[country] = requested_at
                    handled_changed = True
                self.exit_unranked.discard(country)
                members = state.get("_member_names") or {}
                state["node_tag"] = chosen
                state["node"] = members.get(chosen, chosen)
                self.record_exit_node(country, state,
                                      reason=str(request.get("reason") or "") if pending
                                      else "initial-selection")
            else:
                retry["attempts"] = int(retry.get("attempts") or 0) + 1
                if pending and retry["attempts"] >= max(1, EXIT_RESELECT_MAX_ATTEMPTS):
                    print(f"exit reselect: abandoning {country.upper()} request after "
                          f"{retry['attempts']} failed rankings", flush=True)
                    self.handled_reselect[country] = requested_at
                    self.reselect_retries.pop(country, None)
                    handled_changed = True
                else:
                    retry["next_at"] = time.monotonic() + EXIT_RESELECT_RETRY_SECONDS
        if handled_changed:
            atomic_json(self.reselect_handled_path,
                        {"version": 1, "countries": self.handled_reselect})

    def record_exit_node(self, country: str, state: dict, reason: str = "observed"):
        """Log every exit-node change so a line's failure window can be correlated with it.

        Changing the exit changes the outer source address, which invalidates the ePDG's IKE SA
        and forces the line to rebuild its tunnel. Without this history a reconnect storm and a
        node change are indistinguishable after the fact.
        """
        node = str(state.get("node") or "")
        previous = self.last_exit_node.get(country)
        if not node or node == previous:
            return
        self.last_exit_node[country] = node
        if previous is None:
            # First observation after an orchestrator start is not a switch.
            return
        record = {"ts": int(time.time()), "country": country, "from": previous, "to": node,
                  "selection": state.get("selection") or "managed", "reason": reason}
        # One extra request, only on an actual change: the delay that made urltest switch is
        # the whole reason the record is useful.
        try:
            member = urllib.parse.quote(str(state.get("node_tag") or ""), safe="")
            with urllib.request.urlopen(f"http://{CLASH_API}/proxies/{member}", timeout=1.5) as response:
                history = (json.load(response) or {}).get("history") or []
            if history:
                record["delay_ms"] = history[-1].get("delay")
        except Exception:
            pass
        self.exit_last_change[country] = record
        append_jsonl(self.exit_node_history, record)

    def apply_singbox(self, config: dict):
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if fingerprint == self.last_proxy_fingerprint and self.singbox and self.singbox.poll() is None:
            return
        if self.last_proxy_config is not None and self.singbox and self.singbox.poll() is None:
            resumed_config = deepcopy(self.last_proxy_config)
            selectors = {outbound.get("tag"): outbound
                         for outbound in config.get("outbounds") or []
                         if outbound.get("type") == "selector"}
            for outbound in resumed_config.get("outbounds") or []:
                tag = str(outbound.get("tag") or "")
                current = selectors.get(tag)
                if (outbound.get("type") == "selector" and tag.startswith("exit-")
                        and current and current.get("default")
                        and self.exit_resume.get(tag[len("exit-"):]) == current["default"]):
                    outbound["default"] = current["default"]
            if resumed_config == config:
                atomic_json(self.generated, config)
                self.last_proxy_fingerprint = fingerprint
                self.last_proxy_config = deepcopy(config)
                return
        # Restarting resets every selector to its configured default. Where that default is
        # the node already carrying this country's tunnels the restart is a no-op for the
        # exit, so the memory of it is kept and nothing is ranked: re-ranking there would
        # move a working line on latency grounds, which is the one input this design refuses
        # to act on. Only a country whose node did not survive the rewrite starts over.
        for tag in [str(x.get("tag") or "") for x in config.get("outbounds") or []
                    if x.get("type") == "selector"]:
            if tag.startswith("exit-"):
                country = tag[len("exit-"):]
                if self.exit_resume.get(country):
                    continue
                self.exit_unranked.add(country)
                self.last_exit_node.pop(country, None)
        self.singbox_started_at = time.time()
        if self.dry_run:
            atomic_json(self.generated, config)
            self.last_proxy_fingerprint = fingerprint
            self.last_proxy_config = deepcopy(config)
            return
        binary = shutil.which(os.environ.get("MDD_SINGBOX_BIN", "sing-box"))
        if not binary:
            raise RuntimeError("sing-box executable not found")
        candidate = self.generated.with_name("sing-box.candidate.json")
        atomic_json(candidate, config)
        check = run([binary, "check", "-c", str(candidate)])
        if check.returncode:
            raise RuntimeError("sing-box config invalid: " + (check.stderr or check.stdout).strip())
        old = self.singbox
        old_config = self.generated.read_bytes() if self.generated.exists() else None
        if old and old.poll() is None:
            old.terminate()
            try: old.wait(5)
            except subprocess.TimeoutExpired: old.kill(); old.wait()
        os.replace(candidate, self.generated)
        self.singbox = subprocess.Popen([binary, "run", "-c", str(self.generated)])
        # The restore path below relaunches the previous config, whose tuns register the same
        # way; the union covers whichever of the two ends up running.
        self.tun_dns_pending |= {str(item.get("interface_name"))
                                 for item in config.get("inbounds") or []
                                 if item.get("type") == "tun" and item.get("interface_name")}
        time.sleep(0.8)
        if self.singbox.poll() is not None:
            # Restore and restart the last checked/running config. Routes are kept only after
            # apply_singbox succeeds, so a broken update cannot silently fall through direct.
            if old_config is not None:
                self.generated.write_bytes(old_config)
                self.singbox = subprocess.Popen([binary, "run", "-c", str(self.generated)])
            else:
                self.singbox = None
            raise RuntimeError("sing-box exited during startup")
        self.last_proxy_fingerprint = fingerprint
        self.last_proxy_config = deepcopy(config)

    def release_tun_dns(self):
        """Take the country tuns back out of the host's DNS.

        sing-tun runs ``resolvectl domain <tun> ~.``, ``default-route <tun> true`` and
        ``dns <tun> <address+1>`` on every tun it brings up, whether or not auto_route is set,
        and sing-box exposes no option to stop it. On a host resolving through
        systemd-resolved (Ubuntu desktop and server) that makes the tun the resolver for every
        name, and nothing answers there: the exits only carry routed ePDG addresses, so the
        whole host lost DNS the moment an exit was enabled (Discussion #104). Hosts without
        resolvectl, such as Raspberry Pi OS, never received the registration.

        The registration is made once, asynchronously, shortly after start, so it is looked
        for on each pass for a bounded time and reverted as soon as it appears.
        """
        if self.dry_run or not self.tun_dns_pending:
            return
        ctl = shutil.which("resolvectl")
        if not ctl:
            self.tun_dns_pending.clear()
            return
        for iface in sorted(self.tun_dns_pending):
            shown = run([ctl, "domain", iface])
            if shown.returncode == 0 and "~." in shown.stdout.split():
                run([ctl, "revert", iface])
                self.tun_dns_pending.discard(iface)
                self.log(f"removed {iface} from the host DNS configuration "
                         "(sing-box registers every tun as the catch-all resolver)")
        if time.time() - self.singbox_started_at > TUN_DNS_WATCH_SECONDS:
            self.tun_dns_pending.clear()

    def apply_xray(self, config: dict | None):
        if not config:
            if self.xray and self.xray.poll() is None:
                self.xray.terminate()
                try: self.xray.wait(5)
                except subprocess.TimeoutExpired: self.xray.kill(); self.xray.wait()
            self.xray = None
            self.last_xray_fingerprint = ""
            return
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if fingerprint == self.last_xray_fingerprint and self.xray and self.xray.poll() is None:
            return
        if self.dry_run:
            atomic_json(self.xray_generated, config)
            self.last_xray_fingerprint = fingerprint
            return
        binary = shutil.which(os.environ.get("MDD_XRAY_BIN", "xray"))
        if not binary:
            raise RuntimeError(
                "Xray-core executable not found; REALITY and XHTTP nodes are carried by it "
                "(install it with: sudo ./install.sh reload)")
        candidate = self.xray_generated.with_name("xray.candidate.json")
        atomic_json(candidate, config)
        check = run([binary, "run", "-test", "-config", str(candidate)])
        if check.returncode:
            raise RuntimeError("Xray config invalid: " + (check.stderr or check.stdout).strip())
        old, old_config = self.xray, self.xray_generated.read_bytes() if self.xray_generated.exists() else None
        if old and old.poll() is None:
            old.terminate()
            try: old.wait(5)
            except subprocess.TimeoutExpired: old.kill(); old.wait()
        os.replace(candidate, self.xray_generated)
        self.xray = subprocess.Popen([binary, "run", "-config", str(self.xray_generated)])
        time.sleep(0.6)
        if self.xray.poll() is not None:
            if old_config is not None:
                self.xray_generated.write_bytes(old_config)
                self.xray = subprocess.Popen([binary, "run", "-config", str(self.xray_generated)])
            else:
                self.xray = None
            raise RuntimeError("Xray exited during startup")
        self.last_xray_fingerprint = fingerprint

    @staticmethod
    def resolve(host: str) -> list[str]:
        result = set()
        for family, _, _, _, address in socket.getaddrinfo(host, 0, type=socket.SOCK_DGRAM):
            ip = address[0]
            if family == socket.AF_INET:
                result.add(ip)
        return sorted(result)

    def retained_epdg_addresses(self, key: str, resolved: list[str]) -> list[str]:
        """Every ePDG address that must stay routed, not just the one DNS just returned.

        A carrier rotates this record every few seconds while an IKE SA lives for hours, so the
        set of addresses in use is always wider than the current answer. Entries age out on a
        timer and are capped, which keeps the route table bounded without ever revoking the
        address an established tunnel is still using.
        """
        now = time.time()
        seen = self.epdg_seen.setdefault(key, {})
        for address in resolved:
            seen[address] = now + EPDG_ADDRESS_TTL
        for address, expiry in list(seen.items()):
            if expiry <= now:
                del seen[address]
        if len(seen) > EPDG_ADDRESS_MAX:
            for address, _ in sorted(seen.items(), key=lambda item: item[1])[:len(seen) - EPDG_ADDRESS_MAX]:
                del seen[address]
        return sorted(seen)

    def current_managed_routes(self) -> set[tuple[str, str]]:
        result = run(["ip", "-4", "route", "show", "proto", MANAGED_ROUTE_PROTO])
        found = set()
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts and "dev" in parts:
                    found.add((parts[0].split("/")[0], parts[parts.index("dev") + 1]))
        return found

    def apply_routes(self, wanted: set[tuple[str, str]]):
        if self.dry_run:
            return
        current = self.current_managed_routes()
        for ip, iface in current - wanted:
            run(["ip", "-4", "route", "del", f"{ip}/32", "dev", iface, "proto", MANAGED_ROUTE_PROTO])
        for ip, iface in wanted:
            run(["ip", "-4", "route", "replace", f"{ip}/32", "dev", iface, "proto", MANAGED_ROUTE_PROTO])

    def reconcile_proxy(self, desired: dict):
        proxy = desired.get("proxy") or {}
        lines_status, wanted, owners = {}, set(), {}
        exits_state = {}
        try:
            if not proxy.get("enabled"):
                self.apply_routes(set())
                if self.singbox and self.singbox.poll() is None: self.singbox.terminate()
                self.singbox = None; self.last_proxy_fingerprint = ""
                self.apply_xray(None)
                for line in desired.get("lines") or []:
                    lines_status[str(line.get("id"))] = {"ready": True, "mode": "direct"}
                atomic_json(self.status_path, {"updated_at": int(time.time()), "enabled": False,
                                               "exits": {}, "lines": lines_status})
                return
            config, exits_state = self.build_proxy_config(proxy)
            configured = [x for x in exits_state.values() if x.get("mode") != "direct" and x.get("ready")]
            if configured:
                # REALITY made Xray load-bearing for ordinary exits, where it used to matter
                # only to the rare XHTTP node. Letting its failure escape here took every
                # country down with it — including exits that never touch Xray. Only the
                # countries it actually carries are failed; the rest still get their routes.
                try:
                    self.apply_xray(self.next_xray_config)
                except Exception as exc:
                    for country in self._xray_countries:
                        if country in exits_state:
                            exits_state[country] = {**exits_state[country], "ready": False,
                                                    "error": f"Xray is unavailable: {exc}"}
                    self.log(f"Xray failed; {len(self._xray_countries)} exit(s) affected: {exc}")
                try:
                    self.apply_singbox(config)
                finally:
                    self.release_tun_dns()
            else:
                self.apply_xray(None)
            # Ranking must come first: update_selected_nodes then reports the node this cycle
            # actually settled on, so proxy-status.json never shows the pre-selection default.
            self.process_reselect_requests(exits_state)
            # After the reselect pass: a country that just moved node has fresh sessions, and
            # nothing stale left worth closing.
            self.process_stalled_reports(exits_state)
            self.update_selected_nodes(exits_state)
            for line in desired.get("lines") or []:
                iid, country, host = str(line.get("id")), str(line.get("country") or "").lower(), str(line.get("epdg") or "")
                exit_state = exits_state.get(country) or {"ready": False, "terminal": True,
                                                           "error": f"no enabled {country.upper()} exit"}
                state = dict(exit_state)
                if not line.get("enabled", True):
                    state = {"ready": True, "mode": "disabled"}
                elif exit_state.get("ready") and exit_state.get("mode") != "direct":
                    try:
                        resolved = self.resolve(host)
                        if not resolved: raise RuntimeError("ePDG DNS returned no IPv4 address")
                        addresses = self.retained_epdg_addresses(f"{country}:{host}", resolved)
                        iface = exit_state["interface"]
                        for ip in addresses:
                            if ip in owners and owners[ip] != iface:
                                if ip not in resolved:
                                    # A retained address the carrier has since handed to another
                                    # country. The fresh answer wins; stop routing the old one.
                                    self.epdg_seen.get(f"{country}:{host}", {}).pop(ip, None)
                                    continue
                                raise RuntimeError(f"ePDG {ip} is already assigned to another country exit")
                            owners[ip] = iface; wanted.add((ip, iface))
                        state["epdg"] = host
                        # What DNS says right now vs everything kept routed for live tunnels.
                        state["addresses"] = resolved
                        state["routed_addresses"] = addresses
                    except Exception as exc:
                        state = {**state, "ready": False, "error": str(exc)}
                lines_status[iid] = state
            self.apply_routes(wanted)
        except Exception as exc:
            for line in desired.get("lines") or []:
                lines_status[str(line.get("id"))] = {"ready": False, "error": str(exc)}
            # build_proxy_config marks an exit ready once its config renders; a sing-box that
            # then refused to start leaves no listener behind it. Publishing those exits as
            # ready sent the UDP test at a socket nobody was serving and blamed the node.
            for country, state in exits_state.items():
                if state.get("mode") != "direct" and state.get("ready"):
                    exits_state[country] = {**state, "ready": False, "error": str(exc)}
        atomic_json(self.status_path, {"updated_at": int(time.time()), "enabled": True,
                                       "exits": exits_state, "lines": lines_status})

    def loop(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # Runs once, before the first pass: a restart that took this process with it can only
        # be completed by the process that comes back.
        self.settle_service_restart()
        while not self.stop:
            self.process_service_restart_request()
            self.reconcile_timezone()
            desired = read_json(self.desired_path)
            self.reconcile_proxy(desired)
            # Compare what this cycle concluded, not what it observed: timestamps and counters
            # differ every time and would defeat the comparison.
            fingerprint = json.dumps([desired, self.last_proxy_fingerprint], sort_keys=True)
            idle = fingerprint == self._last_conclusion
            self._last_conclusion = fingerprint
            self._sleep_for_work(IDLE_INTERVAL_SECONDS if idle else self.interval)

    def _input_mtimes(self) -> tuple:
        """Cheap change detector for the documents an operator action writes."""
        stamps = []
        for path in (self.desired_path, self.data / "config.yaml", self.reselect_path,
                     self.stalled_path):
            try:
                stamps.append(path.stat().st_mtime)
            except OSError:
                stamps.append(0.0)
        return tuple(stamps)

    def _sleep_for_work(self, seconds: float) -> None:
        """Wait, but wake immediately when an input document changes.

        Backing off must not make the gateway feel unresponsive: a settings save or a line
        start writes one of these files, and noticing that costs a stat rather than the
        fifteen subprocesses a full reconcile spends.
        """
        watched = self._input_mtimes()
        deadline = time.time() + seconds
        while not self.stop:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(self.interval, max(0.1, remaining)))
            if self._input_mtimes() != watched:
                return

    def request_stop(self):
        self.stop = True

    def close(self):
        self.request_stop()
        if self.singbox and self.singbox.poll() is None: self.singbox.terminate()
        if self.xray and self.xray.poll() is None: self.xray.terminate()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    app = Orchestrator(args.data.resolve(), args.repo.resolve(), args.interval, args.dry_run)
    signal.signal(signal.SIGTERM, lambda *_: app.request_stop())
    signal.signal(signal.SIGINT, lambda *_: app.request_stop())
    try: app.loop()
    finally: app.close()


if __name__ == "__main__":
    main()
