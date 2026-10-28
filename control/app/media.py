"""How call media reaches a line's Asterisk from the browser softphone.

Only direct mode remains (docs/SLIM_PLAN.md D4). The orchestrator DNATs each line's RTP range
from the host's own addresses into the line's namespace (host/mdd_netns.py), and Asterisk
rewrites its ICE host candidate to the host's LAN address (engine/templates/rtp.conf.j2), so a
browser on the LAN reaches the engine directly. Clients get no ICE servers.
"""
from __future__ import annotations

DIRECT = "direct"


def mode() -> str:
    return DIRECT


def provisioning(principal: str, request_host: str) -> dict:
    """What a softphone needs for media. The shape predates the removal of relay mode, and
    the WebUI still reads every key."""
    return {"media_mode": DIRECT, "ice_servers": [], "ice_transport_policy": "all",
            "relay_ready": None}
