"""Login / logout with vCenter credentials."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from helper_app.auth import require_session, session_token
from helper_app.models import LoginRequest, SessionInfo
from helper_app.sessions import UserSession
from helper_app.vsphere.session import VCenterAuthError, VCenterError

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/config")
def auth_config(request: Request):
    """Unauthenticated: tells the login page which vCenter it talks to by default."""
    settings = request.app.state.settings
    return {"vcenter_host": settings.vcenter_host, "vcenter_port": settings.vcenter_port}


@router.post("/login", response_model=SessionInfo)
async def login(body: LoginRequest, request: Request, response: Response):
    st = request.app.state
    try:
        vc = await asyncio.to_thread(st.vcenter.login, body.username, body.password, body.vcenter_host,
                                     body.vcenter_port)
    except VCenterAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except VCenterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    # replace a previous session of this browser, if any
    st.sessions.logout(session_token(request))
    session = st.sessions.create(vc)
    response.set_cookie(
        st.settings.session_cookie_name,
        session.token,
        httponly=True,
        secure=st.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return session.info()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response):
    st = request.app.state
    st.sessions.logout(session_token(request))
    response.delete_cookie(st.settings.session_cookie_name, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=SessionInfo)
def me(session: UserSession = Depends(require_session)):
    return session.info()
