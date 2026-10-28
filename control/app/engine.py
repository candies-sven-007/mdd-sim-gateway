"""
engine.py - per-line engine lifecycle through systemd (docs/SLIM_PLAN.md section 12).

Each line N runs ``mdd-engine@N.service`` in network namespace ``mdd-N``; it owns the line's
ePDG tunnel and Asterisk. The control plane is not root: it starts and stops engines only
through ``sudo -n /usr/local/sbin/mdd-enginectl``, and reads their state with ``systemctl show``
and their output with ``journalctl``, neither of which needs privileges. The engine reaches
the control plane at 10.88.N.1, and the control plane reaches the engine's AMI, softphone
WebSocket and Asterisk control socket at 10.88.N.2 or in the line's run directory.

PC/SC: engines are clients of the host pcscd (/run/pcscd), built from the same pcsc-lite.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

from . import config as cfg, egress, sysinfo

log = logging.getLogger("mdd.engine")

# Bounded so a line that rebuilds every two minutes cannot fill a Pi's SD card. Only the
# recent tail is diagnostically useful.
DIAGNOSTIC_RECORDS = 200
LIFECYCLE_RECORDS = 500
LIFECYCLE_EVENTS = {
    "recovery_scheduled", "recovery_blocked", "recovery_started", "recovery_failed",
    "recovery_succeeded", "recovery_cancelled", "vowifi_disabled",
    # systemd restarted the engine without the manager asking it to (Restart=always), i.e. the
    # engine process died on its own. reason_code carries how it left (engine_exit /
    # engine_signal / unknown) as reported by the entrypoint supervisor.
    "engine_restarted",
    # A start/reprovision refused by the PIN/identity preflight before the engine ran.
    # reason_code carries the closed code (no_card / pin_required / pin_invalid /
    # card_mismatch / card_unreadable); the ICCID itself never enters this public record.
    "preflight_blocked",
}
_LIFECYCLE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LIFECYCLE_INSTANCE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_diagnostic_jsonl_lock = threading.Lock()
_lifecycle_jsonl_lock = threading.Lock()
# Asterisk writes colour escapes even when captured to a file; strip them so the stored
# record stays greppable.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Lines worth keeping from an engine that is about to be restarted: the IMS registration
# exchange and the reasons Asterisk gives for not completing it. Matching on words such as
# "registration" or the endpoint name pulls in DEBUG chatter instead — nearly every debug
# line names res_pjsip_outbound_registration.c — which then crowds the real evidence out of
# the bounded tail. Match protocol lines and operator-visible failures only.
_SIP_EVIDENCE = re.compile(
    r"SIP/2\.0 \d{3}"                       # response status line
    r"|^(?:REGISTER|INVITE|MESSAGE|SUBSCRIBE) sip:"   # request line
    r"|No response received"                # the registration timed out with no answer
    r"|transport '[^']+' failed"            # the tunnel died under an established transport
    r"|Status: \w+"                         # Asterisk's own registration verdict
    r"|Failed to authenticate"
    r"|[Uu]nable to register")
# A DEBUG line only earns its place when it carries an actual SIP status line.
_DEBUG_LINE = re.compile(r"\bDEBUG\b")
_DISPLAY_TIMESTAMP = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4}\] ")
# journalctl -o short-iso: "2026-08-07T11:17:18+0800 host entrypoint.sh[42]: message".
_JOURNAL_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})([+-]\d{4}) \S+ [^:]+: ?(.*)$")
_ASTERISK_TIMESTAMP = re.compile(r"^\[[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\]\s*")
# Enough to cover a full REGISTER exchange plus the failures around it.
SIP_EVIDENCE_LINES = 40

DATA_DIR = cfg.DATA_DIR
ENGINECTL = os.environ.get("MDD_ENGINECTL", "/usr/local/sbin/mdd-enginectl")
ASTERISK = os.environ.get("MDD_ASTERISK", "/opt/mdd/current/asterisk/sbin/asterisk")
MAX_LINE = 10
ENGINECTL_TIMEOUT = 90
QUERY_TIMEOUT = 10
CLI_TIMEOUT = 5
SHOW_PROPERTIES = ("ActiveState", "SubState", "MainPID", "NRestarts", "InvocationID",
                   "ActiveEnterTimestamp", "ExecMainCode", "ExecMainStatus")


def line_number(iid) -> int:
    """1-10, the only ids an engine unit, a namespace and an address exist for."""
    text = str(iid)
    if not re.fullmatch(r"[1-9][0-9]?", text) or int(text) > MAX_LINE:
        raise ValueError(f"line id must be 1-{MAX_LINE}")
    return int(text)


def unit_name(iid) -> str:
    return f"mdd-engine@{line_number(iid)}.service"


def engine_address(iid) -> str:
    """The line's address inside its namespace (host/mdd_netns.Line.ns_addr)."""
    return f"10.88.{line_number(iid)}.2"


def manager_address(iid) -> str:
    """The host's end of the line's veth, where the engine reaches the control plane."""
    return f"10.88.{line_number(iid)}.1"


def _enginectl(action: str, iid) -> None:
    """The one privileged operation: ask mdd-enginectl to act on a line's unit."""
    command = ["sudo", "-n", ENGINECTL, action, str(line_number(iid))]
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=ENGINECTL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"mdd-enginectl {action} {iid} failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"mdd-enginectl {action} {iid} failed: {detail}")


def _private_group_dir(path: str, mode: int) -> None:
    """A line directory the engine (root, group mdd, no CAP_DAC_OVERRIDE) can use."""
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass    # created by the engine; already group-writable through its UMask


def _instance_paths(iid: str) -> str:
    base = os.path.join(DATA_DIR, "instances", str(iid))
    for path in (base, os.path.join(base, "run"), os.path.join(base, "logs")):
        _private_group_dir(path, 0o770)
    return base


def _clear_runtime_state(base: str):
    """Remove observations owned by the previous engine process.

    Runtime files live in the line's directory and therefore survive an engine restart.
    Keeping an old CONNECTED marker makes the new process look online before it has completed
    IKE and IMS registration.
    """
    run_dir = os.path.join(base, "run")
    for name in ("swu_status.json", "pcscf", "pcscf.applied", "pin_status.json",
                 "usim_status.json", "engine.env", "swu.ctl", "media.json", "media.nft",
                 "media.iptables"):
        try:
            os.unlink(os.path.join(run_dir, name))
        except FileNotFoundError:
            pass


def _tail_lines(path: str, limit: int) -> list[str]:
    try:
        with open(path, errors="replace") as handle:
            return handle.read().splitlines()[-limit:]
    except OSError:
        return []


def _charon_evidence(base: str) -> dict:
    """Summarise IKE health from the tunnel log.

    Retransmits and outright IKE timeouts are the signature of a lossy country exit, which
    looks identical to a carrier problem from the status machine's point of view.
    """
    lines = _tail_lines(os.path.join(base, "run", "charon.log"), 400)
    last_state = ""
    for line in reversed(lines):
        bare = _DISPLAY_TIMESTAMP.sub("", line, count=1)
        if bare.startswith("STATE ") or "tunnel CONNECTED" in bare:
            last_state = line.strip()
            break
    return {"available": bool(lines),
            "retransmits": sum(1 for line in lines if "retransmit" in line),
            "timeouts": sum(1 for line in lines if "TIMEOUT" in line),
            "last_state": last_state, "tail": lines[-40:]}


def ike_evidence(iid: str) -> dict:
    """Retransmit/timeout counts for one line, without building a whole diagnostic snapshot.

    The failover policy needs to know whether the tunnel's own signalling was answered; it
    runs on the freeze path, where reading the engine's journal would be far too heavy.
    """
    base = _instance_paths(str(iid))
    return _charon_evidence(base)


def registration_failure_evidence(log_tail: str) -> dict:
    """Classify the newest concrete REGISTER failure and retain its SIP response code.

    Asterisk reports both as "Rejected", but they are different events: a "Fatal response
    '403'" is the IMS refusing this line, while "No response received" is the IMS no longer
    hearing it — on this gateway almost always an ESP session the carrier aged out while
    the IKE side still answered keepalives. The newest marker in the log decides.
    """
    for line in reversed(log_tail.splitlines()):
        low = line.lower()
        # The real Asterisk message says "on registration attempt", not "on REGISTER
        # attempt".  ``registration`` does not contain the substring ``register``, so the
        # old extra guard made this production path unreachable.  This exact marker is emitted
        # by outbound registration's timeout path and is already the evidence retained by
        # _SIP_EVIDENCE.  A Docker log read failure is returned as "error: ...", which
        # deliberately does not match and therefore remains on the conservative slow path.
        if "no response received" in low:
            return {"kind": "unanswered"}
        match = re.search(r"fatal response '(\d+)'", low)
        if match:
            return {"kind": "rejected", "sip_status": int(match.group(1))}
    return {"kind": "unknown"}


def _sip_evidence(raw: str) -> list[str]:
    """Keep the SIP protocol lines and registration failures from an engine log."""
    kept = []
    for line in raw.splitlines():
        line = _ANSI.sub("", line).rstrip()
        bare = _DISPLAY_TIMESTAMP.sub("", line, count=1)
        if not _SIP_EVIDENCE.search(bare):
            continue
        if _DEBUG_LINE.search(bare) and "SIP/2.0" not in bare:
            continue
        kept.append(line)
    return kept[-SIP_EVIDENCE_LINES:]


def _egress_evidence(inst: dict) -> dict:
    """Which exit node this line was using when it failed."""
    try:
        country = egress.line_country(inst)
        current = (egress.status().get("exits") or {}).get(country) or {}
        return {"country": country, "node": current.get("node", ""),
                "selection": current.get("selection", ""),
                "candidate_count": current.get("candidate_count"),
                "ready": current.get("ready")}
    except Exception:
        return {}


def _host_evidence() -> dict:
    """The host conditions that can take every line down at once, plus what they mean."""
    try:
        snapshot = sysinfo.collect(DATA_DIR)
        return {"alerts": [item["code"] for item in sysinfo.alerts(snapshot)],
                "throttling": snapshot.get("throttling") or {},
                "undervoltage": snapshot.get("undervoltage") or {},
                "temperature_c": snapshot.get("temperature_c"),
                "load": (snapshot.get("load") or {}).get("per_core"),
                "memory": snapshot.get("memory") or {},
                "network": snapshot.get("network") or {}}
    except Exception:
        return {}


def _append_bounded_jsonl(path: str, record: dict, limit: int,
                          lock: threading.Lock, *, create_parent: bool = True) -> None:
    """Atomically append one record while retaining only a bounded tail."""
    if create_parent:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with lock:
        keep = max(0, int(limit) - 1)
        lines = _tail_lines(path, keep) if keep else []
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(tmp, path)


def _append_diagnostic(base: str, record: dict):
    _append_bounded_jsonl(
        os.path.join(base, "logs", "diagnostics.jsonl"), record, DIAGNOSTIC_RECORDS,
        _diagnostic_jsonl_lock)


def valid_lifecycle_reason(value: object) -> bool:
    """One shared validator for producers and the persistence boundary."""
    return bool(_LIFECYCLE_REASON.fullmatch(str(value or "")))


def record_lifecycle(iid: str, event: str, *, reason_code: str = "", **facts) -> None:
    """Persist one recovery decision without exporting identifiers or exception text.

    This is intentionally a closed schema.  Free-form exception strings, paths, hardware ids
    and subscriber identifiers must never enter a file intended for public support bundles.
    """
    event = str(event or "")
    reason_code = str(reason_code or "")
    iid = str(iid or "")
    if event not in LIFECYCLE_EVENTS:
        raise ValueError("invalid lifecycle event")
    if not _LIFECYCLE_INSTANCE.fullmatch(iid):
        raise ValueError("invalid lifecycle instance")
    if reason_code and not valid_lifecycle_reason(reason_code):
        raise ValueError("invalid lifecycle reason code")
    record = {"ts": int(time.time()), "instance": iid, "event": event}
    if reason_code:
        record["reason_code"] = reason_code
    for key in ("retry_count", "delay_seconds"):
        if key in facts and facts[key] is not None:
            record[key] = max(0, int(facts[key]))
    # The SIP response code behind a reg_rejected freeze (e.g. 403). A bare code is
    # support-safe and is exactly what distinguishes a carrier refusal from a dead tunnel.
    if facts.get("sip_status") is not None:
        record["sip_status"] = max(0, min(999, int(facts["sip_status"])))
    for key in ("card_present", "imei_valid", "imei_source_matches", "iccid_matches"):
        if key in facts and facts[key] is not None:
            record[key] = bool(facts[key])
    base = os.path.join(DATA_DIR, "instances", iid)
    # A lifecycle record may only extend an existing line directory.  Creating it here would
    # resurrect deleted/unknown ids and violate the API's "delete history" guarantee.
    if not os.path.isdir(base):
        return
    _append_bounded_jsonl(
        os.path.join(base, "logs", "lifecycle.jsonl"), record, LIFECYCLE_RECORDS,
        _lifecycle_jsonl_lock, create_parent=False)


def capture_diagnostics(iid: str, inst: dict, base: str, reason: str):
    """Persist the evidence that restarting the engine is about to destroy.

    Asterisk's live registration view disappears with the process, and the journal keeps only
    a bounded tail. A line stuck in the health policy's rebuild loop destroys its own evidence
    every couple of minutes, which is precisely when that evidence is needed. Never raises: a
    failed capture must not block the rebuild it is documenting.
    """
    try:
        record = {"ts": int(time.time()), "instance": str(iid), "reason": reason,
                  "registration": registration_state(iid),
                  "pcscf": read_pcscf(iid) or "",
                  "charon": _charon_evidence(base),
                  "egress": _egress_evidence(inst),
                  # A brown-out takes out the USB-attached NIC, the modem and the reader at
                  # once, and reaches the status machine as a tunnel that simply stopped
                  # passing traffic. Recorded here so that cause is visible beside the effect
                  # instead of being reconstructed from a shell session days later.
                  "host": _host_evidence()}
        for name in ("swu_status.json", "usim_status.json", "pin_status.json"):
            record[name[:-5]] = read_run_json(iid, name) or {}
        tail = logs(iid, 600)
        record["sip"] = _sip_evidence(tail)
        # The bare "Rejected" registration string cannot say WHY (issue #33 shipped a bundle
        # whose SIP response code was already rotated away); keep the classified verdict and
        # its numeric status code beside it. Digits-only, so redaction passes it through.
        record["registration_evidence"] = registration_failure_evidence(tail)
        _append_diagnostic(base, record)
    except Exception as exc:  # noqa
        log.warning("diagnostic capture failed for instance %s: %s", iid, exc)


def start(inst: dict, settings: dict, dev_mounts: bool = False, reason: str = "rebuild"):
    """(Re)start a line's engine; returns the new generation (systemd InvocationID).

    Everything that can refuse -- the line id, the exit, the ePDG lookup -- is settled before
    a running engine is touched, so a refusal leaves a working line as it was.
    """
    iid = str(inst["id"])
    line_number(iid)
    line_state = egress.ensure_line(inst, settings) or {}
    # The engine has no DNS of its own: the ePDG goes in as an address, resolved through the
    # line's country exit, or by the host for a direct line.
    epdg = egress.resolve_epdg(inst, line_state)
    rendered = {**inst, "epdg": epdg} if epdg else inst
    base = _instance_paths(iid)
    if is_running(iid):
        # Only a replacement destroys evidence; a first start has none to keep.
        capture_diagnostics(iid, inst, base, reason)
        _enginectl("stop", iid)
    _clear_runtime_state(base)
    cfg.write_instance_json(rendered, settings)
    try:
        _enginectl("reset-failed", iid)
    except RuntimeError:
        pass    # nothing to reset
    _enginectl("start", iid)
    # The namespace exists now; tell the orchestrator so it routes the line without waiting
    # for its next idle pass.
    try:
        egress.publish(settings=settings)
    except OSError as exc:
        log.warning("could not republish desired state after starting line %s: %s", iid, exc)
    log.info("started engine %s", unit_name(iid))
    return unit_runtime(iid).get("generation")


def stop(iid: str, expected_generation: str | None = None) -> bool:
    """Stop a line's engine. With ``expected_generation`` only that very run is stopped."""
    runtime = unit_runtime(iid)
    if expected_generation and str(runtime.get("generation")) != str(expected_generation):
        log.info("not stopping replacement engine %s (expected generation %s, found %s)",
                 iid, expected_generation, runtime.get("generation"))
        return False
    if runtime.get("active_state") in (None, "", "inactive") and not runtime.get("running"):
        return False
    _enginectl("stop", iid)
    return True


def capture_and_stop(iid: str, inst: dict, reason: str,
                     expected_generation: str | None = None) -> bool:
    """Snapshot a failing line, then stop its engine.

    The health policy gives up by stopping the engine and only restarts it after a cooldown,
    so by the time ``start()`` runs there is nothing left to read. This is the path that
    destroys the evidence in practice, and it is also the exact moment worth recording: the
    policy has just concluded the line cannot register.

    Blocking (CLI and journal reads); callers on the event loop must use a worker thread.
    """
    if expected_generation:
        if str(unit_runtime(iid).get("generation")) != str(expected_generation):
            return False
    base = _instance_paths(iid)
    capture_diagnostics(iid, inst, base, reason)
    return (stop(iid, expected_generation=expected_generation)
            if expected_generation else stop(iid))


def delete_instance_data(iid: str) -> bool:
    """Remove one deleted line's rendered config, runtime markers and bounded logs."""
    root = os.path.realpath(os.path.join(DATA_DIR, "instances"))
    target = os.path.realpath(os.path.join(root, str(iid)))
    if os.path.dirname(target) != root:
        raise ValueError("invalid instance id")
    try:
        _enginectl("down", iid)     # the engine and the namespace
    except (RuntimeError, ValueError) as exc:
        log.info("line %s: no engine to take down before deleting its data: %s", iid, exc)
    if not os.path.isdir(target):
        return False
    shutil.rmtree(target)
    return True


def parse_show(text: str) -> list[dict[str, str]]:
    """``systemctl show`` output for one or more units, one dict per unit, in order."""
    units, current = [], {}
    for row in text.splitlines():
        if not row.strip():
            if current:
                units.append(current)
                current = {}
            continue
        key, _, value = row.partition("=")
        current[key] = value
    if current:
        units.append(current)
    return units


def runtime_from_show(iid: str, props: dict[str, str]) -> dict:
    active = props.get("ActiveState", "")
    running = active == "active" and props.get("SubState", "") == "running"
    try:
        restarts = int(props.get("NRestarts") or 0)
    except ValueError:
        restarts = 0
    return {"running": running, "ip": engine_address(iid) if running else None,
            "generation": props.get("InvocationID") or None,
            "restart_count": restarts,
            "started_at": props.get("ActiveEnterTimestamp", "") if running else "",
            "active_state": active, "sub_state": props.get("SubState", "")}


def _stopped(iid: str) -> dict:
    return {"running": False, "ip": None, "generation": None, "restart_count": 0,
            "started_at": "", "active_state": "", "sub_state": ""}


def units_runtime(iids: list[str]) -> dict[str, dict]:
    """The runtime of several lines from one ``systemctl show`` (no privileges needed)."""
    valid = []
    for iid in iids:
        try:
            line_number(iid)
            valid.append(str(iid))
        except ValueError:
            continue
    result = {str(iid): _stopped(str(iid)) for iid in iids}
    if not valid:
        return result
    command = ["systemctl", "show", "--property=" + ",".join(SHOW_PROPERTIES),
               *[unit_name(iid) for iid in valid]]
    try:
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=QUERY_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("systemctl show failed: %s", exc)
        return result
    blocks = parse_show(completed.stdout) if completed.returncode == 0 else []
    for iid, props in zip(valid, blocks):
        result[iid] = runtime_from_show(iid, props)
    return result


def unit_runtime(iid: str) -> dict:
    """Running state, address and generation of one line's engine."""
    return units_runtime([str(iid)])[str(iid)]


def is_running(iid: str) -> bool:
    return unit_runtime(iid)["running"]


def last_engine_exit(iid: str) -> dict:
    """The entrypoint supervisor's last record of how Asterisk left, if any.

    The unit's exit status belongs to the supervisor script, not to Asterisk, so it cannot say
    whether Asterisk shut down cleanly or died some other way. The supervisor writes the
    disposition it observed directly; this reads it back.
    """
    base = _instance_paths(iid)
    path = os.path.join(base, "logs", "asterisk", "supervisor.jsonl")
    try:
        for line in reversed(_tail_lines(path, 40)):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "asterisk_exited":
                return record
    except OSError:
        pass
    return {}


def read_run_json(iid: str, name: str) -> dict | None:
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", name)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def read_pcscf(iid: str) -> str | None:
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", "pcscf")
    try:
        with open(path) as f:
            v = f.read().strip()
            return v or None
    except Exception:
        return None


def tunnel_installed(iid: str) -> bool:
    """True if the ims tunnel is up: the swu_ike daemon writes run/swu_status.json
    {state: CONNECTED} once the SWu (ePDG) IPsec tunnel is established."""
    st = read_run_json(iid, "swu_status.json")
    return st is not None and st.get("state") == "CONNECTED"


def control_socket(iid: str) -> str:
    """Asterisk's control socket; asterisk.conf puts it in the line's run directory."""
    return os.path.join(DATA_DIR, "instances", str(iid), "run", "asterisk", "asterisk.ctl")


def _cli(iid: str, command: str, timeout: float = CLI_TIMEOUT) -> str:
    """One Asterisk CLI command over the line's control socket. Raises on failure."""
    completed = subprocess.run([ASTERISK, "-r", "-s", control_socket(iid), "-x", command],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout)
    if completed.returncode:
        raise RuntimeError((completed.stdout or "").strip()[:300] or
                           f"asterisk exited {completed.returncode}")
    return completed.stdout


def exec_cli(iid: str, command: str) -> str:
    try:
        return _cli(iid, command)
    except Exception as e:  # noqa
        return f"error: {e}"


def registration_state(iid: str) -> str:
    """Read IMS registration through the local Asterisk CLI.

    Some IMS-patched Asterisk builds accept AMI's PJSIPShowRegistrationsDetailed action but
    never complete it. Treating that management timeout as a carrier failure eventually stops
    an otherwise healthy line. The CLI is the authoritative local view and has proven reliable
    on those same builds. It can block behind an IMS TCP connect, hence the short timeout.
    """
    try:
        output = _cli(iid, "pjsip show registrations")
    except Exception:
        return "unknown"
    if re.search(r"\bRejected\b", output, re.I):
        return "Rejected"
    if re.search(r"\bUnregistered\b", output, re.I):
        return "Unregistered"
    if re.search(r"\bRegistered\b", output):
        return "Registered"
    return "unknown"


def _format_journal(raw: str) -> str:
    """Render journal lines in the same local format as the IKE log."""
    rendered = []
    for line in raw.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        match = _JOURNAL_LINE.match(content)
        if not match:
            rendered.append(line)
            continue
        date, clock, zone, message = match.groups()
        message = _ASTERISK_TIMESTAMP.sub("", message, count=1)
        rendered.append(f"[{date} {clock}{zone}] {message}{ending}")
    return "".join(rendered)


def logs(iid: str, tail: int = 200, since=None) -> str:
    """The engine's recent output from the journal (the control plane's user is in the
    systemd-journal group), timestamped at emission."""
    try:
        command = ["journalctl", "-u", unit_name(iid), "-n", str(int(tail)), "-o", "short-iso",
                   "--no-pager", "-q"]
        if since is not None:
            stamp = since.timestamp() if isinstance(since, datetime) else float(since)
            command += ["--since", f"@{int(stamp)}"]
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=QUERY_TIMEOUT)
        if completed.returncode:
            return f"error: {(completed.stderr or '').strip()[:300]}"
        return _format_journal(completed.stdout)
    except Exception as e:  # noqa
        return f"error: {e}"


def charon_log(iid: str, tail: int = 200) -> str:
    """Recent SWu tunnel (IKE) log lines from the instance run dir. The file is named
    charon.log for control-plane/WebUI compatibility (the log-view key is 'charon')."""
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", "charon.log")
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-tail:])
    except Exception:
        return ""


def usim_status(iid: str) -> dict:
    return read_run_json(iid, "usim_status.json") or {}
