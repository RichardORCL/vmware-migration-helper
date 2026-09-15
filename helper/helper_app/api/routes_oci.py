"""OCI inventory for the target form and seed image maintenance."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from helper_app.auth import require_session
from helper_app.models import OciOptions
from helper_app.oci.options import build_options

router = APIRouter(prefix="/api", tags=["oci"], dependencies=[Depends(require_session)])


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


@router.delete("/seed-images")
async def delete_seed_images(request: Request):
    seeds = request.app.state.provisioner.seeds
    try:
        deleted = await asyncio.to_thread(seeds.cleanup)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"seed cleanup failed: {exc}")
    return {"deleted": deleted}
