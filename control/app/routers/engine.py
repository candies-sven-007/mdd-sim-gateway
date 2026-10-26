"""/api/engine/event: the callback each line's engine posts calls and registration events to."""
from __future__ import annotations

import asyncio
import logging
from fastapi import APIRouter

from .. import config as cfg
from .. import store
from .. import state as app_state

log = logging.getLogger("vowifi.main")
router = APIRouter()


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


@router.post("/api/engine/event")
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
        asyncio.create_task(app_state.push_status(iid))
    return {"ok": True}
