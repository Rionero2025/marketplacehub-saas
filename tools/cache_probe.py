from __future__ import annotations

import json

from services.shared_cache import cache_get_or_set, cache_info


def main() -> int:
    value = cache_get_or_set("probe", "health", lambda: {"ok": True}, ttl_seconds=30)
    print(json.dumps({"cache": cache_info(), "probe": value}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
