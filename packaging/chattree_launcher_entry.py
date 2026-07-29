from __future__ import annotations

import sys


def main() -> int:
    if sys.argv[1:2] == ["server"]:
        del sys.argv[1]
        from backend.server_cli import main as server_main

        return int(server_main())

    from client_launcher.__main__ import main as launcher_main

    launcher_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
