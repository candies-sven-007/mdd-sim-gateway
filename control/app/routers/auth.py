"""/api/auth/*: first-admin setup, login, logout and password change."""
from __future__ import annotations

from fastapi import HTTPException, Request, APIRouter
from fastapi.responses import JSONResponse

from .. import auth, gate

router = APIRouter()


@router.get("/api/auth/status")
def api_auth_status(request: Request):
    who = gate.current(request)
    body = {"configured": auth.configured(), "authenticated": who.kind == "admin",
            "username": auth.username(), "csrf": who.csrf, "api": 1, "features": []}
    return body


@router.post("/api/auth/setup")
def api_auth_setup(body: dict, request: Request):
    if auth.configured():
        raise HTTPException(409, "administrator account is already configured")
    try:
        auth.setup(str(body.get("password") or ""), str(body.get("username") or "admin"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Setting up again follows `install.sh reset-admin` -- typically because the password was
    # lost -- so nothing signed in with the old account survives it.
    gate.revoke_kind("session")
    remember = bool(body.get("remember"))
    result = auth.login(str(body.get("username") or "admin"), str(body.get("password") or ""),
                        request.client.host if request.client else "", remember=remember)
    token, csrf = result
    response = JSONResponse({"ok": True, "authenticated": True, "csrf": csrf})
    response.set_cookie(auth.SESSION_COOKIE, token,
                        max_age=auth.SESSION_TTL_REMEMBER if remember else auth.SESSION_TTL,
                        httponly=True, secure=True, samesite="strict", path="/")
    return response


@router.post("/api/auth/login")
def api_auth_login(body: dict, request: Request):
    if not auth.configured():
        raise HTTPException(409, "administrator setup is required")
    peer = request.client.host if request.client else ""
    retry = auth.throttled(peer)
    if retry:
        return JSONResponse({"detail": "too many attempts", "retry_after": retry},
                            status_code=429, headers={"Retry-After": str(retry)})
    remember = bool(body.get("remember"))
    result = auth.login(str(body.get("username") or "admin"), str(body.get("password") or ""),
                        peer, remember=remember)
    if not result:
        raise HTTPException(401, "invalid username or password")
    token, csrf = result
    response = JSONResponse({"ok": True, "authenticated": True, "csrf": csrf})
    # The cookie has to outlive the session it names, or the browser discards a credential the
    # gateway would still have honoured.
    response.set_cookie(auth.SESSION_COOKIE, token,
                        max_age=auth.SESSION_TTL_REMEMBER if remember else auth.SESSION_TTL,
                        httponly=True, secure=True, samesite="strict", path="/")
    return response


@router.post("/api/auth/logout")
def api_auth_logout(request: Request):
    auth.logout(request.cookies.get(auth.SESSION_COOKIE))
    gate.revoke(gate.current(request).credential)
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@router.post("/api/auth/password")
def api_auth_password(body: dict, request: Request):
    try:
        auth.change_password(str(body.get("current_password") or ""),
                             str(body.get("new_password") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # A new password ends every sign-in made with the old one.
    gate.revoke_kind("session")
    response = JSONResponse({"ok": True, "reauthenticate": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response
