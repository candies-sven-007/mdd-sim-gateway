"""Line logs and the redacted support bundle."""
from __future__ import annotations

import asyncio
import os
import time
from fastapi import APIRouter
from fastapi.responses import Response

from .. import config as cfg
from .. import engine, egress, device_state, operations
from .. import state as app_state

router = APIRouter()


def _line_diagnostics(inst: dict) -> dict:
    """One line's identity plus the last sampled verdict on it.

    ``reason_code`` is what the health policy acted on, and the retry budget says how close
    that line is to being torn down and rebuilt — the pair that distinguishes a carrier
    rejecting the attach from a tunnel that comes up fine and never registers.
    """
    iid = str(inst.get("id"))
    entry = {"id": inst.get("id"), "name": inst.get("name")}
    status = app_state.hub.status_cache.get(iid) or {}
    if not status:
        entry["note"] = "no status sampled yet for this line"
        return entry
    detail = status.get("detail") or {}
    health = (app_state.hub.health or {}).get(iid) or {}
    now = time.monotonic()
    entry.update({
        "state": status.get("state"),
        "reason_code": status.get("reason_code"),
        "reason": status.get("reason"),
        "retry": status.get("retry") or {},
        "registration": detail.get("registration"),
        "active_channels": detail.get("active_channels"),
        "frozen_code": health.get("frozen_code"),
        # Elapsed rather than absolute: these are monotonic clock readings, meaningless to a
        # reader, but their distance from now is exactly the retry-budget story.
        "failing_for_seconds": (round(now - health["fail_start"], 1)
                                if health.get("fail_start") else None),
        "ok_for_seconds": (round(now - app_state.hub.ok_since[iid], 1)
                           if iid in app_state.hub.ok_since else None),
    })
    return entry


@router.get("/api/diagnostics/support-bundle")
async def api_support_bundle():
    settings = cfg.get_settings()
    documents = {"devices": device_state.hardware(), "egress": egress.status(),
                 # What pcscd is actually exposing right now — the one fact every card-path
                 # report needed and the bundle never carried. Passive: monitor state only.
                 "readers": [{"name": item.get("name"), "present": item.get("present"),
                              "hardware_kind": item.get("hardware_kind")}
                             for item in app_state.hub.cards_list()],
                 # Why each line is in the state it is in. A bundle full of tunnel logs that
                 # all end at "CONNECTED" says nothing about why the line kept being rebuilt;
                 # the classified reason and the retry budget are what name the loop.
                 # Passive: the background sampler's last result, never a fresh probe.
                 "instances": [_line_diagnostics(item) for item in cfg.list_instances()]}
    content = await asyncio.to_thread(
        operations.support_bundle, documents,
        (settings.get("maintenance") or {}).get("support_bundle_log_lines", 500))
    headers = {"Content-Disposition": 'attachment; filename="vowifi-support-redacted.zip"'}
    return Response(content, media_type="application/zip", headers=headers)


@router.get("/api/instances/{iid}/logs")
def api_instance_logs(iid: str, tail: int = 200):
    return {"engine": engine.logs(iid, tail),
            "charon": _read_run_text(iid, "charon.log", 200),
            # Survives container rebuilds, unlike the two above.
            "diagnostics": _read_log_text(iid, "diagnostics.jsonl", 50)}


def _read_run_text(iid, name, tail):
    return _read_instance_text(iid, "run", name, tail)


def _read_log_text(iid, name, tail):
    return _read_instance_text(iid, "logs", name, tail)


def _read_instance_text(iid, folder, name, tail):
    path = os.path.join(cfg.DATA_DIR, "instances", str(iid), folder, name)
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-tail:])
    except Exception:
        return ""
