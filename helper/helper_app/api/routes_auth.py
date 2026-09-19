"""Login / logout with vCenter credentials or an Azure service principal."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from helper_app.auth import require_session, session_token
from helper_app.azure.client import AzureAuthError, AzureError
from helper_app.gcp.client import GcpAuthError, GcpError
from helper_app.models import AzureLoginRequest, GcpLoginRequest, LoginRequest, SessionInfo
from helper_app.sessions import UserSession
from helper_app.vsphere.session import VCenterAuthError, VCenterError

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/config")
def auth_config(request: Request):
    """Unauthenticated: the defaults for the login page (vCenter, if one is configured, and TLS verification)."""
    settings = request.app.state.settings
    return {"vcenter_host": settings.vcenter_host, "vcenter_port": settings.vcenter_port,
            "verify_ssl": settings.vcenter_verify_ssl}


@router.post("/login", response_model=SessionInfo)
async def login(body: LoginRequest, request: Request, response: Response):
    st = request.app.state
    try:
        vc = await asyncio.to_thread(st.vcenter.login, body.username, body.password, body.vcenter_host,
                                     body.vcenter_port, body.verify_ssl)
    except VCenterAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except VCenterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    # replace a previous session of this browser, if any
    st.sessions.logout(session_token(request))
    session = st.sessions.create(vc)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/azure/login", response_model=SessionInfo)
async def azure_login(body: AzureLoginRequest, request: Request, response: Response):
    """Log in with an Azure service principal (tenant, application/client ID, client secret).  The
    credentials stay in memory with the session, like a vCenter login."""
    st = request.app.state
    try:
        az = await asyncio.to_thread(st.azure.login, body.tenant_id, body.client_id, body.client_secret)
    except AzureAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except AzureError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    st.sessions.logout(session_token(request))
    session = st.sessions.create(None, azure=az)
    _set_cookie(st, response, session.token)
    return session.info()


@router.post("/gcp/login", response_model=SessionInfo)
async def gcp_login(body: GcpLoginRequest, request: Request, response: Response):
    """Log in with a GCP service account JSON key and export bucket."""
    st = request.app.state
    try:
        gcp = await asyncio.to_thread(st.gcp.login, body.service_account_json, body.export_bucket)
    except GcpAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    except GcpError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    st.sessions.logout(session_token(request))
    session = st.sessions.create(None, gcp=gcp)
    _set_cookie(st, response, session.token)
    return session.info()


def _set_cookie(st, response: Response, token: str) -> None:
    response.set_cookie(
        st.settings.session_cookie_name,
        token,
        httponly=True,
        secure=st.settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/anonymous", response_model=SessionInfo)
def anonymous(request: Request, response: Response):
    """Session for the ISO flow, which needs no vCenter.  A vCenter login already in this browser is kept
    (it can do everything the anonymous session can); otherwise an anonymous session is created."""
    st = request.app.state
    existing = st.sessions.get(session_token(request))
    if existing is not None:
        return existing.info()
    session = st.sessions.create(None)
    _set_cookie(st, response, session.token)
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
