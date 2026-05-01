from __future__ import annotations

from app.cli import build_parser


def test_cli_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.host is None
    assert args.port is None
    assert args.reload is False
