"""Saved records for the physical smart-card readers.

A reader's id is derived from its USB port. Its record holds what the operator set for it --
a display name and the IMEI the line on it presents to the carrier -- and never SIM identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import config as cfg

ROOT = os.path.join(cfg.DATA_DIR, "orchestrator")
HARDWARE = os.path.join(ROOT, "devices-hardware.json")


def native_reader_devices(cards: list[dict]) -> dict[str, dict]:
    """Return stable VoWiFi devices for the USB PC/SC readers in the card monitor."""
    result = {}
    for card_info in cards:
        # The vsmartcard package's own reader definition is a software endpoint, never a
        # reader an operator plugged in.
        if str(card_info.get("name") or "").startswith("Virtual PCD"):
            continue
        if card_info.get("hardware_kind") != "reader":
            continue
        identity = str(card_info.get("reader_port") or card_info.get("name") or "")
        if not identity:
            continue
        device_id = "reader-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        result[device_id] = card_info
    return result


def native_vowifi_capability(desired: bool, running: bool, line_status: dict | None) -> dict:
    """Map a native reader's engine state without inventing a cellular/bridge dependency."""
    if not desired:
        return {"desired": False, "actual": "stopping" if running else "off", "reason": ""}
    if not running:
        return {"desired": True, "actual": "degraded",
                "reason": "VoWiFi is configured but the line is not running"}
    raw = str((line_status or {}).get("state") or (line_status or {}).get("label") or "").lower()
    if raw in {"ok", "working", "registered"}:
        return {"desired": True, "actual": "on", "reason": ""}
    if raw in {"error", "failed", "stopped"}:
        return {"desired": True, "actual": "error",
                "reason": str((line_status or {}).get("reason") or "VoWiFi failed")}
    return {"desired": True, "actual": "starting", "reason": ""}


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError, TypeError):
        return default


def _write(path: str, value):
    os.makedirs(ROOT, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def hardware() -> dict[str, dict]:
    """User-managed physical-device metadata; never contains SIM identity."""
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    return {str(device_id): dict(record) for device_id, record in devices.items()
            if str(device_id) and isinstance(record, dict)}


def migrate_reader_records(live_readers: dict[str, dict]) -> list[tuple[str, str]]:
    """Move a saved reader record onto the id its hardware now derives.

    A reader's id comes from its USB port, so moving it to a hub mints a new one and strands
    the old record: the control plane unions saved records into its device list, rendering a
    connected reader twice — once live, once permanently offline, with nothing in the UI able
    to remove the copy.

    The record is moved rather than dropped because it carries the IMEI this line presents to
    the carrier, and the line refreshes that from here on every start. Matching is on the
    reader's own name, which contains its serial and therefore survives a change of port; a
    record is only moved when exactly one stranded record and one unclaimed live reader share
    that name, so an ambiguous set is left untouched for a person to resolve.
    """
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    saved_readers = {device_id: record for device_id, record in devices.items()
                     if isinstance(record, dict) and record.get("device_type") == "reader"}
    stranded = {device_id: record for device_id, record in saved_readers.items()
                if device_id not in live_readers}
    unclaimed = {device_id: card for device_id, card in live_readers.items()
                 if device_id not in saved_readers}
    moved = []
    for new_id, card in unclaimed.items():
        name = str(card.get("name") or "")
        if not name:
            continue
        matches = [old_id for old_id, record in stranded.items()
                   if str(record.get("name") or "") == name]
        if len(matches) != 1:
            continue
        old_id = matches[0]
        record = dict(stranded.pop(old_id))
        record["stable_path"] = str(card.get("reader_port") or "")
        record["updated_at"] = int(time.time())
        devices.pop(old_id, None)
        devices[new_id] = record
        moved.append((old_id, new_id))
    if moved:
        _write(HARDWARE, {"version": 1, "updated_at": int(time.time()), "devices": devices})
    return moved


def set_hardware(device_id: str, patch: dict) -> dict:
    device_id = str(device_id).strip()
    if not device_id:
        raise ValueError("device_id is required")
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    current = dict(devices.get(device_id) or {})
    current.update(dict(patch or {}))
    current["updated_at"] = int(time.time())
    devices[device_id] = current
    _write(HARDWARE, {"version": 1, "updated_at": int(time.time()), "devices": devices})
    return current


def remove_hardware(device_id: str) -> bool:
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    removed = devices.pop(str(device_id), None) is not None
    if removed:
        _write(HARDWARE, {"version": 1, "updated_at": int(time.time()), "devices": devices})
    return removed
