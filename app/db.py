from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


def _ensure_schema_compatibility(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    if "server_profiles" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("server_profiles")}
        if "start_with_host" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE server_profiles ADD COLUMN start_with_host BOOLEAN DEFAULT 0 NOT NULL")
                )

    if "host_settings" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("host_settings")}
        if "steam_web_api_key" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE host_settings ADD COLUMN steam_web_api_key VARCHAR(255)")
                )


def create_session_factory(settings: Settings) -> sessionmaker:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    _ensure_schema_compatibility(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
