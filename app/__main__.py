from __future__ import annotations

import os

from app import create_app


def local_port() -> int:
    raw = os.environ.get("POC_PORT", "5000")
    if not raw.isascii() or not raw.isdecimal():
        raise RuntimeError("local_port_invalid")
    port = int(raw)
    if not 1024 <= port <= 65535:
        raise RuntimeError("local_port_invalid")
    return port


def main() -> None:
    application = create_app()
    application.run(host="127.0.0.1", port=local_port(), debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
