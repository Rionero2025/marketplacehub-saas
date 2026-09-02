from __future__ import annotations

import argparse
import time

from services.db import init_db
from services.background_jobs import run_next_job


def main() -> int:
    parser = argparse.ArgumentParser(description="Marketplace Hub persistent background worker")
    parser.add_argument("--once", action="store_true", help="process one job and exit")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between empty polls")
    parser.add_argument("--kind-prefix", default="", help="optional job-kind prefix")
    args = parser.parse_args()
    init_db()
    while True:
        worked = run_next_job(kind_prefix=args.kind_prefix)
        if args.once:
            return 0
        if not worked:
            time.sleep(max(0.2, float(args.poll)))


if __name__ == "__main__":
    raise SystemExit(main())
