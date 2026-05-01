from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, get_settings
from app.db import create_session_factory
from app.models import ServerProfile
from app.routes import api, web
from app.services.backup_scheduler import BackupScheduler
from app.services.config_files import ProjectZomboidConfigService
from app.services.imports import LocalServerImportService
from app.services.runtime import RuntimeManager
from app.services.sandbox_files import ProjectZomboidSandboxService
from app.services.workshop_browser import WorkshopBrowserService
from app.services.zomboid import ZomboidService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def start_profiles_marked_for_host(app: FastAPI) -> None:
    with app.state.session_factory() as db:
        profiles = db.scalars(
            select(ServerProfile)
            .where(ServerProfile.start_with_host.is_(True))
            .order_by(ServerProfile.display_name)
        ).all()

    for profile in profiles:
        try:
            await app.state.runtime_manager.start_profile(profile)
        except ValueError:
            continue


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    session_factory = create_session_factory(app_settings)
    zomboid_service = ZomboidService(app_settings)
    config_service = ProjectZomboidConfigService(app_settings)
    import_service = LocalServerImportService(app_settings, config_service)
    workshop_browser_service = WorkshopBrowserService(config_service)
    sandbox_service = ProjectZomboidSandboxService(app_settings)
    runtime_manager = RuntimeManager(session_factory, zomboid_service)
    backup_scheduler = BackupScheduler(session_factory, zomboid_service, runtime_manager)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await backup_scheduler.start()
        await start_profiles_marked_for_host(_app)
        try:
            yield
        finally:
            await backup_scheduler.stop()

    app = FastAPI(title=app_settings.app_name, lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.secret_key,
        same_site="lax",
        https_only=app_settings.session_https_only,
    )

    templates = Jinja2Templates(directory=str(app_settings.templates_root))
    web.configure_templates(templates)

    app.state.settings = app_settings
    app.state.session_factory = session_factory
    app.state.runtime_manager = runtime_manager
    app.state.zomboid_service = zomboid_service
    app.state.backup_scheduler = backup_scheduler
    app.state.config_service = config_service
    app.state.import_service = import_service
    app.state.workshop_browser_service = workshop_browser_service
    app.state.sandbox_service = sandbox_service
    app.state.host_shutdown_request = None
    app.state.now = utcnow

    app.mount("/static", StaticFiles(directory=str(app_settings.static_root)), name="static")
    app.include_router(web.router)
    app.include_router(api.router)
    return app


app = create_app()
