"""Background tasks started by the application lifespan: card monitor, status poller, host
health poller and media supervisor."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from . import config as cfg
from . import store, engine, status as status_mod, sim, card, usbreader, sysinfo
from . import state as app_state

log = logging.getLogger("vowifi.main")


STATUS_OK_GRACE_SECONDS = 20
STATUS_POLL_FAST_SECONDS = 4.0
STATUS_POLL_HEALTHY_SECONDS = 15.0
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
LINE_HISTORY_PRUNE_INTERVAL_SECONDS = 3600
# Comfortably below store.LINE_STATE_CONTINUITY_SECONDS, so throttled writes still read back
# as one uninterrupted observation.
LINE_STATE_WRITE_INTERVAL_SECONDS = 30
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
    if app_state._draft_needs_card_read(name):
        asyncio.create_task(_complete_draft_from_card(name))
    log.info("card inserted reader=%s (%s) identity=%s matched=%s", idx, name,
             "available" if info["iccid"] else "unknown", info["matched"])
    if info.get("matched"):
        asyncio.create_task(app_state._auto_start_hotplugged_line(str(info["matched"])))
# Seen on an Alcor AK9563: an early read at insertion can come back incomplete where a later
# read succeeds. Retry on a widening schedule, then leave it to the operator, who is shown the
# missing field and a re-read button.
_CARD_REREAD_DELAYS = (10, 30, 60, 120, 300)


async def _complete_draft_from_card(name: str):
    """Re-read a draft's card on a widening schedule while it lacks card-only fields."""
    if name in app_state.hub.card_rereads:
        return
    app_state.hub.card_rereads.add(name)
    try:
        for delay in _CARD_REREAD_DELAYS:
            await asyncio.sleep(delay)
            iid = app_state._draft_needs_card_read(name)
            if not iid:
                return
            if await app_state._reread_card(name) and not app_state._draft_needs_card_read(name):
                log.info("draft %s: a later card read supplied what the first one missed", iid)
                asyncio.create_task(app_state._auto_start_hotplugged_line(iid))
                return
        if app_state._draft_needs_card_read(name):
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
        # Stop the line's engine on card/reader removal.
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
        stopped_status = app_state._with_status_activity(str(target["id"]), stopped_status)
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
            # A SIM ignored for the line limit gets its line as soon as a line is deleted or
            # the limit is raised, without having to be re-inserted.
            waiting = app_state.line_limited_cards()
            for iid in (await asyncio.to_thread(app_state.retry_line_limited_cards, waiting)
                        if waiting else []):
                asyncio.create_task(app_state._auto_start_hotplugged_line(iid))
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
        app_state._line_state_written.pop(iid, None)
        return
    previous = app_state._line_state_written.get(iid)
    if (previous and previous[0] == kind
            and time.monotonic() - previous[1] < LINE_STATE_WRITE_INTERVAL_SECONDS):
        return
    app_state._line_state_written[iid] = (kind, time.monotonic())
    # Only a disconnect needs explaining. The first down sample carries the cause; the store
    # keeps it for the whole segment.
    reason = str((st or {}).get("reason_code") or "") if kind == "down" else ""
    try:
        await asyncio.to_thread(store.record_line_state, iid, kind, reason=reason,
                                detail=_outage_detail(st) if reason else "")
    except Exception as exc:  # noqa
        # History is diagnostic only; never let it interrupt status sampling.
        app_state._line_state_written.pop(iid, None)
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
            snapshot = await asyncio.to_thread(sysinfo.collect, cfg.DATA_DIR)
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
                stopped = app_state._with_status_activity(iid, {
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

        st = app_state._with_status_activity(
            iid, app_state.apply_health(iid, inst, st, runtime.get("generation")))
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
