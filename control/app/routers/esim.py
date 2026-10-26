"""/api/esim/*: eUICC chip info, profiles, downloads and notifications through lpac."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from fastapi import HTTPException, APIRouter

from .. import config as cfg
from .. import sim, lpa, estkme, egress
from .. import state as app_state

log = logging.getLogger("vowifi.main")
router = APIRouter()


ESIM_CARD_REFRESH_ATTEMPTS = int(
    os.environ.get("MDD_ESIM_CARD_REFRESH_ATTEMPTS", "12"))
ESIM_CARD_REFRESH_INTERVAL = float(
    os.environ.get("MDD_ESIM_CARD_REFRESH_INTERVAL", "0.75"))



def _esim_resolve_reader(reader_index: int | None = None, reader: str | None = None) -> tuple[str, int]:
    """Resolve (reader_name, index) for eSIM APIs. Prefer NAME when provided."""
    rlist = sim.list_readers()
    if not rlist:
        raise HTTPException(409, "no PC/SC readers connected")
    if reader:
        if reader not in rlist:
            raise HTTPException(409, f"reader '{reader}' is no longer connected")
        return reader, rlist.index(reader)
    idx = 0 if reader_index is None else int(reader_index)
    if idx < 0 or idx >= len(rlist):
        raise HTTPException(400, "reader index out of range")
    return rlist[idx], idx


def _esim_imei_for_reader(name: str, override: str | None = None) -> str:
    if override and str(override).strip():
        return str(override).strip()
    entry = app_state.hub.cards.get(name) or {}
    matched = entry.get("matched")
    if matched:
        inst = cfg.get_instance(matched)
        if inst and inst.get("imei"):
            return str(inst["imei"])
    # A profile switch changes the ICCID before a matching line exists. Native readers keep
    # their configured device identity in devices-hardware.json, so downloads and automatic
    # provisioning must still be able to resolve that physical reader's IMEI in this gap.
    hardware_imei, _device_id, _device_type = app_state._hardware_imei_for_card(entry)
    if hardware_imei:
        return hardware_imei
    return ""


def _esim_resolve_se(
    name: str,
    idx: int,
    se_id: str | None = None,
    aid: str | None = None,
    *,
    require: bool = False,
) -> dict:
    """Resolve which ISD-R / SE to target. Dual-SE cards need se_id or aid when require=True."""
    ses = estkme.discover_ses(name, idx)
    try:
        if require and len(ses) > 1 and not (se_id or aid):
            raise KeyError("eUICC SE is required for dual-SE cards")
        return estkme.resolve_se(ses, se_id=se_id, aid=aid)
    except KeyError as e:
        raise HTTPException(400, str(e)) from e


def _esim_guard_engine(name: str):
    """Refuse LPA while a VoWiFi engine holds the card (lpac needs exclusive PC/SC).

    The detail is structured so the WebUI can offer "stop the blocking line and
    retry" instead of leaving the user to guess which line holds the reader (the
    UI's own lineRunning view can be stale right after a hotplug auto-start)."""
    inst = app_state._find_running_by_reader(name)
    if inst is not None:
        raise HTTPException(
            409,
            {"code": "engine_running",
             "message": f"Line {inst.get('id')} is running on this reader — "
                        "stop it before eSIM operations",
             "instance_id": str(inst.get("id") or "")},
        )


async def _esim_probe_card(idx: int, *, expect_iccid: str | None = None, attempts: int = 1):
    """Read the card, retrying through the eUICC REFRESH window a profile switch opens.

    Right after `profile enable` the card resets itself; a single probe lands inside that
    window often enough that the reader keeps showing the previous profile forever (the
    remove/insert events REFRESH generates are deliberately ignored while lpa_busy is set,
    so this probe is the only chance to observe the new identity). Retry until the read
    succeeds — and, when the caller knows which ICCID must appear, until the card reports
    it. The last successful read wins even if the expectation never matched: the card's
    answer is the truth, however unexpected.
    """
    last_error: Exception | None = None
    result = None
    for attempt in range(max(1, int(attempts))):
        if attempt:
            await asyncio.sleep(ESIM_CARD_REFRESH_INTERVAL)
        try:
            result = await asyncio.to_thread(sim.read_card, idx)
        except Exception as e:  # noqa
            last_error = e
            continue
        if not expect_iccid or str(result.iccid or "") == str(expect_iccid):
            return result
    if result is not None:
        return result
    raise last_error if last_error else RuntimeError("card probe produced no result")


async def _esim_refresh_card(
    name: str,
    idx: int,
    card_data=None,
    *,
    auto_start: bool = True,
    broadcast: bool = True,
    expect_iccid: str | None = None,
    attempts: int = 1,
):
    """Re-probe USIM identity after profile enable/disable/download and broadcast."""
    info = app_state.hub.cards.get(name) or {"index": idx, "name": name, "present": True}
    try:
        c = card_data if card_data is not None else await _esim_probe_card(
            idx, expect_iccid=expect_iccid, attempts=attempts)
        info.update(
            present=True, index=idx, name=name,
            iccid=c.iccid, imsi=c.imsi, mcc=c.mcc, mnc=c.mnc,
            mnc_len=getattr(c, "mnc_len", None),
            pin_enabled=c.pin_enabled, pin_tries=c.pin_tries, smsc=c.smsc,
            carrier_identity=app_state._carrier_identity(c),
        )
        inst = app_state._match_instance_by_iccid(c.iccid)
        if inst and app_state._carrier_identity_update(c):
            inst = await asyncio.to_thread(cfg.upsert_instance, {
                "id": str(inst["id"]), **app_state._carrier_identity_update(c)})
        if not inst and c.iccid and not cfg.card_auto_create_suppressed(c.iccid):
            # REFRESH arrives while lpa_busy is set, so the normal card-insert callback
            # deliberately keeps the previous ICCID and cannot create/start the newly active
            # profile. Do it from the authoritative post-LPA probe instead.
            inst = await asyncio.to_thread(app_state._ensure_card_draft, info)
        info["matched"] = inst["id"] if inst else None
    except Exception as e:  # noqa
        log.debug("post-LPA card refresh failed: %r", e)
        info.update(index=idx, name=name, present=True)
    app_state.hub.cards[name] = info
    if broadcast:
        await app_state.hub.broadcast({"type": "cards", "cards": app_state._client_cards()})
    if auto_start and info.get("matched"):
        asyncio.create_task(app_state._auto_start_hotplugged_line(str(info["matched"])))
    return info


async def _esim_prepare_reader_profile_switch(name: str) -> dict[str, dict]:
    """Fail-close the line whose profile a native reader is switching away from.

    Without this step the old profile's line keeps `enabled` after a successful switch, so
    the UI shows the old and new SIM as active at the same time and auto-start can try
    to revive a line whose profile no longer answers. Only the saved intent flag is
    touched — a running engine is still rejected by the LPA guard, and a failure path
    restores the flag through `_esim_restore_profile_switch`.
    """
    entry = app_state.hub.cards.get(name) or {}
    iid = str(entry.get("matched") or "")
    if not iid:
        inst = app_state._match_instance_by_iccid(str(entry.get("iccid") or ""))
        iid = str(inst["id"]) if inst else ""
    inst = cfg.get_instance(iid) if iid else None
    if not inst:
        return {}
    previous = {iid: {"enabled": bool(inst.get("enabled", True)), "running": False}}
    if inst.get("enabled", True):
        await asyncio.to_thread(cfg.upsert_instance, {"id": iid, "enabled": False})
        app_state.hub.reset_health(iid, "esim_profile_switch")
        egress.publish()
    return previous


async def _esim_restore_profile_switch(previous: dict[str, dict]):
    """Restore both saved intent and the exact running snapshot after an LPA failure."""
    changed = False
    for iid, state in previous.items():
        inst = cfg.get_instance(iid)
        if not inst:
            continue
        enabled = bool(state.get("enabled"))
        if bool(inst.get("enabled", True)) != enabled:
            inst = await asyncio.to_thread(
                cfg.upsert_instance, {"id": iid, "enabled": enabled})
            changed = True
        if state.get("running") and enabled:
            try:
                await asyncio.to_thread(
                    app_state._start_engine_checked, inst, cfg.get_settings(),
                    os.environ.get("MDD_DEV_MOUNTS", "") == "1")
            except Exception as exc:  # noqa
                log.warning("could not restore line %s after failed eSIM switch: %s", iid, exc)
    if changed:
        egress.publish()


async def _esim_run(
    name: str,
    idx: int,
    coro,
    *,
    refresh: bool = False,
    keep_busy: bool = False,
    refresh_expect_iccid: str | None = None,
):
    """Serialize an LPA call: engine gate + per-reader lock + lpa_busy + optional refresh."""
    try:
        await asyncio.to_thread(_esim_guard_engine, name)
        async with app_state.hub.reader_lock(name):
            app_state.hub.lpa_busy[name] = True
            try:
                result = await coro
                if refresh:
                    await _esim_refresh_card(
                        name, idx, expect_iccid=refresh_expect_iccid,
                        attempts=ESIM_CARD_REFRESH_ATTEMPTS if refresh_expect_iccid else 1)
                return result
            except lpa.LpaError as e:
                raise HTTPException(400, e.user_message()) from e
            except FileNotFoundError as e:
                raise HTTPException(503, str(e)) from e
            finally:
                if not keep_busy:
                    app_state.hub.lpa_busy.pop(name, None)
    except BaseException:
        # The coroutine is created at the call site, before this gate runs. When the
        # engine gate (or the reader lock) rejects the call, nothing awaits it and
        # CPython logs "coroutine ... was never awaited" — close it on those paths.
        if asyncio.iscoroutine(coro):
            coro.close()
        raise


# ----------------------------- eSIM / LPA (lpac) -----------------------------
@router.get("/api/esim/status")
async def api_esim_status():
    """Whether lpac is installed and basic settings."""
    settings = cfg.get_settings().get("esim") or {}
    bin_path = lpa.lpac_bin()
    return {
        "available": lpa.lpac_available(),
        "lpac_bin": bin_path,
        "download_timeout": int(settings.get("download_timeout") or 300),
        "auto_process_notifications": bool(settings.get("auto_process_notifications", True)),
        "busy_readers": list(app_state.hub.lpa_busy.keys()),
    }


# ---------------------------------------------------------------- eSIM chip cache
# Last successful chip read per eUICC (keyed by EID), persisted in the data dir so every
# browser/session can show the profile list — and switch profiles — without stopping a
# running line for a fresh exclusive read. Entries are matched to the inserted card via the
# ICCIDs of their profiles (the card monitor reads the active ICCID without exclusivity).
_ESIM_CACHE_PATH = os.path.join(cfg.DATA_DIR, "esim-chip-cache.json")


def _read_json_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _esim_cache_load() -> dict:
    doc = _read_json_file(_ESIM_CACHE_PATH)
    return doc if isinstance(doc, dict) else {}


def _esim_cache_write(data: dict):
    tmp = _ESIM_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _ESIM_CACHE_PATH)


def _esim_cache_store(ses: list, imei: str):
    eid = next((str(se.get("eid")) for se in ses if se.get("eid")), "")
    # Only a fully successful read may overwrite the cache — a partial/failed load would
    # replace a good profile list with an empty one.
    if not eid or any(se.get("error") for se in ses):
        return
    data = _esim_cache_load()
    data[eid] = {"ses": ses, "imei": imei or "", "ts": int(time.time())}
    _esim_cache_write(data)


def _esim_cache_for_iccid(iccid: str) -> dict | None:
    if not iccid:
        return None
    for entry in _esim_cache_load().values():
        for se in entry.get("ses") or []:
            if any(p.get("iccid") == iccid for p in (se.get("profiles") or [])):
                return entry
    return None


def _esim_cache_update_profile(iccid: str, *, state: str | None = None,
                               nickname: str | None = None, remove: bool = False):
    """Mirror a successful enable/disable/delete/nickname onto the cached view."""
    data = _esim_cache_load()
    changed = False
    for entry in data.values():
        for se in entry.get("ses") or []:
            profiles = se.get("profiles") or []
            hit = next((p for p in profiles if p.get("iccid") == iccid), None)
            if hit is None:
                continue
            if remove:
                se["profiles"] = [p for p in profiles if p.get("iccid") != iccid]
            else:
                if state == "enabled":
                    for p in profiles:
                        if str(p.get("profileState") or "").lower() == "enabled":
                            p["profileState"] = "disabled"
                if state is not None:
                    hit["profileState"] = state
                if nickname is not None:
                    hit["profileNickname"] = nickname
            changed = True
    if changed:
        _esim_cache_write(data)


@router.get("/api/esim/chip/cached")
async def api_esim_chip_cached(reader_index: int = 0, reader: str | None = None):
    """Cached chip view for the card in this reader — never touches the card, so it is safe
    while a VoWiFi line holds the reader."""
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    iccid = str((app_state.hub.cards.get(name) or {}).get("iccid") or "")
    entry = await asyncio.to_thread(_esim_cache_for_iccid, iccid)
    if not entry:
        return {"ok": True, "cached": False, "reader": name, "reader_index": idx}
    return {"ok": True, "cached": True, "reader": name, "reader_index": idx,
            "ses": entry.get("ses") or [], "imei": entry.get("imei") or "",
            "ts": entry.get("ts") or 0}


@router.get("/api/esim/chip")
async def api_esim_chip(reader_index: int = 0, reader: str | None = None):
    """Load chip info for every SE on the card (dual SE → two entries)."""
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    running = await asyncio.to_thread(app_state._find_running_by_reader, name)
    payload = await _esim_run(name, idx, lpa.load_all_ses(name, idx))
    ses = payload.get("ses") or []
    await asyncio.to_thread(_esim_cache_store, ses, _esim_imei_for_reader(name))
    # Backward-compatible single-chip view = first SE that loaded successfully.
    primary = next((s for s in ses if s.get("chip")), ses[0] if ses else None)
    return {
        "ok": True,
        "reader": name,
        "reader_index": idx,
        "dual": bool(payload.get("dual")),
        "ses": ses,
        "chip": (primary or {}).get("chip"),
        "imei": _esim_imei_for_reader(name),
        "line_running": bool(running),
        "matched_instance": running["id"] if running else (app_state.hub.cards.get(name) or {}).get("matched"),
    }


@router.get("/api/esim/profiles")
async def api_esim_profiles(reader_index: int = 0, reader: str | None = None):
    """List profiles grouped per SE (same load as chip — prefer /api/esim/chip for full view)."""
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    running = await asyncio.to_thread(app_state._find_running_by_reader, name)
    payload = await _esim_run(name, idx, lpa.load_all_ses(name, idx))
    ses = payload.get("ses") or []
    flat = []
    for se in ses:
        flat.extend(se.get("profiles") or [])
    return {
        "ok": True,
        "reader": name,
        "reader_index": idx,
        "dual": bool(payload.get("dual")),
        "ses": ses,
        "profiles": flat,
        "imei": _esim_imei_for_reader(name),
        "line_running": bool(running),
        "matched_instance": running["id"] if running else (app_state.hub.cards.get(name) or {}).get("matched"),
        "lpa_busy": bool(app_state.hub.lpa_busy.get(name)),
    }


@router.post("/api/esim/profiles/{iccid}/enable")
async def api_esim_enable(iccid: str, body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    async with app_state.hub.esim_switch_lock(f"reader:{name}"):
        se = await asyncio.to_thread(
            _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"),
            body.get("aid"), require=True)
        previous = await _esim_prepare_reader_profile_switch(name)
        try:
            await _esim_run(
                name, idx, lpa.profile_enable(name, iccid, aid=se.get("aid")),
                refresh=True, refresh_expect_iccid=iccid)
        except Exception:
            await _esim_restore_profile_switch(previous)
            raise
        await asyncio.to_thread(_esim_cache_update_profile, iccid, state="enabled")
        return {"ok": True, "iccid": iccid, "se_id": se["id"],
                "card": app_state.hub.cards.get(name)}


@router.post("/api/esim/profiles/{iccid}/disable")
async def api_esim_disable(iccid: str, body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    await _esim_run(
        name, idx, lpa.profile_disable(name, iccid, aid=se.get("aid")), refresh=True)
    await asyncio.to_thread(_esim_cache_update_profile, iccid, state="disabled")
    return {"ok": True, "iccid": iccid, "se_id": se["id"], "card": app_state.hub.cards.get(name)}


@router.delete("/api/esim/profiles/{iccid}")
async def api_esim_delete(
    iccid: str, reader_index: int = 0, reader: str | None = None,
    se_id: str | None = None, aid: str | None = None,
):
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    se = await asyncio.to_thread(_esim_resolve_se, name, idx, se_id, aid, require=True)
    await _esim_run(
        name, idx, lpa.profile_delete(name, iccid, aid=se.get("aid")), refresh=True)
    await asyncio.to_thread(_esim_cache_update_profile, iccid, remove=True)
    return {"ok": True, "iccid": iccid, "se_id": se["id"]}


@router.post("/api/esim/profiles/{iccid}/nickname")
async def api_esim_nickname(iccid: str, body: dict):
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    nick = body.get("nickname", "")
    await _esim_run(
        name, idx, lpa.profile_nickname(name, iccid, nick, aid=se.get("aid")))
    await asyncio.to_thread(_esim_cache_update_profile, iccid, nickname=nick)
    return {"ok": True, "iccid": iccid, "nickname": nick, "se_id": se["id"]}


@router.post("/api/esim/download")
async def api_esim_download(body: dict):
    """Start a profile download as a background task; progress via WS type=esim_download."""
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    if app_state.hub.lpa_busy.get(name):
        raise HTTPException(409, "an eSIM operation is already running on this reader")
    await asyncio.to_thread(_esim_guard_engine, name)
    imei = _esim_imei_for_reader(name, body.get("imei"))
    # Claim busy before returning so a second concurrent POST cannot start another job.
    app_state.hub.lpa_busy[name] = True
    se_id = se["id"]
    aid = se.get("aid")

    async def _job():
        try:
            async with app_state.hub.reader_lock(name):
                try:
                    await app_state.hub.broadcast({
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "started", "step": "started", "imei": imei,
                    })

                    async def on_progress(event):
                        # lpa.run_lpac passes {"step", "data", "code"}
                        step = (event or {}).get("step") or ""
                        data = (event or {}).get("data")
                        msg = {
                            "type": "esim_download", "reader": name, "reader_index": idx,
                            "se_id": se_id, "event": "progress", "step": step,
                        }
                        if isinstance(data, dict):
                            msg["metadata"] = data
                            msg["data"] = data
                        elif data is not None:
                            msg["data"] = data
                        if step == "es8p_metadata_parse" and isinstance(data, dict):
                            msg["event"] = "preview"
                        await app_state.hub.broadcast(msg)

                    result = await lpa.download(
                        name,
                        activation_code=body.get("activation_code"),
                        smdp=body.get("smdp"),
                        matching_id=body.get("matching_id"),
                        confirmation_code=body.get("confirmation_code"),
                        imei=imei or None,
                        aid=aid,
                        on_progress=on_progress,
                    )
                    await _esim_refresh_card(name, idx)
                    await app_state.hub.broadcast({
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "completed", "step": "completed",
                        "result": result, "card": app_state.hub.cards.get(name),
                    })
                except lpa.LpaError as e:
                    # lpac puts the failing function name in message (e.g. es9p_authenticate_client).
                    err = {
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "error",
                        "step": (e.message or "").strip() or None,
                        "error": e.user_message(),
                    }
                    await app_state.hub.broadcast(err)
                except Exception as e:  # noqa
                    log.exception("esim download failed")
                    await app_state.hub.broadcast({
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "error", "error": str(e),
                    })
        finally:
            app_state.hub.lpa_busy.pop(name, None)

    asyncio.create_task(_job())
    return {
        "ok": True, "started": True, "reader": name, "reader_index": idx,
        "se_id": se_id, "imei": imei,
    }


@router.post("/api/esim/download/cancel")
async def api_esim_download_cancel(body: dict | None = None):
    body = body or {}
    name, _idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    cancelled = lpa.cancel_download(name)
    if cancelled:
        await app_state.hub.broadcast({
            "type": "esim_download", "reader": name,
            "event": "cancelling", "step": "cancelling",
        })
    return {"ok": True, "cancelled": cancelled}


@router.post("/api/esim/discovery")
async def api_esim_discovery(body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    imei = _esim_imei_for_reader(name, body.get("imei"))
    entries = await _esim_run(
        name, idx,
        lpa.discovery(name, imei=imei or None, smds=body.get("smds"), aid=se.get("aid")))
    return {
        "ok": True, "reader": name, "se_id": se["id"],
        "entries": entries or [], "imei": imei,
    }


@router.get("/api/esim/notifications")
async def api_esim_notifications(reader_index: int = 0, reader: str | None = None):
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    payload = await _esim_run(name, idx, lpa.load_all_ses(name, idx))
    ses = payload.get("ses") or []
    flat = []
    for se in ses:
        flat.extend(se.get("notifications") or [])
    return {
        "ok": True, "reader": name, "dual": bool(payload.get("dual")),
        "ses": ses, "notifications": flat,
    }


@router.post("/api/esim/notifications/process")
async def api_esim_notifications_process(body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    seq = body.get("seq")
    remove = bool(body.get("remove", True))
    if seq is None:
        coro = lpa.notification_process(
            name, all_notifications=True, autoremove=remove, aid=se.get("aid"))
    else:
        coro = lpa.notification_process(
            name, int(seq), autoremove=remove, aid=se.get("aid"))
    await _esim_run(name, idx, coro)
    return {"ok": True, "se_id": se["id"]}


@router.delete("/api/esim/notifications/{seq}")
async def api_esim_notification_remove(
    seq: int, reader_index: int = 0, reader: str | None = None,
    se_id: str | None = None, aid: str | None = None,
):
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    se = await asyncio.to_thread(_esim_resolve_se, name, idx, se_id, aid, require=True)
    await _esim_run(name, idx, lpa.notification_remove(name, seq, aid=se.get("aid")))
    return {"ok": True, "seq": seq, "se_id": se["id"]}
