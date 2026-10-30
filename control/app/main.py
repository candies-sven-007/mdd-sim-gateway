"""
main.py - MDD Sim Gateway control surface (FastAPI).

Serves the management REST API + WebSocket live feed + the built WebUI, and (for the
browser softphone) relays signalling. Runs as the unprivileged user mdd; starts and stops the
per-line engine units through mdd-enginectl (engine.py) and talks to Asterisk over AMI
(ami.py). HTTPS with an auto-generated self-signed cert by default.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfg
from . import store, engine, egress, gate
from . import state as app_state
from . import tasks as app_tasks
from .routers import auth as auth_routes
from .routers import bluetooth as bluetooth_routes
from .routers import egress as egress_routes
from .routers import engine as engine_routes
from .routers import esim as esim_routes
from .routers import lines as lines_routes
from .routers import logs as logs_routes
from .routers import sms as sms_routes
from .routers import softphone as softphone_routes
from .routers import system as system_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vowifi.main")

WEBUI_DIR = os.environ.get("MDD_WEBUI", os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "webui", "dist"))

HOST_ALERT_TEXT = {
    "undervoltage_now": "供电电压不足（正在发生）。网口、蜂窝模块和读卡器共用同一路供电，"
                        "欠压会让所有线路同时掉线。请更换更强的电源或为 USB 设备加独立供电。",
    "undervoltage_seen": "检测到历史欠压事件。所有线路会在欠压瞬间同时中断。",
    "throttled_now": "CPU 正在降频/节流，处理能力下降。",
    "temperature_high": "主机温度过高，已接近或进入热节流。",
    "disk_critical": "磁盘空间即将耗尽，可能损坏历史数据并导致线路无法写入运行状态。",
    "disk_low": "磁盘空间偏低。",
    "swap_pressure": "正在频繁换页。在 SD 卡上换页会拖慢所有操作并造成状态查询超时。",
    "default_route_changed": "默认路由在上行之间发生了切换，所有出站连接的源地址随之改变。",
}


def _host_alert_summary(alerts: list[dict]) -> str:
    lines = []
    for item in alerts:
        detail = ", ".join(f"{k}={v}" for k, v in (item.get("detail") or {}).items())
        text = HOST_ALERT_TEXT.get(item["code"], item["code"])
        lines.append(f"[{item['severity']}] {text}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    # A saved configuration may hold more lines than the current limit (settings.max_lines).
    # Keep every saved record, but stop excess engines before background recovery begins.
    for saved_line in cfg.list_instances():
        iid = str(saved_line.get("id") or "")
        if iid and not cfg.line_allowed(iid):
            try:
                await asyncio.to_thread(engine.stop, iid)
                log.warning("stopped line %s because it exceeds the SIM line limit", iid)
            except Exception as exc:  # noqa: BLE001 - startup must continue to surface status
                log.error("could not stop over-limit line %s: %s", iid, exc)
    # Re-publish after every manager restart so the host orchestrator can reconstruct routes
    # from persistent config without waiting for a settings edit/line restart.
    egress.publish()
    await app_state.hub.runtime.start(app_state.hub.runtime_changed)
    poller = asyncio.create_task(app_tasks.status_poller())
    monitor = asyncio.create_task(app_tasks.card_monitor())
    host_poller = asyncio.create_task(app_tasks.host_health_poller())
    yield
    poller.cancel()
    monitor.cancel()
    host_poller.cancel()
    # Reap the cancelled tasks (the monitor may be parked in a to_thread wait for up to
    # its timeout; awaiting keeps shutdown deterministic instead of leaking the error).
    await asyncio.gather(poller, monitor, host_poller, return_exceptions=True)
    await app_state.hub.runtime.close()
    for c in app_state.hub.ami.values():
        await c.close()


# No /docs, /redoc or /openapi.json: they sit outside /api/, where the gate lets anyone
# through, and would hand out the whole API surface (SLIM_PLAN P-1).
app = FastAPI(title="MDD Sim Gateway", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)

# Authentication for every API request and WebSocket handshake; see gate.py.
app.add_middleware(gate.Gate)
app.include_router(auth_routes.router)
app.include_router(lines_routes.router)
app.include_router(engine_routes.router)
app.include_router(softphone_routes.router)
app.include_router(sms_routes.router)
app.include_router(esim_routes.router)
app.include_router(egress_routes.router)
app.include_router(logs_routes.router)
app.include_router(system_routes.router)
app.include_router(bluetooth_routes.router)


def _audit_client(request: Request, settings: dict) -> str:
    peer = request.client.host if request.client else ""
    if gate.trusted_proxy(peer, settings):
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


@app.middleware("http")
async def audit_mutations(request: Request, call_next):
    response = await call_next(request)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        settings = cfg.get_settings()
        _write_audit_record({"at": int(time.time()), "method": request.method,
                             "path": request.url.path, "status": response.status_code,
                             "client": _audit_client(request, settings),
                             "actor": gate.current(request).label}, settings)
    return response


def _write_audit_record(record: dict, settings: dict | None = None) -> None:
    """Append one administrative action from the authenticated control surface."""
    settings = settings if settings is not None else cfg.get_settings()
    if not (settings.get("security") or {}).get("audit_enabled", True):
        return
    path = os.path.join(cfg.DATA_DIR, "audit", "operations.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("could not write administrative audit record")


def _local_tz() -> ZoneInfo:
    """The user's configured zone, for timestamps shown in local time."""
    try:
        return ZoneInfo(str(cfg.get_settings().get("timezone") or "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


# ----------------------------- static WebUI -----------------------------
# The page that names which build to load must be revalidated every time; the files it names
# never change, because their names contain a hash of their contents.
#
# Without this the upgrade does not arrive. The answers carry an ETag but no Cache-Control, so a
# client is free to guess how long they stay fresh -- and a WebView, which has no reload button,
# can keep showing the previous build until its cache is evicted. That looks exactly like an
# upgrade that did not apply.
INDEX_CACHE_CONTROL = "no-cache"
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


class _HashedAssets(StaticFiles):
    """Static files whose names carry a content hash, so a name maps to one immutable body."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        return response


if os.path.isdir(WEBUI_DIR):
    app.mount("/assets", _HashedAssets(directory=os.path.join(WEBUI_DIR, "assets")),
              name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Unknown API paths must stay real 404s. Returning index.html here makes clients parse a
        # missing endpoint as a successful empty feature (and hid the removed stack API).
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "API endpoint not found"}, status_code=404)
        candidate = os.path.join(WEBUI_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            # Everything else at the root -- the logo, the icons -- is small, rarely changed and
            # not hashed, so it revalidates like the page itself.
            return FileResponse(candidate, headers={"Cache-Control": INDEX_CACHE_CONTROL})
        index = os.path.join(WEBUI_DIR, "index.html")
        if os.path.isfile(index):
            return FileResponse(index, headers={"Cache-Control": INDEX_CACHE_CONTROL})
        return JSONResponse({"error": "webui not built"}, status_code=404)
