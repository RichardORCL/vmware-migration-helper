"""Cookie based authentication for the web UI / API."""

from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket, status

from helper_app.sessions import UserSession


def session_token(request: Request | WebSocket) -> str | None:
    return request.cookies.get(request.app.state.settings.session_cookie_name)


def session_from_websocket(ws: WebSocket) -> UserSession | None:
    """The logged-in session behind a WebSocket handshake (browsers send the cookie with it)."""
    return ws.app.state.sessions.get(session_token(ws))


async def require_session(request: Request) -> UserSession:
    session = request.app.state.sessions.get(session_token(request))
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    return session
