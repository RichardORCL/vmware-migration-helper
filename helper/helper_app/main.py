"""FastAPI application factory and entry point for the vCenter to OCI helper.

Serves the web UI (``/ui``), the REST API (``/api``) and runs the migrations.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from helper_app import __version__, logging_config
from helper_app.api import routes_auth, routes_jobs, routes_oci, routes_setup, routes_vms
from helper_app.config import Settings, get_settings
from helper_app.jobs.runner import MigrationRunner
from helper_app.jobs.store import JobStore
from helper_app.oci.clients import OciClients, build_clients
from helper_app.oci.provision import Provisioner
from helper_app.sessions import SessionStore
from helper_app.updater import Runner, Updater, _default_runner
from helper_app.vsphere.session import VCenterConnector

log = logging.getLogger(__name__)


def create_app(
    settings: Optional[Settings] = None,
    clients: Optional[OciClients] = None,
    store: Optional[JobStore] = None,
    vcenter: Optional[VCenterConnector] = None,
    export_factory=None,
    updater: Optional[Updater] = None,
    command_runner: Runner = _default_runner,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging_config.configure_stdout()
        logging_config.load_overrides(settings)  # Setup page changes from a previous run
        logging_config.apply(settings)
        app.state.settings = settings
        app.state.store = store or JobStore(settings.db_path)
        app.state.clients = clients or build_clients(settings)
        logging_config.apply(settings)  # the SDK clients exist now; enable their request loggers if asked
        app.state.vcenter = vcenter or VCenterConnector(settings)
        app.state.sessions = SessionStore(settings.session_ttl_s)
        app.state.command_runner = command_runner  # runs git/systemctl/journalctl (injectable for tests)
        app.state.updater = updater or Updater(settings, runner=command_runner)
        app.state.commit = app.state.updater.local_state().get("commit", "")
        app.state.provisioner = Provisioner(app.state.clients, settings, app.state.store.put)
        app.state.runner = MigrationRunner(settings, app.state.store, app.state.provisioner,
                                           export_factory=export_factory)
        app.state.runner.fail_stale_jobs()
        ident = app.state.clients.identity_info
        log.info("helper %s ready in %s / %s; vCenter %s", ident.instance_id, ident.region,
                 ident.availability_domain, settings.vcenter_host or "(not configured)")
        try:
            yield
        finally:
            app.state.runner.shutdown()
            app.state.sessions.close_all()
            app.state.store.close()

    app = FastAPI(title="vCenter to OCI helper", version=__version__, lifespan=lifespan)
    app.include_router(routes_auth.router)
    app.include_router(routes_vms.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_oci.router)
    app.include_router(routes_setup.router)

    @app.get("/api/health")
    def health():
        ident = app.state.clients.identity_info
        return {"status": "ok", "version": __version__, "commit": app.state.commit, "instance_id": ident.instance_id,
                "availability_domain": ident.availability_domain, "region": ident.region,
                "vcenter_host": settings.vcenter_host}

    @app.get("/")
    def root():
        return RedirectResponse("/ui/")

    ui_dir = Path(settings.ui_dir)
    if ui_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        ssl_certfile=settings.tls_cert_file,
        ssl_keyfile=settings.tls_key_file,
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    run()
