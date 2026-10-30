"""Lines and their SIM hardware: /api/instances, /api/provision, /api/cards, /api/readers,
/api/sim/*, /api/ports/suggest and the reader device view /api/devices."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from fastapi import HTTPException, Request, APIRouter

from .. import config as cfg
from .. import (store, engine, status as status_mod, sim, card, usbreader, egress, device_state,
                carrier_id, vowifi_support, gate, authz)
from .. import state as app_state

log = logging.getLogger("vowifi.main")
router = APIRouter()


# Connectivity timeline shown per line. The window follows how much history has actually
# accumulated so a fresh install is not stretched across an empty two-day axis.
LINE_HISTORY_MAX_SECONDS = 2 * 24 * 3600
LINE_HISTORY_MIN_SECONDS = 3600


def _carrier_description(inst: dict | None, card_info: dict | None,
                         cellular: dict | None = None) -> dict:
    """Resolve a safe display value; never return IMSI, ICCID, SPN or GID."""
    inst, card_info = inst or {}, card_info or {}
    identity = {**inst, **{key: value for key, value in card_info.items()
                           if value not in (None, "")}}
    identity["carrier_identity"] = app_state._carrier_identity(card_info) or app_state._carrier_identity(inst)
    resolved = carrier_id.lookup(identity) or {
        "name": "", "home_network": "",
        "plmn": "-".join(filter(None, (str(identity.get("mcc") or ""),
                                          str(identity.get("mnc") or "")))),
        "match_source": "mccmnc", "database": "none",
    }
    current = str((cellular or {}).get("operator") or "").strip()
    if current.casefold() in {"--", "unknown", "none", "n/a"}:
        current = ""
    return {**resolved, "current_network": current}
capability_lock = asyncio.Lock()


_DRAFT_FIELD_KEYS = {"IMSI": "imsi", "MCC/MNC": "mcc_mnc", "IMEI": "imei", "SIM PIN": "pin"}


def _cached_line_status(inst: dict) -> dict:
    """Return an immediate status snapshot without contacting Docker/Asterisk/AMI."""
    iid = str(inst["id"])
    cached = app_state.hub.status_cache.get(iid)
    # A disabled line is stopped by definition, so the last sample taken while it ran says
    # nothing about it now. Serving that cache is what left a device reading "no SIM card"
    # after VoWiFi was switched off, until the next poll happened to overwrite it.
    if cached and (inst.get("enabled", True) or cached.get("state") == "STOPPED"):
        return dict(cached)
    if not inst.get("enabled", True):
        return app_state._with_status_activity(iid, {
            "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
            "reason_code": "stopped", "reason": "Stopped.", "detail": {}})
    return app_state._with_status_activity(iid, {
        "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
        "reason_code": "registering", "reason": "Refreshing line status…", "detail": {}})


# ----------------------------- SIM / readers -----------------------------
@router.get("/api/readers")
def api_readers():
    try:
        return {"readers": sim.list_readers(), "stale": False}
    except Exception as exc:  # noqa
        # The card monitor already owns a last-known, index-sorted view. Returning it is
        # safer than telling the UI there are zero readers after one transient pcsc-lite
        # context failure. The stale flag asks the browser to retry in the background.
        cached = [str(item.get("name") or "") for item in app_state.hub.cards_list()
                  if item.get("name")]
        if cached:
            log.warning("PC/SC reader enumeration temporarily unavailable; using %d cached readers: %r",
                        len(cached), exc)
            return {"readers": cached, "stale": True}
        log.warning("PC/SC reader enumeration unavailable and no cache exists: %r", exc)
        raise HTTPException(503, "card readers are temporarily unavailable") from exc


@router.get("/api/sim/detect")
async def api_sim_detect(reader_index: int = 0):
    rlist = await asyncio.to_thread(sim.list_readers)
    if reader_index < 0 or reader_index >= len(rlist):
        raise HTTPException(400, "reader index out of range")
    name = rlist[reader_index]
    async with app_state.hub.reader_lock(name):
        card = await asyncio.to_thread(sim.read_card, reader_index)
    # This read used to reach only the form. A draft waiting on a field the insertion read
    # missed then stayed a draft until the operator also pressed Save.
    entry = app_state.hub.cards.get(name)
    if entry is not None and app_state._merge_card_read(entry, card):
        iid = str(entry.get("matched") or "")
        if iid and not app_state._draft_needs_card_read(name):
            asyncio.create_task(app_state._auto_start_hotplugged_line(iid))
    return app_state._client_card_info(card.dict())


def _resolve_reader_index(body: dict) -> int:
    """Resolve the target reader for index-taking SIM APIs. When the caller supplies the
    physical reader NAME we re-resolve the index at request time. A configured line's
    ``reader`` value may instead be a logical selector (``imsi:...``), which must never be
    mistaken for a PC/SC name. Prefer stable port/card identity over a cached enumeration
    index so hotplug cannot redirect a PIN or provisioning request to another SIM."""
    rlist = sim.list_readers()
    if not rlist:
        raise HTTPException(409, "no PC/SC readers connected")
    try:
        cached_idx = int(body.get("reader_index", 0))
    except (TypeError, ValueError):
        cached_idx = 0
    rname = str(body.get("reader") or "").strip()
    logical_selector = rname.startswith(("imsi:", "iccid:"))

    if rname and not logical_selector:
        if rname not in rlist:
            raise HTTPException(409, "the selected card reader is no longer connected")
        return rlist.index(rname)

    port = str(body.get("reader_port") or "").strip()
    if port:
        try:
            port_idx = usbreader.index_for_port(port)
        except Exception as exc:  # noqa
            log.debug("request port->index resolve failed for %s: %r", port, exc)
            port_idx = None
        if port_idx is not None and 0 <= int(port_idx) < len(rlist):
            return int(port_idx)

    iccid = str(body.get("iccid") or "").strip()
    imsi = str(body.get("imsi") or "").strip()
    if rname.startswith("iccid:"):
        iccid = rname.split(":", 1)[1].strip()
    elif rname.startswith("imsi:"):
        imsi = rname.split(":", 1)[1].strip()
    if iccid or imsi:
        for card_info in app_state.hub.cards.values():
            if not card_info.get("present"):
                continue
            if ((iccid and str(card_info.get("iccid") or "") == iccid)
                    or (imsi and str(card_info.get("imsi") or "") == imsi)):
                idx = card_info.get("index")
                if idx is not None and 0 <= int(idx) < len(rlist):
                    return int(idx)
        raise HTTPException(409, "the SIM for this line is no longer connected")

    if cached_idx < 0 or cached_idx >= len(rlist):
        raise HTTPException(409, "the selected card reader is no longer connected")
    return cached_idx


@router.post("/api/sim/verify-pin")
async def api_verify_pin(body: dict):
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    rlist = await asyncio.to_thread(sim.list_readers)
    name = rlist[idx] if 0 <= idx < len(rlist) else ""
    async with app_state.hub.reader_lock(name or f"idx:{idx}"):
        res = await asyncio.to_thread(sim.verify_pin, body["pin"], idx)
        if res.get("ok"):
            # PIN now satisfied — re-read the (previously locked) IMSI + SMSC and refresh the
            # detected-card entry so the dashboard can move from "locked" to "ready to provision".
            try:
                c = await asyncio.to_thread(sim.read_card, idx, body["pin"])
                # Key strictly by the reader NAME the read actually used — an index-keyed
                # lookup could merge this card's identity into a stale entry of a reader
                # that was just unplugged.
                card_entry = app_state.hub.cards.get(c.reader) or {"index": idx, "name": c.reader,
                                                         "present": True}
                card_entry.update(present=True, iccid=c.iccid, imsi=c.imsi, mcc=c.mcc,
                                  mnc=c.mnc, mnc_len=getattr(c, "mnc_len", None),
                                  pin_enabled=c.pin_enabled, pin_tries=c.pin_tries,
                                  smsc=c.smsc, reader_port=c.reader_port,
                                  carrier_identity=app_state._carrier_identity(c))
                inst = app_state._match_instance_by_iccid(c.iccid)
                if inst and app_state._carrier_identity_update(c):
                    await asyncio.to_thread(cfg.upsert_instance, {
                        "id": str(inst["id"]), **app_state._carrier_identity_update(c)})
                card_entry["matched"] = inst["id"] if inst else None
                app_state.hub.cards[c.reader] = card_entry
                res["card"] = app_state._client_card_info(card_entry)
                await app_state.hub.broadcast({"type": "cards", "cards": app_state._client_cards()})
            except Exception as e:  # noqa
                log.debug("post-verify re-read failed: %r", e)
    return res


@router.post("/api/sim/change-pin")
async def api_change_pin(body: dict):
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    rlist = await asyncio.to_thread(sim.list_readers)
    name = rlist[idx] if 0 <= idx < len(rlist) else f"idx:{idx}"
    async with app_state.hub.reader_lock(name):
        return await asyncio.to_thread(sim.change_pin, body["old"], body["new"], idx)


@router.post("/api/sim/pin-enabled")
async def api_pin_enabled(body: dict):
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    rlist = await asyncio.to_thread(sim.list_readers)
    name = rlist[idx] if 0 <= idx < len(rlist) else f"idx:{idx}"
    async with app_state.hub.reader_lock(name):
        return await asyncio.to_thread(
            sim.set_pin_enabled, body["pin"], bool(body["enabled"]), idx)


def _refresh_card_matches():
    """Recompute each detected card's matched instance against current config. Only for
    entries whose ICCID is known — entries mapped via a running engine's pin_status
    (identity not probed) must keep that match instead of being wiped to None."""
    for c in app_state.hub.cards.values():
        if c.get("present") and c.get("iccid"):
            inst = app_state._match_instance_by_iccid(c.get("iccid"))
            c["matched"] = inst["id"] if inst else None


@router.get("/api/cards")
async def api_cards():
    """Physically detected readers/cards (from the real-time monitor)."""
    if not app_state.hub.scanned:
        # The monitor hasn't finished its first scan yet (manager just started) — answer
        # from a live reader scan so the UI never sees a false "no readers" flash. Map
        # present cards to running engines by pin_status reader name (no card access).
        def scan():
            out = []
            for st in card.reader_states() or []:
                inst = app_state._find_running_by_reader(st["name"]) if st["present"] else None
                out.append({**st,
                            "iccid": inst.get("iccid") if inst else None,
                            "imsi": inst.get("imsi") if inst else None,
                            "matched": inst["id"] if inst else None,
                            "pin_enabled": None, "pin_tries": None})
            return out
        cards = await asyncio.to_thread(scan)
        return {"cards": app_state._with_detected_imei(cards)}
    _refresh_card_matches()
    return {"cards": app_state._client_cards()}


@router.get("/api/ports/suggest")
def api_ports_suggest():
    """Preview the SIP port the automatic allocator would pick for a NEW line right now
    (conflict-checked against other lines + live host listeners). Lets the manual-port UI
    show a sensible default and the auto option show what it will use."""
    try:
        block = cfg.alloc_ports_auto(cfg.load())
        return {"auto_sip_udp": block["sip_udp"], "auto_sip_tls": block["sip_tls"],
                "min": cfg.MIN_USER_PORT, "max": cfg.MAX_USER_PORT}
    except Exception as e:  # noqa
        raise HTTPException(409, f"no free port block: {e}")


def _reader_index_for_instance(inst: dict) -> int | None:
    """Resolve the PC/SC reader index this instance should address, preferring the STABLE
    physical USB port binding over the (unstable) enumeration index/ICCID.

    Priority:
      1. inst.reader_port -> live index via the USB port map. This is authoritative: it sticks
         to the physical reader socket even when pcscd flips the indices of two identical
         readers. It does not require the card to be readable/matched by the monitor.
      2. ICCID match against the live card monitor (works once the card's identity is known).
    Returns None if neither resolves (card/reader not present)."""
    # A VPCD multi-slot bridge intentionally exposes the same physical SIM on
    # several logical readers. ICCID matching is therefore ambiguous; preserve
    # the dedicated SWu slot selected by the instance instead of collapsing to
    # the first matching slot (which is reserved for pin_keeper).
    swu_name = inst.get("swu_reader")
    if swu_name:
        for c in app_state.hub.cards.values():
            if c.get("name") == swu_name:
                return c.get("index")
    if "pin_reader" in inst and "ami_reader" in inst:
        try:
            return int(inst.get("reader_index", 1))
        except (TypeError, ValueError):
            return 1
    port = inst.get("reader_port")
    if port:
        try:
            idx = usbreader.index_for_port(port)
        except Exception as e:  # noqa
            log.debug("port->index resolve failed for %s: %r", port, e)
            idx = None
        if idx is not None:
            return idx
    iccid = inst.get("iccid")
    for c in app_state.hub.cards.values():
        if c.get("present") and iccid and c.get("iccid") == iccid:
            return c.get("index")
    return None


def _reader_port_for_instance(inst: dict) -> str | None:
    """The stable USB port path this instance's SIM currently sits at. Resolved from the live
    card monitor by ICCID (the port is captured per-reader on each scan). Used to (re)learn /
    refresh a line's reader_port binding at start time so it self-heals if the SIM was moved."""
    iccid = inst.get("iccid")
    if iccid:
        for c in app_state.hub.cards.values():
            if c.get("present") and c.get("iccid") == iccid and c.get("reader_port"):
                return c.get("reader_port")
    return None


def _card_identity_mismatch(inst: dict) -> dict | None:
    """Detect that the reader this line uses now holds a DIFFERENT SIM identity — the
    signature of an eSIM profile switch (enable/disable/download changes the eUICC's
    active profile, so the same physical reader re-enumerates with a new ICCID/IMSI).

    Starting the line anyway is what used to break things: the engine grabs whatever
    card is in the reader, runs EAP-AKA with the OLD line's IMSI against the NEW
    profile's keys, the carrier rejects it (tunnel_sim_auth), and the bounded retry
    loop stops the container. Refuse the start up-front with a structured error
    instead. Only a positive, known conflict blocks — absent readers/unknown ICCIDs
    keep the existing fail-open behavior (engine start surfaces NO_CARD as before)."""
    want = (inst.get("iccid") or "").strip()
    if not want:
        return None
    if _reader_index_for_instance(inst) is not None:
        return None      # this line's SIM/profile is present somewhere — all good
    # Prefer the stable USB port binding when present; fall back to stored index.
    port = (inst.get("reader_port") or "").strip()
    idx = inst.get("reader_index")
    for c in app_state.hub.cards.values():
        if not c.get("present"):
            continue
        if port and c.get("reader_port") == port:
            pass
        elif not port and c.get("index") == idx:
            pass
        else:
            continue
        got = (c.get("iccid") or "").strip()
        if got and got != want:
            return {"reader": c.get("name") or (f"USB {port}" if port else f"reader {idx}"),
                    "iccid": got}
    return None


def _raise_card_mismatch(inst: dict, mism: dict):
    raise HTTPException(409, {
        "code": "card_mismatch",
        "reader": mism["reader"],
        "card_iccid": mism["iccid"],
        "line_iccid": inst.get("iccid") or "",
        "message": (f"The card in {mism['reader']} now has a different identity "
                    f"(ICCID {mism['iccid']}; this line expects {inst.get('iccid')}). "
                    "This usually means the eSIM profile was switched. Provision the "
                    "active profile as its own line, or switch the eSIM back to this "
                    "profile, then start again."),
    })


def _preflight_pin_locked(inst: dict, idx: int) -> dict:
    """PIN preflight body — caller must already hold the reader asyncio.Lock.
    Sync so it can run under asyncio.to_thread (PC/SC is blocking)."""
    try:
        probe = sim.read_card(idx)          # no VERIFY: learns pin_enabled + presence
    except Exception as e:  # noqa
        log.debug("preflight probe failed: %r", e)
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    want = (inst.get("iccid") or "").strip()
    if not probe.present:
        # Nothing readable at the reader index this line binds to. Carry the expected ICCID
        # so the operator-facing error can say which SIM we were looking for (support bundles
        # get only the closed reason code, never the identifier).
        return {"ok": False, "code": "no_card", "line_iccid": want}
    # Real-time identity check. _card_identity_mismatch runs first but off the (sampled) card
    # monitor cache; inside an eSIM REFRESH/profile-switch window that cache lags, so a live
    # read here is what actually catches "the reader now holds a different profile" instead of
    # letting it fall through to a misleading no_card.
    got = (probe.iccid or "").strip()
    if want and got and got != want:
        return {"ok": False, "code": "card_mismatch", "card_iccid": got, "line_iccid": want}
    if probe.pin_enabled is None:
        # The card answered, but read_card never got far enough to learn its PIN state (an
        # ADF.USIM select that failed leaves every PIN field unset). Falling through would
        # report 'pin_required' with an unknown retry counter, and the UI would ask for a PIN
        # that cannot help — the dead end issues #51 and #60 both ended in. Report the read
        # failure itself so the operator sees a fixable fact.
        return {"ok": False, "code": "card_unreadable", "error": probe.error or ""}
    if probe.pin_enabled is False:
        return {"ok": True, "need_pin": False}
    saved = inst.get("pin")
    if not saved:
        return {"ok": False, "code": "pin_required", "tries": probe.pin_tries}
    try:
        chk = sim.read_card(idx, saved)
    except Exception as e:  # noqa
        log.debug("preflight verify failed: %r", e)
        return {"ok": True, "need_pin": True}     # couldn't verify now; let the engine try
    if chk.error and "PIN" in (chk.error or "").upper():
        return {"ok": False, "code": "pin_invalid", "clear": True, "tries": chk.pin_tries}
    return {"ok": True, "need_pin": True}


async def _preflight_pin(inst: dict) -> dict:
    """Actively check the SIM's PIN state BEFORE starting the engine (so we never spin up
    the SWu tunnel/IMS against a locked card). Reads the physical card:
      - card absent                         -> {ok:False, code:'no_card'}
      - card present but unreadable         -> {ok:False, code:'card_unreadable'}
      - PIN not required (disabled)          -> {ok:True,  need_pin:False}
      - PIN required, no saved PIN           -> {ok:False, code:'pin_required'}
      - PIN required, saved PIN verifies     -> {ok:True,  need_pin:True}
      - PIN required, saved PIN wrong/blocked -> {ok:False, code:'pin_invalid', clear:True}
    On 'pin_invalid' the saved PIN is stale and should be cleared so the user re-enters it.
    If the card can't be located/read we fail OPEN (ok:True) rather than block a start that
    might otherwise work (e.g. an engine already holds the card)."""
    idx = _reader_index_for_instance(inst)
    if idx is None:
        # Card not seen by the monitor — could be held by a running engine, or truly gone.
        # Don't block here; engine start + status FSM will surface NO_CARD if it's absent.
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    # Skip while LPA owns the reader (exclusive PC/SC) — let the engine try later.
    rlist = await asyncio.to_thread(sim.list_readers)
    rname = rlist[idx] if 0 <= idx < len(rlist) else None
    if rname and app_state.hub.lpa_busy.get(rname):
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    lock = app_state.hub.reader_lock(rname or f"idx:{idx}")
    # asyncio.Lock has no blocking=False; try a short acquire, fail-open if busy.
    try:
        await asyncio.wait_for(lock.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    try:
        return await asyncio.to_thread(_preflight_pin_locked, inst, idx)
    finally:
        lock.release()


def _pin_preflight_http(pf: dict) -> HTTPException:
    """Structured 409 for a failed PIN preflight. Always carries a human-readable
    `message` — the generic error path in the UI renders detail.message, and without
    it the toast degrades to the bare HTTP status text ("Conflict")."""
    tries = pf.get("tries")
    left = f" ({tries} tries left)" if tries is not None else ""
    want = (pf.get("line_iccid") or "").strip()
    expect = f" (this line expects ICCID {want})" if want else ""
    detail = str(pf.get("error") or "").strip()
    because = f" ({detail})" if detail else ""
    messages = {
        "no_card": ("no readable SIM at this line's reader — it is empty or the card is "
                    "not ready yet (an eSIM resets briefly while switching profiles); if "
                    f"you switched the eSIM, activate this line's profile first{expect}"),
        "pin_required": ("the SIM asks for a PIN and none is saved — enter the "
                         f"SIM PIN to start this line{left}"),
        "pin_invalid": f"the saved SIM PIN was rejected{left} — re-enter the PIN",
        "card_unreadable": ("the SIM was detected but could not be read"
                            f"{because} — this is not a PIN problem, so entering one will "
                            "not help; reseat the card and report this error if it persists"),
    }
    return HTTPException(409, {
        "code": pf["code"], "tries": tries,
        "message": messages.get(pf["code"], f"SIM preflight failed: {pf['code']}"),
    })


def _raise_preflight_block(iid: str, pf: dict):
    """Record a closed-schema lifecycle event for the refused start (so support bundles
    show the preflight decision without any identifier) and raise the operator-facing 409.

    A live ICCID conflict is reported as the existing card_mismatch error — same shape the
    cache-based guard produces — so the UI's message is identical however it was detected."""
    code = pf.get("code") or ""
    facts: dict = {}
    if code in {"pin_required", "pin_invalid", "card_mismatch", "card_unreadable"}:
        facts["card_present"] = True
    elif code == "no_card":
        facts["card_present"] = False
    if code == "card_mismatch":
        facts["iccid_matches"] = False
    app_state._record_lifecycle(str(iid), "preflight_blocked", reason_code=code, **facts)
    if code == "card_mismatch":
        _raise_card_mismatch({"iccid": pf.get("line_iccid") or ""},
                             {"reader": pf.get("reader") or "the reader",
                              "iccid": pf.get("card_iccid") or ""})
    raise _pin_preflight_http(pf)


@router.post("/api/provision")
async def api_provision(body: dict):
    """Provision a detected card: verify PIN, read identity, create the line and start it.
    PIN is required only when CHV1 is enabled. IMEI is auto-read from bridge metadata when
    available, otherwise the caller must supply it. Optional: imeisv (auto-derived from imei if blank), name, smsc,
    reader_index, reader (name), sip, webrtc, id, port_mode ('auto'|'manual'), sip_port
    (int, when manual), apn (default 'ims'), idr_mode ('apn'|'fqdn', default 'apn')."""
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    pin = body.get("pin", "")
    rlist = await asyncio.to_thread(sim.list_readers)
    rname = rlist[idx] if 0 <= idx < len(rlist) else body.get("reader") or f"idx:{idx}"
    async with app_state.hub.reader_lock(rname):
        c = await asyncio.to_thread(sim.read_card, idx, pin or None)
    if c.error and "PIN" in (c.error or "").upper():
        raise HTTPException(400, f"PIN error: {c.error} ({c.pin_tries} tries left)")
    if not c.imsi:
        raise HTTPException(400, "could not read IMSI (is the PIN correct?)")
    sip = cfg.merge_carrier_sip_defaults(
        c.mcc, c.mnc, c.iccid or c.imsi,
        body.get("sip") or {"transport": "udp", "external": []})
    sip.setdefault("webrtc", {"enable": bool(body.get("webrtc", True))})
    # The SMS centre is kept for reference only: this build sends no SMS, so a SIM that does
    # not expose EF_SMSP is still a usable line.
    smsc = (body.get("smsc") or "").strip() or c.smsc or ""
    live_cards = app_state.hub.cards_list()
    live_card = next((item for item in live_cards
                      if (item.get("name") == c.reader or item.get("index") == idx
                          or (c.iccid and item.get("iccid") == c.iccid))), {})
    imei, _hardware_id, _hardware_type = app_state._hardware_imei_for_card(live_card, live_cards)
    if len(imei) != 15:
        raise HTTPException(422, "imei_unavailable: configure a 15-digit IMEI in "
                                 "Device > Hardware before provisioning this SIM.")
    inst = {
        "id": str(body.get("id") or (len(cfg.list_instances()) + 1)),
        "name": body.get("name") or f"{c.mcc}-{c.mnc}",
        "provisioning_state": "ready",
        "imsi": c.imsi, "mcc": c.mcc, "mnc": c.mnc, "iccid": c.iccid,
        **app_state._carrier_identity_update(c),
        # Blank means automatic MCC->ISO country mapping; a two-letter value is a per-line
        # override for MVNO/roaming/operator edge cases.
        "proxy_country": egress.normalize_country(body.get("proxy_country")),
        "imei": imei,
        "imei_source_device_id": _hardware_id,
        # IMEISV for DEVICE_IDENTITY: user value if provided, else auto-derive (14-digit IMEI
        # base + random 2-digit SVN) so each line looks like a distinct handset build.
        "imeisv": (body.get("imeisv") or "").strip()
                  or cfg.imeisv_from_imei(imei, svn=app_state._random_svn()),
        "pin": pin,
        "reader": f"imsi:{c.imsi}",
        "reader_index": idx,  # store the physical reader index for USB device passthrough
        # Stable USB port path of the reader this SIM was provisioned in. This is the primary
        # binding used at start time (resolved back to a live index), so the line sticks to its
        # physical reader socket even if pcscd re-enumerates two identical readers in a different
        # order. Falls back to reader_index/ICCID when absent.
        "reader_port": c.reader_port or usbreader.port_for_index(idx) or "",
        "smsc": smsc,
        "msisdn": body.get("msisdn", ""),
        "msisdn_source": "manual" if str(body.get("msisdn") or "").strip() else "",
        "enabled": True, "sip": sip,
        # APN + ePDG identity (IDr) encoding for the SWu tunnel. apn defaults to 'ims'; idr_mode
        # defaults to 'fqdn' (real-UE APN-FQDN form). Normalised in config.render_instance_json.
        "apn": cfg.normalize_apn(body.get("apn", "")),
        "idr_mode": cfg.normalize_idr_mode(body.get("idr_mode", "")),
        # CFG request address family. Defaults to 'auto' (discovery ladder + carrier DB, seamless);
        # 'v6' Telus/EE, 'v4' Vodafone UK, 'dual'. Normalised in config.render_instance_json.
        "cp_mode": cfg.normalize_cp_mode(body.get("cp_mode", "")),
        # Full Asterisk debug writes SIP identities and authentication headers to the container
        # log. Nothing the control plane does needs it: the line number is read from the
        # identity Asterisk announces for its own registrations.
        "debug": {**(body.get("debug") or {}), "asterisk": False},
    }
    # Port mapping: 'manual' pins the SIP UDP port the user chose (the rest of the block
    # derives from it, validated for range + host/instance conflicts). 'auto' (default)
    # allocates a conflict-free block now — and when re-provisioning an existing line it
    # RE-allocates (so switching an already-provisioned line back to Auto actually moves it
    # off a manual port), stepping past anything in use.
    iid = str(inst["id"])
    if body.get("port_mode") == "manual":
        try:
            inst["ports"] = cfg.ports_from_sip_base(cfg.load(), int(body.get("sip_port", 0)),
                                                    exclude_iid=iid)
        except (ValueError, TypeError) as e:
            raise HTTPException(422, f"port_error: {e}")
    else:
        try:
            inst["ports"] = cfg.alloc_ports_auto(cfg.load(), exclude_iid=iid)
        except ValueError as e:
            raise HTTPException(422, f"port_error: {e}")
    try:
        inst = cfg.upsert_instance(inst)
    except cfg.LineLimitError as exc:
        raise HTTPException(409, {
            "code": "line_limit", "message": str(exc)}) from exc
    app_state.hub._msisdn_tries.pop(str(inst["id"]), None)
    app_state.hub.reset_health(inst["id"], "user_requested")
    # engine.start force-removes any existing container; retire AMI first so a cached
    # client can't keep Login'ing the old (or IP-reused) engine with a stale secret.
    await app_state.hub.drop_ami(str(inst["id"]))
    await asyncio.to_thread(app_state._start_engine_checked, inst, cfg.get_settings(),
                            dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
    _refresh_card_matches()
    await app_state.hub.broadcast({"type": "cards", "cards": app_state._client_cards()})
    safe = {k: v for k, v in inst.items() if k not in ("pin", "carrier_identity")}
    return {"ok": True, "instance": safe}


def _masked_identifier(value) -> str:
    text = str(value or "")
    return ("*" * max(0, len(text) - 4) + text[-4:]) if text else ""


def _follow_imei_source(old_id: str, new_id: str) -> list[str]:
    """Repoint lines that name a device id the reader migration just retired.

    The field records which physical device supplied a line's IMEI, and doubles as the marker
    that the one-time legacy migration already ran for that line. It is not load-bearing — the
    IMEI is resolved from whichever device currently holds the card — so a stale value is
    harmless to the engine but reads as a device that no longer exists, and clearing it would
    be worse than leaving it: an empty marker lets the legacy migration run a second time.
    """
    followed = []
    for inst in cfg.list_instances():
        if str(inst.get("imei_source_device_id") or "") != old_id:
            continue
        cfg.upsert_instance({"id": str(inst["id"]), "imei_source_device_id": new_id})
        followed.append(str(inst["id"]))
    return followed


async def _unified_devices() -> list[dict]:
    cards = app_state.hub.cards_list()
    native_readers = device_state.native_reader_devices(cards)
    # A reader that moved to a different USB port derives a new id and strands its saved
    # record, which this list would then render as a second, permanently offline copy of a
    # connected reader. Move the record first: it holds the IMEI the line presents.
    if cards:
        try:
            moved = await asyncio.to_thread(device_state.migrate_reader_records, native_readers)
            for old_id, new_id in moved:
                log.info("reader record migrated: %s -> %s (same reader on a new USB port)",
                         old_id, new_id)
                followed = await asyncio.to_thread(_follow_imei_source, old_id, new_id)
                if followed:
                    log.info("line(s) %s now name the migrated reader as their IMEI source",
                             ", ".join(followed))
        except Exception as exc:  # noqa
            log.debug("reader record migration failed: %r", exc)
    hardware_records = device_state.hardware()
    saved_reader_ids = {device_id for device_id, record in hardware_records.items()
                        if record.get("device_type") == "reader"}
    device_ids = sorted(set(native_readers) | saved_reader_ids)

    settings = cfg.get_settings()
    configured_exits = settings.get("proxy", {}).get("exits", {}) or {}
    available_countries = sorted(country for country, value in configured_exits.items()
                                 if isinstance(value, dict) and value.get("enabled", False))
    egress_state = egress.status()
    egress_lines = egress_state.get("lines") or {}
    egress_exits = egress_state.get("exits") or {}
    rekey = settings.get("rekey") or {}
    result = []
    for device_id in device_ids:
        native_card = native_readers.get(device_id)
        hardware_record = hardware_records.get(device_id) or {}
        device_present = native_card is not None
        inst = (app_state._match_instance_by_iccid(native_card.get("iccid"))
                if native_card and native_card.get("present") and native_card.get("iccid")
                else None)
        vowifi_desired = bool((inst or {}).get("enabled", bool(inst)))
        line_status = _cached_line_status(inst) if inst else None
        running = bool(inst) and (line_status or {}).get("state") != "STOPPED"
        vowifi = device_state.native_vowifi_capability(vowifi_desired, running, line_status)
        is_draft = bool(inst) and inst.get("provisioning_state") == "draft"
        # A readable SIM the line limit kept out (state._ensure_card_draft). Without this
        # the page could only say "insert a readable SIM", which is not what happened.
        blocked = (native_card or {}).get("not_enabled") or {}
        line_limited = bool(native_card and native_card.get("present") and not inst
                            and native_card.get("iccid")
                            and blocked.get("reason") == "line_limit")
        if not device_present:
            vowifi.update(actual="off", available=False, reason="Device not connected")
        elif not inst and line_limited:
            vowifi.update(available=False, reason=(
                "This SIM has no line: the SIM line limit is reached"))
        elif not inst:
            vowifi.update(available=False, reason="Insert a readable SIM before enabling VoWiFi")
        elif is_draft:
            vowifi.update(available=False,
                          reason="Automatic setup is waiting for SIM or hardware information")
        support_source = inst or native_card or {}
        vowifi["support"] = vowifi_support.for_instance(
            support_source if support_source.get("mcc") else {})
        if (vowifi["support"]["status"] == vowifi_support.UNSUPPORTED
                and vowifi.get("actual") == "off" and not vowifi.get("reason")):
            vowifi["reason"] = vowifi["support"]["reason"]

        card_info = native_card or {}
        carrier = _carrier_description(inst, card_info, None)
        exit_country = egress.line_country(inst or card_info)
        line_exit = egress_lines.get(str(inst["id"]) if inst else "", {}) or {}
        country_exit = egress_exits.get(exit_country, {}) or {}
        active_exit = line_exit if line_exit.get("node") else country_exit
        if native_card:
            hardware_imei, _hardware_id, _hardware_type = app_state._hardware_imei_for_card(
                native_card, cards)
            hardware_record = device_state.hardware().get(device_id) or hardware_record
        else:
            hardware_imei = cfg.normalize_imei(hardware_record.get("imei", ""))
        draft_missing = None
        if is_draft and card_info.get("present"):
            # Exactly the rule that decides promotion, so the page cannot name one field
            # while the line is really waiting for another.
            missing = draft_missing = app_state._draft_missing(inst, card_info, cards)
            if missing == ["IMEI"]:
                vowifi.update(available=False, reason=(
                    "Set a 15-digit IMEI in Hardware; the line will then start automatically"))
            elif "IMEI" in missing:
                vowifi.update(available=False, reason=(
                    "Set the IMEI in Hardware and complete the SIM details; the line will then start automatically"))  # noqa: E501 - one literal: the i18n coverage test reads it
            elif missing:
                vowifi.update(available=False, reason=(
                    "Complete the SIM details; the line will then start automatically"))
        result.append({
            "id": device_id, "device_type": "reader",
            "name": (hardware_record.get("display_name") or card_info.get("display_name")
                     or card_info.get("name") or hardware_record.get("name")
                     or "Smart-card reader"),
            "display_name": hardware_record.get("display_name") or "",
            "hardware_name": (card_info.get("name") or hardware_record.get("name")
                              or "Smart-card reader"),
            "present": device_present,
            "imei": hardware_imei,
            "imei_masked": _masked_identifier(hardware_imei),
            "stable_path": card_info.get("reader_port") or hardware_record.get("stable_path") or "",
            "reader": card_info.get("name") or "", "instance_id": str(inst["id"]) if inst else None,
            "status": line_status,
            "sim": {"name": ((inst or {}).get("name") or "SIM") if inst else "",
                    "number": (inst or {}).get("msisdn") or "",
                    "present": bool(card_info.get("present")),
                    "carrier": carrier},
            "vowifi": {"epdg": (line_status or {}).get("detail") or "",
                       "ims": (line_status or {}).get("label") or "",
                       "rekey_minutes": (inst or {}).get("rekey_minutes",
                                                         rekey.get("minutes", 30)),
                       "ike_rekey_minutes": (inst or {}).get("ike_rekey_minutes",
                                                             rekey.get("ike_minutes", 150)),
                       # With the data-channel rekey at 0, whether the line accepts the ePDG's
                       # own rekey decides what 0 means: the carrier renews the keys, or nobody
                       # does and the first ePDG rekey rebuilds the tunnel. Same default as the
                       # engine config (config.py).
                       "accept_epdg_rekey": bool((inst or {}).get("accept_epdg_esp_rekey",
                                                                  rekey.get("accept_epdg", False)))},
            "egress": {"node": active_exit.get("node") or "",
                # The picker lives on the settings page, so without these the device page shows
                # a node that silently disagrees with what the operator chose.
                **{key: (country_exit.get(key) or "")
                   for key in ("pinned_node", "pin_mode", "selection",
                               # Why the exit moved, and whether the pinned node is still
                               # serving a cooldown — otherwise a mismatch looks arbitrary.
                               "last_change", "pinned_cooldown_seconds")},
                "country": exit_country,
                "detected_country": egress.country_for_mcc((inst or card_info).get("mcc")),
                "override": egress.normalize_country((inst or {}).get("proxy_country")),
                "available_countries": available_countries},
            "provisioning": {"state": ("draft" if is_draft else "ready" if inst
                                       else "line_limit" if line_limited else "detecting"),
                **({"line_limit": {"max_lines": cfg.max_lines(),
                                   "iccid_tail": blocked.get("iccid_tail") or ""}}
                   if line_limited else {}),
                # IMEI is set on the Hardware tab; everything else on the SIM tab.
                "missing": ([_DRAFT_FIELD_KEYS[name] for name in draft_missing]
                            if draft_missing is not None else
                            [key for key, value in (
                                ("imsi", (inst or card_info).get("imsi")),
                                ("imei", hardware_imei)) if not value])},
            "capabilities": {"vowifi": vowifi},
        })
    return result


@router.get("/api/devices")
async def api_devices():
    # Sessions are memory-only, so a sign-in usually follows a control-plane restart — right
    # when the card monitor is still completing its first scan and smart-card readers are not
    # in the list yet. `discovering` lets the UI say so instead of reporting a confident zero.
    return {"devices": await _unified_devices(), "discovering": not app_state.hub.scanned}


@router.post("/api/devices/{device_id}/sim/reread")
async def api_device_sim_reread(device_id: str):
    """Read this device's SIM again; a draft completed by it is promoted and started."""
    cards = app_state.hub.cards_list()
    name = next((str(card.get("name") or "") for card in cards
                 if card.get("present") and app_state._device_for_card(card, cards)[0] == device_id), "")
    if not name:
        raise HTTPException(404, "no SIM is present in this device")
    filled = await app_state._reread_card(name)
    iid = str((app_state.hub.cards.get(name) or {}).get("matched") or "")
    inst = cfg.get_instance(iid) if iid else None
    is_draft = bool(inst and inst.get("provisioning_state") == "draft")
    missing = app_state._draft_missing(inst, app_state.hub.cards.get(name) or {}, app_state.hub.cards_list()) if is_draft else []
    # Only a complete draft is handed on; one still waiting for the IMEI or a PIN reports that.
    completing = is_draft and not missing
    if completing:
        asyncio.create_task(app_state._auto_start_hotplugged_line(iid))
    await app_state.hub.broadcast({"type": "cards", "cards": app_state._client_cards()})
    return {"ok": True, "updated": filled, "completing": completing, "missing": missing}


@router.put("/api/devices/{device_id}/name")
async def api_device_name(device_id: str, body: dict):
    """Set a local display name without changing the hardware identity."""
    if set(body or {}) != {"display_name"} or not isinstance(body.get("display_name"), str):
        raise HTTPException(400, "display_name must be a string")
    name = body["display_name"].strip()
    if len(name) > 80 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise HTTPException(422, "display_name must be at most 80 characters without control characters")
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    patch = {"device_type": device["device_type"], "display_name": name}
    if device["device_type"] == "reader":
        # Keep the raw PC/SC name for unambiguous reader-port migration.
        patch["name"] = (device.get("reader")
                         or device_state.hardware().get(device_id, {}).get("name") or "")
        patch["stable_path"] = device.get("stable_path") or ""
    device_state.set_hardware(device_id, patch)
    await app_state.hub.broadcast({"type": "hardware", "device": device_id})
    return {"ok": True, "display_name": name}


@router.put("/api/devices/{device_id}/hardware")
async def api_device_hardware(device_id: str, body: dict):
    """Save user-managed physical hardware identity (currently native-reader IMEI)."""
    if set(body or {}) - {"imei"}:
        raise HTTPException(400, "only imei can be changed")
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    raw = str((body or {}).get("imei") or "").strip()
    imei = cfg.normalize_imei(raw)
    if len(imei) != 15:
        raise HTTPException(422, "IMEI must contain exactly 15 digits")
    record = device_state.set_hardware(device_id, {
        "device_type": "reader", "name": (device.get("reader")
            or device_state.hardware().get(device_id, {}).get("name") or "Smart-card reader"),
        "stable_path": device.get("stable_path") or "", "imei": imei})

    # A running line renders the device identity inside its container. Apply a hardware
    # change immediately to the SIM currently inserted in this reader. A new reader line is
    # deliberately a stopped draft until its hardware IMEI exists; saving that last missing
    # fact must also promote and start it, without requiring a second Save on the SIM tab.
    iid = str(device.get("instance_id") or "")
    applied = False
    started = False
    if iid and imei:
        inst = cfg.get_instance(iid) or {}
        cards = app_state.hub.cards_list()
        card_info = next((item for item in cards if item.get("present") and (
            str(item.get("hardware_id") or "") == device_id
            or (inst.get("iccid") and str(item.get("iccid") or "")
                == str(inst.get("iccid") or "")))), None)
        if inst.get("provisioning_state") == "draft" and card_info:
            inst = await asyncio.to_thread(app_state._auto_promote_card_draft, inst, card_info, cards)
        else:
            previous_imeisv = str(inst.get("imeisv") or "")
            svn = (previous_imeisv[-2:] if len(previous_imeisv) == 16
                   and previous_imeisv[-2:].isdigit() else app_state._random_svn())
            inst = cfg.upsert_instance({"id": iid, "imei": imei,
                                        "imei_source_device_id": device_id,
                                        "imeisv": cfg.imeisv_from_imei(imei, svn=svn)})
        running = await asyncio.to_thread(engine.is_running, iid)
        if running:
            await app_state.hub.drop_ami(iid)
            await asyncio.to_thread(app_state._start_engine_checked, inst, cfg.get_settings(),
                                    dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
            app_state.hub.reset_health(iid, "configuration_restart")
            applied = True
        elif inst.get("provisioning_state") != "draft":
            allowed, _reason = app_state._line_auto_start_allowed(inst)
            if allowed:
                await asyncio.to_thread(app_state._start_engine_checked, inst, cfg.get_settings(),
                                        dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
                app_state.hub.reset_health(iid, "hardware_identity_completed")
                applied = True
                started = True
    await app_state.hub.broadcast({"type": "hardware", "device": device_id})
    still_missing = (list(inst.get("auto_provision_missing") or [])
                     if iid and imei and inst.get("provisioning_state") == "draft" else [])
    return {"ok": True, "imei_masked": _masked_identifier(record.get("imei")),
            "applied": applied, "started": started, "missing": still_missing}


@router.delete("/api/devices/{device_id}")
async def api_device_delete(device_id: str):
    """Forget an offline physical device without deleting any SIM/line configuration."""
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    if device.get("present"):
        raise HTTPException(409, "disconnect the physical device before forgetting it")
    device_state.remove_hardware(device_id)
    await app_state.hub.broadcast({"type": "hardware", "device": device_id, "event": "forgotten"})
    return {"ok": True, "device_id": device_id, "lines_preserved": True}


@router.post("/api/devices/{device_id}/diagnostics")
async def api_device_diagnostics(device_id: str):
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    checks = [
        {"name": "hardware", "ok": bool(device.get("present")),
         "detail": "detected" if device.get("present") else "not detected"},
        {"name": "vowifi", "ok": device["capabilities"]["vowifi"]["actual"] in {"on", "off"},
         "detail": device["capabilities"]["vowifi"]["actual"]},
        {"name": "country_egress", "ok": (not device["capabilities"]["vowifi"]["desired"]
                                             or bool(device.get("egress", {}).get("node"))),
         "detail": device.get("egress", {}).get("node") or "not selected"},
    ]
    return {"ok": all(item["ok"] for item in checks), "device_id": device_id,
            "checked_at": int(time.time()), "checks": checks}


@router.patch("/api/devices/{device_id}/capabilities")
async def api_device_capabilities(device_id: str, body: dict):
    """Switch VoWiFi on or off for the line whose SIM is in this reader."""
    if not body or set(body) != {"vowifi_enabled"}:
        raise HTTPException(400, "provide vowifi_enabled only")
    if not isinstance(body["vowifi_enabled"], bool):
        raise HTTPException(400, "capability values must be boolean")

    async with capability_lock:
        unified = await _unified_devices()
        device = next((item for item in unified if item["id"] == device_id), None)
        if not device:
            raise HTTPException(404, "no such physical device")
        iid = str(device.get("instance_id") or "")
        if not iid:
            if body["vowifi_enabled"]:
                raise HTTPException(409, "configure the SIM before enabling VoWiFi")
            return device
        inst = cfg.get_instance(iid)
        previous = bool((inst or {}).get("enabled", True))
        wanted = body["vowifi_enabled"]
        retry = bool(wanted and not await asyncio.to_thread(engine.is_running, iid))
        if wanted == previous and not retry:
            return device
        if wanted:
            cfg.upsert_instance({"id": iid, "enabled": True})
            await api_instance_start(iid)
        else:
            cfg.upsert_instance({"id": iid, "enabled": False})
            app_state._record_lifecycle(iid, "vowifi_disabled", "user_requested")
            await _stop_instance(iid, "device_vowifi_disabled")
        refreshed = await _unified_devices()
        return next(item for item in refreshed if item["id"] == device_id)


# ----------------------------- instances -----------------------------
@router.get("/api/instances")
async def api_instances(request: Request):
    out = []
    for inst in cfg.list_instances():
        st = _cached_line_status(inst)
        safe = {k: v for k, v in inst.items() if k not in ("pin", "carrier_identity")}
        safe["has_pin"] = bool(inst.get("pin"))
        safe["proxy_country_effective"] = egress.line_country(inst)
        # What the carrier profile turns on when the line leaves a setting unset, so the form can
        # show what is actually in effect. Only the switches: a PANI identity is not for display.
        carrier = cfg.carrier_sip_defaults(str(inst.get("mcc") or ""), str(inst.get("mnc") or ""))
        safe["sip_carrier_defaults"] = {key: carrier[key] for key in
                                        (*cfg.CARRIER_SIP_FLAGS, *cfg.CARRIER_SIP_TEXT)
                                        if key in carrier}
        # Report the reader index that PHYSICALLY holds this line's SIM right now (ICCID-matched
        # against the live monitor) instead of the stored one. PC/SC indices shift when readers
        # are unplugged, so a stored index can be stale and make the SIM-config "Detect card"
        # button probe a reader that no longer exists ("No SIM card in reader N").
        live_idx = _reader_index_for_instance(inst)
        if live_idx is not None:
            safe["reader_index"] = live_idx
        # Also report the SIM's current USB port (by ICCID from the live monitor) so the UI can
        # show the stable binding and re-persist it if the SIM was moved to another reader socket.
        live_port = _reader_port_for_instance(inst)
        if live_port:
            safe["reader_port"] = live_port
        out.append({**safe, "status": st})
    return {"instances": out}


def _only_instance_name_changed(before: dict | None, after: dict,
                                live_binding: dict | None = None) -> bool:
    """Whether a saved edit changed display metadata and no engine configuration.

    A line name is resolved by the manager whenever it builds UI, notification or diagnostic
    output. It is never consumed by the running IKE/Asterisk engine, so replacing that engine
    for a rename only interrupts working calls and tunnels without applying anything useful.
    Compare the persisted documents rather than trusting a client-side flag: a request that also
    changes any operational field must continue through the normal fail-closed rebuild.

    The WebUI sends the whole form back, and two of its fields differ from the stored document
    without changing anything the engine sees. It pads the MNC to three digits ("15" -> "015"),
    and the engine pads it the same way wherever it uses it. /api/instances also replaces the
    stored reader_index/reader_port with the live binding (``live_binding``), which is the
    binding the running engine already resolves. Either one alone restarted a line on rename.
    """
    if before is None or before == after:
        return False
    ignored = cfg.RUNTIME_ONLY_INSTANCE_FIELDS | {"name"}
    live_binding = live_binding or {}

    def operational(inst: dict) -> dict:
        fields = {key: value for key, value in inst.items() if key not in ignored}
        for key in ("mcc", "mnc"):
            if fields.get(key) not in (None, ""):
                fields[key] = str(fields[key]).zfill(3)
        return fields

    before_runtime, after_runtime = operational(before), operational(after)
    for key, kind in (("reader_index", int), ("reader_port", str)):
        live = live_binding.get(key)
        if (isinstance(live, kind) and not isinstance(live, bool)
                and after_runtime.get(key) == live):
            before_runtime.pop(key, None)
            after_runtime.pop(key, None)
    return before_runtime == after_runtime


@router.post("/api/instances")
async def api_instance_upsert(body: dict):
    if "id" not in body:
        raise HTTPException(400, "id required")
    iid = str(body["id"])
    body = {key: value for key, value in body.items() if key != "carrier_identity"}
    # Reject an explicit rename onto another line's name rather than silently suffixing it:
    # the operator asked for that exact label, and a duplicate makes the name useless as a
    # handle in the UI and audit history.
    if "name" in body and cfg.instance_name_taken(body.get("name"), exclude_iid=iid):
        raise HTTPException(409, "another line already uses that name")
    previous = cfg.get_instance(iid)
    was_running = await asyncio.to_thread(engine.is_running, iid)
    live_binding = {}
    if previous:
        live_binding = {
            "reader_index": await asyncio.to_thread(_reader_index_for_instance, previous),
            "reader_port": await asyncio.to_thread(_reader_port_for_instance, previous)}
    try:
        inst = cfg.upsert_instance(body)
    except cfg.LineLimitError as exc:
        raise HTTPException(409, {
            "code": "line_limit", "message": str(exc)}) from exc
    name_only_change = _only_instance_name_changed(previous, inst, live_binding)
    applied = False
    # A running line holds its config in the engine container (rendered instance.json:
    # WebRTC credentials, IMEI, SMSC, User-Agent, …). Editing the config alone doesn't reach
    # the running Asterisk — so restart the container to re-render + reload the new config.
    if was_running and not name_only_change:
        try:
            app_state.hub._msisdn_tries.pop(iid, None)
            app_state.hub.reset_health(iid, "configuration_restart")
            await app_state.hub.drop_ami(iid)
            await asyncio.to_thread(app_state._start_engine_checked, inst, cfg.get_settings(),
                                    dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
            applied = True
            asyncio.create_task(app_state.push_status(iid))
        except Exception as e:  # noqa
            log.warning("apply-on-save restart failed for %s: %r", iid, e)
    elif name_only_change:
        # The host-side desired document carries the label for observability. Its proxy
        # fingerprint excludes line names, so publishing this metadata does not reload sing-box
        # or disturb the line either.
        egress.publish()
    safe = {k: v for k, v in inst.items() if k not in ("pin", "carrier_identity")}
    safe["applied"] = applied      # true => config was re-applied to the running engine
    return safe


@router.put("/api/instances/{iid}/country")
async def api_instance_country(iid: str, body: dict):
    """Select a per-line country exit, or clear it to return to MCC auto-detection."""
    if not cfg.get_instance(iid):
        raise HTTPException(404, "no such instance")
    raw = str(body.get("country") or "").strip()
    country = egress.normalize_country(raw)
    if raw and not country:
        raise HTTPException(400, "country must be a two-letter ISO code")
    safe = await api_instance_upsert({"id": str(iid), "proxy_country": country})
    egress.publish()
    return {"ok": True, "country": country,
            "effective_country": egress.line_country(cfg.get_instance(iid) or {}),
            "applied": bool(safe.get("applied"))}


@router.delete("/api/instances/{iid}")
async def api_instance_delete(iid: str, delete_history: bool = True, confirm_id: str = ""):
    """Delete one SIM line and its engine data; optionally retain its call history.

    If the card is still inserted, suppress automatic draft creation until it is physically
    removed. Otherwise the card monitor would recreate the line immediately and make a
    successful delete look broken.
    """
    if str(confirm_id) != str(iid):
        raise HTTPException(400, "confirm_id must exactly match the SIM line id")
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    inserted = any(card_info.get("present") and (
        str(card_info.get("matched") or "") == str(iid)
        or (inst.get("iccid") and str(card_info.get("iccid") or "") == str(inst["iccid"])))
        for card_info in app_state.hub.cards_list())
    # Old migrations could leave two records for the same ICCID. Deleting one must not pause
    # or strand the surviving line that should take ownership of the still-inserted SIM.
    replacements = [item for item in cfg.list_instances()
                    if str(item.get("id")) != str(iid)
                    and inst.get("iccid")
                    and str(item.get("iccid") or "") == str(inst.get("iccid"))]
    if inserted and inst.get("iccid") and not replacements:
        await asyncio.to_thread(cfg.suppress_card_until_removal, inst["iccid"])
    await asyncio.to_thread(engine.stop, iid)
    await app_state.hub.drop_ami(iid)
    app_state.hub.status_cache.pop(str(iid), None)
    app_state.hub.status_sampled_at.pop(str(iid), None)
    app_state.hub.health.pop(str(iid), None)
    app_state.hub._msisdn_tries.pop(str(iid), None)
    cfg.delete_instance(iid)
    await asyncio.to_thread(engine.delete_instance_data, iid)
    deleted_calls = 0
    if delete_history:
        deleted_calls = await asyncio.to_thread(store.clear_calls, iid)
        await asyncio.to_thread(store.delete_sms, None, str(iid))
    # Line ids are reused by the next created line, so its connectivity timeline always goes
    # with the line it describes — a new SIM must never inherit another SIM's outages.
    app_state._line_state_written.pop(str(iid), None)

    await asyncio.to_thread(store.clear_line_states, iid)

    _refresh_card_matches()
    if inserted and replacements:
        replacement = next((item for item in replacements if item.get("enabled", True)), None)
        if replacement:
            asyncio.create_task(app_state._auto_start_hotplugged_line(str(replacement["id"])))
    await app_state.hub.broadcast({"type": "cards", "cards": app_state._client_cards()})
    await app_state.hub.broadcast({"type": "line", "instance": str(iid), "event": "deleted"})
    if delete_history:
        await app_state.hub.broadcast({"type": "call", "instance": str(iid),
                             "deleted": deleted_calls})
    return {"ok": True, "history_deleted": bool(delete_history),
            "deleted_calls": deleted_calls}


@router.post("/api/instances/{iid}/start")
async def api_instance_start(iid: str, body: dict | None = None):
    """Start (or restart) a line. Actively checks the SIM PIN state first: if the card
    requires a PIN and we have no valid saved one, the start is refused with a structured
    error so the UI can prompt for the PIN — we never bring up the IPsec/IMS engine against
    a locked card. A PIN supplied in the body (re-entry) is verified, saved, and used."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")

    # eSIM-profile-switch guard: never start a line whose reader now holds a different
    # identity — EAP-AKA with mismatched IMSI/keys is guaranteed to be rejected by the
    # carrier (and can burn PIN tries on the wrong profile).
    mism = _card_identity_mismatch(inst)
    if mism:
        _raise_card_mismatch(inst, mism)

    # If the caller re-supplied a PIN (unlock flow), verify + persist it before preflight.
    supplied = (body or {}).get("pin")
    if supplied:
        idx = await asyncio.to_thread(_reader_index_for_instance, inst)
        if idx is not None:
            chk = await asyncio.to_thread(sim.read_card, idx, supplied)
            if chk.error and "PIN" in (chk.error or "").upper():
                raise HTTPException(400, f"PIN error: {chk.error}"
                                         + (f" ({chk.pin_tries} tries left)" if chk.pin_tries is not None else ""))
        inst = cfg.upsert_instance({"id": str(iid), "pin": supplied})

    pf = await _preflight_pin(inst)
    if not pf["ok"]:
        if pf.get("clear"):
            cfg.clear_pin(str(iid))     # stale saved PIN — force re-entry next time
        _raise_preflight_block(str(iid), pf)

    settings = cfg.get_settings()
    dev = os.environ.get("MDD_DEV_MOUNTS", "") == "1"
    # Bind the line to the reader that CURRENTLY holds its SIM, keyed on the STABLE physical USB
    # port. Two identical readers (no serial) get their pcscd enumeration order — and thus their
    # indices — flipped at boot/pcscd-restart with the cables untouched; a stored index then points
    # at the wrong (or empty) reader, and the engine authenticates against no card -> DEFAULT
    # RES/CK/IK -> carrier rejects EAP-AKA. So:
    #   1. (Re)learn the SIM's current USB port (by ICCID from the live monitor) and persist it —
    #      this refreshes the binding if the SIM was physically moved to another socket.
    #   2. Resolve the live PC/SC index from that port (falls back to ICCID) and persist it too.
    # The engine also self-resolves the port->index in-container, so its self-heal restarts stay
    # correct without the control plane.
    updates: dict = {}
    live_port = await asyncio.to_thread(_reader_port_for_instance, inst)
    if live_port and live_port != inst.get("reader_port"):
        log.info("instance %s: reader port %s -> %s (live ICCID match)",
                 iid, inst.get("reader_port"), live_port)
        updates["reader_port"] = live_port
        inst = {**inst, "reader_port": live_port}
    live_idx = await asyncio.to_thread(_reader_index_for_instance, inst)
    if live_idx is not None and live_idx != inst.get("reader_index"):
        log.info("instance %s: reader index %s -> %s (port/ICCID resolve)",
                 iid, inst.get("reader_index"), live_idx)
        updates["reader_index"] = live_idx
    if updates:
        inst = cfg.upsert_instance({"id": str(iid), **updates})
    app_state.hub._msisdn_tries.pop(str(iid), None)
    app_state.hub.reset_health(iid, "user_requested")
    await app_state.hub.drop_ami(iid)      # engine.start recreates the container (maybe new IP) -> stale client
    cid = await asyncio.to_thread(app_state._start_engine_checked, inst, settings, dev_mounts=dev)
    asyncio.create_task(app_state.push_status(str(iid)))
    return {"ok": True, "container": cid}


@router.post("/api/instances/{iid}/reprovision")
async def api_reprovision(iid: str, body: dict | None = None):
    """Manual re-provision: reset retry state and re-establish the line using the stored
    config (re-reads the SIM, no PIN re-entry). Optional body overrides fields (e.g. sip
    user_agent) before restart. Runs the same PIN preflight as start."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    if body:
        inst = cfg.upsert_instance({"id": str(iid), **body})
    mism = _card_identity_mismatch(inst)
    if mism:
        _raise_card_mismatch(inst, mism)
    pf = await _preflight_pin(inst)
    if not pf["ok"]:
        if pf.get("clear"):
            cfg.clear_pin(str(iid))
        _raise_preflight_block(str(iid), pf)
    app_state.hub._msisdn_tries.pop(str(iid), None)
    app_state.hub.reset_health(iid, "user_requested")
    await app_state.hub.drop_ami(iid)      # engine.start recreates the container (maybe new IP) -> stale client
    dev = os.environ.get("MDD_DEV_MOUNTS", "") == "1"
    cid = await asyncio.to_thread(app_state._start_engine_checked, inst, cfg.get_settings(), dev_mounts=dev)
    asyncio.create_task(app_state.push_status(str(iid)))
    return {"ok": True, "container": cid}


@router.post("/api/instances/{iid}/pin/clear")
async def api_clear_pin(iid: str):
    """Delete the saved SIM PIN for a line. If it's running, stop it — the next start must
    re-run the PIN flow (the whole point of forgetting the PIN)."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    had = cfg.clear_pin(str(iid))
    app_state.hub.reset_health(str(iid), "pin_cleared")
    if await asyncio.to_thread(engine.is_running, str(iid)):
        await asyncio.to_thread(engine.stop, str(iid))
        await app_state.hub.drop_ami(str(iid))
        asyncio.create_task(app_state.push_status(str(iid)))
    return {"ok": True, "had_pin": had}


@router.post("/api/instances/{iid}/stop")
async def api_instance_stop(iid: str):
    return await _stop_instance(iid, "user_requested")


async def _stop_instance(iid: str, cancel_reason: str) -> dict:
    """Stop one engine without pretending every internal stop disabled VoWiFi."""
    # Cancel frozen cooldown intent before stopping. Otherwise a pending health recovery can
    # recreate the line after the explicit/manual operation.
    app_state.hub.reset_health(iid, cancel_reason)
    await asyncio.to_thread(engine.stop, iid)
    # Tear down the AMI client too — otherwise its Manager keeps auto-reconnecting to the
    # now-removed container (and floods a container that later reuses the docker IP).
    await app_state.hub.drop_ami(iid)
    app_state.hub.status_cache[str(iid)] = app_state._with_status_activity(str(iid), {
        "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
        "reason_code": "stopped", "reason": "Stopped.", "detail": {}})
    app_state.hub.status_sampled_at[str(iid)] = time.monotonic()
    return {"ok": True}


@router.get("/api/instances/{iid}/status")
async def api_instance_status(iid: str, request: Request):
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    return authz.status_for(gate.current(request), _cached_line_status(inst))


def _availability_window(now: int, recorded_since: int | None) -> int:
    """How far back the chart reaches: as far as history goes, bounded on both sides."""
    span = (LINE_HISTORY_MIN_SECONDS if recorded_since is None
            else max(LINE_HISTORY_MIN_SECONDS, now - int(recorded_since)))
    return min(span, LINE_HISTORY_MAX_SECONDS)


@router.get("/api/instances/{iid}/availability")
async def api_instance_availability(iid: str):
    """VoWiFi connectivity history for one line, as a gap-aware up/down timeline."""
    inst = cfg.get_instance(str(iid))
    if not inst:
        raise HTTPException(404, "no such instance")
    now = int(time.time())
    recorded_since = await asyncio.to_thread(store.line_state_recorded_since, str(iid))
    span = _availability_window(now, recorded_since)
    start = now - span
    segments = await asyncio.to_thread(store.line_state_timeline, str(iid), start, now)
    return {"instance": str(iid), "start": start, "end": now, "span_seconds": span,
            "max_span_seconds": LINE_HISTORY_MAX_SECONDS,
            "recorded_since": int(recorded_since) if recorded_since is not None else None,
            "segments": segments, "summary": store.line_state_summary(segments)}


@router.post("/api/instances/{iid}/register")
async def api_instance_register(iid: str):
    return {"output": engine.exec_cli(iid, "pjsip send register volte_ims")}
