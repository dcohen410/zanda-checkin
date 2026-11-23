"""Run the service:  python -m zanda_checkin [--host 0.0.0.0] [--port 8000]

Runs a single process on purpose: live notifications are broadcast in memory.
Behind a reverse proxy, set FORWARDED_ALLOW_IPS to the proxy's address so client IPs
(used for sign-in rate limits) are read correctly.
"""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Zanda Checkin service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("zanda_checkin.app:create_app", factory=True, host=args.host, port=args.port,
                workers=1, proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"), access_log=False)


if __name__ == "__main__":
    main()
