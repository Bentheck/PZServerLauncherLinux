from __future__ import annotations

import argparse

import uvicorn

from app.config import get_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pzserverlauncherlinux",
        description="Run the PZServerLauncherLinux web application.",
    )
    parser.add_argument("--host", default=None, help="Bind host override.")
    parser.add_argument("--port", type=int, default=None, help="Bind port override.")
    parser.add_argument("--reload", action="store_true", help="Enable Uvicorn reload mode.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=args.host or settings.bind_host,
        port=args.port or settings.bind_port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
