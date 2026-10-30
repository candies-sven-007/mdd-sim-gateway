"""
store.py - SQLite persistence for the call log, received texts and the connectivity timeline.

One database per gateway at $MDD_DATA/mdd-sim-gateway.sqlite. Rows carry the line (instance)
id. New rows are broadcast to the WebSocket layer by the caller (main.py).

This build only RECEIVES text messages (table `sms`, kept SMS_RETENTION_SECONDS); it keeps no
MMS, voicemail, sent-message or contact data. It is installed fresh; there is no upgrade path
from the container deployment's database.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("MDD_DATA", os.path.join(os.getcwd(), "data"))

# Rows that belong to a person rather than to the gateway carry an owner. This gateway has a
# single administrator, so that owner is the same everywhere.
ADMIN_OWNER = 1
DB_PATH = os.path.join(DATA_DIR, "mdd-sim-gateway.sqlite")
_lock = threading.Lock()

# Connectivity timeline. Line state is sampled every few seconds, so it is stored as merged
# segments instead of one row per sample: two days of history stays a handful of rows.
LINE_STATES = ("up", "down", "off")
# The longest silence still treated as one continuous observation. Anything longer is a hole
# in the record (control plane restarted / host powered off) and must stay visible as one
# instead of being interpolated into a healthy stretch.
LINE_STATE_CONTINUITY_SECONDS = 90
LINE_STATE_RETENTION_SECONDS = 3 * 24 * 3600

SCHEMA_VERSION = 2

# Received texts are deleted after this long (the page can delete them sooner).
SMS_RETENTION_SECONDS = 30 * 24 * 3600
# Parts of a long text that never complete are saved as the part of the text that did arrive.
SMS_SEGMENT_WAIT_SECONDS = 600


@contextlib.contextmanager
def _sqlite_conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    return _sqlite_conn(DB_PATH)


def init():
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance TEXT NOT NULL,
                direction TEXT NOT NULL,        -- 'in' | 'out'
                peer TEXT NOT NULL,
                status TEXT DEFAULT '',         -- ringing|answered|ended|missed|failed
                start_ts INTEGER NOT NULL,
                end_ts INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_calls_inst ON calls(instance, start_ts);
            CREATE TABLE IF NOT EXISTS line_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance TEXT NOT NULL,
                state TEXT NOT NULL,        -- 'up' | 'down' | 'off'
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',  -- reason_code when the segment began
                detail TEXT NOT NULL DEFAULT ''   -- evidence behind it (names, resolvers...)
            );
            CREATE INDEX IF NOT EXISTS idx_line_states ON line_states(instance, start_ts);
            CREATE TABLE IF NOT EXISTS sms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance TEXT NOT NULL,
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_ts INTEGER,                -- the service centre's timestamp, if known
                received_ts INTEGER NOT NULL,
                read INTEGER NOT NULL DEFAULT 0,
                parts INTEGER NOT NULL DEFAULT 1,   -- how many parts the text had
                complete INTEGER NOT NULL DEFAULT 1 -- 0: saved before every part arrived
            );
            CREATE INDEX IF NOT EXISTS idx_sms ON sms(instance, received_ts);
            CREATE TABLE IF NOT EXISTS sms_parts (
                instance TEXT NOT NULL,
                sender TEXT NOT NULL,
                ref INTEGER NOT NULL,
                total INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                body TEXT NOT NULL,
                sent_ts INTEGER,
                received_ts INTEGER NOT NULL,
                PRIMARY KEY (instance, sender, ref, total, seq)
            );
            """
        )
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def snapshot_history(database_target: str) -> None:
    """A consistent copy of the live database (SQLite online backup) for a local backup."""
    if os.path.exists(database_target):
        os.remove(database_target)
    os.makedirs(os.path.dirname(database_target) or ".", exist_ok=True)
    with _sqlite_conn(DB_PATH) as source, _sqlite_conn(database_target) as copy:
        source.backup(copy)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def add_call(instance: str, direction: str, peer: str, status: str = "ringing") -> dict:
    ts = int(time.time())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO calls(instance,direction,peer,status,start_ts) VALUES(?,?,?,?,?)",
            (str(instance), direction, peer, status, ts),
        )
        cid = cur.lastrowid
    return {"id": cid, "instance": str(instance), "direction": direction,
            "peer": peer, "status": status, "start_ts": ts}


def get_open_call(instance: str, direction: str, within_s: int | None = None) -> dict | None:
    """The most recent still-open (not yet finalized, end_ts IS NULL) call for (instance,
    direction), or None. `within_s` bounds how old the open record may be so a genuinely new
    call after a stale unfinalized one still starts fresh."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM calls WHERE instance=? AND direction=? AND end_ts IS NULL "
            "ORDER BY start_ts DESC LIMIT 1", (str(instance), direction)).fetchone()
        if not row:
            return None
        if within_s is not None and int(time.time()) - row["start_ts"] > within_s:
            return None
        return dict(row)


def add_call_deduped(instance: str, direction: str, peer: str, status: str = "ringing",
                     open_within_s: int = 90) -> dict:
    """Insert an inbound-call record, coalescing concurrent duplicate `call_in` events for the
    SAME still-ringing call into ONE record.

    The IMS can deliver a call_in more than once while the call is still being set up (VoLTE
    preconditions / GRUU fork): those extra events all arrive BEFORE the call_result, so the
    record is still open (end_ts IS NULL) and is simply reused — no ghost row, and no reliance
    on any time heuristic that could swallow a genuine call-back.

    (The other historical source of duplicates — the dialplan 'h' hangup handler falling
    through to the broad `_.` pattern and firing a SECOND call_in AFTER finalization — is fixed
    at the source in extensions.conf.j2 with `h => …,Return()`, so no post-finalize dedupe is
    needed here.)

    An anonymous first call_in ('') whose number arrives on a later duplicate is filled in."""
    open_rec = get_open_call(instance, direction, within_s=open_within_s)
    # Only coalesce into an open record with a compatible peer: an anonymous dup ('') matches
    # anything; a numbered dup must match (or fill) the open record's peer. A different number
    # is a distinct call and starts its own record.
    if open_rec:
        rp = open_rec.get("peer") or ""
        if not peer or not rp or peer == rp:
            if peer and not rp:
                with _lock, _conn() as c:
                    c.execute("UPDATE calls SET peer=? WHERE id=?", (peer, open_rec["id"]))
                open_rec["peer"] = peer
            return open_rec
    return add_call(instance, direction, peer, status)


def update_call(cid: int, status: str, ended: bool = False):
    with _lock, _conn() as c:
        if ended:
            c.execute("UPDATE calls SET status=?, end_ts=? WHERE id=?",
                      (status, int(time.time()), cid))
        else:
            c.execute("UPDATE calls SET status=? WHERE id=?", (status, cid))


def update_last_call(instance: str, direction: str, peer: str | None, status: str) -> dict | None:
    """Finalize the most recent still-open call for (instance, direction[, peer]).

    peer may be None/empty: Asterisk's 'h' hangup handler loses pre-Dial channel variables
    (incl. the dialled number) when the caller hangs up mid-Dial and the channel is
    masqueraded, so the disposition callback can arrive with no peer. Since a softphone
    handles one call at a time, finalizing the most-recent OPEN call of that direction is
    unambiguous and correct in that case."""
    with _lock, _conn() as c:
        if peer:
            row = c.execute(
                "SELECT id FROM calls WHERE instance=? AND direction=? AND peer=? AND end_ts IS NULL "
                "ORDER BY start_ts DESC LIMIT 1", (str(instance), direction, peer)).fetchone()
        else:
            row = c.execute(
                "SELECT id FROM calls WHERE instance=? AND direction=? AND end_ts IS NULL "
                "ORDER BY start_ts DESC LIMIT 1", (str(instance), direction)).fetchone()
        if not row:
            return None
        c.execute("UPDATE calls SET status=?, end_ts=? WHERE id=?",
                  (status, int(time.time()), row["id"]))
        r = c.execute("SELECT * FROM calls WHERE id=?", (row["id"],)).fetchone()
        return dict(r) if r else None



def list_calls(instance: str, limit: int = 100) -> list:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM calls WHERE instance=? ORDER BY start_ts DESC LIMIT ?",
            (str(instance), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_calls(instance: str, ids: list[int]) -> int:
    """Delete specific call-log entries of this instance by id. Returns rows removed."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with _lock, _conn() as c:
        cur = c.execute(
            f"DELETE FROM calls WHERE instance=? AND id IN ({_placeholders(len(ids))})",
            (str(instance), *ids),
        )
        return cur.rowcount


def clear_calls(instance: str) -> int:
    """Delete the ENTIRE call log for this instance. Returns rows removed."""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM calls WHERE instance=?", (str(instance),))
        return cur.rowcount


def record_line_state(instance: str, state: str, ts: int | None = None,
                      reason: str = "", detail: str = "") -> None:
    """Append one connectivity observation for a line to its timeline.

    A sample that continues the current state only moves the segment's end. A different
    state starts a new segment where the previous one ended, so an uninterrupted record has
    no artificial holes; a silence longer than the continuity window deliberately leaves
    one, because the control plane cannot claim to know what happened while it was down.

    ``reason`` is the status reason_code that came with the sample, ``detail`` the evidence
    behind it (the name that failed to resolve, the resolvers it was tried against…). A
    segment keeps the first non-empty reason it saw — an outage's cause is what broke it,
    not the "registering…" it passes through on the way back up — and detail travels with
    the reason it belongs to.
    """
    state = state if state in LINE_STATES else "down"
    ts = int(ts if ts is not None else time.time())
    reason, detail = str(reason or ""), str(detail or "")
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id, state, end_ts, reason FROM line_states WHERE instance=? "
            "ORDER BY end_ts DESC, id DESC LIMIT 1", (str(instance),)).fetchone()
        if row and ts < row["end_ts"]:
            # A clock stepping backwards (NTP sync after boot) must never produce a segment
            # that ends before it starts.
            ts = int(row["end_ts"])
        continuous = bool(row) and ts - int(row["end_ts"]) <= LINE_STATE_CONTINUITY_SECONDS
        if continuous and row["state"] == state:
            # The first symptom can be above the actual fault: an IMS registration may become
            # Rejected seconds before swu_ike proves that the ePDG ignored every CHILD_SA rekey.
            # Replace only with a strictly stronger causal observation; recovery's generic
            # "registering" state must never erase the fault that began the outage.
            cause_priority = {
                "": 0, "registering": 5, "tunnel_setup": 10, "reg_rejected": 20,
                "reg_unanswered": 30, "tunnel_network": 35,
                "tunnel_child_rekey_timeout": 50, "tunnel_ike_rekey_timeout": 50,
                "tunnel_rekey_send_error": 50, "tunnel_sim_auth": 50,
                "tunnel_not_authorized": 50, "tunnel_proposal": 50,
                "reg_reauth_failed": 50, "maintenance_rebuild": 60,
                "client_engine_failure": 60,
            }
            stronger = (reason and cause_priority.get(reason, 25)
                        > cause_priority.get(str(row["reason"] or ""), 25))
            if reason and (not row["reason"] or stronger):
                c.execute("UPDATE line_states SET end_ts=?, reason=?, detail=? WHERE id=?",
                          (ts, reason, detail, row["id"]))
            else:
                c.execute("UPDATE line_states SET end_ts=? WHERE id=?", (ts, row["id"]))
            return
        start = int(row["end_ts"]) if continuous else ts
        c.execute("INSERT INTO line_states(instance,state,start_ts,end_ts,reason,detail) "
                  "VALUES(?,?,?,?,?,?)", (str(instance), state, start, ts, reason, detail))


def line_states(instance: str, since_ts: int) -> list[dict]:
    """Recorded segments of one line that overlap [since_ts, now], oldest first."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT state, start_ts, end_ts, reason, detail FROM line_states "
            "WHERE instance=? AND end_ts>=? "
            "ORDER BY start_ts ASC, id ASC", (str(instance), int(since_ts))).fetchall()
    return [dict(r) for r in rows]


def line_state_timeline(instance: str, start_ts: int, end_ts: int) -> list[dict]:
    """Gap-aware timeline for a window: recorded segments clipped to it, every hole in the
    record reported as `unknown`.

    A hole shorter than the continuity window is sampling jitter and is absorbed by the
    neighbouring segment. A longer one is left as `unknown` on purpose: the control plane
    was not watching, so drawing that period as either healthy or failed would be a claim
    it cannot make.
    """
    start, end = int(start_ts), int(end_ts)
    result: list[dict] = []
    cursor = start
    for row in line_states(instance, start):
        segment_start = max(int(row["start_ts"]), start)
        segment_end = min(int(row["end_ts"]), end)
        # A segment holding a single sample is still zero-length; keep it so the newest
        # state appears immediately instead of after the second sample of that state.
        if segment_end < segment_start:
            continue
        hole = segment_start - cursor
        if hole > 0:
            if result and hole <= LINE_STATE_CONTINUITY_SECONDS:
                result[-1]["end"] = segment_start
            else:
                result.append({"state": "unknown", "start": cursor, "end": segment_start})
        if result and result[-1]["state"] == row["state"] and result[-1]["end"] >= segment_start:
            result[-1]["end"] = max(result[-1]["end"], segment_end)
            if not result[-1].get("reason"):
                # Reason and detail describe the same sample; they move as a pair.
                result[-1]["reason"] = str(row.get("reason") or "")
                result[-1]["detail"] = str(row.get("detail") or "")
        else:
            result.append({"state": str(row["state"]), "start": segment_start,
                           "end": segment_end, "reason": str(row.get("reason") or ""),
                           "detail": str(row.get("detail") or "")})
        cursor = result[-1]["end"]
    if end > cursor:
        if result and end - cursor <= LINE_STATE_CONTINUITY_SECONDS:
            result[-1]["end"] = end
        else:
            result.append({"state": "unknown", "start": cursor, "end": end})
    return result


def line_state_summary(segments: list[dict]) -> dict:
    """Totals for a timeline. Availability counts observed time only, never assumed time."""
    totals = {"up": 0, "down": 0, "off": 0, "unknown": 0}
    outages, longest = 0, 0
    for segment in segments:
        length = max(0, int(segment["end"]) - int(segment["start"]))
        totals[segment["state"]] = totals.get(segment["state"], 0) + length
        if segment["state"] == "down":
            outages += 1
            longest = max(longest, length)
    observed = totals["up"] + totals["down"]
    return {**totals, "observed_seconds": observed, "outages": outages,
            "longest_outage_seconds": longest,
            "uptime_ratio": (totals["up"] / observed) if observed else None}


def line_state_recorded_since(instance: str) -> int | None:
    """Oldest retained observation for this line, or None when nothing was ever recorded."""
    with _lock, _conn() as c:
        row = c.execute("SELECT MIN(start_ts) AS first_ts FROM line_states WHERE instance=?",
                        (str(instance),)).fetchone()
    return int(row["first_ts"]) if row and row["first_ts"] is not None else None


def prune_line_states(before_ts: int) -> int:
    """Drop history that has aged past retention. Returns rows removed."""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM line_states WHERE end_ts < ?", (int(before_ts),))
        return cur.rowcount


def clear_line_states(instance: str) -> int:
    """Delete the connectivity timeline of one line. Returns rows removed."""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM line_states WHERE instance=?", (str(instance),))
        return cur.rowcount


# ----------------------------------------------------------------------- received texts
def _sms_row(row) -> dict:
    return {"id": row["id"], "instance": row["instance"], "sender": row["sender"],
            "body": row["body"], "sent_ts": row["sent_ts"], "received_ts": row["received_ts"],
            "read": bool(row["read"]), "parts": row["parts"], "complete": bool(row["complete"])}


def add_sms(instance: str, sender: str, body: str, sent_ts: int | None = None,
            parts: int = 1, complete: bool = True, now: int | None = None) -> dict | None:
    """Store one received text; None when the same text from the same sender is already there.

    A carrier re-delivers a message whose RP-ACK it did not get, and an IMS fork can deliver one
    twice. The service centre timestamp makes the repeat recognisable; without one, a repeat
    within a minute is treated as the same text.
    """
    now = int(now if now is not None else time.time())
    with _lock, _conn() as c:
        if sent_ts is not None:
            same = c.execute("SELECT 1 FROM sms WHERE instance=? AND sender=? AND body=? "
                             "AND sent_ts=?", (instance, sender, body, sent_ts)).fetchone()
        else:
            same = c.execute("SELECT 1 FROM sms WHERE instance=? AND sender=? AND body=? "
                             "AND received_ts>=?", (instance, sender, body, now - 60)).fetchone()
        if same:
            return None
        cur = c.execute(
            "INSERT INTO sms (instance, sender, body, sent_ts, received_ts, parts, complete) "
            "VALUES (?,?,?,?,?,?,?)",
            (instance, sender, body, sent_ts, now, parts, 1 if complete else 0))
        row = c.execute("SELECT * FROM sms WHERE id=?", (cur.lastrowid,)).fetchone()
    return _sms_row(row)


def add_sms_part(instance: str, sender: str, ref: int, total: int, seq: int, body: str,
                 sent_ts: int | None = None, now: int | None = None) -> dict | None:
    """Buffer one part of a long text. Returns the joined text once every part is in, else None.

    The parts of one message are not guaranteed to arrive in order, and each is acknowledged to
    the network as it comes, so they are kept here until the set is complete.
    {"body", "sent_ts", "parts"} is returned (and the parts removed) when it is.
    """
    now = int(now if now is not None else time.time())
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO sms_parts (instance, sender, ref, total, seq, body, "
                  "sent_ts, received_ts) VALUES (?,?,?,?,?,?,?,?)",
                  (instance, sender, ref, total, seq, body, sent_ts, now))
        rows = c.execute("SELECT seq, body, sent_ts FROM sms_parts WHERE instance=? AND sender=? "
                         "AND ref=? AND total=? ORDER BY seq", (instance, sender, ref, total)
                         ).fetchall()
        if len(rows) < total:
            return None
        c.execute("DELETE FROM sms_parts WHERE instance=? AND sender=? AND ref=? AND total=?",
                  (instance, sender, ref, total))
    stamps = [r["sent_ts"] for r in rows if r["sent_ts"] is not None]
    return {"body": "".join(r["body"] for r in rows), "parts": total,
            "sent_ts": min(stamps) if stamps else sent_ts}


def flush_stale_sms_parts(now: int | None = None) -> list[dict]:
    """Save, as incomplete texts, the groups whose missing parts never arrived."""
    now = int(now if now is not None else time.time())
    saved = []
    with _lock, _conn() as c:
        groups = c.execute("SELECT instance, sender, ref, total, MIN(received_ts) AS first "
                           "FROM sms_parts GROUP BY instance, sender, ref, total "
                           "HAVING first < ?", (now - SMS_SEGMENT_WAIT_SECONDS,)).fetchall()
        collected = []
        for g in groups:
            rows = c.execute("SELECT seq, body, sent_ts FROM sms_parts WHERE instance=? AND "
                             "sender=? AND ref=? AND total=? ORDER BY seq",
                             (g["instance"], g["sender"], g["ref"], g["total"])).fetchall()
            c.execute("DELETE FROM sms_parts WHERE instance=? AND sender=? AND ref=? AND "
                      "total=?", (g["instance"], g["sender"], g["ref"], g["total"]))
            collected.append((g, rows))
    for g, rows in collected:
        stamps = [r["sent_ts"] for r in rows if r["sent_ts"] is not None]
        rec = add_sms(g["instance"], g["sender"], "".join(r["body"] for r in rows),
                      sent_ts=min(stamps) if stamps else None, parts=g["total"],
                      complete=False, now=now)
        if rec:
            saved.append(rec)
    return saved


def list_sms(instance: str | None = None, limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    with _lock, _conn() as c:
        if instance:
            rows = c.execute("SELECT * FROM sms WHERE instance=? ORDER BY received_ts DESC, "
                             "id DESC LIMIT ?", (str(instance), limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM sms ORDER BY received_ts DESC, id DESC LIMIT ?",
                             (limit,)).fetchall()
    return [_sms_row(r) for r in rows]


def unread_sms_count() -> dict:
    with _lock, _conn() as c:
        rows = c.execute("SELECT instance, COUNT(*) AS n FROM sms WHERE read=0 "
                         "GROUP BY instance").fetchall()
    return {r["instance"]: r["n"] for r in rows}


def mark_sms_read(ids: list[int] | None = None, instance: str | None = None) -> int:
    with _lock, _conn() as c:
        if ids:
            cur = c.execute(f"UPDATE sms SET read=1 WHERE read=0 AND id IN "
                            f"({_placeholders(len(ids))})", [int(i) for i in ids])
        elif instance:
            cur = c.execute("UPDATE sms SET read=1 WHERE read=0 AND instance=?",
                            (str(instance),))
        else:
            cur = c.execute("UPDATE sms SET read=1 WHERE read=0")
        return cur.rowcount


def delete_sms(ids: list[int] | None = None, instance: str | None = None) -> int:
    """Delete the listed texts, or every text of a line, or (neither given) all of them."""
    with _lock, _conn() as c:
        if ids:
            cur = c.execute(f"DELETE FROM sms WHERE id IN ({_placeholders(len(ids))})",
                            [int(i) for i in ids])
        elif instance:
            cur = c.execute("DELETE FROM sms WHERE instance=?", (str(instance),))
            c.execute("DELETE FROM sms_parts WHERE instance=?", (str(instance),))
        else:
            cur = c.execute("DELETE FROM sms")
            c.execute("DELETE FROM sms_parts")
        return cur.rowcount


def prune_sms(now: int | None = None) -> int:
    """Delete texts older than the retention period."""
    cutoff = int(now if now is not None else time.time()) - SMS_RETENTION_SECONDS
    with _lock, _conn() as c:
        return c.execute("DELETE FROM sms WHERE received_ts < ?", (cutoff,)).rowcount
