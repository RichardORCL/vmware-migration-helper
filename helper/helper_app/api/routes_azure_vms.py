"""Azure inventory for the web UI (needs an Azure login on the session)."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status

from helper_app.auth import require_azure_session
from helper_app.azure.client import AzureAuthError, AzureError
from helper_app.azure.inventory import AzureVmDetails, inspect_vm, list_vm_summaries
from helper_app.azure.preflight import preflight, warnings
from helper_app.models import AzureCaptureMode, GuestOsMapping, VmInspection, VmSummary
from helper_app.oci.mapping import map_guest_os, os_version_choices
from helper_app.sessions import UserSession

router = APIRouter(prefix="/api/azure", tags=["azure"])

VM_LIST_CACHE_S = 30.0


def _list_cached(session: UserSession, refresh: bool) -> list[VmSummary]:
    cache = session.cache.get("azure_vms")
    now = time.monotonic()
    if not refresh and cache and now - cache[0] < VM_LIST_CACHE_S:
        return cache[1]
    vms = list_vm_summaries(session.azure)
    session.cache["azure_vms"] = (now, vms)
    return vms


@router.get("/vms", response_model=list[VmSummary])
async def list_vms(refresh: bool = Query(default=False), session: UserSession = Depends(require_azure_session)):
    try:
        return await asyncio.to_thread(_list_cached, session, refresh)
    except AzureAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except AzureError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Azure inventory failed: {exc}")


def inspect(session: UserSession, vm_id: str, capture_mode: AzureCaptureMode = "deallocate",
            details: AzureVmDetails | None = None) -> VmInspection:
    details = details or inspect_details(session, vm_id)
    problems = preflight(details, capture_mode)
    spec = details.spec
    os_meta = map_guest_os(spec.guest_id, spec.guest_full_name)
    return VmInspection(
        vm=spec, can_export=not problems, problems=problems, warnings=warnings(details, capture_mode),
        needs_power_off=(capture_mode == "deallocate" and spec.power_state != "poweredOff"),
        tools_running=False,
        os=GuestOsMapping(
            operating_system=os_meta.operating_system,
            operating_system_version=os_meta.operating_system_version,
            version_detected=os_meta.version_detected,
            version_choices=os_version_choices(os_meta),
        ),
    )


def inspect_details(session: UserSession, vm_id: str) -> AzureVmDetails:
    size_cache = session.cache.setdefault("azure_vm_sizes", {})
    try:
        return inspect_vm(session.azure, vm_id, size_cache)
    except AzureAuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except AzureError as exc:
        if exc.status == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Azure virtual machine not found: {exc}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.get("/vm", response_model=VmInspection)
async def inspect_vm_route(id: str = Query(description="Azure resource ID of the VM"),
                           capture_mode: AzureCaptureMode = Query(default="deallocate"),
                           session: UserSession = Depends(require_azure_session)):
    """Inspection of one VM (resource IDs contain slashes, hence a query parameter)."""
    return await asyncio.to_thread(inspect, session, id, capture_mode)
