"""
main.py - MDD Sim Gateway control surface (FastAPI).

Serves the management REST API + WebSocket live feed + the built WebUI, and (for the
browser softphone) proxies provisioning. Runs natively or in a container; talks to
engine containers via the Docker SDK (engine.py) and Asterisk AMI (ami.py). HTTPS with
an auto-generated self-signed cert by default.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from contextlib import asynccontextmanager

import docker
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfg
from . import (store, engine, status as status_mod, sim, card, usbreader, egress, device_state,
               operations, sysinfo, failover, carrier_id, softphone_ws, vowifi_support, gate,
               media, authz)
from . import state as app_state
from .routers import auth as auth_routes
from .routers import egress as egress_routes
from .routers import esim as esim_routes
from .routers import logs as logs_routes
from .routers import system as system_routes

STATUS_OK_GRACE_SECONDS = 20
STATUS_POLL_FAST_SECONDS = 4.0
STATUS_POLL_HEALTHY_SECONDS = 15.0
# Once Asterisk has completed its own bounded REGISTER transaction and explicitly reports no
# response, another two minutes of same-session retries cannot repair the stale carrier-side
# P-CSCF/ESP state.  Rebuild promptly, but leave enough time for the diagnostic worker to capture
# and remove the expected container generation before auto-start runs.  Per-line rate limiting
# prevents a broken carrier from turning this fast path into a rebuild loop.
REG_UNANSWERED_RECOVERY_DELAY_SECONDS = float(
    os.environ.get("MDD_REG_UNANSWERED_RECOVERY_DELAY", "10"))
REG_UNANSWERED_MIN_INTERVAL_SECONDS = float(
    os.environ.get("MDD_REG_UNANSWERED_MIN_INTERVAL", "300"))
# Number portability is a rare administrative event. Verification re-reads the public identity
# from the registrations the line makes anyway, so it costs one log read; a six-hour cadence
# detects a change promptly without re-reading every healthy line's log every ten minutes.
MSISDN_VERIFY_INTERVAL_SECONDS = float(os.environ.get("MDD_MSISDN_VERIFY", "21600"))
MSISDN_VERIFY_FAILURE_RETRY_SECONDS = float(
    os.environ.get("MDD_MSISDN_VERIFY_FAILURE_RETRY", "600"))
# Spacing between the capped attempts to read a not-yet-learned number out of the engine log.
# The identity is logged when the line registers, so the first attempt normally succeeds; the
# retries only cover a log tail that has not caught up with a registration that just completed.
MSISDN_LEARN_RETRY_SECONDS = float(os.environ.get("MDD_MSISDN_LEARN_RETRY", "30"))
# How far back to look for the identity. It is re-announced on every registration refresh, so
# the tail only has to outlast one refresh interval — generous here because the alternative,
# not finding it, silently stops the ported-number check on a line whose engine talks a lot.
MSISDN_LOG_TAIL_LINES = int(os.environ.get("MDD_MSISDN_LOG_TAIL", "4000"))
# Conditions that are genuinely measured but routinely spike for one sample. Starting a
# container on a memory-tight box pages a batch back in; that is the cost of the operation,
# not a problem anyone can act on. Only a rate that holds across consecutive polls is one.
SUSTAINED_ALERT_CODES = {"swap_pressure"}
SUSTAINED_ALERT_SAMPLES = int(os.environ.get("MDD_SUSTAINED_ALERT_SAMPLES", "3"))
# Connectivity timeline shown per line. The window follows how much history has actually
# accumulated so a fresh install is not stretched across an empty two-day axis.
LINE_HISTORY_MAX_SECONDS = 2 * 24 * 3600
LINE_HISTORY_MIN_SECONDS = 3600
LINE_HISTORY_PRUNE_INTERVAL_SECONDS = 3600
# Comfortably below store.LINE_STATE_CONTINUITY_SECONDS, so throttled writes still read back
# as one uninterrupted observation.
LINE_STATE_WRITE_INTERVAL_SECONDS = 30

_line_state_written: dict[str, tuple[str, float]] = {}


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vowifi.main")

WEBUI_DIR = os.environ.get("MDD_WEBUI", os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "webui", "dist"))


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
PCSC_MAINTENANCE_WINDOW_SECONDS = 45


async def _on_card_insert(name, idx):
    info = {"index": idx, "name": name, "present": True, "iccid": None,
            "pin_enabled": None, "pin_tries": None, "matched": None, "imsi": None,
            "mcc": None, "mnc": None, "mnc_len": None, "smsc": None,
            "carrier_identity": {}, "reader_port": None}
    # Resolve the STABLE physical USB port for this reader index (DIRECT connect, no APDU —
    # safe even if a running engine holds the card). This is the binding a line pins to, so it
    # survives pcscd re-enumerating two identical readers into a different order.
    try:
        info["reader_port"] = await asyncio.to_thread(usbreader.port_for_index, idx)
    except Exception as e:  # noqa
        log.debug("reader_port resolve failed for idx %s: %r", idx, e)
    # A running engine may already hold this card (manager restart, or pcscd flapped
    # while the engine kept running) — probing it could clash with the engine's card
    # access. Always map the reader to the running instance whose pin_keeper reports
    # using THIS reader name first, and only probe when no running engine claims it.
    # Also skip probing while an LPA (lpac) operation holds the reader exclusively —
    # profile enable/disable triggers eUICC REFRESH that looks like remove+insert.
    inst = await asyncio.to_thread(
        app_state._find_running_by_reader, name, info.get("reader_port"), require_port_match=True)
    if inst is not None:
        info.update(iccid=inst.get("iccid"), imsi=inst.get("imsi"), matched=inst["id"],
                    smsc=inst.get("smsc"), mcc=inst.get("mcc"), mnc=inst.get("mnc"),
                    mnc_len=inst.get("mnc_len"),
                    carrier_identity=inst.get("carrier_identity") or {})
    elif app_state.hub.lpa_busy.get(name):
        prev = app_state.hub.cards.get(name) or {}
        info.update(iccid=prev.get("iccid"), imsi=prev.get("imsi"),
                    matched=prev.get("matched"), smsc=prev.get("smsc"),
                    mcc=prev.get("mcc"), mnc=prev.get("mnc"),
                    mnc_len=prev.get("mnc_len"),
                    carrier_identity=prev.get("carrier_identity") or {},
                    pin_enabled=prev.get("pin_enabled"), pin_tries=prev.get("pin_tries"))
        log.info("card insert during LPA busy — skipping probe reader=%s", name)
    else:
        lock = app_state.hub.reader_lock(name)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            prev = app_state.hub.cards.get(name) or {}
            info.update(iccid=prev.get("iccid"), imsi=prev.get("imsi"),
                        matched=prev.get("matched"), smsc=prev.get("smsc"),
                        mcc=prev.get("mcc"), mnc=prev.get("mnc"),
                        mnc_len=prev.get("mnc_len"),
                        carrier_identity=prev.get("carrier_identity") or {})
            app_state.hub.cards[name] = info
            log.debug("card probe skipped — reader lock busy: %s", name)
            return
        try:
            c = await asyncio.to_thread(sim.read_card, idx)
            info.update(iccid=c.iccid, pin_enabled=c.pin_enabled, pin_tries=c.pin_tries,
                        imsi=c.imsi, mcc=c.mcc, mnc=c.mnc,
                        mnc_len=getattr(c, "mnc_len", None), smsc=c.smsc,
                        carrier_identity=app_state._carrier_identity(c))
        except Exception as e:  # noqa
            log.debug("card probe failed: %r", e)
        finally:
            lock.release()
        inst = app_state._match_instance_by_iccid(info["iccid"])
        if inst:
            info["matched"] = inst["id"]
            info["imsi"] = info["imsi"] or inst.get("imsi")
            # A SIM line follows the card, not the reader it occupied previously. Refresh
            # the live binding immediately on hotplug so a swap between two readers cannot
            # leave the line pinned on the other one's USB port.
            update = {"id": str(inst["id"]), **app_state._carrier_identity_update(info),
                      "reader_index": idx, "reader_port": str(info.get("reader_port") or "")}
            if any(inst.get(key) != value for key, value in update.items() if key != "id"):
                inst = await asyncio.to_thread(cfg.upsert_instance, update)
        elif info.get("iccid") and cfg.card_auto_create_suppressed(info["iccid"]):
            # The user explicitly deleted this SIM line while the card was still inserted.
            # Keep it visibly unconfigured, but do not immediately recreate the record behind
            # their back. Physical removal clears the suppression; a later insertion is new
            # intent and follows the normal automatic provisioning flow again.
            log.info("deleted SIM remains inserted; automatic line creation is paused")
        elif info.get("iccid"):
            # A newly inserted SIM becomes a stopped line draft automatically. The UI only asks
            # for fields that cannot be learned from this hardware (for example a native reader
            # has no IMEI) and then promotes this same id through /api/provision.
            inst = await asyncio.to_thread(app_state._ensure_card_draft, info)
            if inst:
                info["matched"] = inst["id"]
    app_state.hub.cards[name] = info
    if _draft_needs_card_read(name):
        asyncio.create_task(_complete_draft_from_card(name))
    log.info("card inserted reader=%s (%s) identity=%s matched=%s", idx, name,
             "available" if info["iccid"] else "unknown", info["matched"])
    if info.get("matched"):
        asyncio.create_task(app_state._auto_start_hotplugged_line(str(info["matched"])))


def _line_auto_start_allowed(inst: dict) -> tuple[bool, str]:
    """Whether background maintenance may create this line's engine right now.

    A saved line is not proof that its SIM is inserted: offline lines deliberately remain in
    config so their history and settings survive.  Every non-interactive start/recovery must
    therefore re-check the live card monitor and the physical device's VoWiFi desired state.
    Explicit user starts keep their existing PIN/card preflight and actionable API errors.
    A reader's VoWiFi switch is the line's ``enabled`` flag.
    """
    if not inst.get("enabled", True):
        return False, "line_disabled"
    iid = str(inst.get("id") or "")
    iccid = str(inst.get("iccid") or "")
    cards = app_state.hub.cards_list()
    card_info = next((item for item in cards if item.get("present") and (
        (iccid and str(item.get("iccid") or "") == iccid)
        or str(item.get("matched") or "") == iid)), None)
    if card_info is None:
        return False, "no_card"
    return True, ""


_DRAFT_FIELD_KEYS = {"IMSI": "imsi", "MCC/MNC": "mcc_mnc", "IMEI": "imei", "SIM PIN": "pin"}


# Draft fields that only a card read can supply; the rest come from the operator.
_CARD_READ_FIELDS = {"IMSI", "MCC/MNC"}
# Seen on an Alcor AK9563: an early read at insertion can come back incomplete where a later
# read succeeds. Retry on a widening schedule, then leave it to the operator, who is shown the
# missing field and a re-read button.
_CARD_REREAD_DELAYS = (10, 30, 60, 120, 300)


def _merge_card_read(entry: dict, card) -> bool:
    """Fill what an earlier read of the same card left empty. Returns whether anything did.

    Identity is only ever added, never replaced: a read that fails half way must not erase
    what a better one found. PIN retry counters are live state and are always refreshed.
    """
    if not card.iccid or str(card.iccid) != str(entry.get("iccid") or ""):
        return False
    filled = False
    for key, value in (("imsi", card.imsi), ("mcc", card.mcc), ("mnc", card.mnc),
                       ("mnc_len", getattr(card, "mnc_len", None)), ("smsc", card.smsc)):
        if value not in (None, "") and not entry.get(key):
            entry[key] = value
            filled = True
    if filled:
        entry["carrier_identity"] = entry.get("carrier_identity") or app_state._carrier_identity(card)
    for key in ("pin_enabled", "pin_tries"):
        if getattr(card, key, None) is not None:
            entry[key] = getattr(card, key)
    return filled


async def _reread_card(name: str) -> bool:
    """Read the card in reader `name` again and merge what the first read missed."""
    entry = app_state.hub.cards.get(name)
    if not entry or not entry.get("present") or app_state.hub.lpa_busy.get(name):
        return False
    lock = app_state.hub.reader_lock(name)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=0.5)
    except asyncio.TimeoutError:
        return False
    try:
        card = await asyncio.to_thread(sim.read_card, entry["index"])
    except Exception as exc:  # noqa: a failed read leaves the cache as it was
        log.debug("card re-read failed for %s: %r", name, exc)
        return False
    finally:
        lock.release()
    return _merge_card_read(entry, card)


def _draft_needs_card_read(name: str) -> str:
    """The draft id behind reader `name` if a card read could still complete it, else ''."""
    entry = app_state.hub.cards.get(name) or {}
    iid = str(entry.get("matched") or "")
    inst = cfg.get_instance(iid) if iid and entry.get("present") else None
    if not inst or inst.get("provisioning_state") != "draft":
        return ""
    missing = app_state._draft_missing(inst, entry, app_state.hub.cards_list())
    return iid if set(missing) & _CARD_READ_FIELDS else ""


async def _complete_draft_from_card(name: str):
    """Re-read a draft's card on a widening schedule while it lacks card-only fields."""
    if name in app_state.hub.card_rereads:
        return
    app_state.hub.card_rereads.add(name)
    try:
        for delay in _CARD_REREAD_DELAYS:
            await asyncio.sleep(delay)
            iid = _draft_needs_card_read(name)
            if not iid:
                return
            if await _reread_card(name) and not _draft_needs_card_read(name):
                log.info("draft %s: a later card read supplied what the first one missed", iid)
                asyncio.create_task(app_state._auto_start_hotplugged_line(iid))
                return
        if _draft_needs_card_read(name):
            log.info("draft behind %s still lacks card data after %d re-reads",
                     name, len(_CARD_REREAD_DELAYS))
    finally:
        app_state.hub.card_rereads.discard(name)


async def _on_card_remove(entry: dict, reader_unplugged: bool = False) -> bool:
    """Card pulled from a reader, or (reader_unplugged) the whole reader disconnected.
    Stops the SIP engine container serving that card. The entry must be the reader's
    LAST-KNOWN state (name/matched/iccid) — the caller must not blank it first.
    Returns True when a running line was stopped."""
    name, idx = entry.get("name", ""), entry.get("index")
    matched, iccid = entry.get("matched"), entry.get("iccid")
    if iccid:
        await asyncio.to_thread(cfg.unsuppress_card, iccid)
    if not reader_unplugged:
        app_state.hub.cards[name] = {"index": idx, "name": name, "present": False, "iccid": None,
                           "matched": None, "imsi": None, "pin_enabled": None,
                           "pin_tries": None}
    log.info("%s reader=%s (%s) (identity=%s matched=%s)",
             "reader unplugged" if reader_unplugged else "card removed",
             idx, name, "available" if iccid else "unknown", matched)
    target = None
    if matched:
        target = cfg.get_instance(matched)
    if target is None and iccid:
        target = app_state._match_instance_by_iccid(iccid)
    if target is None:
        # Unknown/unmatched identity: map by the reader NAME the running engine reports
        # using (pin_status.json). This is the only safe fallback — guessing "the single
        # running instance" could stop a healthy line on ANOTHER reader.
        target = await asyncio.to_thread(app_state._find_running_by_reader, name)
    # Card removal is an explicit terminal condition for the current run. Cancel a frozen
    # cooldown even when its container was already removed: otherwise that in-memory recovery
    # timer can recreate an engine minutes after the SIM disappeared.
    if target:
        app_state.hub.reset_health(str(target["id"]), "no_card")
    if target and await asyncio.to_thread(engine.is_running, str(target["id"])):
        # Stop the SIP server + docker container on card/reader removal.
        await asyncio.to_thread(engine.stop, str(target["id"]))
        await app_state.hub.drop_ami(str(target["id"]))
        await app_state.hub.broadcast({"type": "engine", "instance": target["id"],
                             "event": "reader_lost" if reader_unplugged else "card_removed",
                             "args": [name]})
        stopped_status = {"state": "NO_CARD",
                          "label": "Reader unplugged" if reader_unplugged
                                   else "No SIM card (removed)",
                          "reason_code": "no_card", "reason": "SIM card is not available.",
                          "detail": {}}
        stopped_status = _with_status_activity(str(target["id"]), stopped_status)
        app_state.hub.status_cache[str(target["id"])] = stopped_status
        app_state.hub.status_sampled_at[str(target["id"])] = time.monotonic()
        await app_state.hub.broadcast({"type": "status", "instance": str(target["id"]),
                             **stopped_status})
        return True
    return False


async def card_monitor():
    """Real-time monitor for BOTH reader hotplug (plug/unplug) and card insert/remove.
    State is keyed by reader NAME: PC/SC indices shift when a reader is unplugged, so
    names are the stable identity; each entry's `index` field is refreshed every scan for
    the API calls that take reader_index. Between scans it blocks in
    card.wait_for_change (PnP-aware SCardGetStatusChange), so hotplug is reflected
    near-instantly without hammering pcscd."""
    first = True
    while True:
        try:
            states = await asyncio.to_thread(card.reader_states)
            if states is None:
                # Transient PC/SC error (pcscd restarting?) — NOT "all readers gone".
                # Skip this cycle; keep known state and engines untouched.
                log.debug("card monitor: PC/SC unavailable, skipping scan")
                await asyncio.sleep(1.2)
                continue
            current = {st["name"]: st for st in states}
            changed = False

            # The host orchestrator briefly restarts pcscd after changing generated VPCD reader
            # stanzas.  Treat that explicit maintenance window as enumeration churn, not as a
            # physical unplug, so healthy engine containers are not stopped.
            maintenance = False
            marker = os.path.join(cfg.DATA_DIR, "orchestrator", "pcsc-maintenance")
            try:
                # Rebuilding sing-box, ModemManager ownership and all virtual readers can take
                # more than 15 seconds on a Pi. Keep this comfortably above the observed full
                # orchestrator restart time so planned churn cannot be mistaken for an unplug.
                maintenance = (time.time() - os.path.getmtime(marker)
                               < PCSC_MAINTENANCE_WINDOW_SECONDS)
            except OSError:
                pass
            if maintenance:
                await asyncio.sleep(0.5)
                continue

            # reader unplugged -> drop its row + stop any engine bound to it
            for name in [n for n in app_state.hub.cards if n not in current]:
                entry = app_state.hub.cards.pop(name)
                stopped = await _on_card_remove(entry, reader_unplugged=True)
                if not stopped:
                    # _on_card_remove already broadcast the (more informative)
                    # "reader_lost — line stopped" event; only emit the generic one
                    # when no line was affected, so the UI shows a single toast.
                    await app_state.hub.broadcast({"type": "engine", "instance": "",
                                         "event": "reader_removed", "args": [name]})
                changed = True

            for name, st in current.items():
                entry = app_state.hub.cards.get(name)
                # LPA holds the reader exclusively and enable/disable triggers REFRESH
                # (looks like remove+insert). Keep last-known state; skip insert/remove.
                if app_state.hub.lpa_busy.get(name):
                    if entry is None:
                        app_state.hub.cards[name] = {**st, "iccid": None, "matched": None,
                                           "imsi": None, "pin_enabled": None,
                                           "pin_tries": None}
                        changed = True
                    elif entry.get("index") != st["index"]:
                        entry["index"] = st["index"]
                        changed = True
                    continue
                if entry is None:
                    # reader newly plugged in (or first scan after manager start)
                    if not first:
                        log.info("reader plugged in: %s", name)
                        await app_state.hub.broadcast({"type": "engine", "instance": "",
                                             "event": "reader_added", "args": [name]})
                    if st["present"]:
                        await _on_card_insert(name, st["index"])
                    else:
                        app_state.hub.cards[name] = {**st, "iccid": None, "matched": None,
                                           "imsi": None, "pin_enabled": None,
                                           "pin_tries": None}
                    changed = True
                    continue
                if entry.get("index") != st["index"]:
                    entry["index"] = st["index"]     # indices shift on unplug
                    # The physical reader behind this name/index may have changed — refresh the
                    # stable USB port binding so the display + ICCID->port learning stay correct.
                    try:
                        entry["reader_port"] = await asyncio.to_thread(
                            usbreader.port_for_index, st["index"])
                    except Exception:  # noqa
                        pass
                    changed = True
                if bool(entry.get("present")) != st["present"]:
                    # eUICC REFRESH during LPA looks like remove+insert — keep last-known
                    # state and do not stop engines / probe until the LPA op finishes.
                    if app_state.hub.lpa_busy.get(name):
                        entry["present"] = st["present"]
                        changed = True
                        continue
                    if st["present"]:
                        await _on_card_insert(name, st["index"])
                    else:
                        await _on_card_remove(entry)
                    changed = True
            # The first completed scan is always announced, even when it found nothing:
            # it is what turns the UI's "detecting devices" state into a real answer.
            if changed or first:
                await app_state.hub.broadcast({"type": "cards", "cards": app_state._client_cards()})
            # Only a completed scan counts: a failed first scan must retry as "first"
            # (readers seen later may belong to already-running engines).
            app_state.hub.scanned = True
            first = False
        except Exception as e:  # noqa
            log.debug("card monitor error: %r", e)
        # Instant wake on any reader/card change; the timeout bounds the worst case for
        # changes that slip between a scan and the next wait (fresh-snapshot window).
        # The short sleep bounds the rescan rate if something reports changes endlessly.
        await asyncio.to_thread(card.wait_for_change, 2.5)
        await asyncio.sleep(0.25)


# Lines of the engine log that carry the public identity the carrier bound at registration.
# "IMS public identity" is the patched Asterisk's own NOTICE (engine/patches/asterisk/
# ims_public_identity_log.py), emitted on every ordinary REGISTER and refresh — reading it
# costs nothing and perturbs nothing. The raw header is still matched so a container running
# with Asterisk debug (SIP tracing) enabled is read just the same.
_IMS_IDENTITY_LINE = re.compile(
    r'^.*(?:IMS public identity:|P-Associated-Uri:).*$', re.I | re.M)
_IMS_IDENTITY_NUMBER = re.compile(r'(?:tel|sip):(\+\d{5,})', re.I)


def extract_msisdn(iid):
    """Learn the registered MSISDN from the P-Associated-URI the engine logged.

    The newest identity wins: a ported number is the same SIM being answered with a different
    public identity, so the last registration is the only one that still describes the line.
    Within one line the first dialable URI wins — carriers list the IMSI-derived IMPU (no
    number in it) alongside the E.164 identities, and the first `+` URI is the primary one.
    """
    logs = engine.logs(iid, MSISDN_LOG_TAIL_LINES)
    for line in reversed(_IMS_IDENTITY_LINE.findall(logs)):
        found = _IMS_IDENTITY_NUMBER.search(line)
        if found:
            return found.group(1)
    return None


def _needs_ims_msisdn_learning(inst: dict) -> bool:
    """Whether the line number still has to be read out of the IMS registration.

    ModemManager OwnNumbers is only a hint: modems commonly retain a stale value across SIM
    swaps, omit the leading '+', or expose a service number instead of the IMS public identity.
    Only an unknown or hinted number justifies the initial learning loop; a number already learned
    from IMS is re-checked on a much slower cadence (see _verify_ims_msisdn).
    """
    return (not str(inst.get("msisdn") or "").strip()
            or inst.get("msisdn_source") == "modemmanager")


async def _verify_ims_msisdn(iid: str, inst: dict) -> None:
    """Follow the number the carrier hands out at registration.

    A ported number is exactly this: the same SIM, registering normally, answered with a
    different public identity. Treating the first IMS answer as permanent left the line
    presenting its previous number as caller identity indefinitely.

    This reads the identity the engine already logged for its own registrations. It used to
    force one REGISTER with the SIP packet logger enabled instead, which some IMS cores answer
    with "503 Service Unavailable" (issue #8) — a rejected registration that the health policy
    then acts on, so asking a healthy line for its number was enough to take it down.

    A manually entered number is never overridden: that is a deliberate operator choice.
    """
    if inst.get("msisdn_source") != "ims":
        return
    # A freshly booted host can have a monotonic clock below the interval.  Treat a missing
    # entry as "never checked" instead of comparing it with the clock's zero point.
    if (iid in app_state.hub._msisdn_checked
            and time.monotonic() - app_state.hub._msisdn_checked[iid]
            < MSISDN_VERIFY_INTERVAL_SECONDS):
        return
    app_state.hub._msisdn_checked[iid] = time.monotonic()
    try:
        observed = await asyncio.to_thread(extract_msisdn, iid)
    except Exception as exc:  # noqa: a transient log read failure is retried on the next interval
        log.debug("IMS number verification failed for line %s: %s", iid, type(exc).__name__)
        app_state.hub._msisdn_checked[iid] = (
            time.monotonic() - MSISDN_VERIFY_INTERVAL_SECONDS
            + MSISDN_VERIFY_FAILURE_RETRY_SECONDS)
        return
    stored = str(inst.get("msisdn") or "")
    if not observed or observed == stored:
        return
    log.warning("line %s number changed at the carrier: %s -> %s", iid, stored, observed)
    # instance.json and Asterisk's dialplan are snapshots taken at container start, so the
    # line would keep presenting the old number as caller identity until it is rebuilt. Rebuild
    # BEFORE committing the new number: if fail-closed egress or Docker rejects the rebuild, the
    # stored old value makes the next verification retry instead of declaring a half-applied
    # change complete.
    candidate = {**inst, "id": iid, "msisdn": observed, "msisdn_source": "ims"}
    try:
        await app_state.hub.drop_ami(iid)
        await asyncio.to_thread(app_state._start_engine_checked, candidate, cfg.get_settings(),
                                os.environ.get("MDD_DEV_MOUNTS", "") == "1",
                                "number-changed")
        updated = await asyncio.to_thread(
            cfg.upsert_instance, {"id": iid, "msisdn": observed, "msisdn_source": "ims"})
    except Exception as exc:  # noqa: background verification must never leak an unhandled task
        app_state.hub._msisdn_checked[iid] = (
            time.monotonic() - MSISDN_VERIFY_INTERVAL_SECONDS
            + MSISDN_VERIFY_FAILURE_RETRY_SECONDS)
        log.warning("IMS number change could not be applied for line %s (%s); will retry",
                    iid, type(exc).__name__)
        return
    client = app_state.hub.ami.get(iid)
    if client:
        client.msisdn = observed
    await app_state.hub.broadcast({"type": "engine", "instance": iid,
                         "event": "msisdn_updated", "args": []})


async def learn_msisdn(iid):
    """Read the line number out of the identity the engine logged for its own registration.

    Nothing is sent to the carrier. The previous implementation enabled the SIP packet logger
    and forced an extra REGISTER to produce a 200 OK it could read; some IMS cores answer that
    unsolicited re-registration with 503 (issue #8), which Asterisk reports as a rejected
    registration and the health policy acts on — so a line that had just come up was dropped
    purely by the attempt to learn its number. A registration the line makes anyway carries the
    same P-Associated-URI.

    An attempt that finds nothing holds the per-line learning slot for the retry interval, so
    the capped retries in _poll_instance_status span the settling period instead of being spent
    in one burst; each registration refresh re-announces the identity in any case.
    """
    try:
        msisdn = await asyncio.to_thread(extract_msisdn, iid)
        if not msisdn:
            # Hold this line's learning slot for the interval: the caller's attempt cap is
            # what stops the loop, and spending it in one burst would give up seconds after
            # the line registered instead of across the window a slow log tail needs.
            await asyncio.sleep(MSISDN_LEARN_RETRY_SECONDS)
            return
        current = cfg.get_instance(iid) or {}
        # IMS registration is authoritative and may correct an OwnNumbers value
        # previously learned from ModemManager. Never overwrite a manual value.
        if current.get("msisdn") and current.get("msisdn_source") != "modemmanager":
            return
        identity_changed = str(current.get("msisdn") or "") != msisdn
        updated = cfg.upsert_instance({"id": iid, "msisdn": msisdn,
                                       "msisdn_source": "ims"})
        c = app_state.hub.ami.get(iid)
        if c:
            c.msisdn = msisdn
        log.info("learned line number for instance %s", iid)
        # instance.json and Asterisk's pjsip/dialplan are snapshots from container start.
        # When IMS corrected a ModemManager hint, persist-only is insufficient: outgoing
        # INVITEs would keep sending the stale P-Preferred-Identity and the carrier would
        # immediately terminate them (observed as 487 -> browser-side 603). Recreate the
        # running engine once so registration, From and PPI all use the authoritative value.
        if identity_changed and await asyncio.to_thread(engine.is_running, iid):
            await app_state.hub.drop_ami(iid)
            await asyncio.to_thread(app_state._start_engine_checked, updated, cfg.get_settings(),
                                    os.environ.get("MDD_DEV_MOUNTS", "") == "1")
            app_state.hub.reset_health(iid, "configuration_restart")
            log.info("restarted instance %s to apply IMS line identity", iid)
        await app_state.hub.broadcast({"type": "engine", "instance": iid, "event": "msisdn", "args": [msisdn]})
    except Exception as e:  # noqa
        log.debug("learn_msisdn error: %r", e)
    finally:
        app_state.hub._learning.discard(iid)


def _line_state_kind(st: dict) -> str | None:
    """Map the status machine onto the states the connectivity timeline records.

    Returns None when a sample carries no evidence about connectivity, which must not be
    written as a disconnect. compute() only reaches REGISTERING after the tunnel is
    installed, so a registration of "unknown" there means the read itself failed — the
    management timeout this codebase already refuses to treat as a carrier failure. Charting
    it as an outage makes a healthy line look like it keeps dropping.
    """
    state = str((st or {}).get("state") or "").upper()
    if state == "OK":
        return "up"
    if state == "STOPPED":
        return "off"
    detail = (st or {}).get("detail") or {}
    if state == "REGISTERING" and str(detail.get("registration") or "unknown").lower() == "unknown":
        return None
    return "down"


def _outage_detail(st: dict) -> str:
    """Compact structured evidence behind a disconnect's reason code.

    The database keeps this JSON as text for schema compatibility. The WebUI localises its
    evidence code into a short "which side failed to answer what" sentence; older free-form
    rows still render unchanged.
    """
    code = str((st or {}).get("reason_code") or "")
    detail = (st or {}).get("detail") or {}
    fqdn = str(detail.get("epdg_fqdn") or "")
    def evidence(evidence_code: str, **values) -> str:
        return json.dumps({"code": evidence_code,
                          **{key: value for key, value in values.items() if value not in (None, "", [])}},
                         ensure_ascii=False, separators=(",", ":"))

    if code == "epdg_unresolved":
        return evidence("client_dns_unresolved", peer=fqdn,
                        servers=[str(s) for s in (detail.get("nameservers") or [])])
    if code == "tunnel_child_rekey_timeout":
        return evidence("server_epdg_child_rekey_unanswered", peer=fqdn)
    if code == "tunnel_ike_rekey_timeout":
        return evidence("server_epdg_ike_rekey_unanswered", peer=fqdn)
    if code == "tunnel_rekey_send_error":
        return evidence("client_rekey_send_failed", peer=fqdn)
    if code == "tunnel_network":
        return evidence("server_epdg_ike_unanswered", peer=fqdn)
    if code == "tunnel_sim_auth":
        return evidence("client_sim_auth_failed", peer=fqdn)
    if code == "tunnel_not_authorized":
        return evidence("server_epdg_identity_rejected", peer=fqdn)
    if code == "tunnel_proposal":
        return evidence("server_epdg_proposal_rejected", peer=fqdn)
    if code == "tunnel_setup":
        # CONNECTING is a recovery state, not a root cause. If no timeout, rejection or
        # local send failure has appeared yet, say the earlier fault was not captured
        # instead of presenting the rebuild itself as the reason for the outage.
        return evidence("tunnel_cause_not_captured", peer=fqdn)
    if code == "reg_unanswered":
        return evidence("server_pcscf_register_unanswered", peer=detail.get("pcscf"))
    if code in {"reg_rejected", "reg_reauth_failed"}:
        return evidence("server_pcscf_sip_rejected", peer=detail.get("pcscf"),
                        status=detail.get("sip_status"))
    if code == "registering":
        return evidence("client_registration_incomplete", peer=detail.get("pcscf"))
    if code == "maintenance_rebuild":
        return evidence("client_maintenance_rebuild")
    if code == "client_engine_failure":
        return evidence("client_engine_worker_failed")
    return ""


async def _record_line_state(iid: str, st: dict) -> None:
    """Persist one observation, skipping writes that would only repeat the known state.

    Status is sampled every few seconds; committing that to SQLite unchanged would be a
    constant write load on the SD card an appliance boots from. A transition is always
    written immediately — it is the event the timeline exists to show — and an unchanged
    state is refreshed often enough to stay well inside the segment continuity window.
    """
    iid = str(iid)
    kind = _line_state_kind(st)
    if kind is None:
        # Leave the timeline untouched. A brief blind spot is absorbed by the segment
        # continuity window; a long one falls outside it and surfaces as `unknown`, which is
        # exactly what the record can honestly claim. Forget the last write so the next real
        # observation is committed immediately rather than waiting out the refresh interval.
        _line_state_written.pop(iid, None)
        return
    previous = _line_state_written.get(iid)
    if (previous and previous[0] == kind
            and time.monotonic() - previous[1] < LINE_STATE_WRITE_INTERVAL_SECONDS):
        return
    _line_state_written[iid] = (kind, time.monotonic())
    # Only a disconnect needs explaining. The first down sample carries the cause; the store
    # keeps it for the whole segment.
    reason = str((st or {}).get("reason_code") or "") if kind == "down" else ""
    try:
        await asyncio.to_thread(store.record_line_state, iid, kind, reason=reason,
                                detail=_outage_detail(st) if reason else "")
    except Exception as exc:  # noqa
        # History is diagnostic only; never let it interrupt status sampling.
        _line_state_written.pop(iid, None)
        log.debug("line state record failed instance=%s: %r", iid, exc)


def _status_poll_delay(instances: list[dict]) -> float:
    """Fast only while a running line is actively converging.

    Enabled lines can legitimately have no container while their reader is absent. PC/SC and
    Docker events wake those paths immediately, so treating STOPPED/NO_CARD as perpetually busy
    kept the whole gateway on the four-second cadence for no useful reason.
    """
    active = []
    for inst in instances:
        if not inst.get("enabled", True):
            continue
        active.append((app_state.hub.status_cache.get(str(inst["id"])) or {}).get("state"))
    return (STATUS_POLL_FAST_SECONDS
            if any(state in {"REGISTERING", "TUNNEL_DOWN"} for state in active)
            else STATUS_POLL_HEALTHY_SECONDS)


MEDIA_SUPERVISE_SECONDS = 30
MEDIA_CONVERGE_SECONDS = 15


MEDIA_RETRY_FIRST_SECONDS = 60
MEDIA_RETRY_MAX_SECONDS = 600
# iid -> {"at": monotonic time of the next attempt, "failures": consecutive failures}
_media_retry: dict[str, dict] = {}


async def _media_converge_once() -> bool:
    """Rebuild one running line whose container was made for the other media mode. One per
    pass, so the lines re-register one after another instead of all at once. True if a line
    was rebuilt.

    A line that cannot be rebuilt (its exit is down, the uplink network is missing, the line
    limit refuses it) fails before its old container is touched, so it would be picked again
    on every pass and hold up every line after it. It backs off on its own instead, and the
    pass moves on to the next line."""
    wanted = media.mode()
    now = time.monotonic()
    for inst in cfg.list_instances():
        iid = str(inst.get("id") or "")
        if not iid:
            continue
        current = await asyncio.to_thread(engine.media_mode_of, iid)
        if current is None or current == wanted:
            _media_retry.pop(iid, None)
            continue
        retry = _media_retry.get(iid) or {}
        if now < retry.get("at", 0.0):
            continue
        log.info("line %s: rebuilding for %s media mode", iid, wanted)
        app_state._record_lifecycle(iid, "media_mode_rebuild", reason_code=wanted)
        try:
            await asyncio.to_thread(app_state._start_engine_checked, cfg.get_instance(iid) or inst,
                                    cfg.get_settings(),
                                    os.environ.get("MDD_DEV_MOUNTS", "") == "1", "media_mode")
        except Exception as exc:  # noqa: BLE001 - this line waits, the others go on
            _media_back_off(iid, retry, now, exc)
            continue
        # The old container is gone only now; its AMI connection goes with it.
        await app_state.hub.drop_ami(iid)
        app_state.hub.reset_health(iid, "configuration_restart")
        started = await asyncio.to_thread(engine.media_mode_of, iid)
        if started is not None and started != wanted:
            # Started, but not in the wanted mode: relay-pending, because the media network
            # could not be prepared. That start succeeded, so without this the line would be
            # rebuilt, and made to register again, on every pass for as long as the cause
            # lasts. It waits like a failure instead.
            _media_back_off(iid, retry, now, f"started as {started}")
        else:
            _media_retry.pop(iid, None)
        return True
    return False


def _media_back_off(iid: str, retry: dict, now: float, reason) -> None:
    failures = int(retry.get("failures", 0)) + 1
    delay = min(MEDIA_RETRY_MAX_SECONDS, MEDIA_RETRY_FIRST_SECONDS * 2 ** (failures - 1))
    _media_retry[iid] = {"at": now + delay, "failures": failures}
    log.warning("line %s: not in the recorded media mode, retrying in %ds: %s",
                iid, delay, reason)


async def media_supervisor():
    """Keep the relay running in relay mode and move running lines to the recorded mode.

    The mode is switched outside this process (python -m app.media), so it is read from its
    file on every pass rather than cached. A change to that file is acted on at the next pass
    instead of waiting for the next periodic check, so the relay counts as ready within
    MEDIA_CONVERGE_SECONDS of a switch."""
    last_supervise = 0.0
    last_state_mtime = None
    while True:
        try:
            try:
                state_mtime = os.stat(media._state_path()).st_mtime
            except OSError:
                state_mtime = None
            if (state_mtime != last_state_mtime
                    or time.monotonic() - last_supervise >= MEDIA_SUPERVISE_SECONDS):
                last_supervise = time.monotonic()
                last_state_mtime = state_mtime
                await asyncio.to_thread(media.supervise)
                await asyncio.to_thread(engine.reconcile_rtp_forward)
            await _media_converge_once()
        except Exception as exc:  # noqa: BLE001 - supervision must never stop
            log.warning("media supervision failed: %s", exc)
        await asyncio.sleep(MEDIA_CONVERGE_SECONDS)


def _line_media_report(iid: str) -> dict:
    """The engine's own report on its media interface (engine/entrypoint.sh), relay mode."""
    return engine.read_run_json(iid, "media.json") or {}


def _line_media_state(iid: str) -> str:
    return str(_line_media_report(iid).get("state") or "starting")


async def status_poller():
    last_prune = 0.0
    while True:
        # Clear before sampling: an event arriving during the work remains set and causes an
        # immediate next pass instead of being lost between the sample and the wait.
        app_state.hub.status_wakeup.clear()
        instances = []
        try:
            instances = cfg.list_instances()
            await asyncio.gather(*(_poll_instance_status(inst)
                                   for inst in instances))

            if time.monotonic() - last_prune >= LINE_HISTORY_PRUNE_INTERVAL_SECONDS:
                last_prune = time.monotonic()
                await asyncio.to_thread(store.prune_line_states,
                                        int(time.time()) - store.LINE_STATE_RETENTION_SECONDS)
        except Exception as e:  # noqa
            log.debug("poller error: %r", e)
        try:
            await asyncio.wait_for(app_state.hub.status_wakeup.wait(), timeout=_status_poll_delay(instances))
        except asyncio.TimeoutError:
            pass


HOST_ALERT_POLL_SECONDS = 60.0
# Re-announce a condition that simply persists, rather than staying silent for days.
HOST_ALERT_REPEAT_SECONDS = float(os.environ.get("MDD_HOST_ALERT_REPEAT", "21600"))
# A condition has to stay absent this long before it counts as recovered. Without it, a
# measurement sitting near its threshold crosses back and forth all day and each re-entry
# looks like a new problem worth notifying about.
HOST_ALERT_CLEAR_SECONDS = float(os.environ.get("MDD_HOST_ALERT_CLEAR", "1800"))

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


async def host_health_poller():
    """Announce host conditions that take every line down at once.

    Displaying this only in a panel is not enough: the whole point is that it explains an
    outage nobody would otherwise attribute to the box. Notifications fire on the transition
    into a condition, then only on a long repeat interval, so a persistent brown-out does not
    become a stream nobody reads.

    The suppression state is persisted because it is measured in hours: keeping it in memory
    meant every manager restart re-announced everything that was already known, and an
    appliance is restarted for upgrades far more often than these conditions change.
    """
    if app_state.hub.host_alert_state is None:
        app_state.hub.host_alert_state = app_state._load_host_alert_state()
    state = app_state.hub.host_alert_state
    streaks: dict[str, int] = {}
    previous_alerts = None
    while True:
        try:
            # Docker df walks every image-layer xattr on DSM and can occupy dockerd for minutes.
            # The minute health sampler needs host health, not an inventory of reclaimable
            # layers; explicit cleanup actions refresh that inventory after the operator asks.
            snapshot = await asyncio.to_thread(
                sysinfo.collect, cfg.DATA_DIR, include_docker_storage=False)
            # Rate-based conditions need the previous sample; the first pass reports none.
            alerts = sysinfo.alerts(snapshot, app_state.hub.host_snapshot or None)
            alerts = _sustained_alerts(alerts, streaks)
            now = time.time()
            codes = {item["code"] for item in alerts}
            for code, entry in list(state.items()):
                if code in codes:
                    entry.pop("missing_since", None)
                    continue
                missing_since = entry.setdefault("missing_since", now)
                if now - missing_since >= HOST_ALERT_CLEAR_SECONDS:
                    state.pop(code, None)       # genuinely recovered; a return may notify again
            visible = app_state._visible_host_alerts(alerts, state)
            app_state.hub.host_snapshot, app_state.hub.host_alerts = snapshot, visible
            fresh = [item for item in visible
                     if now - float((state.get(item["code"]) or {}).get("at", 0))
                     >= HOST_ALERT_REPEAT_SECONDS]
            if fresh:
                for item in fresh:
                    state[item["code"]] = {"at": now}
                    log.warning("host alert %s (%s): %s", item["code"], item["severity"],
                                item.get("detail"))
                await app_state.hub.broadcast({"type": "host_alert",
                                     "alerts": [item["code"] for item in fresh]})
            if fresh or codes != previous_alerts:
                previous_alerts = codes
                await asyncio.to_thread(app_state._save_host_alert_state, state)
        except Exception as exc:  # noqa
            log.debug("host health poll failed: %r", exc)
        await asyncio.sleep(HOST_ALERT_POLL_SECONDS)


def _sustained_alerts(alerts: list[dict], streaks: dict[str, int]) -> list[dict]:
    """Drop conditions that have not held long enough to be worth acting on.

    A one-sample spike is real but not actionable: it is what a container start costs on this
    hardware. Reporting it anyway is how an indicator earns the reputation that makes people
    ignore the one explaining a genuine outage.
    """
    present = {item["code"] for item in alerts}
    for code in list(streaks):
        if code not in present:
            del streaks[code]
    kept = []
    for item in alerts:
        code = item["code"]
        if code not in SUSTAINED_ALERT_CODES:
            kept.append(item)
            continue
        streaks[code] = streaks.get(code, 0) + 1
        if streaks[code] >= SUSTAINED_ALERT_SAMPLES:
            kept.append({**item, "detail": {**(item.get("detail") or {}),
                                            "samples": streaks[code]}})
    return kept


def _save_exit_ledgers() -> None:
    path = app_state._exit_ledger_path()
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(app_state.hub.exit_ledgers, handle)
        os.replace(temporary, path)
    except OSError as exc:
        log.debug("cannot persist exit failover ledger: %r", exc)


def _peer_line_registered(iid: str, country: str) -> bool:
    """Whether another line of the same country is registered right now.

    Lines of one country share one exit. A registered peer is living proof the exit can carry
    IMS — and a tunnel that moving the exit would tear down, which is the disruption this
    design exists to avoid. While one holds, eviction is off the table.
    """
    if not country:
        return False
    for other in cfg.list_instances():
        oid = str(other.get("id") or "")
        if oid == str(iid) or not other.get("enabled", True):
            continue
        if egress.line_country(other) != country:
            continue
        if (app_state.hub.status_cache.get(oid) or {}).get("state") == "OK":
            return True
    return False


def _distinct_cards_present() -> int:
    """How many different SIMs this host can currently see.

    Decides only how a SW=9862 is worded, so an unreadable cache falls back to the cautious
    plural reading rather than asserting a single-SIM host that may not be one.
    """
    try:
        return len({str(entry.get("iccid") or "")
                    for entry in app_state.hub.cards.values() if entry.get("iccid")}) or 2
    except Exception:  # noqa - the card cache is a hint here, never the decision itself
        return 2


def _local_card_fault(iid: str, inst: dict) -> str:
    """Why this line's own reader binding, not its exit, explains the freeze — or "".

    No exit node can make a line open the wrong SIM, yet that failure arrives looking exactly
    like one the exit caused: the tunnel comes up, IMS registration is rejected, and the freeze
    lands on the failover path. Attributed to the exit it walks the whole candidate pool and
    tears down sibling lines' working tunnels, while the real fault — a reader binding pointing
    at another line's card — sits untouched and unmentioned for as long as anyone is willing to
    watch containers rebuild. Checked before classify() so the exit is never blamed for it.
    """
    pin = engine.read_run_json(iid, "pin_status.json") or {}
    swu = engine.read_run_json(iid, "swu_status.json") or {}
    usim = engine.read_run_json(iid, "usim_status.json") or {}
    # pin_keeper, ami_usim and swu_ike each check the card they were handed, so any of the three
    # can be the one that caught it.
    if "WRONG_CARD" in (pin.get("state"), usim.get("state"), swu.get("state")):
        detail = str(pin.get("detail") or usim.get("detail") or "").strip()
        if not detail and swu.get("iccid"):
            detail = f"the tunnel reader holds ICCID {swu['iccid']}"
        return detail or "the bound reader holds another line's SIM"
    # A reader NAME that differs from the stored one is not by itself a fault. The USB-port
    # binding exists precisely so a line keeps opening the reader that physically holds its SIM
    # after pcscd renames or re-enumerates it. When the card in that reader identified itself as
    # this line's, the name is settled evidence of nothing and must not be second-guessed:
    # treating it as a fault would hold the line forever and, worse, silently disable exit
    # failover for a line whose real problem is its exit. The name only speaks when the card
    # would not — which is exactly the case the check was added for.
    card_iccid = str(pin.get("iccid") or "").strip()
    line_iccid = str(inst.get("iccid") or "").strip()
    card_confirmed = bool(card_iccid and line_iccid and card_iccid == line_iccid)
    # Only full PC/SC names are comparable: legacy numeric indexes and imsi:/iccid: specs name
    # a search, not a slot.
    bound = str(inst.get("pin_reader") or "").strip()
    opened = str(pin.get("reader") or "").strip()
    comparable = bound and not bound.isdigit() and not bound.startswith(("imsi:", "iccid:"))
    if not card_confirmed and comparable and opened and opened != bound:
        return f"the engine opened {opened!r} but this line is bound to {bound!r}"
    # SW=9862 is "incorrect MAC": the card computed a different response to the carrier's AKA
    # challenge than the network expected. Which cause that points at depends on how many SIMs
    # the host can even see — a mix-up is physically impossible with one, and naming it anyway
    # sends the operator to the hardware when the answer is on the carrier's side.
    if "9862" in str(usim.get("detail") or ""):
        if _distinct_cards_present() > 1:
            return ("USIM AUTHENTICATE returned SW=9862 (incorrect MAC) — with more than one "
                    "SIM on this host, the reader is most likely holding another line's SIM")
        return ("USIM AUTHENTICATE returned SW=9862 (incorrect MAC) — the carrier rejected "
                "this SIM's key material; on a single-SIM host that points at provisioning "
                "or subscription, not at a reader mix-up")
    return ""


def _judge_exit_failure(iid: str, inst: dict, st: dict, stable_for: float) -> str:
    """Attribute one line freeze and act on it: hold, move the exit, back off, or stop.

    Runs on the freeze path only, so reading the tunnel's own evidence here costs nothing in
    the steady state. Both reads are deliberately cheap — the full diagnostic snapshot is
    captured separately and must not be on this decision's critical path.
    """
    iid = str(iid)
    local = _local_card_fault(iid, inst)
    if local:
        log.warning("line %s froze (%s): %s — a local binding fault, not evidence against "
                    "this line's exit", iid, st.get("reason_code"), local)
        return failover.HOLD
    country = egress.line_country(inst)
    exits = (egress.status().get("exits") or {}).get(country) or {}
    if (not cfg.get_settings().get("proxy", {}).get("enabled", False)
            or exits.get("mode") != "subscription"):
        # Direct, single-node and disabled routes have no pool to walk, so a ledger for them
        # is stale by definition.
        if app_state.hub.exit_ledgers.pop(iid, None) is not None:
            _save_exit_ledgers()
        return failover.HOLD
    if not exits.get("node"):
        # A subscription exit whose node is momentarily unknown: the host blanks it on every
        # status cycle until the Clash API answers, so a slow query or a sing-box restart
        # leaves it empty for one cycle. That says nothing about the exit — keep the walk
        # (tried, exhausted, given_up) intact and judge again on the next freeze.
        return failover.HOLD
    node = str(exits.get("node") or "")
    candidates = [str(name) for name in (exits.get("candidates") or [])]
    pinned = exits.get("selection") == "manual"
    peer_registered = _peer_line_registered(iid, country)
    swu, retransmits = "", None
    try:
        swu = (engine.read_run_json(iid, "swu_status.json") or {}).get("state") or ""
    except Exception as exc:  # noqa
        log.debug("cannot read tunnel evidence for line %s: %r", iid, exc)
    try:
        evidence = engine.ike_evidence(iid) or {}
        if evidence.get("available", True) and evidence.get("retransmits") is not None:
            retransmits = int(evidence["retransmits"])
    except Exception as exc:
        log.debug("cannot read IKE evidence for line %s: %r", iid, exc)
    verdict = failover.classify(swu, retransmits, stable_for,
                                egress.RESELECT_MIN_STABLE_SECONDS,
                                reason_code=st.get("reason_code") or "unknown")
    was_backing_off = bool((app_state.hub.exit_ledgers.get(iid) or {}).get("exhausted"))
    action, ledger = failover.record(app_state.hub.exit_ledgers.get(iid), verdict, node,
                                     pinned, candidates, peer_registered=peer_registered)
    app_state.hub.exit_ledgers[iid] = ledger
    _save_exit_ledgers()
    log.info("line %s froze (%s) after %.0fs healthy; tunnel=%s ike_retransmits=%s "
             "-> blames %s, action %s (node=%s strikes=%d tried=%d/%d peer=%s)",
             iid, st.get("reason_code"), stable_for, swu or "unknown", retransmits,
             verdict, action, node or "unknown", ledger.get("strikes") or 0,
             len(ledger.get("tried") or []), len(candidates), peer_registered)
    if action == failover.SWITCH:
        try:
            egress.request_reselect(inst, f"health-freeze:{st['reason_code']}",
                                    stable_for=stable_for)
        except Exception as exc:  # noqa
            log.warning("exit reselect request failed for line %s: %s", iid, exc)
    elif verdict == failover.BLAMES_EXIT and not peer_registered and country:
        # The exit is at fault and we are NOT moving off it — either the strike count is still
        # short, the pool is exhausted, or the node is pinned. Holding is right, but it leaves
        # one recoverable failure unattended: a sing-box session whose outbound died is kept
        # alive by this line's own IKE retransmits (each one refreshes the idle timer), so the
        # rebuild loop feeds packets to a connection that can never answer and the line never
        # recovers on its own. Ask the host to close that country's sessions so the next
        # attempt dials afresh.
        #
        # Safe by construction against the failover principles: it changes no node, respects a
        # pin, and the peer check above means no sibling line is registered over this exit, so
        # nothing that currently works is torn down.
        try:
            egress.report_stalled_exit(country, node, f"health-freeze:{st['reason_code']}", iid)
        except Exception as exc:  # noqa
            log.debug("stalled-exit report failed for line %s: %r", iid, exc)
    # Independent of the branch above, not an alternative to it: closing dead sessions and
    # telling the operator the line is stuck are different jobs, and a backed-off line needs
    # both. (Chaining this onto the same if/elif silently swallowed the notification.)
    if action in (failover.GIVE_UP, failover.REPORT) or (
            action == failover.BACK_OFF and not was_backing_off):
        text = failover.summarise(ledger, action, country, pinned)
        log.warning("line %s: %s", iid, text)
    return action


async def _poll_instance_status(inst: dict) -> None:
    """Sample one line in the background; slow carrier state never blocks HTTP pages."""
    iid = str(inst["id"])
    try:
        # One inspect supplies both running state and bridge IP to the whole sample. Previously
        # ami_for(), compute() and the grace-path each queried Docker independently.
        runtime = await app_state.hub.runtime.get(iid)
        app_state.hub.seed_restart_baseline(iid, runtime)
        # A disabled line is authoritative user intent. Automatic recovery must never
        # resurrect a stale container left behind by an earlier retry or process restart;
        # doing so can retain the SIM/PCSC channel and disrupt another active line.
        if not inst.get("enabled", True):
            # The poll list is a snapshot. A reader-enable request may have persisted ON after
            # this iteration began; re-read before enforcing OFF so an old snapshot cannot stop
            # the container that the request is about to start.
            current = await asyncio.to_thread(cfg.get_instance, iid)
            if current and current.get("enabled", True):
                inst = current
            else:
                if runtime["running"]:
                    await asyncio.to_thread(engine.stop, iid)
                    await app_state.hub.drop_ami(iid)
                app_state.hub.reset_health(iid, "line_disabled")
                stopped = _with_status_activity(iid, {
                    "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
                    "reason_code": "stopped", "reason": "Stopped.", "detail": {},
                    "retry": {"count": 0, "max": 0}})
                app_state.hub.status_cache[iid] = stopped
                app_state.hub.status_sampled_at[iid] = time.monotonic()
                await _record_line_state(iid, stopped)
                await app_state.hub.broadcast({"type": "status", "instance": iid, **stopped})
                return
        ami = await app_state.hub.ami_for(iid, runtime)
        st = await status_mod.compute(inst, ami, runtime)
        registration = str((st.get("detail") or {}).get("registration") or "unknown")
        previous = app_state.hub.status_cache.get(iid)
        previous_sampled_at = app_state.hub.status_sampled_at.get(iid)
        observed_at = time.monotonic()
        held_previous = False
        # A single management timeout is not evidence that a known-good registration vanished.
        # Hold the last confirmed OK briefly, but never refresh that timestamp from unknown
        # samples: a dead Asterisk must become unhealthy after the bounded grace period.
        if (st.get("state") == "REGISTERING" and registration == "unknown"
                and (previous or {}).get("state") == "OK"
                and observed_at - app_state.hub.status_sampled_at.get(iid, 0) <= STATUS_OK_GRACE_SECONDS
                and runtime["running"]):
            st = previous
            held_previous = True
        if st["state"] == "OK" and _needs_ims_msisdn_learning(inst) \
                and iid not in app_state.hub._learning and app_state.hub._msisdn_tries.get(iid, 0) < 4:
            app_state.hub._learning.add(iid)
            app_state.hub._msisdn_tries[iid] = app_state.hub._msisdn_tries.get(iid, 0) + 1
            asyncio.create_task(learn_msisdn(iid))
        elif st["state"] == "OK":
            asyncio.create_task(_verify_ims_msisdn(iid, inst))

        st = _with_status_activity(
            iid, apply_health(iid, inst, st, runtime.get("container_id")))
        app_state.hub.status_cache[iid] = st
        if held_previous and previous_sampled_at is not None:
            # apply_health(OK) clears health and its related cache bookkeeping. Restore the
            # original authoritative timestamp, never the current poll time, so unknown
            # samples cannot extend the grace window indefinitely.
            app_state.hub.status_sampled_at[iid] = previous_sampled_at
        elif not held_previous:
            app_state.hub.status_sampled_at[iid] = observed_at
        await _record_line_state(iid, st)
        await app_state.hub.broadcast({"type": "status", "instance": iid, **st})
    except Exception as exc:  # noqa
        log.debug("status sample failed instance=%s: %r", iid, exc)


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
        return _with_status_activity(iid, {
            "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
            "reason_code": "stopped", "reason": "Stopped.", "detail": {}})
    return _with_status_activity(iid, {
        "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
        "reason_code": "registering", "reason": "Refreshing line status…", "detail": {}})


def _with_status_activity(iid: str, st: dict) -> dict:
    """Explain the status machine in user terms: now, why, and what happens next."""
    st = dict(st)
    state = str(st.get("state") or "").upper()
    detail = st.get("detail") or {}
    retry = st.get("retry") or {}
    health = app_state.hub.health_for(str(iid))
    retrying = bool(health.get("auto_retrying"))
    remaining = st.get("automatic_retry_in")

    if retrying:
        current = "Rebuilding the VoWiFi line automatically"
        next_action = "The SIM will be read again, then ePDG and IMS will reconnect."
    elif st.get("frozen") and remaining:
        current = "Automatic recovery is waiting"
        next_action = "The VoWiFi line will be rebuilt in {seconds} seconds."
    elif st.get("frozen"):
        current = st.get("reason") or "Waiting for manual attention"
        next_action = ("Verify the SIM PIN before automatic setup can continue."
                       if str(st.get("reason_code") or "").startswith("pin_")
                       else "Restart the line after resolving the reported problem.")
    elif state == "OK":
        current = "IMS is registered and the line is being monitored"
        next_action = "Automatic recovery will run if the connection is lost."
    elif state == "STOPPED":
        current = "The VoWiFi line is stopped"
        next_action = "Enable VoWiFi to start the line."
    elif state == "NO_CARD":
        current = "Waiting for the SIM card"
        next_action = "Insert the SIM card to continue automatically."
    elif state == "PIN_PROBLEM":
        current = "Waiting for SIM PIN attention"
        next_action = "Verify the SIM PIN before automatic setup can continue."
    elif state == "EPDG_UNRESOLVED":
        current = "Resolving the carrier ePDG gateway"
        next_action = "The backend will retry automatically."
    elif state == "TUNNEL_DOWN":
        current = "Establishing the secure ePDG tunnel"
        next_action = "If the tunnel remains unavailable, the line will be rebuilt automatically."
    elif state == "REGISTERING" and detail.get("pcscf"):
        current = "Contacting the carrier IMS through P-CSCF"
        next_action = "If IMS remains unavailable, the ePDG session will be rebuilt automatically."
    elif state == "REGISTERING":
        current = "Waiting for carrier P-CSCF discovery"
        next_action = "IMS registration starts automatically after discovery."
    else:
        current = st.get("label") or "Checking the VoWiFi line"
        next_action = "The backend will keep monitoring the line."

    st["activity"] = {
        "current": current,
        "next": next_action,
        "automatic": (state not in {"STOPPED", "NO_CARD", "PIN_PROBLEM"}
                      and not (st.get("frozen") and not remaining)),
        "retry_count": int(retry.get("count") or 0),
        "retry_max": int(retry.get("max") or 0),
        "seconds": int(remaining or 0),
    }
    return st


def _frozen(h, st, rmax):
    remaining = max(0, int((h.get("next_retry_at") or 0) - time.monotonic()))
    return {"state": "ERROR", "label": status_mod.LABELS["ERROR"],
            "reason_code": h["frozen_code"], "reason": h["frozen_reason"],
            "detail": st.get("detail", {}), "retry": {"count": rmax, "max": rmax},
            "frozen": True, "automatic_retry_in": remaining or None}


def _recovery_failure_code(exc: Exception) -> str:
    """Reduce arbitrary start errors to a support-safe closed reason code."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = str(detail.get("code") or "")
        if engine.valid_lifecycle_reason(code):
            return code
    if isinstance(exc, egress.EgressError):
        return "egress_unavailable"
    return "engine_start_failed"


async def _auto_recover_instance(iid: str, inst: dict, delay: int):
    h = app_state.hub.health_for(iid)
    try:
        allowed, blocked_reason = _line_auto_start_allowed(inst)
        if not allowed:
            no_card = blocked_reason == "no_card"
            if no_card:
                # Card discovery is a sampled cache, not an authoritative removal event. A
                # native reader can briefly lose its ICCID/match while pcscd settles after the
                # health policy removes the old container. Clearing the frozen state here made
                # that one missed sample terminal: the ordinary poller then saw only STOPPED
                # and had no recovery timer left to recreate the line. A real card-removal
                # event still cancels recovery in _on_card_remove(); this path merely re-arms
                # the cheap eligibility check until the cache identifies the card again.
                h["auto_retrying"] = False
                h["next_retry_at"] = time.monotonic() + delay
                h["frozen_reason"] = (
                    "The SIM card is temporarily unavailable; automatic recovery will retry.")
                if h.get("last_blocked_reason") != "no_card":
                    app_state._record_lifecycle(
                        iid, "recovery_blocked", "no_card", retry_count=h.get("retry_count"),
                        delay_seconds=delay, card_present=False)
                h["last_blocked_reason"] = "no_card"
            else:
                # A disabled line or VoWiFi switch is durable user intent, unlike a transient
                # card-cache miss. It must cancel a pending automatic start.
                app_state._record_lifecycle(
                    iid, "recovery_cancelled", blocked_reason,
                    retry_count=h.get("retry_count"), card_present=True)
                app_state.hub.reset_health(iid, None)
            stopped = _with_status_activity(iid, {
                "state": "NO_CARD" if no_card else "STOPPED",
                "label": "No SIM card" if no_card else status_mod.LABELS["STOPPED"],
                "reason_code": blocked_reason,
                "reason": ("SIM card is not available." if no_card
                           else "The line or its device VoWiFi switch is disabled."),
                "detail": {}, "retry": {
                    "count": h.get("retry_count", 0) if no_card else 0,
                    "max": h.get("retry_count", 0) if no_card else 0,
                }})
            app_state.hub.status_cache[str(iid)] = stopped
            app_state.hub.status_sampled_at[str(iid)] = time.monotonic()
            await app_state.hub.broadcast({"type": "status", "instance": str(iid), **stopped})
            return
        h["last_blocked_reason"] = None
        app_state._record_lifecycle(
            iid, "recovery_started", h.get("frozen_code") or "unhealthy",
            retry_count=h.get("retry_count"), card_present=True)
        recovering = _with_status_activity(iid, {
            "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
            "reason_code": h.get("frozen_code") or "registering",
            "reason": h.get("frozen_reason") or "Automatic recovery is rebuilding the line.",
            "detail": {}, "retry": {"count": 0, "max": 0}})
        app_state.hub.status_cache[str(iid)] = recovering
        app_state.hub.status_sampled_at[str(iid)] = time.monotonic()
        await app_state.hub.broadcast({"type": "status", "instance": str(iid), **recovering})
        await asyncio.to_thread(
            app_state._start_engine_checked, inst, cfg.get_settings(),
            os.environ.get("MDD_DEV_MOUNTS", "") == "1",
            # Records why the health policy gave up on the previous container, so the captured
            # snapshot explains itself without cross-referencing the journal.
            f"auto-recover:{h.get('frozen_code') or 'unhealthy'}")
        recovered_reason = h.get("frozen_code") or "unhealthy"
        app_state.hub.reset_health(iid, None)
        app_state._record_lifecycle(iid, "recovery_succeeded", recovered_reason, card_present=True)
        starting = _with_status_activity(iid, {
            "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
            "reason_code": "registering", "reason": "The line was rebuilt successfully.",
            "detail": {}, "retry": {"count": 0, "max": 0}})
        app_state.hub.status_cache[str(iid)] = starting
        app_state.hub.status_sampled_at[str(iid)] = time.monotonic()
        await app_state.hub.broadcast({"type": "status", "instance": str(iid), **starting})
    except Exception as exc:
        h["auto_retrying"] = False
        h["next_retry_at"] = time.monotonic() + delay
        h["frozen_reason"] = str(getattr(exc, "detail", exc))
        code = _recovery_failure_code(exc)
        detail = getattr(exc, "detail", None)
        source_matches = None
        if code == "hardware_imei_required" and isinstance(detail, dict):
            saved_source = str(inst.get("imei_source_device_id") or "")
            failed_source = str(detail.get("device_id") or "")
            source_matches = bool(saved_source and failed_source
                                  and saved_source == failed_source)
        h["last_blocked_reason"] = None
        app_state._record_lifecycle(
            iid, "recovery_failed", code, retry_count=h.get("retry_count"),
            delay_seconds=delay, card_present=True, imei_valid=False
            if code == "hardware_imei_required" else None,
            imei_source_matches=source_matches)


def apply_health(iid, inst, st, container_id: str | None = None):
    """Overlay bounded auto-retry state. After max attempts of continuous failure (with the
    SIM still present) the engine is stopped and the status frozen to ERROR + reason, until
    the user retries/re-provisions or the card is re-inserted."""
    rcfg = inst.get("retry") or cfg.get_settings().get("retry", {})
    rmax = max(1, int(rcfg.get("max", 3)))
    rint = max(5, int(rcfg.get("interval", 40)))
    h = app_state.hub.health_for(iid)
    state = st["state"]
    now = time.monotonic()

    if not inst.get("enabled", True):
        app_state.hub.reset_health(iid, "line_disabled")
        return {"state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
                "reason_code": "stopped", "reason": "Stopped.", "detail": {},
                "retry": {"count": 0, "max": rmax}}

    if state == "OK":
        # Set once per healthy stretch: this is when the current exit last proved it can
        # carry IMS, and reset_health() below would otherwise lose it.
        app_state.hub.ok_since.setdefault(str(iid), time.monotonic())
        # A registration proves the current exit works, so nothing the ledger holds against it
        # still stands. Clearing here is also what lets a reported line report again later.
        if app_state.hub.exit_ledgers.pop(str(iid), None) is not None:
            _save_exit_ledgers()
        app_state.hub.reset_health(iid, "line_recovered")
        st["retry"] = {"count": 0, "max": rmax}
        return st
    if h.get("frozen_code"):
        # PIN/PUK failures require a person. Network, tunnel and IMS failures recover after a
        # cooldown so a brief carrier rejection never leaves WiFi Calling stopped forever.
        if (h.get("frozen_code") not in {"pin_wrong", "pin_blocked", "pin_required"}
                and time.monotonic() >= (h.get("next_retry_at") or float("inf"))
                and not h.get("auto_retrying")):
            h["auto_retrying"] = True
            asyncio.create_task(_auto_recover_instance(iid, inst, max(60, rint * 4)))
        return _frozen(h, st, rmax)
    if state == "STOPPED":
        st["retry"] = {"count": 0, "max": rmax}
        return st
    if state == "NO_CARD":
        # SIM removed/absent -> handled by the card monitor; don't count as a retry.
        h["fail_start"] = None
        h["retry_count"] = 0
        st["retry"] = {"count": 0, "max": rmax}
        return st
    if state == "EPDG_UNRESOLVED":
        support = vowifi_support.for_instance(inst)
        if support["source"] == "carrier_table":
            # The user chose to try a carrier that does not offer Wi-Fi Calling. Retrying on
            # a timer only repeats the same DNS answer; stop and say why. Only the carrier
            # table is trusted this far: a missing DNS answer can also be an outage.
            h["frozen_code"] = st["reason_code"]
            h["frozen_reason"] = support["reason"]
            h["next_retry_at"] = None
            app_state._record_lifecycle(iid, "recovery_cancelled", "carrier_unsupported",
                              retry_count=h.get("retry_count"), card_present=True)
            asyncio.create_task(asyncio.to_thread(
                engine.capture_and_stop, iid, inst, "health-freeze:carrier_unsupported",
                container_id))
            asyncio.create_task(app_state.hub.drop_ami(str(iid)))
            return _frozen(h, st, rmax)
    if state == "PIN_PROBLEM":
        # wrong/blocked PIN won't recover by retrying — surface immediately.
        h["frozen_code"] = st["reason_code"]
        h["frozen_reason"] = st["reason"]
        h["next_retry_at"] = None
        app_state._record_lifecycle(
            iid, "recovery_cancelled", st["reason_code"],
            retry_count=h.get("retry_count"), card_present=True)
        return _frozen(h, st, rmax)

    # Asterisk has already spent a complete SIP transaction proving that this established
    # P-CSCF session no longer answers.  If AMI also proves no call is active, skip the generic
    # retry budget and rebuild the exact container generation.  Missing logs, missing AMI, an
    # active call, a missing generation, or the rate limiter all fall through unchanged.
    fast_unanswered = False
    if (st.get("reason_code") == "reg_unanswered"
            and (st.get("detail") or {}).get("active_channels") == 0
            and container_id):
        last_fast = app_state.hub.reg_unanswered_recovery_at.get(str(iid), float("-inf"))
        if now - last_fast >= REG_UNANSWERED_MIN_INTERVAL_SECONDS:
            fast_unanswered = True
            app_state.hub.reg_unanswered_recovery_at[str(iid)] = now
            # Reuse the established freeze/capture/failover path below.  Backdating fail_start
            # makes this observation exhaust only this reason's retry budget immediately.
            h["fail_start"] = now - (rmax * rint)
            log.warning("line %s IMS registration is unanswered with no active channels; "
                        "fast recovery will rebuild container generation %s", iid,
                        str(container_id)[:12])

    # EPDG_UNRESOLVED / TUNNEL_DOWN / REGISTERING -> the engine keeps retrying internally;
    # we bound the total time and then give up.
    if h["fail_start"] is None:
        h["fail_start"] = now
    elapsed = now - h["fail_start"]
    count = min(rmax, int(elapsed // rint) + 1)
    h["retry_count"] = count
    if elapsed >= rmax * rint:
        h["frozen_code"] = st["reason_code"]
        h["frozen_reason"] = st["reason"]
        # A connected tunnel with an unresponsive IMS/P-CSCF is commonly one bad carrier
        # session or endpoint. Re-establish it after one normal retry interval so discovery can
        # return a healthy P-CSCF; keep the long cooldown for auth/network/provisioning failures
        # to avoid hammering the carrier.
        if fast_unanswered:
            cooldown = max(1.0, REG_UNANSWERED_RECOVERY_DELAY_SECONDS)
        else:
            cooldown = (max(20, rint) if st["reason_code"] == "registering"
                        else max(60, rint * 4))
        h["next_retry_at"] = now + cooldown
        # Capture before removal, off the event loop: reading the container's logs and asking
        # Asterisk for its registration state both block, and the cooldown leaves ample time
        # before the rebuild. Any failure here still leaves the container for stop() to remove.
        asyncio.create_task(asyncio.to_thread(
            engine.capture_and_stop, iid, inst, f"health-freeze:{st['reason_code']}",
            container_id))
        # Giving up on a line is a signal that its exit node may be the problem — but only
        # when the line never worked on it. A node that carried a registered line for a long
        # time and then broke is being blamed for something else (a carrier-side problem, or
        # a rekey that a marginal path failed to survive), and moving the exit costs another
        # tunnel teardown while changing nothing. Blaming it also evicts a node the operator
        # deliberately pinned.
        stable_for = max(0.0, time.monotonic() - app_state.hub.ok_since.pop(str(iid), time.monotonic()))
        try:
            action = _judge_exit_failure(str(iid), inst, st, stable_for)
        except Exception as exc:  # noqa
            log.warning("exit failover judgement failed for line %s: %s", iid, exc)
            action = failover.HOLD
        exit_gave_up = (action == failover.GIVE_UP
                        or bool((app_state.hub.exit_ledgers.get(str(iid)) or {}).get("given_up")))
        if exit_gave_up:
            # Stop the automatic rebuild the same way a PIN problem does: the operator pinned
            # this exit and it has had its chances, so rebuilding again is pure churn. A
            # person (or a successful start) clears this.
            h["next_retry_at"] = None
        elif action == failover.BACK_OFF:
            # Every exit failed the same way, which points upstream of the nodes — a host-side
            # outage, a dead subscription. Those pass, so instead of stopping (or churning
            # every few minutes) the line re-tests its exit on a slow cadence and registers
            # by itself when the outside world comes back.
            h["next_retry_at"] = now + failover.EXHAUSTED_RETRY_SECONDS
        # The SIP code that condemned a reg_rejected line (403 vs 5xx points at completely
        # different fixes) is only in the status detail at this moment; issue #33 shipped a
        # bundle where it had already rotated out of every log, so persist it with the record.
        frozen_sip_status = (st.get("detail") or {}).get("sip_status")
        if h.get("next_retry_at") is not None:
            app_state._record_lifecycle(
                iid, "recovery_scheduled", h.get("frozen_code") or "unhealthy",
                retry_count=h.get("retry_count"),
                delay_seconds=max(0, int(h["next_retry_at"] - now)),
                sip_status=frozen_sip_status)
        else:
            app_state._record_lifecycle(
                iid, "recovery_cancelled", "exit_give_up" if exit_gave_up else
                (h.get("frozen_code") or "unhealthy"),
                retry_count=h.get("retry_count"),
                sip_status=frozen_sip_status)
        asyncio.create_task(app_state.hub.drop_ami(str(iid)))
        return _frozen(h, st, rmax)
    st["retry"] = {"count": count, "max": rmax}
    return st


@asynccontextmanager
async def lifespan(app: FastAPI):
    if operations.container_stack_enabled():
        await asyncio.to_thread(operations.settle_container_service_restart)
    store.init()
    # An upgrade from an older/self-use build may inherit more than five running containers.
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
    poller = asyncio.create_task(status_poller())
    monitor = asyncio.create_task(card_monitor())
    host_poller = asyncio.create_task(host_health_poller())
    media_task = asyncio.create_task(media_supervisor())
    yield
    poller.cancel()
    monitor.cancel()
    host_poller.cancel()
    media_task.cancel()
    # Reap the cancelled tasks (the monitor may be parked in a to_thread wait for up to
    # its timeout; awaiting keeps shutdown deterministic instead of leaking the error).
    await asyncio.gather(poller, monitor, host_poller, media_task,
                         return_exceptions=True)
    await app_state.hub.runtime.close()
    for c in app_state.hub.ami.values():
        await c.close()
    await asyncio.to_thread(engine.close_client)


app = FastAPI(title="MDD Sim Gateway", lifespan=lifespan)

# Authentication for every API request and WebSocket handshake; see gate.py.
app.add_middleware(gate.Gate)
app.include_router(auth_routes.router)
app.include_router(esim_routes.router)
app.include_router(egress_routes.router)
app.include_router(logs_routes.router)
app.include_router(system_routes.router)


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


# ----------------------------- SIM / readers -----------------------------
@app.get("/api/readers")
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


@app.get("/api/sim/detect")
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
    if entry is not None and _merge_card_read(entry, card):
        iid = str(entry.get("matched") or "")
        if iid and not _draft_needs_card_read(name):
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


@app.post("/api/sim/verify-pin")
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


@app.post("/api/sim/change-pin")
async def api_change_pin(body: dict):
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    rlist = await asyncio.to_thread(sim.list_readers)
    name = rlist[idx] if 0 <= idx < len(rlist) else f"idx:{idx}"
    async with app_state.hub.reader_lock(name):
        return await asyncio.to_thread(sim.change_pin, body["old"], body["new"], idx)


@app.post("/api/sim/pin-enabled")
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


@app.get("/api/cards")
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


@app.get("/api/ports/suggest")
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


@app.post("/api/provision")
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
        if not device_present:
            vowifi.update(actual="off", available=False, reason="Device not connected")
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
            "provisioning": {"state": "draft" if is_draft else "ready" if inst else "detecting",
                # IMEI is set on the Hardware tab; everything else on the SIM tab.
                "missing": ([_DRAFT_FIELD_KEYS[name] for name in draft_missing]
                            if draft_missing is not None else
                            [key for key, value in (
                                ("imsi", (inst or card_info).get("imsi")),
                                ("imei", hardware_imei)) if not value])},
            "capabilities": {"vowifi": vowifi},
        })
    return result


@app.get("/api/devices")
async def api_devices():
    # Sessions are memory-only, so a sign-in usually follows a control-plane restart — right
    # when the card monitor is still completing its first scan and smart-card readers are not
    # in the list yet. `discovering` lets the UI say so instead of reporting a confident zero.
    return {"devices": await _unified_devices(), "discovering": not app_state.hub.scanned}


@app.post("/api/devices/{device_id}/sim/reread")
async def api_device_sim_reread(device_id: str):
    """Read this device's SIM again; a draft completed by it is promoted and started."""
    cards = app_state.hub.cards_list()
    name = next((str(card.get("name") or "") for card in cards
                 if card.get("present") and app_state._device_for_card(card, cards)[0] == device_id), "")
    if not name:
        raise HTTPException(404, "no SIM is present in this device")
    filled = await _reread_card(name)
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


@app.put("/api/devices/{device_id}/name")
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


@app.put("/api/devices/{device_id}/hardware")
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
            allowed, _reason = _line_auto_start_allowed(inst)
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


@app.delete("/api/devices/{device_id}")
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


@app.post("/api/devices/{device_id}/diagnostics")
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


@app.patch("/api/devices/{device_id}/capabilities")
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
@app.get("/api/instances")
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


@app.post("/api/instances")
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
            asyncio.create_task(push_status(iid))
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


@app.put("/api/instances/{iid}/country")
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


@app.delete("/api/instances/{iid}")
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
    # Line ids are reused by the next created line, so its connectivity timeline always goes
    # with the line it describes — a new SIM must never inherit another SIM's outages.
    _line_state_written.pop(str(iid), None)

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


@app.post("/api/instances/{iid}/start")
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
    asyncio.create_task(push_status(str(iid)))
    return {"ok": True, "container": cid}


@app.post("/api/instances/{iid}/reprovision")
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
    asyncio.create_task(push_status(str(iid)))
    return {"ok": True, "container": cid}


@app.post("/api/instances/{iid}/pin/clear")
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
        asyncio.create_task(push_status(str(iid)))
    return {"ok": True, "had_pin": had}


@app.post("/api/instances/{iid}/stop")
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
    app_state.hub.status_cache[str(iid)] = _with_status_activity(str(iid), {
        "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
        "reason_code": "stopped", "reason": "Stopped.", "detail": {}})
    app_state.hub.status_sampled_at[str(iid)] = time.monotonic()
    return {"ok": True}


@app.get("/api/instances/{iid}/status")
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


@app.get("/api/instances/{iid}/availability")
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


@app.post("/api/instances/{iid}/register")
async def api_instance_register(iid: str):
    return {"output": engine.exec_cli(iid, "pjsip send register volte_ims")}


def _local_tz() -> ZoneInfo:
    """The user's configured zone, for timestamps shown in local time."""
    try:
        return ZoneInfo(str(cfg.get_settings().get("timezone") or "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


# ----------------------------- Calls -----------------------------
@app.get("/api/instances/{iid}/calls")
def api_calls(iid: str):
    return {"calls": store.list_calls(iid)}


@app.post("/api/instances/{iid}/calls/delete")
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


@app.post("/api/instances/{iid}/call")
async def api_call(iid: str, body: dict):
    result = await place_call_on_line(iid, body["to"], body.get("from_endpoint", "webrtc"))
    if result.pop("unavailable", False):
        raise HTTPException(409, result["error"])
    return result


@app.post("/api/instances/{iid}/hangup")
async def api_hangup(iid: str):
    result = await hangup_on_line(iid)
    if result.pop("unavailable", False):
        raise HTTPException(409, result["error"])
    return result


@app.get("/api/instances/{iid}/softphone")
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


@app.get("/api/media")
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


@app.websocket("/api/instances/{iid}/softphone/ws")
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


# ----------------------------- engine event hook -----------------------------
# Q.850 causes that mean "the network would not act on this request" rather than "the call
# did not connect". Grouped rather than mapped one-to-one from SIP: Asterisk's SIP -> Q.850
# translation differs per response, but every cause here arrives from a 4xx/5xx saying the
# destination is unknown, malformed or unimplemented — which for a service code all mean the
# same thing to the operator, namely that this carrier will not serve the code.
SERVICE_CODE_UNSUPPORTED_CAUSES = {
    1,    # unallocated number          <- 404 Not Found: the carrier does not know this code
    28,   # invalid number format       <- 484 Address Incomplete
    58,   # bearer capability not available
    63,   # service or option not available
    79,   # service or option not implemented   <- 501 Not Implemented
    88,   # incompatible destination            <- 488 Not Acceptable Here
}


def _is_service_code(peer: str) -> bool:
    """A dialled string carrying '*' or '#' is supplementary-service or USSD signalling."""
    return any(ch in str(peer or "") for ch in "*#")


def _service_code_disposition(dialstatus: str, cause: int) -> str:
    """Score a service code (*21*<number>#, #225#) on its own scale rather than a call's.

    These are signalling, not conversation: there is no audio in either direction, so the
    call-shaped outcomes ('busy', 'no answer') would all misdescribe what happened. What is
    knowable here is only whether the carrier acted on the request. The response CONTENT — a
    balance, a divert status — rides in a USSD payload this gateway does not parse, so even an
    accepted code shows no text; see the USSI note in the changelog.
    """
    if dialstatus == "ANSWER":
        return "code accepted"
    if cause in SERVICE_CODE_UNSUPPORTED_CAUSES:
        return "code unsupported"
    if cause == 21:                     # 403/603 — the carrier got it and refused it
        return "code rejected"
    if dialstatus == "CANCEL":
        return "cancelled"
    # Everything else: the carrier reacted but said nothing decisive. Observed in the field are
    # cause 38 (network out of order, from SIP 503) and cause 127 (interworking, unspecified —
    # Asterisk's fallback when a response has no Q.850 mapping), plus cause 0 when nothing came
    # back at all. None of these distinguish "this carrier does not offer the code" from "it
    # could not serve it right now", so do NOT report them as unsupported — that would send the
    # operator chasing a permanent limitation that may just be a transient failure.
    return "code failed"


def _call_disposition(dialstatus: str, cause: int, direction: str = "out",
                      peer: str = "") -> str:
    """Map Asterisk DIALSTATUS + Q.850 hangupcause to a friendly outcome. No retry — a
    rejected/busy/no-answer call is simply recorded as such. Incoming and outgoing read the
    same DIALSTATUS differently: for an inbound call the Dial targets our local softphone, so
    BUSY/decline means WE declined and CANCEL/NOANSWER means we missed it. An outgoing service
    code is not a call and is scored separately."""
    if direction == "out" and _is_service_code(peer):
        return _service_code_disposition(dialstatus, cause)
    if dialstatus == "ANSWER":
        return "answered"
    if direction == "in":
        if dialstatus == "BUSY" or cause == 21:
            return "rejected"        # local softphone actively declined
        return "missed"              # remote hung up first, no answer, or rang out
    # outgoing
    if cause == 21:                     # 603 Decline — far end actively rejected
        return "rejected"
    if cause == 17 or dialstatus == "BUSY":
        return "busy"
    if dialstatus == "NOANSWER" or cause == 19:
        return "no answer"
    if dialstatus == "CANCEL":
        return "cancelled"
    if dialstatus in ("CONGESTION", "CHANUNAVAIL"):
        return "failed"
    # empty DIALSTATUS in the hangup handler => caller hung up before/while dialing.
    return (dialstatus.lower() if dialstatus else "cancelled")


@app.post("/api/engine/event")
async def api_engine_event(payload: dict):
    """Receives notify.py callbacks from engine containers."""
    iid = str(payload.get("instance", ""))
    event = payload.get("event", "")
    args = payload.get("args", [])
    if event in ("sms_in", "sms_out", "ussd", "voicemail_new"):
        # Messaging, USSD and voicemail are not part of this build. The network's SMS is
        # acknowledged inside Asterisk and its content discarded there; an event that still
        # arrives (an older engine) is dropped here and never stored or broadcast.
        return {"ok": True, "ignored": True}
    if event == "call_in":
        # Log inbound calls even when the caller withholds/omits their number (peer "") so an
        # anonymous call still gets a record that the 'h' disposition can finalize. The IMS
        # delivers one INVITE several times (VoLTE preconditions / GRUU fork / retransmit),
        # firing call_in more than once per call — both while the record is still open AND as a
        # trailing retransmit a few seconds AFTER it was finalized. add_call_deduped coalesces
        # both into the single record so no ghost 'ringing' row is left behind.
        peer = args[0] if args else ""
        rec = store.add_call_deduped(iid, "in", peer, status="ringing")
        await app_state.hub.broadcast({"type": "call", "instance": iid, "call": rec})
    elif event == "call_out" and args:
        rec = store.add_call(iid, "out", args[0], status="dialing")
        await app_state.hub.broadcast({"type": "call", "instance": iid, "call": rec})
    elif event == "call_result" and args:
        # New form: call_result <direction> <peer> <dialstatus> <cause> (fired from the 'h'
        # hangup handler for BOTH directions). Legacy form: call_result <peer> <dialstatus>
        # <cause> (outgoing only) — kept for engines running an older dialplan.
        if args[0] in ("in", "out"):
            direction = args[0]
            to = args[1] if len(args) > 1 else ""
            dialstatus = (args[2] if len(args) > 2 else "").upper()
            cause = int(args[3]) if len(args) > 3 and str(args[3]).isdigit() else 0
        else:
            direction = "out"
            to = args[0]
            dialstatus = (args[1] if len(args) > 1 else "").upper()
            cause = int(args[2]) if len(args) > 2 and str(args[2]).isdigit() else 0
        disp = _call_disposition(dialstatus, cause, direction, to)
        rec = store.update_last_call(iid, direction, to, disp)
        if not rec and to:
            # The dialplan fires call_out and call_result from separate backgrounded
            # notify.py processes ('&'), so nothing orders them. On a call that lasts under a
            # second — a dialled service code answered on the BYE does exactly that — the
            # result can land BEFORE the record it is meant to close, and used to be dropped
            # silently, stranding the call on 'dialing' forever. call_out is already on its
            # way, so wait for it.
            #
            # Match the EXACT peer while waiting. Falling back to "the latest open call" in
            # here would attach this verdict to a DIFFERENT call that merely happens to be
            # open — dialling two codes in quick succession made a #225# record carry a
            # *#21# outcome, because #225# was the only open record when *#21#'s result
            # overtook its own call_out.
            for _ in range(15):
                await asyncio.sleep(0.2)
                rec = store.update_last_call(iid, direction, to, disp)
                if rec:
                    log.info("call_result for %s arrived before its record; applied on retry",
                             to)
                    break
        if not rec and to:
            # Only now, with the ordering window spent: 'h' can lose the number to a
            # masquerade, so finalize the latest open call of this direction rather than
            # leaving it stuck on dialing forever.
            rec = store.update_last_call(iid, direction, None, disp)
        if rec:
            await app_state.hub.broadcast({"type": "call", "instance": iid, "call": rec})
    elif event == "cp_mode_resolved" and args:
        # CP auto-discovery success: the engine found the address family (v6/v4/dual) that yields a
        # usable PDN on this carrier. Repin the line from 'auto' to the resolved family so it stops
        # re-walking the ladder on future starts (fast, deterministic), and record that it was
        # auto-detected. Only acts on an auto line; a pinned line ignores a stray report.
        resolved = (args[0] or "").strip().lower()
        if resolved in ("v6", "v4", "dual"):
            inst = cfg.get_instance(iid)
            if inst and cfg.normalize_cp_mode(inst.get("cp_mode", "")) == "auto":
                try:
                    cfg.upsert_instance({"id": iid, "cp_mode": resolved, "cp_mode_source": "auto"})
                    log.info("instance %s: CP auto-discovery resolved to %s (repinned)", iid, resolved)
                except Exception as e:  # noqa
                    log.warning("cp_mode_resolved persist failed for %s: %r", iid, e)
            await app_state.hub.broadcast({"type": "engine", "instance": iid, "event": event, "args": args})
    else:
        await app_state.hub.broadcast({"type": "engine", "instance": iid, "event": event, "args": args})
    # real-time: any tunnel/registration transition triggers an immediate status push
    if event in ("tunnel_up", "tunnel_down", "pcscf", "registered", "unregistered"):
        asyncio.create_task(push_status(iid))
    return {"ok": True}


async def push_status(iid: str):
    """Compute + broadcast status for a single instance immediately (event-driven)."""
    inst = cfg.get_instance(iid)
    if not inst:
        return
    try:
        runtime = await app_state.hub.runtime.get(iid)
        ami = await app_state.hub.ami_for(iid, runtime)
        st = await status_mod.compute(inst, ami, runtime)
        st = _with_status_activity(
            iid, apply_health(iid, inst, st, runtime.get("container_id")))
        app_state.hub.status_cache[str(iid)] = st
        app_state.hub.status_sampled_at[str(iid)] = time.monotonic()
        await app_state.hub.broadcast({"type": "status", "instance": str(iid), **st})
    except Exception as e:  # noqa
        log.debug("push_status error: %r", e)


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
