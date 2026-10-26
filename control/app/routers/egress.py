"""/api/egress/*: country exit status, refresh and tests."""
from __future__ import annotations

import asyncio
import time
from fastapi import HTTPException, APIRouter

from .. import config as cfg
from .. import egress
from .. import state as app_state

router = APIRouter()


@router.get("/api/egress/status")
def api_egress_status():
    return egress.status()


@router.post("/api/egress/refresh")
def api_egress_refresh():
    return app_state.refresh_egress()


def _egress_not_ready_reason(country: str, latest: dict) -> str:
    """Say why an exit never became testable, instead of blaming the node pool.

    "no healthy UDP-capable node is ready" was returned for a disabled exit, a master
    switch left off and an orchestrator that had not published anything — three different
    problems with three different fixes, none of them the node the operator just pasted.
    """
    if latest.get("error"):
        return str(latest["error"])
    proxy = cfg.get_settings().get("proxy") or {}
    if not proxy.get("enabled"):
        return ("country proxy routing is switched off — enable it before testing an exit")
    if not ((proxy.get("exits") or {}).get(country) or {}).get("enabled"):
        return f"the {country.upper()} exit is configured but not enabled"
    document = egress.status()
    updated_at = float(document.get("updated_at") or 0)
    if not updated_at:
        return ("the host orchestrator has not published any exit status yet — check that "
                "mdd-sim-gateway-orchestrator is running")
    age = time.time() - updated_at
    if age > 60:
        return ("the host orchestrator last published exit status "
                f"{int(age)}s ago — it is not reconciling; check its service log")
    return ("the exit did not come up within 25s and reported no error — check the "
            "orchestrator log for sing-box startup failures")


async def _test_egress_country(country: str):
    country = egress.normalize_country(country)
    exits = (cfg.get_settings().get("proxy") or {}).get("exits") or {}
    if not country or country not in exits:
        raise HTTPException(404, "country exit is not configured")
    egress.publish()
    deadline = time.monotonic() + 25
    latest = {}
    while time.monotonic() < deadline:
        latest = (egress.status().get("exits") or {}).get(country) or {}
        if latest.get("ready"):
            host, port = str(latest.get("proxy_host") or ""), int(latest.get("proxy_port") or 0)
            if not host or not port:
                raise HTTPException(503, "country exit has no UDP test endpoint")
            try:
                latency = await asyncio.to_thread(egress.test_udp_proxy, host, port)
            except egress.EgressError as exc:
                raise HTTPException(503, str(exc)) from exc
            return {"ok": True, "country": country, "node": latest.get("node") or "",
                    "interface": latest.get("interface") or "", "latency_ms": latency}
        if latest.get("error"):
            break
        await asyncio.sleep(.5)
    raise HTTPException(503, await asyncio.to_thread(
        _egress_not_ready_reason, country, latest))


@router.post("/api/egress/profile/{profile_id}/test")
async def api_egress_profile_test(profile_id: str, body: dict | None = None):
    saved = ((cfg.get_settings().get("proxy") or {}).get("profiles") or {}).get(profile_id)
    profile = body if isinstance(body, dict) and body else saved
    if not isinstance(profile, dict):
        raise HTTPException(404, "save this proxy before testing it")
    if profile.get("type") not in {"node", "socks5"}:
        raise HTTPException(400, "only individual nodes and SOCKS5 proxies can be tested here")
    parsed = await asyncio.to_thread(egress.describe_proxy_profile, profile)
    try:
        latency = await asyncio.to_thread(egress.test_proxy_profile, profile)
    except egress.EgressError as exc:
        # The parsed view travels with the failure: it is what tells an operator whether the
        # gateway read their link the same way their other client did.
        raise HTTPException(503, {"message": str(exc), "parsed": parsed}) from exc
    return {"ok": True, "profile_id": profile_id, "latency_ms": latency, "parsed": parsed}


@router.post("/api/egress/{country}/test")
async def api_egress_test(country: str):
    return await _test_egress_country(country)
