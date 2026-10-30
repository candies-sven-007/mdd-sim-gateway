#!/usr/bin/env python3
"""
notify.py - Best-effort event hook: POST engine events to the manager.

Usage (from the Asterisk dialplan or swu_ike tunnel hooks):
  notify.py <event> [arg1] [arg2] ...

Events: call_in <from>, call_out <to>, call_result <dir> <peer> <dialstatus> <cause> <by>,
        sms_in <from> <body_b64> <concat ref/total/seq> <pid> <dcs> <udh> <tpdu>, tunnel_up, tunnel_down, registered, unregistered, cp_mode_resolved, engine_stopped

Reads MANAGER_URL and MDD_ID from $MDD_RUNDIR/engine.env (written by render.py).
Never fails the caller (all exceptions swallowed).
"""
import os
import sys
import json
import time

LOGDIR = os.environ.get("MDD_LOGDIR", "/logs")


def load_env():
    env = {}
    path = os.environ.get("MDD_ENV") or os.path.join(
        os.environ.get("MDD_RUNDIR", "/run/mdd-sim-gateway"), "engine.env")
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    except Exception:
        pass
    return env


def main():
    if len(sys.argv) < 2:
        return
    event = sys.argv[1]
    args = sys.argv[2:]
    env = load_env()
    manager_url = os.environ.get("MANAGER_URL") or env.get("MANAGER_URL", "")
    inst_id = os.environ.get("MDD_ID") or env.get("MDD_ID", "1")
    payload = {"ts": int(time.time()), "instance": inst_id, "event": event, "args": args}
    # Always append to a local event log so nothing is lost if the manager is down. A received
    # text is the exception: its body stays out of every log and goes to the manager only.
    try:
        if event == "sms_in":
            raise OSError("not logged")
        os.makedirs(LOGDIR, exist_ok=True)
        with open(os.path.join(LOGDIR, "events.jsonl"), "a") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass
    if not manager_url:
        return
    url = f"{manager_url.rstrip('/')}/api/engine/event"
    token = os.environ.get("MANAGER_EVENT_TOKEN") or env.get("MANAGER_EVENT_TOKEN", "")
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
    except ImportError:
        requests = None
    except Exception as e:
        _warn(f"{event} -> {manager_url}: {type(e).__name__}")
        return
    if requests is not None:
        try:
            r = requests.post(url, json=payload,
                              headers={"X-MDD-Engine-Token": token}, timeout=3, verify=False)
            if r.status_code >= 300:
                _warn(f"{event} -> {manager_url}: HTTP {r.status_code}")
            return
        except Exception as e:
            # Still never fail the caller, but leave a trace in the container log: an unreachable
            # manager used to drop every inbound SMS without a single line anywhere.
            _warn(f"{event} -> {manager_url}: {type(e).__name__}")
            return
    # Fallback for interpreters without requests (e.g. a host python3 that the
    # dialplan shebang resolves to outside the container image): stdlib urllib.
    try:
        import ssl
        import urllib.request
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "X-MDD-Engine-Token": token})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=3, context=ctx) as r:
            if r.status >= 300:
                _warn(f"{event} -> {manager_url}: HTTP {r.status}")
    except Exception as e:
        _warn(f"{event} -> {manager_url}: {type(e).__name__}")


def _warn(msg: str):
    try:
        print(f"[notify] event not delivered: {msg} (kept in {LOGDIR}/events.jsonl)",
              file=sys.stderr, flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
