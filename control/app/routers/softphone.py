"""Browser softphone and call log: softphone config and WebSocket, call, hangup, calls,
/api/media."""
from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket, HTTPException, Request, APIRouter

from .. import config as cfg
from .. import store, engine, softphone_ws, media
from .. import state as app_state

log = logging.getLogger("vowifi.main")
router = APIRouter()


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
    # Media needs nothing: the line's RTP range is reachable on the host's LAN address.
    media_prov = media.provisioning(str(iid), request.url.hostname or host)
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
    """The media mode, for display. Direct is the only one left."""
    return {"mode": media.mode()}


@router.websocket("/api/instances/{iid}/softphone/ws")
async def ws_softphone(ws: WebSocket, iid: str):
    """The browser softphone's SIP-over-WebSocket, relayed to the line's engine.

    The session and the page's origin are checked by the gateway middleware before this runs
    (gate.py). A line that cannot take the call is refused after accepting, with an explicit
    close code and reason (softphone_ws.REFUSAL_REASONS), so the page can say why and stop
    retrying; only a client that does not speak SIP at all fails the handshake."""
    if not softphone_ws.offers_sip(ws.headers.get("sec-websocket-protocol")):
        await ws.close(code=1008)
        return
    inst = cfg.get_instance(iid)
    webrtc = ((inst or {}).get("sip") or {}).get("webrtc") or {}
    if not inst:
        return await softphone_ws.refuse(ws, softphone_ws.REFUSED_NO_LINE)
    if not webrtc.get("enable", True):
        return await softphone_ws.refuse(ws, softphone_ws.REFUSED_DISABLED)
    try:
        runtime = await asyncio.to_thread(engine.unit_runtime, str(iid))
    except Exception as exc:  # noqa: BLE001 - an id outside 1-10, or systemctl unavailable
        log.warning("softphone relay: cannot inspect engine %s: %s", iid, exc)
        return await softphone_ws.refuse(ws, softphone_ws.REFUSED_UNAVAILABLE)
    if not runtime["running"] or not runtime["ip"]:
        return await softphone_ws.refuse(ws, softphone_ws.REFUSED_NOT_RUNNING)
    await softphone_ws.relay(ws, softphone_ws.engine_url(runtime["ip"]))
