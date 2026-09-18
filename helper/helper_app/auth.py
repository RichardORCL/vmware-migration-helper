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
    """Any UI session: a vCenter login or the anonymous session of the ISO flow."""
    session = request.app.state.sessions.get(session_token(request))
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    return session


async def require_vcenter_session(request: Request) -> UserSession:
    """A session with a vCenter connection behind it (VM inventory, VMware migrations)."""
    session = await require_session(request)
    if session.vc is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this function needs a vCenter login")
    return session


async def require_azure_session(request: Request) -> UserSession:
    """A session with an Azure service principal behind it (Azure VM inventory and migrations)."""
    session = await require_session(request)
    if session.azure is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this function needs an Azure login")
    return session
