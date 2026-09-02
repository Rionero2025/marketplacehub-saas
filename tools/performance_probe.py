from __future__ import annotations

import statistics
import time

from services.db import init_db, sellers
from services.dashboard import dashboard_snapshot


def timed(label, fn, runs=3):
    values=[]
    result=None
    for _ in range(runs):
        start=time.perf_counter()
        result=fn()
        values.append((time.perf_counter()-start)*1000)
    print(f"{label}: best={min(values):.1f}ms median={statistics.median(values):.1f}ms runs={runs}")
    return result


def main():
    init_db()
    seller_rows=timed("sellers", sellers)
    snap=timed("dashboard_snapshot", dashboard_snapshot)
    print(f"seller={len(seller_rows or [])} rows_loaded={int((snap or {}).get('rows_loaded') or 0)}")


if __name__ == "__main__":
    main()
