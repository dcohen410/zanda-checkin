"""Run the check-in server:  python -m zanda_checkin [--port 8080] [--host 0.0.0.0]"""
from __future__ import annotations

import argparse
import logging
import os
import socket

from waitress import serve

from . import create_app
from .config import Config, data_dir


def lan_address() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    parser = argparse.ArgumentParser(description="Zanda Checkin kiosk server")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.environ.get("ZANDA_CHECKIN_DEMO") == "1":
        from .demo import DemoZandaClient
        app = create_app(Config(data_dir().with_name(data_dir().name + "-demo") / "config.json"),
                         client_factory=DemoZandaClient)
        mode = "DEMO MODE (pretend data, any API key works)"
    else:
        app = create_app()
        mode = "Connected to Zanda"
    print(f"\n  Zanda Checkin is running. {mode}\n  On the iPad, open:  http://{lan_address()}:{args.port}/\n")
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
