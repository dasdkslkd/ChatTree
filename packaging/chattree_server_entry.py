from __future__ import annotations

from backend.server_cli import main as server_main


def main() -> int:
    return int(server_main())


if __name__ == "__main__":
    raise SystemExit(main())
