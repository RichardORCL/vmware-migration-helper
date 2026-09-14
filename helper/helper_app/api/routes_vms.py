"""vCenter inventory for the web UI."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status

from helper_app.auth import require_session
from helper_app.models import VmInspection, VmSummary
from helper_app.sessions import UserSession
from helper_app.vsphere.inventory import preflight, vm_spec_from_vm, warnings
from helper_app.vsphere.session import VCenterError

router = APIRouter(prefix="/api/vms", tags=["vms"])

VM_LIST_CACHE_S = 30.0


def _list_cached(session: UserSession, refresh: bool) -> list[VmSummary]:
    cache = session.cache.get("vms")
    now = time.monotonic()
    if not refresh and cache and now - cache[0] < VM_LIST_CACHE_S:
        return cache[1]
    vms = [vm for vm in session.vc.list_vms() if not vm.is_template]
    session.cache["vms"] = (now, vms)
    return vms


@router.get("", response_model=list[VmSummary])
async def list_vms(refresh: bool = Query(default=False), session: UserSession = Depends(require_session)):
    try:
        return await asyncio.to_thread(_list_cached, session, refresh)
    except VCenterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"vCenter inventory failed: {exc}")


def inspect(session: UserSession, moid: str) -> VmInspection:
    try:
        spec = vm_spec_from_vm(session.vc.vm(moid))
    except VCenterError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    problems = preflight(spec)
    return VmInspection(vm=spec, can_export=not problems, problems=problems, warnings=warnings(spec))


@router.get("/{moid}", response_model=VmInspection)
async def inspect_vm(moid: str, session: UserSession = Depends(require_session)):
    return await asyncio.to_thread(inspect, session, moid)
