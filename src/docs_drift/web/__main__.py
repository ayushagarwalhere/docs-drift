"""Run one web process: the queue and rate limiter deliberately share its memory."""

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(
        "docs_drift.web.app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        workers=1,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
