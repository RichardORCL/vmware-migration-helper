"""Migration jobs: create, list, monitor, cancel, resume finalize."""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from helper_app import diagnostics
from helper_app.api.routes_vms import inspect
from helper_app.auth import require_session
from helper_app.jobs.store import utcnow
from helper_app.models import (
    CreateJobRequest,
    DiskState,
    InstanceStatus,
    Job,
    JobPhase,
    WindowsLicenseType,
)
from helper_app.oci.clients import describe_error
from helper_app.oci.mapping import (
    WINDOWS_CLIENT_VERSIONS,
    is_arm_shape,
    map_guest_os,
    os_version_choices,
    with_os_version,
)
from helper_app.sessions import UserSession

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_session)])


def _get_job(request: Request, job_id: str) -> Job:
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    return job


@router.get("", response_model=list[Job])
def list_jobs(request: Request, vm_moid: Optional[str] = None):
    return request.app.state.store.list(vm_moid=vm_moid)


@router.post("", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def create_job(body: CreateJobRequest, request: Request, session: UserSession = Depends(require_session)):
    st = request.app.state
    inspection = await asyncio.to_thread(inspect, session, body.vm_moid)
    if not inspection.can_export:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(inspection.problems))
    if inspection.vm.is_windows and body.target.windows_license_type is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a Windows license type must be selected")
    os_meta = map_guest_os(inspection.vm.guest_id, inspection.vm.guest_full_name)
    if not os_meta.version_detected and not body.target.operating_system_version and os_version_choices(os_meta):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"vSphere does not report which {os_meta.operating_system} release the guest runs; select the "
            f"OS version ({', '.join(os_version_choices(os_meta))})",
        )
    os_meta = with_os_version(os_meta, body.target.operating_system_version)
    if (os_meta.operating_system_version in WINDOWS_CLIENT_VERSIONS
            and body.target.windows_license_type == WindowsLicenseType.OCI_PROVIDED):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "OCI does not provide licenses for Windows 10/11; select Bring your own license")
    helper_ad = st.clients.identity_info.availability_domain
    if body.target.availability_domain != helper_ad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"the availability domain must be the helper's ({helper_ad})")
    shape = body.target.shape or st.settings.default_shape
    if is_arm_shape(shape):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{shape} is an Ampere (ARM) shape; an x86 guest from vSphere needs an x86 shape")
    active = st.store.active_for_vm(body.vm_moid)
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job {active.id} for this VM is still {active.phase.value}")
    now = utcnow()
    info = session.info()
    job = Job(
        id=uuid.uuid4().hex,
        vm=inspection.vm,
        vcenter_host=info.vcenter_host + (f":{info.vcenter_port}" if info.vcenter_port != 443 else ""),
        target=body.target,
        phase=JobPhase.QUEUED,
        message="Queued",
        disks=[DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0))
               for d in inspection.vm.disks],
        created_by=session.username,
        created_at=now,
        updated_at=now,
    )
    st.store.put(job)
    st.runner.submit(job.id, session)
    return job


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: str, request: Request):
    return _get_job(request, job_id)


@router.get("/{job_id}/instance", response_model=InstanceStatus)
async def job_instance(job_id: str, request: Request):
    """Current OCI lifecycle state of the target instance (the job record only knows what the helper did)."""
    st = request.app.state
    job = _get_job(request, job_id)
    if not job.instance_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no OCI instance associated with this job yet")
    try:
        inst = (await asyncio.to_thread(st.clients.compute.get_instance, job.instance_id)).data
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status", None) == 404:  # terminated instances disappear from the API after a while
            return InstanceStatus(instance_id=job.instance_id, display_name=job.instance_display_name,
                                  lifecycle_state="NOT_FOUND", checked_at=utcnow())
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, describe_error(exc))
    return InstanceStatus(instance_id=job.instance_id, display_name=inst.display_name or job.instance_display_name,
                          lifecycle_state=inst.lifecycle_state, checked_at=utcnow())


@router.get("/{job_id}/diagnostics", response_class=PlainTextResponse)
async def job_diagnostics(job_id: str, request: Request):
    """Everything needed to analyse the job (record + relevant journal lines) as plain text."""
    st = request.app.state
    job = _get_job(request, job_id)
    return await asyncio.to_thread(diagnostics.collect, job, st.settings, st.clients.identity_info, st.commit,
                                   st.command_runner)


@router.post("/{job_id}/cancel", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
def cancel_job(job_id: str, request: Request):
    st = request.app.state
    job = _get_job(request, job_id)
    if job.phase in (JobPhase.COMPLETED, JobPhase.CANCELLED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is already {job.phase.value}")
    if st.runner.is_running(job.id):
        st.runner.request_cancel(job.id)
        job.message = "Cancellation requested"
        st.store.put(job)
        return job
    # not running (failed or interrupted by a restart): tear down whatever was created
    job.step = "cancel_queued"
    job.message = "Cleaning up OCI resources"
    st.store.put(job)
    st.runner.cleanup(job.id)
    return job


@router.post("/{job_id}/finalize", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
def resume_finalize(job_id: str, request: Request):
    """Retry the finalize step (attach volumes, start) of a failed job whose disks were all copied."""
    st = request.app.state
    job = _get_job(request, job_id)
    if st.runner.is_running(job.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "job is running")
    if not st.runner.can_resume_finalize(job):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "only a FAILED job with a launched instance and all disks copied can resume finalizing")
    job.step = "finalize_queued"
    job.message = "Resuming finalize"
    st.store.put(job)
    st.runner.resume_finalize(job.id)
    return job
