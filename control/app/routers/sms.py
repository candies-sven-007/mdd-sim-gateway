"""/api/sms: the texts received on the lines (receive only; there is no sending)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from .. import config as cfg
from .. import store

router = APIRouter()


@router.get("/api/sms")
async def api_sms_list(instance: str = "", limit: int = 200):
    """Newest first, with the unread count of every line."""
    messages = await asyncio.to_thread(store.list_sms, instance or None, limit)
    unread = await asyncio.to_thread(store.unread_sms_count)
    return {"messages": messages, "unread": unread,
            "retention_days": store.SMS_RETENTION_SECONDS // 86400}


@router.post("/api/sms/read")
async def api_sms_read(body: dict | None = None):
    """Mark texts read: {"ids": [...]}, or {"instance": "1"}, or nothing for all of them."""
    body = body or {}
    ids = body.get("ids")
    if ids is not None and (not isinstance(ids, list)
                            or not all(isinstance(i, int) and not isinstance(i, bool)
                                       for i in ids)):
        raise HTTPException(400, "ids must be a list of integers")
    changed = await asyncio.to_thread(store.mark_sms_read, ids or None,
                                      str(body["instance"]) if body.get("instance") else None)
    return {"ok": True, "changed": changed}


@router.delete("/api/sms/{message_id}")
async def api_sms_delete(message_id: int):
    deleted = await asyncio.to_thread(store.delete_sms, [message_id])
    if not deleted:
        raise HTTPException(404, "no such message")
    return {"ok": True}


@router.delete("/api/sms")
async def api_sms_clear(instance: str = ""):
    """Delete every text, or those of one line."""
    if instance and not cfg.get_instance(instance):
        raise HTTPException(404, "no such instance")
    deleted = await asyncio.to_thread(store.delete_sms, None, instance or None)
    return {"ok": True, "deleted": deleted}
