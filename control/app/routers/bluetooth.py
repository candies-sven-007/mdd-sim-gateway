"""/api/bluetooth/*: pair and drive a Bluetooth speaker from the web UI.

The control plane runs as the unprivileged user mdd and never talks to bluez or PulseAudio
itself. Every operation is one call of the root helper, `sudo -n mdd-btctl <action> [arg]`
(packaging/sbin/mdd-btctl), which validates its arguments again, serialises changes and
answers with JSON. Authentication is the gateway's own: like every /api/ path these routes
pass the session gate (gate.py) and authz before they run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess

from fastapi import APIRouter, HTTPException

log = logging.getLogger("vowifi.bluetooth")
router = APIRouter()

BTCTL = os.environ.get("MDD_BTCTL", "/usr/local/sbin/mdd-btctl")
MAC_RE = re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}")
ACTIONS = ("pair", "connect", "disconnect", "remove")
# Pairing scans, pairs, trusts and connects in one bluez session (up to ~40 s).
PAIR_TIMEOUT = 40
TIMEOUT = 20
SCAN_SECONDS = 30

# Changes go one at a time from here too, so a second click waits instead of failing on the
# helper's lock. Status is not behind it: the page polls it while a pairing runs.
_lock = asyncio.Lock()


class HelperUnavailable(RuntimeError):
    """sudo, the helper or its sudoers rule is missing, or the helper failed outright."""


def _check_mac(mac: str) -> str:
    mac = str(mac or "").upper()
    if not MAC_RE.fullmatch(mac):
        raise HTTPException(400, "invalid MAC address")
    return mac


def _call(*args: str, timeout: float = TIMEOUT) -> dict:
    command = ["sudo", "-n", BTCTL, *args]
    try:
        done = subprocess.run(command, text=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError as exc:
        raise HelperUnavailable("sudo is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"mdd-btctl {args[0]} did not finish in {timeout:.0f} s") from exc
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        detail = (done.stderr or done.stdout or "").strip()[:300]
        raise HelperUnavailable(detail or f"mdd-btctl exited with {done.returncode}") from None
    if done.returncode == 64:
        raise HTTPException(400, answer.get("message") or "refused")
    if not isinstance(answer, dict):
        raise HelperUnavailable("mdd-btctl answered something that is not an object")
    return answer


async def _run(*args: str, timeout: float = TIMEOUT) -> dict:
    try:
        return await asyncio.to_thread(_call, *args, timeout=timeout)
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except HelperUnavailable as exc:
        raise HTTPException(503, f"Bluetooth control is unavailable: {exc}") from exc


def _outcome(answer: dict) -> dict:
    if not answer.get("ok"):
        raise HTTPException(502, answer.get("message") or "the Bluetooth action failed")
    return {"ok": True, "message": answer.get("message") or ""}


@router.get("/api/bluetooth/status")
async def api_bluetooth_status():
    """Controller, devices, the speaker's volume and the autoconnect address.

    A host without bluez, PulseAudio tools or the helper answers available=false with the
    reason, so the page can explain itself instead of failing.
    """
    try:
        answer = await asyncio.to_thread(_call, "status")
    except (HelperUnavailable, TimeoutError) as exc:
        return {"available": False, "reason": str(exc)}
    if not answer.get("available"):
        return {"available": False, "reason": answer.get("reason") or "unavailable"}
    return answer


@router.post("/api/bluetooth/scan")
async def api_bluetooth_scan():
    async with _lock:
        _outcome(await _run("scan"))
    return {"scanning": True, "seconds": SCAN_SECONDS}


@router.post("/api/bluetooth/devices/{mac}/{action}")
async def api_bluetooth_device(mac: str, action: str):
    if action not in ACTIONS:
        raise HTTPException(400, f"action must be one of: {', '.join(ACTIONS)}")
    mac = _check_mac(mac)
    async with _lock:
        answer = await _run(action, mac,
                            timeout=PAIR_TIMEOUT + 5 if action == "pair" else TIMEOUT)
    return _outcome(answer)


@router.put("/api/bluetooth/volume")
async def api_bluetooth_volume(body: dict):
    volume, muted = body.get("volume"), body.get("muted")
    if volume is not None and (isinstance(volume, bool) or not isinstance(volume, int)
                               or not 0 <= volume <= 100):
        raise HTTPException(400, "volume must be an integer 0-100")
    if muted is not None and not isinstance(muted, bool):
        raise HTTPException(400, "muted must be true or false")
    async with _lock:
        if volume is not None:
            _outcome(await _run("volume", str(volume)))
        if muted is not None:
            _outcome(await _run("mute", "1" if muted else "0"))
    status = await api_bluetooth_status()
    return status.get("sink") if status.get("available") else None


@router.put("/api/bluetooth/autoconnect")
async def api_bluetooth_autoconnect(body: dict):
    mac = body.get("mac")
    mac = _check_mac(mac) if mac else None
    async with _lock:
        _outcome(await _run("autoconnect", mac or "none"))
    return {"autoconnect": mac}
