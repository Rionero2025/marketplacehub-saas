from __future__ import annotations

import threading

from services.db import connect

_LOCK = threading.RLock()
_READY = False


def ensure_performance_indexes() -> None:
    """Create read-path indexes once per process.

    All statements are idempotent and work on both SQLite and PostgreSQL through
    the compatibility layer. They target the high-frequency SaaS list screens.
    """
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        with connect() as con:
            con.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_kaufland_buybox_account_checked
                ON kaufland_buybox_account_checks(
                    seller_id,marketplace_account_id,environment,storefront,checked_at
                );
                CREATE INDEX IF NOT EXISTS idx_kaufland_buybox_account_offer_fast
                ON kaufland_buybox_account_checks(
                    marketplace_account_id,environment,storefront,sku
                );
                CREATE INDEX IF NOT EXISTS idx_worten_buybox_checked
                ON worten_buybox_checks(
                    seller_id,marketplace_account_id,price_list_id,channel_code,checked_at
                );
                CREATE INDEX IF NOT EXISTS idx_worten_buybox_ean_fast
                ON worten_buybox_checks(
                    marketplace_account_id,price_list_id,channel_code,ean
                );
                CREATE INDEX IF NOT EXISTS idx_kaufland_live_units_present_fast
                ON kaufland_live_units(
                    seller_id,marketplace_account_id,environment,is_present,storefront,id_offer
                );
                CREATE INDEX IF NOT EXISTS idx_accounting_lines_scope_date_fast
                ON accounting_order_lines(
                    seller_id,marketplace_account_id,marketplace,order_created
                );
                """
            )
        _READY = True
