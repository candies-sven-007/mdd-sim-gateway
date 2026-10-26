"""Browser softphone and call log: softphone config and WebSocket, call, hangup, calls,
/api/media."""
from __future__ import annotations

import asyncio
import logging

import docker
from fastapi import WebSocket, HTTPException, Request, APIRouter

from .. import config as cfg
from .. import store, engine, softphone_ws, media
from .. import state as app_state

log = logging.getLogger("vowifi.main")
router = APIRouter()


def _line_media_report(iid: str) -> dict:
    """The engine's own report on its media interface (engine/entrypoint.sh), relay mode."""
    return engine.read_run_json(iid, "media.json") or {}


def _line_media_state(iid: str) -> str:
    return str(_line_media_report(iid).get("state") or "starting")


# ----------------------------- Calls -----------------------------
@router.get("/api/instances/{iid}/calls")
def api_calls(iid: str):
    return {"calls": store.list_calls(iid)}


@router.post("/api/instances/{iid}/calls/delete")
async def api_calls_delete(iid: str, body: dict):
    """Delete call-log entries. Body: {ids:[...]} for specific calls or {all:true} to clear
    the whole log. Broadcasts a refresh so open Softphone views reload the list."""
    if body.get("all"):
        n = await asyncio.to_thread(store.clear_calls, iid)
    elif body.get("ids"):
        n = await asyncio.to_thread(store.delete_calls, iid, body["ids"])
    else:
        raise HTTPException(400, "provide ids or all")
    await app_state.hub.broadcast({"type": "call", "instance": str(iid), "deleted": n})
    return {"ok": True, "deleted": n}


async def place_call_on_line(iid: str, to: str, from_endpoint: str = "webrtc") -> dict:
    """Ring the browser endpoint and bridge it to `to` over IMS, logging the call."""
    ami = await app_state.hub.ami_for(iid)
    if not ami:
        return {"ok": False, "unavailable": True, "error": "instance not running"}
    res = await ami.originate(to, from_endpoint)
    store.add_call(iid, "out", to, status="ringing")
    return res


async def hangup_on_line(iid: str) -> dict:
    ami = await app_state.hub.ami_for(iid)
    if not ami:
        return {"ok": False, "unavailable": True, "error": "instance not running"}
    return await ami.hangup_all()


@router.post("/api/instances/{iid}/call")
async def api_call(iid: str, body: dict):
    result = await place_call_on_line(iid, body["to"], body.get("from_endpoint", "webrtc"))
    if result.pop("unavailable", False):
        raise HTTPException(409, result["error"])
    return result


@router.post("/api/instances/{iid}/hangup")
async def api_hangup(iid: str):
    result = await hangup_on_line(iid)
    if result.pop("unavailable", False):
        raise HTTPException(409, result["error"])
    return result


@router.get("/api/instances/{iid}/softphone")
def api_softphone(iid: str, request: Request):
    """Provisioning for the browser softphone (JsSIP over the same-origin WebSocket relay)."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    sip = inst.get("sip", {}) or {}
    wr = sip.get("webrtc", {}) or {}
    host = (request.headers.get("host") or "").split(":")[0] or request.url.hostname
    # Media: nothing in direct mode (the engine's published RTP ports); in relay mode the TURN
    # relay with fresh credentials, which clients re-read before every call. The relay is
    # named by the host the client reached this API by, unless the operator set another.
    media_prov = media.provisioning(str(iid), request.url.hostname or host)
    if media_prov["media_mode"] == media.RELAY:
        media_prov["relay_ready"] = bool(media_prov["relay_ready"]) and \
            _line_media_state(str(iid)) == "ready"
    return {
        "enabled": bool(wr.get("enable", True)),
        "username": wr.get("username", "webrtc"),
        "password": wr.get("password", ""),
        # Same origin as the WebUI, so it works unchanged behind a reverse proxy.
        "ws_path": softphone_ws.path(iid),
        "host": host,
        "realm": cfg.ims_realm(inst["mcc"], inst["mnc"]),
        **media_prov,
    }


@router.get("/api/media")
def api_media():
    """The media mode and the relay's state, for display. The mode is switched by the
    installer (install.sh media), since it rebuilds every line."""
    state = media.load_state()
    current = media.mode(state)
    result = {"mode": current, "relay": media.relay_status()}
    if current == media.RELAY:
        lines, line_filters = {}, {}
        for iid in (str(inst["id"]) for inst in cfg.list_instances()):
            line_mode = engine.media_mode_of(iid)
            if line_mode == media.RELAY_PENDING:
                lines[iid] = "no_media_network"
            elif line_mode == media.RELAY:
                report = _line_media_report(iid)
                lines[iid] = str(report.get("state") or "starting")
                # What the engine actually loaded; absent from engines older than the
                # iptables-legacy fallback, and while the line is still starting.
                line_filters[iid] = str(report.get("filter") or "")
        kind = media.recorded_filter(state)
        result.update({
            "port": state.get("port"),
            "public_host": state.get("public_host") or "",
            "public_port": state.get("public_port"),
            "lines": lines,
            # The engines' media filter, as probed when relay mode was enabled, and whether it
            # tells the browser leg from the IMS leg (iptables-legacy goes by port only).
            "filter": kind,
            "filter_separates_legs": media.filter_separates_legs(kind),
            "line_filters": line_filters,
        })
    return result


@router.websocket("/api/instances/{iid}/softphone/ws")
async def ws_softphone(ws: WebSocket, iid: str):
    """The browser softphone's SIP-over-WebSocket, relayed to the line's engine.

    The session and the page's origin are checked by the gateway middleware before this runs
    (gate.py). Rejections close before accepting (the browser sees a failed handshake)."""
    inst = cfg.get_instance(iid)
    webrtc = ((inst or {}).get("sip") or {}).get("webrtc") or {}
    if not inst or not webrtc.get("enable", True) or \
            not softphone_ws.offers_sip(ws.headers.get("sec-websocket-protocol")):
        await ws.close(code=1008)
        return
    try:
        runtime = await asyncio.to_thread(engine.container_runtime, str(iid))
    except docker.errors.DockerException as exc:
        log.warning("softphone relay: cannot inspect engine %s: %s", iid, exc)
        await ws.close(code=1013)
        return
    if not runtime["running"] or not runtime["ip"]:
        await ws.close(code=1013)
        return
    await softphone_ws.relay(ws, softphone_ws.engine_url(runtime["ip"]))
