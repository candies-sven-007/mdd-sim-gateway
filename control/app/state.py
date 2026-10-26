"""Shared state of the control plane: the hub (AMI clients, live WebSocket feed, runtime
registry, locks) and the helpers more than one router or background task needs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi import WebSocket, HTTPException

from . import config as cfg
from . import (engine, status as status_mod, egress, device_state, failover, vowifi_support, gate,
               authz)
from .ami import AmiClient
from .runtime import RuntimeRegistry

log = logging.getLogger("vowifi.main")


# Once Asterisk has completed its own bounded REGISTER transaction and explicitly reports no
# response, another two minutes of same-session retries cannot repair the stale carrier-side
# P-CSCF/ESP state.  Rebuild promptly, but leave enough time for the diagnostic worker to capture
# and remove the expected container generation before auto-start runs.  Per-line rate limiting
# prevents a broken carrier from turning this fast path into a rebuild loop.
REG_UNANSWERED_RECOVERY_DELAY_SECONDS = float(
    os.environ.get("MDD_REG_UNANSWERED_RECOVERY_DELAY", "10"))
REG_UNANSWERED_MIN_INTERVAL_SECONDS = float(
    os.environ.get("MDD_REG_UNANSWERED_MIN_INTERVAL", "300"))


def _carrier_identity(value) -> dict:
    """Extract locally-held SIM matching attributes without inventing missing values."""
    nested = (value.get("carrier_identity") if isinstance(value, dict)
              else getattr(value, "carrier_identity", None)) or {}
    result = {key: nested[key] for key in ("spn", "gid1", "gid2") if key in nested}
    for key in ("spn", "gid1", "gid2"):
        raw = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        if raw is not None:
            result[key] = str(raw)
    return result


def _carrier_identity_update(value) -> dict:
    identity = _carrier_identity(value)
    result = {"carrier_identity": identity} if identity else {}
    mnc_len = value.get("mnc_len") if isinstance(value, dict) else getattr(value, "mnc_len", None)
    if mnc_len in (2, 3):
        result["mnc_len"] = int(mnc_len)
    return result


def _client_card_info(value: dict) -> dict:
    """Card monitor view for authenticated clients, without carrier matching material."""
    return {key: item for key, item in value.items()
            if key not in {"carrier_identity", "spn", "gid1", "gid2"}}


def _client_cards(values: list[dict] | None = None) -> list[dict]:
    return [_client_card_info(value) for value in (values if values is not None
                                                    else hub.cards_list())]


def _with_detected_imei(cards: list[dict]) -> list[dict]:
    """Annotate each live card with its hardware kind and the country its MCC names.

    Every SIM here sits in a USB PC/SC reader; the name is kept from the modem era, when a
    modem's virtual reader slots were collapsed into one device here.
    """
    enriched = []
    for original in cards:
        card_info = dict(original)
        card_info["hardware_kind"] = "reader"
        card_info["country"] = egress.country_for_mcc(card_info.get("mcc"))
        enriched.append(card_info)
    return enriched


def _next_instance_id() -> str:
    """Return the lowest unused numeric line id without assuming ids are contiguous."""
    used = {str(item.get("id")) for item in cfg.list_instances()}
    candidate = 1
    while str(candidate) in used:
        candidate += 1
    return str(candidate)


def _ensure_card_draft(info: dict) -> dict | None:
    """Persist a safe, stopped line draft as soon as a new SIM identity is readable.

    A draft makes hotplug the normal creation path while deliberately avoiding engine startup
    until mandatory identity fields (notably IMEI on a native reader) are available.
    """
    iccid = str(info.get("iccid") or "").strip()
    if not iccid:
        return None
    existing = _match_instance_by_iccid(iccid)
    if existing:
        return existing
    mcc, mnc = str(info.get("mcc") or ""), str(info.get("mnc") or "")
    try:
        inst = cfg.upsert_instance({
            "id": _next_instance_id(),
            "name": cfg.default_instance_name(mcc, mnc, iccid),
            "provisioning_state": "draft",
            "iccid": iccid,
            "imsi": str(info.get("imsi") or ""),
            "mcc": mcc,
            "mnc": mnc,
            **_carrier_identity_update(info),
            "imei": "",
            "reader": f"imsi:{info['imsi']}" if info.get("imsi") else "",
            "reader_index": int(info.get("index") or 0),
            "reader_port": str(info.get("reader_port") or ""),
            "smsc": str(info.get("smsc") or ""),
            "proxy_country": "",
            "enabled": False,
            "apn": "ims",
            "idr_mode": "apn",
            "cp_mode": "auto",
            "sip": {**cfg.carrier_sip_defaults(mcc, mnc, iccid),
                    "transport": "udp", "external": [],
                    "webrtc": {"enable": True}},
            "debug": {"asterisk": False, "charon": False},
        }, unique_name=True)
    except cfg.LineLimitError:
        log.warning("SIM line limit reached; ignoring newly detected SIM %s",
                    iccid[-4:])
        return None
    egress.publish()
    return inst


def _exit_ledger_path() -> str:
    return os.path.join(cfg.DATA_DIR, "exit-failover.json")


def _load_exit_ledgers() -> dict:
    try:
        with open(_exit_ledger_path(), encoding="utf-8") as handle:
            loaded = json.load(handle)
        return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


class Hub:
    """Holds AMI clients per instance and broadcasts events to WebSocket clients."""
    def __init__(self):
        self.ami: dict[str, AmiClient] = {}
        self.ami_generation: dict[str, str | None] = {}
        self._ami_locks: dict[str, asyncio.Lock] = {}  # per-instance ami_for serialisation
        self.runtime = RuntimeRegistry()
        self.status_wakeup = asyncio.Event()
        self.clients: set[WebSocket] = set()
        self.cards: dict[str, dict] = {}     # reader NAME -> detected card/reader info
        self.scanned = False                 # card_monitor completed its first scan
        self._learning: set[str] = set()     # instances currently learning MSISDN
        # Last observed Docker RestartCount per instance, to tell a restart-policy bounce from
        # a rebuild the manager performed itself.
        self._restart_counts: dict[str, int] = {}
        self._msisdn_tries: dict[str, int] = {}
        self._msisdn_checked: dict[str, float] = {}   # last passive re-check
        # Per-line exit failover ledger. Persisted: a control-plane restart must not
        # re-announce a give-up it already reported, nor re-walk an exhausted pool.
        self.exit_ledgers: dict[str, dict] = _load_exit_ledgers()
        self.health: dict[str, dict] = {}    # per-instance retry/health tracking
        # Kept outside health: a successful registration resets health, but must not erase the
        # anti-churn interval for the next stale-session failure on the replacement container.
        self.reg_unanswered_recovery_at: dict[str, float] = {}
        self.status_cache: dict[str, dict] = {}  # background sampled; HTTP never probes devices
        self.status_sampled_at: dict[str, float] = {}  # last authoritative status observation


        # Per-reader serialization for PC/SC APDU access (sim.read_card / PIN / lpac).
        # lpac opens SCARD_SHARE_EXCLUSIVE; concurrent connect/APDU on the same reader
        # fails with sharing violations or corrupts eUICC sessions.
        self.reader_locks: dict[str, asyncio.Lock] = {}
        self.esim_switch_locks: dict[str, asyncio.Lock] = {}
        self.lpa_busy: dict[str, bool] = {}  # readers currently owned by an LPA op
        self.lpa_downloads: dict[str, dict] = {}  # reader_name -> active download handle
        self.card_rereads: set[str] = set()  # readers with a draft-completing re-read queued
        self.hotplug_starts: set[str] = set()  # debounce duplicate modem VPCD slots
        # When each line last became healthy, so a failure can be attributed. A line that
        # carried IMS for a long time and then broke is not evidence against its exit node.
        self.ok_since: dict[str, float] = {}
        # Latest host snapshot, sampled by host_health_poller so HTTP never shells out.
        self.host_snapshot: dict = {}
        self.host_alerts: list[dict] = []
        # Shared with the acknowledgement endpoint. The poller owns condition lifecycle;
        # the API only marks currently visible items handled.
        self.host_alert_state: dict | None = None
        # Per-line outage clock for the offline notification; loaded on the first pass.


    def cards_list(self) -> list[dict]:
        """Reader/card entries sorted by current PC/SC index (the UI display order)."""
        cards = sorted(self.cards.values(),
                       key=lambda c: (c.get("index") is None, c.get("index") or 0,
                                      c.get("name") or ""))
        return _with_detected_imei(cards)

    def reader_lock(self, name: str) -> asyncio.Lock:
        if name not in self.reader_locks:
            self.reader_locks[name] = asyncio.Lock()
        return self.reader_locks[name]

    def esim_switch_lock(self, key: str) -> asyncio.Lock:
        if key not in self.esim_switch_locks:
            self.esim_switch_locks[key] = asyncio.Lock()
        return self.esim_switch_locks[key]

    def health_for(self, iid: str) -> dict:
        return self.health.setdefault(str(iid), {
            "fail_start": None, "retry_count": 0, "frozen_code": None,
            "frozen_reason": None, "last_state": None, "next_retry_at": None,
            "auto_retrying": False, "last_blocked_reason": None,
        })

    def reset_health(self, iid: str, cancel_reason: str | None = "state_reset"):
        iid = str(iid)
        previous = self.health.get(iid) or {}
        if cancel_reason and (previous.get("frozen_code") or previous.get("next_retry_at")
                              or previous.get("auto_retrying")):
            _record_lifecycle(
                iid, "recovery_cancelled", cancel_reason,
                retry_count=previous.get("retry_count"),
                card_present=False if cancel_reason == "no_card" else None)
        self.health[iid] = {"fail_start": None, "retry_count": 0, "frozen_code": None,
                                 "frozen_reason": None, "last_state": None,
                                 "next_retry_at": None, "auto_retrying": False,
                                 "last_blocked_reason": None}
        self.status_cache.pop(iid, None)
        self.status_sampled_at.pop(iid, None)

    async def drop_ami(self, iid: str):
        """Tear down and forget the AMI client for an instance. MUST be called whenever the
        engine container is stopped or recreated (stop/start/reprovision): the client's
        panoramisk Manager auto-reconnects forever, so a client left pointing at a removed or
        recreated container keeps dialing it — and if the new container has a different AMI
        secret (or the docker IP was reused by another line) it floods that Asterisk with
        'failed to authenticate' every few seconds. close() sets the client's closed flag which
        neutralises the pending reconnect."""
        c = self.ami.pop(str(iid), None)
        self.ami_generation.pop(str(iid), None)
        if c:
            await c.close()

    async def runtime_changed(self, iid: str, runtime: dict, _action: str) -> None:
        """Retire stale AMI immediately and wake the adaptive status sampler."""
        generation = runtime.get("container_id")
        if (not runtime.get("running")
                or self.ami_generation.get(str(iid)) not in (None, generation)):
            await self.drop_ami(iid)
        if runtime.get("running"):
            await self._note_unrequested_restart(str(iid), runtime)
        self.status_wakeup.set()

    def seed_restart_baseline(self, iid: str, runtime: dict) -> None:
        """Record the first RestartCount seen for a running line.

        The baseline used to be set only from Docker start events, so after the manager itself
        restarted, the first bounce of each line looked like a first sighting and was dropped —
        exactly what happened to line 5's 09-17 crash. The status poll sees every line within
        seconds of startup, so seed from there; only a missing baseline is filled in.
        """
        if runtime.get("running") and iid not in self._restart_counts:
            self._restart_counts[iid] = int(runtime.get("restart_count") or 0)

    async def _note_unrequested_restart(self, iid: str, runtime: dict) -> None:
        """Record engine bounces that Docker's restart policy performed on its own.

        A rebuild the manager asks for creates a fresh container, so its RestartCount is 0. A
        restart-policy bounce increments the counter on the same container. Only the latter is
        invisible today: it completes well inside the health policy's threshold, so no recovery
        is scheduled and nothing reaches the timeline even though the line just spent ~40s
        unable to take a call.
        """
        count = int(runtime.get("restart_count") or 0)
        previous = self._restart_counts.get(iid)
        self._restart_counts[iid] = count
        if previous is None or count <= previous:
            return
        exit_record = await asyncio.to_thread(engine.last_engine_exit, iid)
        disposition = str(exit_record.get("disposition") or "")
        reason = {"signal": "engine_signal", "exit": "engine_exit"}.get(disposition, "unknown")
        try:
            await asyncio.to_thread(
                engine.record_lifecycle, iid, "engine_restarted", reason_code=reason)
        except Exception as exc:  # noqa
            log.debug("could not record engine restart instance=%s: %r", iid, exc)
        log.warning("engine %s was restarted by Docker's restart policy "
                    "(restart_count %s -> %s, last exit: %s)", iid, previous, count,
                    exit_record or "unrecorded")

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.clients):
            view = authz.event_for(gate.current(ws), msg)
            if view is None:
                continue
            try:
                await ws.send_json(view)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def ami_for(self, iid: str, runtime: dict | None = None) -> AmiClient | None:
        iid = str(iid)
        # Serialise per-instance so concurrent callers (the 4s status_poller + API handlers) can't
        # each build a client and orphan the other's: an orphaned AmiClient is never close()d, so
        # its panoramisk Manager reconnects forever (flooding the engine's Asterisk with AMI auth
        # failures once a container reuses its docker IP).
        lock = self._ami_locks.setdefault(iid, asyncio.Lock())
        async with lock:
            inst = cfg.get_instance(iid)
            # Docker's API can pause for seconds when the daemon is busy. AMI discovery runs
            # in the background, but synchronous Docker calls here still froze every HTTP
            # request sharing this event loop.
            if runtime is None:
                runtime = await self.runtime.get(iid)
            running = bool(inst) and bool(runtime.get("running"))
            ip = runtime.get("ip") if running else None
            generation = runtime.get("container_id")
            client = self.ami.get(iid)
            # Reuse only a healthy client still pointed at the current container.
            if (client and running and ip and client.connected and client.host == ip
                    and self.ami_generation.get(iid) == generation):
                return client
            # Any other cached client is stale/unusable — drop it (close stops its reconnect loop)
            # so it can't linger and reconnect. This is the leak the old early-returns caused: they
            # returned None when the container was gone/IP-less WITHOUT closing the cached client.
            if client:
                await self.drop_ami(iid)
            if not running or not ip:
                return None
            client = AmiClient(iid, ip, 5038, inst.get("ami_user", "vowifi"),
                               inst["ami_secret"], realm=cfg.ims_realm(inst["mcc"], inst["mnc"]),
                               msisdn=inst.get("msisdn", ""), smsc=inst.get("smsc", ""))
            await client.connect()
            self.ami[iid] = client
            self.ami_generation[iid] = generation
            return client


hub = Hub()


def _start_engine_checked(inst: dict, settings: dict, dev_mounts: bool = False,
                          reason: str = "manual"):
    """Translate fail-closed egress errors into an actionable API response."""
    if not cfg.line_allowed(str(inst.get("id") or "")):
        raise HTTPException(409, {
            "code": "line_limit",
            "message": (f"MDD Sim Gateway supports at most "
                        f"{cfg.MAX_SIM_LINES} SIM lines. Delete an existing line "
                        "before starting this one."),
        })
    try:
        # A line's saved USB port may be where the SIM sat the last time, e.g. an eSIM profile
        # used before in another reader. The engine then finds no reader there, falls back to
        # an index holding another line's card and refuses to authenticate -- a retry every
        # minute until someone re-saves the line. Rebind to where the SIM is now.
        reader = _live_reader_binding_for_instance(inst)
        if reader and any(inst.get(key) != value for key, value in reader.items()):
            log.warning("instance %s: SIM found in the reader at USB port %s, not %s; "
                        "rebinding before %s start", inst.get("id"), reader["reader_port"],
                        inst.get("reader_port") or "(none)", reason)
            inst = cfg.upsert_instance({"id": str(inst["id"]), **reader})
        # A line follows its SIM; its device identity follows the physical modem/reader
        # currently holding that SIM. Refresh the rendered snapshot on every start.
        inst = _apply_current_hardware_imei(inst)
        # A restart begins a new healthy stretch; the old one says nothing about the exit
        # this container will end up using.
        hub.ok_since.pop(str(inst.get("id") or ""), None)
        return engine.start(inst, settings, dev_mounts=dev_mounts, reason=reason)
    except egress.EgressError as exc:
        raise HTTPException(503, {"code": "egress_unavailable", "message": str(exc)})


def _match_instance_by_iccid(iccid):
    if not iccid:
        return None
    for i in cfg.list_instances():
        if i.get("iccid") == iccid:
            return i
    return None


def _random_svn() -> str:
    """Random 2-digit Software Version Number for an auto-derived IMEISV."""
    return f"{random.randint(0, 99):02d}"


def _find_running_by_reader(
    name: str,
    reader_port: str | None = None,
    *,
    require_port_match: bool = False,
):
    """The running instance whose pin_keeper reports using this reader NAME
    (pin_status.json "reader") — per-reader correct with multiple SIMs.

    A native reader name is only a transient pcscd enumeration identity. During insertion
    attribution callers that know the live USB port must also verify it against the line's
    saved port; otherwise two identical serial-less readers can exchange names after reboot
    and make a running engine lend the wrong ICCID to the newly observed card."""
    if not name:
        return None
    for i in cfg.list_instances():
        if not engine.is_running(str(i["id"])):
            continue
        ps = engine.read_run_json(str(i["id"]), "pin_status.json") or {}
        if ps.get("reader") == name:
            saved_port = str(i.get("reader_port") or "")
            live_port = str(reader_port or "")
            if require_port_match and saved_port and saved_port != live_port:
                log.warning(
                    "ignoring stale running-reader claim for instance %s: "
                    "reader=%s saved_port=%s live_port=%s",
                    i.get("id"), name, saved_port, live_port or "unresolved")
                continue
            return i
    return None


async def _auto_start_hotplugged_line(iid: str) -> None:
    """Start one enabled matched line after reader enumeration settles.

    A modem exposes the same SIM through several VPCD slots, so insert events arrive more
    than once. The per-line guard collapses them into one attempt; the normal health policy
    remains responsible for bounded registration retries after the container has started.
    """
    if iid in hub.hotplug_starts:
        return
    hub.hotplug_starts.add(iid)
    try:
        await asyncio.sleep(6)
        inst = cfg.get_instance(iid)
        if not inst or await asyncio.to_thread(engine.is_running, iid):
            return
        cards = hub.cards_list()
        card_info = next((item for item in cards if item.get("present")
                          and str(item.get("iccid") or "") == str(inst.get("iccid") or "")), None)
        if not card_info:
            return

        # Completing a newly discovered line describes the SIM and its hardware; it does not
        # start VoWiFi by itself.
        if inst.get("provisioning_state") == "draft":
            inst = await asyncio.to_thread(_auto_promote_card_draft, inst, card_info, cards)
            if inst.get("provisioning_state") == "draft":
                log.info("hotplug draft %s awaiting: %s", iid,
                         ", ".join(inst.get("auto_provision_missing") or []))
                return

        if not inst.get("enabled", True):
            return
        await asyncio.to_thread(_start_engine_checked, inst, cfg.get_settings(),
                                os.environ.get("MDD_DEV_MOUNTS", "") == "1")
        hub.reset_health(iid, "hotplug_start")
        await hub.broadcast({"type": "engine", "instance": iid, "event": "hotplug_started",
                             "args": []})
    except Exception as exc:  # noqa
        log.warning("hotplug auto-start failed for %s: %s", iid, getattr(exc, "detail", exc))
    finally:
        hub.hotplug_starts.discard(iid)


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
    cards = hub.cards_list()
    card_info = next((item for item in cards if item.get("present") and (
        (iccid and str(item.get("iccid") or "") == iccid)
        or str(item.get("matched") or "") == iid)), None)
    if card_info is None:
        return False, "no_card"
    return True, ""


def _draft_missing(inst: dict, card_info: dict, cards: list[dict]) -> list[str]:
    """What an auto-created draft still lacks before it can become a line.

    The device page shows this list, so the operator is told what is actually missing. It
    used to say only that an IMEI was needed.
    """
    imsi = str(card_info.get("imsi") or inst.get("imsi") or "").strip()
    mcc = str(card_info.get("mcc") or inst.get("mcc") or (imsi[:3] if len(imsi) >= 3 else ""))
    mnc = str(card_info.get("mnc") or inst.get("mnc") or "")
    imei, _hardware_id, _device_type = _hardware_imei_for_card(card_info, cards)
    missing = []
    if not imsi:
        missing.append("IMSI")
    if not mcc or not mnc:
        missing.append("MCC/MNC")
    if len(imei) != 15:
        missing.append("IMEI")
    if card_info.get("pin_enabled") is True and not inst.get("pin"):
        missing.append("SIM PIN")
    return missing


def _auto_promote_card_draft(inst: dict, card_info: dict, cards: list[dict]) -> dict:
    """Promote a complete auto-created draft, or return it with missing-field hints.

    Hardware identity follows the physical reader/modem; SIM identity follows the ICCID.
    Keeping this as a synchronous helper makes the promotion rules independently testable.
    """
    if inst.get("provisioning_state") != "draft":
        return inst

    missing = _draft_missing(inst, card_info, cards)
    if missing:
        return {**inst, "auto_provision_missing": missing}
    imsi = str(card_info.get("imsi") or inst.get("imsi") or "").strip()
    mcc = str(card_info.get("mcc") or inst.get("mcc") or (imsi[:3] if len(imsi) >= 3 else ""))
    mnc = str(card_info.get("mnc") or inst.get("mnc") or "")
    smsc = str(card_info.get("smsc") or inst.get("smsc") or "").strip()
    imei, hardware_id, _device_type = _hardware_imei_for_card(card_info, cards)

    previous_imeisv = str(inst.get("imeisv") or "")
    svn = (previous_imeisv[-2:] if len(previous_imeisv) == 16
           and previous_imeisv[-2:].isdigit() else _random_svn())
    sip = cfg.merge_carrier_sip_defaults(
        mcc, mnc, card_info.get("iccid") or imsi or imei, inst.get("sip"))
    # A draft normally arrives already named; only a name generated here needs deduplicating.
    resolved_iccid = str(card_info.get("iccid") or inst.get("iccid") or "")
    generated_name = not str(inst.get("name") or "").strip()
    update = {
        "id": str(inst["id"]),
        "name": (inst.get("name")
                 or cfg.default_instance_name(mcc, mnc, resolved_iccid)),
        "provisioning_state": "ready",
        "enabled": True,
        "imsi": imsi,
        "mcc": mcc,
        "mnc": mnc,
        **_carrier_identity_update(card_info),
        "iccid": str(card_info.get("iccid") or inst.get("iccid") or ""),
        "smsc": smsc,
        "imei": imei,
        "imei_source_device_id": hardware_id,
        "imeisv": cfg.imeisv_from_imei(imei, svn=svn),
        "reader": f"imsi:{imsi}",
        "sip": sip,
        # Production logs must not expose IMS-AKA material through Asterisk debug output.
        "debug": {**(inst.get("debug") or {}), "asterisk": False},
    }
    update.update({
        "reader_index": int(card_info.get("index") or inst.get("reader_index") or 0),
        "reader_port": str(card_info.get("reader_port") or inst.get("reader_port") or ""),
    })
    # A phone hides the Wi-Fi Calling switch for a carrier that does not offer it. Doing the
    # same here avoids a line that can only fail; the device page says why and the switch
    # still lets the user try.
    support = vowifi_support.for_instance({"mcc": mcc, "mnc": mnc, "epdg": inst.get("epdg")},
                                          probe_now=True)
    if support["status"] == vowifi_support.UNSUPPORTED:
        update["enabled"] = False
    promoted = cfg.upsert_instance(update, unique_name=generated_name)
    egress.publish()
    log.info("hotplug draft %s auto-provisioned for MCC %s%s", inst["id"], mcc,
             f" (VoWiFi left off: {support['source']})"
             if support["status"] == vowifi_support.UNSUPPORTED else "")
    return promoted


def _visible_host_alerts(alerts: list[dict], state: dict) -> list[dict]:
    """Hide acknowledged conditions until the poller observes a sustained recovery."""
    return [item for item in alerts
            if not (state.get(item["code"]) or {}).get("acknowledged")]


def _save_exit_ledgers() -> None:
    path = _exit_ledger_path()
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(hub.exit_ledgers, handle)
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
        if (hub.status_cache.get(oid) or {}).get("state") == "OK":
            return True
    return False


def _distinct_cards_present() -> int:
    """How many different SIMs this host can currently see.

    Decides only how a SW=9862 is worded, so an unreadable cache falls back to the cautious
    plural reading rather than asserting a single-SIM host that may not be one.
    """
    try:
        return len({str(entry.get("iccid") or "")
                    for entry in hub.cards.values() if entry.get("iccid")}) or 2
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
        if hub.exit_ledgers.pop(iid, None) is not None:
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
    was_backing_off = bool((hub.exit_ledgers.get(iid) or {}).get("exhausted"))
    action, ledger = failover.record(hub.exit_ledgers.get(iid), verdict, node,
                                     pinned, candidates, peer_registered=peer_registered)
    hub.exit_ledgers[iid] = ledger
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


def _host_alert_state_path() -> str:
    return os.path.join(cfg.DATA_DIR, "host-alert-state.json")


def _load_host_alert_state() -> dict:
    try:
        with open(_host_alert_state_path(), encoding="utf-8") as handle:
            loaded = json.load(handle)
        return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_host_alert_state(state: dict) -> None:
    path = _host_alert_state_path()
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, path)
    except OSError as exc:
        log.debug("cannot persist host alert state: %r", exc)


def _with_status_activity(iid: str, st: dict) -> dict:
    """Explain the status machine in user terms: now, why, and what happens next."""
    st = dict(st)
    state = str(st.get("state") or "").upper()
    detail = st.get("detail") or {}
    retry = st.get("retry") or {}
    health = hub.health_for(str(iid))
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


_lifecycle_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mdd-lifecycle")


def _record_lifecycle(iid: str, event: str, reason_code: str = "", **facts) -> None:
    """Best-effort bounded recovery audit; it must never affect line operation."""
    try:
        future = _lifecycle_writer.submit(
            engine.record_lifecycle, str(iid), event, reason_code=reason_code, **facts)
        future.add_done_callback(
            lambda done: log.debug(
                "lifecycle record failed instance=%s event=%s: %s",
                iid, event, type(done.exception()).__name__) if done.exception() else None)
    except Exception as exc:  # noqa - diagnostic persistence cannot break recovery
        log.debug("lifecycle record failed instance=%s event=%s: %s",
                  iid, event, type(exc).__name__)


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
    h = hub.health_for(iid)
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
                    _record_lifecycle(
                        iid, "recovery_blocked", "no_card", retry_count=h.get("retry_count"),
                        delay_seconds=delay, card_present=False)
                h["last_blocked_reason"] = "no_card"
            else:
                # A disabled line or VoWiFi switch is durable user intent, unlike a transient
                # card-cache miss. It must cancel a pending automatic start.
                _record_lifecycle(
                    iid, "recovery_cancelled", blocked_reason,
                    retry_count=h.get("retry_count"), card_present=True)
                hub.reset_health(iid, None)
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
            hub.status_cache[str(iid)] = stopped
            hub.status_sampled_at[str(iid)] = time.monotonic()
            await hub.broadcast({"type": "status", "instance": str(iid), **stopped})
            return
        h["last_blocked_reason"] = None
        _record_lifecycle(
            iid, "recovery_started", h.get("frozen_code") or "unhealthy",
            retry_count=h.get("retry_count"), card_present=True)
        recovering = _with_status_activity(iid, {
            "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
            "reason_code": h.get("frozen_code") or "registering",
            "reason": h.get("frozen_reason") or "Automatic recovery is rebuilding the line.",
            "detail": {}, "retry": {"count": 0, "max": 0}})
        hub.status_cache[str(iid)] = recovering
        hub.status_sampled_at[str(iid)] = time.monotonic()
        await hub.broadcast({"type": "status", "instance": str(iid), **recovering})
        await asyncio.to_thread(
            _start_engine_checked, inst, cfg.get_settings(),
            os.environ.get("MDD_DEV_MOUNTS", "") == "1",
            # Records why the health policy gave up on the previous container, so the captured
            # snapshot explains itself without cross-referencing the journal.
            f"auto-recover:{h.get('frozen_code') or 'unhealthy'}")
        recovered_reason = h.get("frozen_code") or "unhealthy"
        hub.reset_health(iid, None)
        _record_lifecycle(iid, "recovery_succeeded", recovered_reason, card_present=True)
        starting = _with_status_activity(iid, {
            "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
            "reason_code": "registering", "reason": "The line was rebuilt successfully.",
            "detail": {}, "retry": {"count": 0, "max": 0}})
        hub.status_cache[str(iid)] = starting
        hub.status_sampled_at[str(iid)] = time.monotonic()
        await hub.broadcast({"type": "status", "instance": str(iid), **starting})
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
        _record_lifecycle(
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
    h = hub.health_for(iid)
    state = st["state"]
    now = time.monotonic()

    if not inst.get("enabled", True):
        hub.reset_health(iid, "line_disabled")
        return {"state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
                "reason_code": "stopped", "reason": "Stopped.", "detail": {},
                "retry": {"count": 0, "max": rmax}}

    if state == "OK":
        # Set once per healthy stretch: this is when the current exit last proved it can
        # carry IMS, and reset_health() below would otherwise lose it.
        hub.ok_since.setdefault(str(iid), time.monotonic())
        # A registration proves the current exit works, so nothing the ledger holds against it
        # still stands. Clearing here is also what lets a reported line report again later.
        if hub.exit_ledgers.pop(str(iid), None) is not None:
            _save_exit_ledgers()
        hub.reset_health(iid, "line_recovered")
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
            _record_lifecycle(iid, "recovery_cancelled", "carrier_unsupported",
                              retry_count=h.get("retry_count"), card_present=True)
            asyncio.create_task(asyncio.to_thread(
                engine.capture_and_stop, iid, inst, "health-freeze:carrier_unsupported",
                container_id))
            asyncio.create_task(hub.drop_ami(str(iid)))
            return _frozen(h, st, rmax)
    if state == "PIN_PROBLEM":
        # wrong/blocked PIN won't recover by retrying — surface immediately.
        h["frozen_code"] = st["reason_code"]
        h["frozen_reason"] = st["reason"]
        h["next_retry_at"] = None
        _record_lifecycle(
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
        last_fast = hub.reg_unanswered_recovery_at.get(str(iid), float("-inf"))
        if now - last_fast >= REG_UNANSWERED_MIN_INTERVAL_SECONDS:
            fast_unanswered = True
            hub.reg_unanswered_recovery_at[str(iid)] = now
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
        stable_for = max(0.0, time.monotonic() - hub.ok_since.pop(str(iid), time.monotonic()))
        try:
            action = _judge_exit_failure(str(iid), inst, st, stable_for)
        except Exception as exc:  # noqa
            log.warning("exit failover judgement failed for line %s: %s", iid, exc)
            action = failover.HOLD
        exit_gave_up = (action == failover.GIVE_UP
                        or bool((hub.exit_ledgers.get(str(iid)) or {}).get("given_up")))
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
            _record_lifecycle(
                iid, "recovery_scheduled", h.get("frozen_code") or "unhealthy",
                retry_count=h.get("retry_count"),
                delay_seconds=max(0, int(h["next_retry_at"] - now)),
                sip_status=frozen_sip_status)
        else:
            _record_lifecycle(
                iid, "recovery_cancelled", "exit_give_up" if exit_gave_up else
                (h.get("frozen_code") or "unhealthy"),
                retry_count=h.get("retry_count"),
                sip_status=frozen_sip_status)
        asyncio.create_task(hub.drop_ami(str(iid)))
        return _frozen(h, st, rmax)
    st["retry"] = {"count": count, "max": rmax}
    return st


def _live_reader_binding_for_instance(inst: dict) -> dict:
    """The reader_port/reader_index where the card monitor sees this line's SIM right now.

    Only for a line on an ordinary PC/SC reader: a modem's virtual readers carry one SIM on
    several logical slots and are rebound from the bridge metadata instead. Uses what the
    monitor has already read -- no APDU is sent, so a running engine is never raced. Empty
    when the SIM is not seen, or when more than one present reader claims it.
    """
    wanted = str(inst.get("iccid") or "").strip()
    if not wanted or inst.get("swu_reader"):
        return {}
    holders = [c for c in hub.cards.values()
               if c.get("present") and c.get("iccid") == wanted and c.get("reader_port")
               and not str(c.get("name") or "").startswith("VoWiFi Modem ")]
    if len(holders) != 1:
        return {}
    found = holders[0]
    binding = {"reader_port": str(found["reader_port"])}
    if found.get("index") is not None:
        binding["reader_index"] = int(found["index"])
    return binding


# ----------------------------- physical devices (smart-card readers) -----------------------------
# This build talks to SIMs only through USB PC/SC readers. A "device" is one physical reader,
# keyed by its USB port (device_state.native_reader_devices), with the operator's saved record
# (display name, the IMEI the line presents) in device_state.hardware().
def _device_for_card(card_info: dict, cards: list[dict] | None = None) -> tuple[str, str]:
    """Return (device_id, "reader") for a live card-monitor entry, or ("", "")."""
    cards = cards or hub.cards_list()
    name = str(card_info.get("name") or "")
    port = str(card_info.get("reader_port") or "")
    for device_id, candidate in device_state.native_reader_devices(cards).items():
        if ((name and candidate.get("name") == name)
                or (port and candidate.get("reader_port") == port)):
            return device_id, "reader"
    return "", ""


def _hardware_imei_for_card(card_info: dict, cards: list[dict] | None = None,
                            *, migrate_legacy: bool = True) -> tuple[str, str, str]:
    """Resolve device IMEI for the hardware currently holding a SIM."""
    cards = cards or hub.cards_list()
    device_id, device_type = _device_for_card(card_info, cards)
    if device_type == "reader":
        record = device_state.hardware().get(device_id) or {}
        imei = cfg.normalize_imei(record.get("imei", ""))
        if len(imei) == 15:
            return imei, device_id, device_type
        # One-time migration: older releases stored a reader's device identity on
        # whichever SIM line was inserted. Move it to the physical reader record.
        inst = _match_instance_by_iccid(card_info.get("iccid"))
        legacy = cfg.normalize_imei((inst or {}).get("imei", ""))
        # The per-line source marker was written only after this reader's IMEI had been
        # verified.  If the hardware document is briefly unreadable during recovery, the
        # matching marker is safer than stopping a present card forever; a marker naming a
        # different reader remains unusable so moving the SIM still adopts the new device.
        if (len(legacy) == 15
                and str((inst or {}).get("imei_source_device_id") or "") == device_id):
            return legacy, device_id, device_type
        if migrate_legacy and len(legacy) == 15 and not (inst or {}).get("imei_source_device_id"):
            device_state.set_hardware(device_id, {
                "device_type": "reader", "name": card_info.get("name") or "Smart-card reader",
                "stable_path": card_info.get("reader_port") or "", "imei": legacy})
            cfg.upsert_instance({"id": str(inst["id"]), "imei_source_device_id": device_id})
            return legacy, device_id, device_type
    return "", device_id, device_type


def _apply_current_hardware_imei(inst: dict) -> dict:
    """Snapshot the current physical device IMEI into the engine line before start."""
    iccid = str(inst.get("iccid") or "")
    cards = hub.cards_list()
    card_info = next((item for item in cards
                      if item.get("present") and str(item.get("iccid") or "") == iccid), None)
    if not card_info:
        return inst
    imei, _device_id, _device_type = _hardware_imei_for_card(card_info, cards)
    if len(imei) != 15:
        raise HTTPException(409, {
            "code": "hardware_imei_required",
            "message": "configure a 15-digit IMEI in Device > Hardware before starting VoWiFi",
            "device_id": _device_id,
        })
    if imei == cfg.normalize_imei(inst.get("imei", "")):
        return inst
    previous_imeisv = str(inst.get("imeisv") or "")
    svn = previous_imeisv[-2:] if len(previous_imeisv) == 16 and previous_imeisv[-2:].isdigit() else _random_svn()
    return cfg.upsert_instance({"id": str(inst["id"]), "imei": imei,
                                "imei_source_device_id": _device_id,
                                "imeisv": cfg.imeisv_from_imei(imei, svn=svn)})


def refresh_egress() -> dict:
    """Drop the cached subscriptions and ask the orchestrator to fetch and reconcile again."""
    cache = os.path.join(cfg.DATA_DIR, "orchestrator", "subscription.yaml")
    try:
        os.remove(cache)
    except FileNotFoundError:
        pass
    cache_dir = os.path.join(cfg.DATA_DIR, "orchestrator", "subscriptions")
    try:
        for name in os.listdir(cache_dir):
            if name.endswith(".yaml"):
                os.remove(os.path.join(cache_dir, name))
    except FileNotFoundError:
        pass
    egress.publish()
    return {"ok": True, "requested_at": int(time.time())}


async def push_status(iid: str):
    """Compute + broadcast status for a single instance immediately (event-driven)."""
    inst = cfg.get_instance(iid)
    if not inst:
        return
    try:
        runtime = await hub.runtime.get(iid)
        ami = await hub.ami_for(iid, runtime)
        st = await status_mod.compute(inst, ami, runtime)
        st = _with_status_activity(
            iid, apply_health(iid, inst, st, runtime.get("container_id")))
        hub.status_cache[str(iid)] = st
        hub.status_sampled_at[str(iid)] = time.monotonic()
        await hub.broadcast({"type": "status", "instance": str(iid), **st})
    except Exception as e:  # noqa
        log.debug("push_status error: %r", e)
