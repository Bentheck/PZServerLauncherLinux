from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.routes import web


@pytest.fixture
def app(tmp_path: Path):
    web.auth_limiter._events.clear()
    settings = Settings(
        environment="test",
        secret_key="test-secret-key",
        data_root=tmp_path / "data",
        logs_root=tmp_path / "logs",
        session_https_only=False,
        steamcmd_path="/usr/games/steamcmd",
    )
    settings.ensure_directories()
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)


def extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]
