"""/api/settings, /api/system/* and the live event WebSocket /ws."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import WebSocket, WebSocketDisconnect, HTTPException, APIRouter

from .. import config as cfg
from .. import engine, egress, operations, sysinfo

from ..version import SOURCE_URL, VERSION
from .. import state as app_state

router = APIRouter()


log = logging.getLogger("vowifi.main")


# ----------------------------- settings -----------------------------


@router.get("/api/settings")
def api_get_settings():
    return {key: value for key, value in cfg.get_settings().items() if key != "system_name"}


@router.put("/api/settings")
def api_put_settings(body: dict):
    # Ignore the legacy field from older cached clients. Product identity is fixed.
    body.pop("system_name", None)
    proxy = body.get("proxy")
    if proxy is not None:
        if not isinstance(proxy, dict) or not isinstance(proxy.get("profiles", {}), dict) \
                or not isinstance(proxy.get("exits", {}), dict):
            raise HTTPException(400, "invalid proxy library")
        profiles = proxy.get("profiles") or {}
        for profile_id, profile in profiles.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(profile_id)) \
                    or not isinstance(profile, dict):
                raise HTTPException(400, "invalid proxy profile id")
            if str(profile.get("type") or "") not in {"subscription", "node", "socks5", "existing"}:
                raise HTTPException(400, "invalid proxy profile type")
            if not str(profile.get("name") or "").strip():
                raise HTTPException(400, "proxy profile name is required")
        for country, exit_cfg in (proxy.get("exits") or {}).items():
            if not egress.normalize_country(country) or not isinstance(exit_cfg, dict):
                raise HTTPException(400, "invalid country exit")
            profile_id = str(exit_cfg.get("profile_id") or "")
            if profile_id and profile_id not in profiles:
                raise HTTPException(400, f"country exit references unknown proxy {profile_id!r}")
    if "timezone" in body:
        try:
            ZoneInfo(str(body.get("timezone") or ""))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(400, "unknown timezone") from exc
    if "max_lines" in body:
        value = body.get("max_lines")
        if isinstance(value, bool) or value not in range(1, cfg.PRODUCT_MAX_LINES + 1):
            raise HTTPException(400, f"max_lines must be 1-{cfg.PRODUCT_MAX_LINES}")
    # Settings that belonged to modem hardware are not accepted any more.
    body.pop("device_defaults", None)
    body.pop("hardware", None)
    saved = cfg.update_settings(body)
    egress.publish(settings=saved)
    if "max_lines" in body:
        # A lowered limit takes effect now: lines beyond it stop, their records stay.
        for inst in cfg.list_instances():
            iid = str(inst.get("id") or "")
            if iid and not cfg.line_allowed(iid):
                try:
                    if engine.stop(iid):
                        log.warning("stopped line %s: it is beyond the new limit of %s line(s)",
                                    iid, cfg.max_lines(saved))
                except Exception as exc:  # noqa: BLE001 - the setting is saved either way
                    log.error("could not stop line %s beyond the line limit: %s", iid, exc)
    return {key: value for key, value in saved.items() if key != "system_name"}


@router.get("/api/system/status")
def api_system_status():
    settings = cfg.get_settings()
    # Served from the poller's sample: collecting here would shell out to vcgencmd/dmesg on
    # every page load of an already power-constrained box.
    host = app_state.hub.host_snapshot or sysinfo.collect(cfg.DATA_DIR)
    return {
        "system_name": "MDD Sim Gateway",
        "host": host,
        "host_alerts": app_state.hub.host_alerts if app_state.hub.host_snapshot else sysinfo.alerts(host),

        "timezone": settings.get("timezone") or "UTC",
        "version": VERSION,
        "deployment": {"host_restart_available": True},
        "line_limit": {"max_lines": cfg.max_lines(settings),
                       "product_max": cfg.PRODUCT_MAX_LINES},
        "repository_url": SOURCE_URL,
        "backups": operations.list_local_backups(),
        "security": {
            "https": True,
            "certificate_mode": "self-signed" if (settings.get("tls") or {}).get("self_signed") else "custom",
            "audit_enabled": bool((settings.get("security") or {}).get("audit_enabled", True)),
        },
    }


@router.delete("/api/system/host-alerts")
def api_host_alerts_clear():
    """Acknowledge active host alerts until each condition genuinely clears."""
    if app_state.hub.host_alert_state is None:
        app_state.hub.host_alert_state = app_state._load_host_alert_state()
    now = time.time()
    cleared = []
    for item in app_state.hub.host_alerts:
        code = str(item.get("code") or "")
        if not code:
            continue
        entry = app_state.hub.host_alert_state.setdefault(code, {})
        entry["acknowledged"] = True
        entry["acknowledged_at"] = now
        entry.setdefault("at", now)
        cleared.append(code)
    app_state.hub.host_alerts = []
    app_state._save_host_alert_state(app_state.hub.host_alert_state)
    return {"ok": True, "cleared": cleared}


@router.post("/api/system/backups")
async def api_system_backup():
    settings = cfg.get_settings()
    return await asyncio.to_thread(
        operations.create_local_backup, "mdd-sim-gateway")


@router.delete("/api/system/backups/{name}")
async def api_system_backup_delete(name: str):
    try:
        return await asyncio.to_thread(operations.delete_local_backup, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc


@router.post("/api/system/maintenance")
async def api_system_maintenance(body: dict):
    action = str(body.get("action") or "")
    if action == "refresh_egress":
        return app_state.refresh_egress()
    if action == "restart_lines":
        restarted, failed = [], {}
        # A saved line may be offline because its SIM is absent or its physical device has
        # VoWiFi disabled. Snapshot only engines that were actually running when the
        # operator requested a restart; never turn a restart operation into "start all saved".
        running = []
        for inst in cfg.list_instances():
            if await asyncio.to_thread(engine.is_running, str(inst["id"])):
                running.append(inst)
        for inst in running:
            iid = str(inst["id"])
            try:
                await asyncio.to_thread(engine.stop, iid)
                await app_state.hub.drop_ami(iid)
                await asyncio.to_thread(app_state._start_engine_checked, inst, cfg.get_settings(),
                                        dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
                restarted.append(iid)
            except Exception as exc:
                failed[iid] = str(getattr(exc, "detail", exc))
        return {"ok": not failed, "action": action, "restarted": restarted, "failed": failed}
    # The root orchestrator consumes the request; this process may be among those restarted.
    scope = {"restart_control": "control", "restart_services": "services",
             "restart_host": "host"}.get(action)
    if scope:
        result = await asyncio.to_thread(operations.request_service_restart, scope)
        return {**result, "action": action}
    raise HTTPException(400, "unknown maintenance action")


@router.get("/api/system/maintenance/restart-progress")
def api_system_restart_progress():
    """Whether the orchestrator has taken the restart request; the restart itself is observed
    by the API going away and coming back."""
    return operations.service_restart_status()


# ----------------------------- WebSocket -----------------------------
@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Signed-out browsers are closed with 4401 by the gateway middleware (gate.py).
    await ws.accept()
    app_state.hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive / ignore inbound
    except WebSocketDisconnect:
        app_state.hub.clients.discard(ws)
    except Exception:
        app_state.hub.clients.discard(ws)
