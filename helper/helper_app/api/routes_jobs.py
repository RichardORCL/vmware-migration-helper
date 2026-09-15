"""Migration jobs: create, list, monitor, cancel, change Windows licensing."""

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
from helper_app.models import CreateJobRequest, DiskState, Job, JobPhase, LicenseUpdateRequest
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
    helper_ad = st.clients.identity_info.availability_domain
    if body.target.availability_domain != helper_ad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"the availability domain must be the helper's ({helper_ad})")
    active = st.store.active_for_vm(body.vm_moid)
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job {active.id} for this VM is still {active.phase.value}")
    now = utcnow()
    job = Job(
        id=uuid.uuid4().hex,
        vm=inspection.vm,
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


@router.post("/{job_id}/licensing")
async def update_license(job_id: str, body: LicenseUpdateRequest, request: Request):
    st = request.app.state
    job = _get_job(request, job_id)
    if not job.instance_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "no OCI instance associated with this job yet")
    if not job.vm.is_windows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "license type applies to Windows instances only")
    try:
        inst = await asyncio.to_thread(st.provisioner.update_windows_license, job.instance_id, body.license_type)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"UpdateInstance failed: {exc}")
    job.target.windows_license_type = body.license_type
    job.message = f"Windows license type set to {body.license_type.value}"
    st.store.put(job)
    configs = [
        {"type": getattr(c, "type", None), "license_type": getattr(c, "license_type", None)}
        for c in (getattr(inst, "licensing_configs", None) or [])
    ]
    return {"instance_id": job.instance_id, "licensing_configs": configs}
