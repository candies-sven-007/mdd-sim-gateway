"""Shared state of the control plane: the hub (AMI clients, live WebSocket feed, runtime
registry, locks) and the helpers more than one router or background task needs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from concurrent.futures import ThreadPoolExecutor
from fastapi import WebSocket, HTTPException

from . import config as cfg
from . import engine, egress, device_state, vowifi_support, gate, authz
from .ami import AmiClient
from .runtime import RuntimeRegistry

log = logging.getLogger("vowifi.main")


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
