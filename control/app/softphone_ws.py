"""Relay the browser softphone's SIP-over-WebSocket to its line's engine.

The browser only ever talks to the control surface origin (HTTPS, or a reverse proxy in front
of it) and picks a line by path. Each engine's Asterisk serves plain WS on its docker-bridge
address, which is never published to the host, so this relay is the only way in. That keeps a
single TLS hop and a single certificate (the WebUI's), and one path per line instead of one
host port per line.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from starlette.websockets import WebSocket
from websockets.asyncio.client import connect

log = logging.getLogger("mdd.softphone_ws")

SUBPROTOCOL = "sip"
# Asterisk's plain HTTP server inside the engine (http.conf bindport). Container-internal, so
# every line uses the same port; the container address is what tells lines apart.
ENGINE_WS_PORT = 8088
# One WebSocket message is one SIP message. A WebRTC INVITE with its SDP and ICE candidates is
# a few KB, so this only bounds a misbehaving peer.
MAX_MESSAGE = 256 * 1024
OPEN_TIMEOUT = 5

# Why a line refused the softphone. Closing before the handshake reached the browser only as
# an anonymous failure (1006), which JsSIP retried every few seconds for ever and the uvicorn
# log showed as 403s. The relay now accepts and closes with one of these instead; the page
# reads the code, says why, and backs off on its own (webui/src/softphone.js).
REFUSED_NO_LINE = 4404
REFUSED_DISABLED = 4410
REFUSED_NOT_RUNNING = 4409
REFUSED_UNAVAILABLE = 4503
REFUSED_UNREACHABLE = 4502
REFUSAL_REASONS = {
    REFUSED_NO_LINE: "no such SIM line",
    REFUSED_DISABLED: "the browser softphone is turned off for this line",
    REFUSED_NOT_RUNNING: "this line is not running",
    REFUSED_UNAVAILABLE: "the line's state could not be read",
    REFUSED_UNREACHABLE: "this line is not answering",
}


async def refuse(browser: WebSocket, code: int) -> None:
    """Accept on the sip subprotocol, then close with an explicit code and reason."""
    await browser.accept(subprotocol=SUBPROTOCOL)
    await browser.close(code=code, reason=REFUSAL_REASONS[code])


def path(iid: str) -> str:
    return f"/api/instances/{quote(str(iid), safe='')}/softphone/ws"


def engine_url(ip: str) -> str:
    return f"ws://{ip}:{ENGINE_WS_PORT}/ws"


def offers_sip(header: str | None) -> bool:
    return SUBPROTOCOL in {p.strip().lower() for p in (header or "").split(",")}


async def relay(browser: WebSocket, url: str) -> None:
    """Open the engine side first, then accept the browser, then pump both ways until either
    side closes. A line whose Asterisk is down is refused with REFUSED_UNREACHABLE rather than
    relayed to a socket that can never register."""
    try:
        engine = await connect(url, subprotocols=[SUBPROTOCOL], open_timeout=OPEN_TIMEOUT,
                               max_size=MAX_MESSAGE, compression=None, proxy=None)
    except Exception as e:  # noqa: BLE001 - any failure means the same thing to the browser
        log.info("softphone relay: engine %s unreachable: %s", url, e)
        await refuse(browser, REFUSED_UNREACHABLE)
        return

    try:
        await browser.accept(subprotocol=SUBPROTOCOL)
    except Exception:
        # The engine connection is already open at this point. Do not leave an Asterisk
        # WebSocket (and eventually a registration) behind when the browser handshake goes
        # away before Starlette can accept it.
        await engine.close()
        raise

    async def browser_to_engine():
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("text")
            if data is None:
                data = message.get("bytes")
            if data is not None:
                await engine.send(data)

    async def engine_to_browser():
        async for data in engine:
            if isinstance(data, str):
                await browser.send_text(data)
            else:
                await browser.send_bytes(data)

    tasks = [asyncio.create_task(browser_to_engine()), asyncio.create_task(engine_to_browser())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.debug("softphone relay %s ended: %r", url, task.exception())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.close()
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - already closed by the browser
            pass
