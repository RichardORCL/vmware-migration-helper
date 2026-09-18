"""OCI inventory for the target form and seed image maintenance."""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from helper_app.auth import require_session
from helper_app.models import OciBucket, OciObject, OciOptions, OsCatalogEntry, PrivateIpCheck
from helper_app.oci.clients import describe_error
from helper_app.oci.options import (
    PrivateIpError,
    build_options,
    check_private_ip,
    list_buckets,
    list_iso_objects,
    os_catalog,
)

router = APIRouter(prefix="/api", tags=["oci"], dependencies=[Depends(require_session)])


@router.get("/oci/buckets", response_model=list[OciBucket])
async def oci_buckets(request: Request, compartment_id: Optional[str] = None):
    """Object Storage buckets of a compartment (the migration tool VM's when omitted) for the ISO picker."""
    st = request.app.state
    comp = compartment_id or st.clients.identity_info.compartment_id
    try:
        return await asyncio.to_thread(list_buckets, st.clients, comp)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"cannot list buckets: {describe_error(exc)}")


@router.get("/oci/objects", response_model=list[OciObject])
async def oci_objects(request: Request, bucket: str, prefix: Optional[str] = None):
    """``.iso`` objects in a bucket."""
    st = request.app.state
    try:
        return await asyncio.to_thread(list_iso_objects, st.clients, bucket, prefix)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"cannot list objects of bucket {bucket}: {describe_error(exc)}")


@router.get("/oci/os-catalog", response_model=list[OsCatalogEntry])
def oci_os_catalog():
    """Operating systems and releases OCI accepts as custom image metadata (ISO form)."""
    return os_catalog()


@router.get("/oci/options", response_model=OciOptions)
async def oci_options(
    request: Request,
    compartment_id: Optional[str] = None,
    network_compartment_id: Optional[str] = None,
):
    st = request.app.state
    try:
        return await asyncio.to_thread(
            build_options, st.clients, st.settings, compartment_id, network_compartment_id
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"OCI inventory failed: {exc}")


@router.get("/oci/private-ip-check", response_model=PrivateIpCheck)
async def private_ip_check(request: Request, subnet_id: str, ip: str):
    """Is ``ip`` usable as fixed private address in ``subnet_id`` right now?  Same verification the job
    creation performs (inside the CIDR, not OCI-reserved, not allocated), offered to the form so the
    user can check before starting the migration."""
    st = request.app.state
    try:
        ip = str(IPv4Address(ip.strip()))
    except ValueError:
        return PrivateIpCheck(ip=ip, subnet_id=subnet_id, available=False,
                              message=f"{ip!r} is not an IPv4 address such as 10.0.1.25")
    try:
        await asyncio.to_thread(check_private_ip, st.clients, subnet_id, ip)
    except PrivateIpError as exc:
        return PrivateIpCheck(ip=ip, subnet_id=subnet_id, available=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"cannot verify {ip}: {describe_error(exc)}")
    return PrivateIpCheck(ip=ip, subnet_id=subnet_id, available=True,
                          message=f"{ip} is free in this subnet (checked with OCI just now)")


@router.delete("/seed-images")
async def delete_seed_images(request: Request):
    seeds = request.app.state.provisioner.seeds
    try:
        deleted = await asyncio.to_thread(seeds.cleanup)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"seed cleanup failed: {exc}")
    return {"deleted": deleted}


@router.delete("/iso-images")
async def delete_iso_images(request: Request):
    """Delete the custom images imported from ISOs (they are kept after a job for reuse)."""
    installer = request.app.state.runner.iso
    try:
        deleted = await asyncio.to_thread(installer.cleanup_images)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"ISO image cleanup failed: {exc}")
    return {"deleted": deleted}
