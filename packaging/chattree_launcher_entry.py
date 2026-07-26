from __future__ import annotations

from client_launcher.__main__ import main as launcher_main


def main() -> int:
    return int(launcher_main())


if __name__ == "__main__":
    raise SystemExit(main())
