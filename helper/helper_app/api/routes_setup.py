"""Setup page: helper information and self-update."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from helper_app import __version__, logging_config
from helper_app.auth import require_session
from helper_app.logging_config import LoggingSettings, LoggingStatus
from helper_app.updater import SoftwareStatus, UpdateError

router = APIRouter(prefix="/api/setup", tags=["setup"], dependencies=[Depends(require_session)])


class UpdateRequest(BaseModel):
    force: bool = False  # update even though migrations are running (they will fail)


def _active_jobs(request: Request) -> int:
    return len(request.app.state.store.active())


@router.get("/info")
def setup_info(request: Request):
    st = request.app.state
    ident = st.clients.identity_info
    s = st.settings
    return {
        "version": __version__,
        "commit": st.commit,
        "instance_id": ident.instance_id,
        "compartment_id": ident.compartment_id,
        "region": ident.region,
        "availability_domain": ident.availability_domain,
        "tenancy_id": ident.tenancy_id,
        "default_vcenter": f"{s.vcenter_host}:{s.vcenter_port}" if s.vcenter_host else "",
        "vcenter_verify_ssl": s.vcenter_verify_ssl,
        "seed_bucket": s.seed_bucket,
        "default_shape": s.default_shape,
        "max_concurrent_jobs": s.max_concurrent_jobs,
        "session_ttl_s": s.session_ttl_s,
        "sessions": len(st.sessions),
        "active_jobs": _active_jobs(request),
    }


@router.get("/logging", response_model=LoggingStatus)
def get_logging(request: Request):
    return logging_config.current(request.app.state.settings)


@router.put("/logging", response_model=LoggingStatus)
def put_logging(body: LoggingSettings, request: Request):
    return logging_config.update(request.app.state.settings, body)


@router.get("/software", response_model=SoftwareStatus)
async def software_status(request: Request, check: bool = True):
    updater = request.app.state.updater
    return await asyncio.to_thread(updater.status, _active_jobs(request), check)


@router.post("/software/update", response_model=SoftwareStatus, status_code=status.HTTP_202_ACCEPTED)
async def software_update(request: Request, body: UpdateRequest = UpdateRequest()):
    updater = request.app.state.updater
    try:
        await asyncio.to_thread(updater.start, _active_jobs(request), body.force)
    except UpdateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return await asyncio.to_thread(updater.status, _active_jobs(request), False)
