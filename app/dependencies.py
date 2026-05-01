from __future__ import annotations

from collections.abc import Generator

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ROLE_WEIGHTS, HostSettings, ServerProfile, User, UserRole
from app.security import new_csrf_token


def get_db(request: Request) -> Generator[Session, None, None]:
    session_factory = request.app.state.session_factory
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.pop("user_id", None)
        return None

    return user


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = new_csrf_token()
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, submitted_token: str | None) -> None:
    expected = ensure_csrf_token(request)
    if not submitted_token or submitted_token != expected:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid CSRF token.")


def require_role(user: User | None, minimum_role: UserRole) -> None:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    actual_weight = ROLE_WEIGHTS[UserRole(user.role)]
    required_weight = ROLE_WEIGHTS[minimum_role]
    if actual_weight < required_weight:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to that action.")


def role_allows(user: User | None, minimum_role: UserRole) -> bool:
    if user is None:
        return False

    return ROLE_WEIGHTS[UserRole(user.role)] >= ROLE_WEIGHTS[minimum_role]


def get_host_settings(db: Session, request: Request) -> HostSettings:
    settings = db.get(HostSettings, 1)
    if settings is None:
        app_settings = request.app.state.settings
        settings = HostSettings(
            id=1,
            bind_host=app_settings.bind_host,
            bind_port=app_settings.bind_port,
            data_root=str(app_settings.data_root),
            logs_root=str(app_settings.logs_root),
            server_user=app_settings.default_server_user,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)

    return settings


def get_profile_or_404(db: Session, profile_id: str) -> ServerProfile:
    profile = db.get(ServerProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    return profile


def has_any_users(db: Session) -> bool:
    return db.scalar(select(User.id).limit(1)) is not None
