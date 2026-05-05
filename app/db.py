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

    if "mods_maps_drafts" in table_names:
        existing_columns = {column["name"] for column in inspector.get_columns("mods_maps_drafts")}
        if "item_metadata_json" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE mods_maps_drafts ADD COLUMN item_metadata_json TEXT DEFAULT '[]' NOT NULL")
                )

    if "mods_maps_draft_items" not in table_names:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS mods_maps_draft_items ("
                    "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                    "profile_id VARCHAR(64) NOT NULL, "
                    "mod_name VARCHAR(255) NOT NULL, "
                    "mod_id VARCHAR(255) NOT NULL, "
                    "workshop_id VARCHAR(64) NOT NULL, "
                    "is_active BOOLEAN NOT NULL DEFAULT 1, "
                    "sort_order INTEGER NOT NULL DEFAULT 0, "
                    "dependency_mod_ids TEXT NOT NULL DEFAULT '', "
                    "created_at DATETIME NOT NULL, "
                    "updated_at DATETIME NOT NULL"
                    ")"
                )
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_mods_maps_draft_items_profile_id ON mods_maps_draft_items (profile_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_mods_maps_draft_items_mod_id ON mods_maps_draft_items (mod_id)"))
    else:
        existing_columns = {column["name"] for column in inspector.get_columns("mods_maps_draft_items")}
        if "dependency_mod_ids" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE mods_maps_draft_items ADD COLUMN dependency_mod_ids TEXT DEFAULT '' NOT NULL")
                )
        if "sort_order" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE mods_maps_draft_items ADD COLUMN sort_order INTEGER DEFAULT 0 NOT NULL")
                )
        if "is_active" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE mods_maps_draft_items ADD COLUMN is_active BOOLEAN DEFAULT 1 NOT NULL")
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
