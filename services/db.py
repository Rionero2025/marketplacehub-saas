from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from services.database_config import database_engine, load_database_config, database_config_public
from services import postgresql_backend
from services.shared_cache import cache_get_or_set

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "marketplace_hub.db"
PENDING_DELETIONS_PATH = DATA_DIR / "pending_deletions.json"

_STORAGE_REPAIR_LOCK = threading.RLock()
_LAST_STORAGE_REPAIR = 0.0

# v251: schema/bootstrap initialization is process-scoped per active database.
# Streamlit reruns the page frequently; repeating CREATE/ALTER statements while
# a long catalogue transaction is writing SQLite can raise "database is locked".
# Keep the initial migration idempotent, but run it only once per database target
# in the current Python process.
_DB_INIT_LOCK = threading.RLock()
_DB_INITIALIZED_KEYS: set[tuple] = set()


def _is_readonly_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "readonly database",
            "read-only database",
            "attempt to write a readonly database",
            "attempt to write a read-only database",
        )
    )


def _make_path_writable(path: Path) -> None:
    """Best-effort removal of read-only attributes without deleting data."""
    if not path.exists():
        return
    try:
        current_mode = path.stat().st_mode
        wanted = stat.S_IWUSR | stat.S_IRUSR
        if path.is_dir():
            wanted |= stat.S_IXUSR
        os.chmod(path, current_mode | wanted)
    except OSError:
        pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["attrib", "-R", str(path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            pass


def repair_database_permissions(*, force: bool = True) -> dict:
    """Repair SQLite file permissions; PostgreSQL requires no local DB repair."""
    if database_engine() == "postgresql":
        return database_storage_status()
    global _LAST_STORAGE_REPAIR
    with _STORAGE_REPAIR_LOCK:
        if not force and time.monotonic() - _LAST_STORAGE_REPAIR < 30:
            return database_storage_status()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _make_path_writable(DATA_DIR)
        for path in (
            DB_PATH,
            Path(f"{DB_PATH}-wal"),
            Path(f"{DB_PATH}-shm"),
            Path(f"{DB_PATH}-journal"),
        ):
            _make_path_writable(path)
        # A copied ZIP/folder can leave nested data files with the Windows
        # read-only attribute. Clearing it recursively prevents later failures
        # when SQLite checkpoints the WAL or when a list/view is updated.
        if os.name == "nt":
            try:
                subprocess.run(
                    ["attrib", "-R", str(DATA_DIR / "*"), "/S", "/D"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                pass
        _LAST_STORAGE_REPAIR = time.monotonic()
        return database_storage_status()


def database_storage_status() -> dict:
    """Return diagnostics for the active database backend."""
    if database_engine() == "postgresql":
        public = database_config_public()
        try:
            connection_info = postgresql_backend.test_connection(load_database_config())
            return {
                "engine": "postgresql",
                "database_path": "",
                "data_dir": str(DATA_DIR),
                "directory_writable": True,
                "database_writable": True,
                "ok": True,
                "host": public["postgresql_host"],
                "port": public["postgresql_port"],
                "database": connection_info.get("database") or public["postgresql_database"],
                "user": connection_info.get("username") or public["postgresql_user"],
                "server_version": connection_info.get("version") or "",
                "pool": postgresql_backend.pool_stats(),
            }
        except Exception as error:
            return {
                "engine": "postgresql",
                "database_path": "",
                "data_dir": str(DATA_DIR),
                "directory_writable": True,
                "database_writable": False,
                "ok": False,
                "host": public["postgresql_host"],
                "port": public["postgresql_port"],
                "database": public["postgresql_database"],
                "user": public["postgresql_user"],
                "server_version": "",
                "pool": {},
                "error": str(error),
            }

    directory_writable = False
    database_writable = not DB_PATH.exists()
    probe = DATA_DIR / f".write_probe_{os.getpid()}_{threading.get_ident()}"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        directory_writable = True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    if DB_PATH.exists():
        try:
            with DB_PATH.open("ab"):
                pass
            database_writable = True
        except OSError:
            database_writable = False
    return {
        "engine": "sqlite",
        "data_dir": str(DATA_DIR),
        "database_path": str(DB_PATH),
        "directory_writable": directory_writable,
        "database_writable": database_writable,
        "ok": bool(directory_writable and database_writable),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    if database_engine() == "postgresql":
        with postgresql_backend.connect() as con:
            # v316: every pooled transaction receives a transaction-local tenant
            # context before any application query executes.
            from services.tenant_db import apply_postgresql_connection_context
            apply_postgresql_connection_context(con)
            yield con
        return

    repair_database_permissions(force=False)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _init_db_once() -> None:
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS sellers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            legal_name TEXT DEFAULT '',
            email TEXT DEFAULT '',
            our_profit_pct REAL NOT NULL DEFAULT 0,
            partner_profit_pct REAL NOT NULL DEFAULT 100,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS marketplace_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace TEXT NOT NULL,
            account_name TEXT NOT NULL,
            credentials_encrypted TEXT NOT NULL,
            settings_json TEXT NOT NULL DEFAULT '{}',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(seller_id, marketplace, account_name)
        );
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_seller_id INTEGER NOT NULL REFERENCES sellers(id),
            name TEXT NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(owner_seller_id, name)
        );
        CREATE TABLE IF NOT EXISTS price_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            owner_seller_id INTEGER NOT NULL REFERENCES sellers(id),
            name TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('private','shared','global')),
            source_type TEXT NOT NULL CHECK(source_type IN ('upload','url')),
            source_url TEXT DEFAULT '',
            source_credentials_encrypted TEXT DEFAULT '',
            local_path TEXT DEFAULT '',
            file_format TEXT DEFAULT '',
            storage_key TEXT NOT NULL DEFAULT '',
            storage_backend TEXT NOT NULL DEFAULT '',
            storage_sha256 TEXT NOT NULL DEFAULT '',
            storage_size_bytes INTEGER NOT NULL DEFAULT 0,
            last_download_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(owner_seller_id, name)
        );
        CREATE TABLE IF NOT EXISTS price_list_access (
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            permission TEXT NOT NULL DEFAULT 'use' CHECK(permission IN ('use','manage')),
            PRIMARY KEY(price_list_id, seller_id)
        );
        CREATE TABLE IF NOT EXISTS commercial_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            marketplace TEXT NOT NULL,
            storefront TEXT NOT NULL,
            margin_pct REAL NOT NULL DEFAULT 35,
            commission_pct REAL NOT NULL DEFAULT 15,
            minimum_margin_pct REAL NOT NULL DEFAULT 10,
            minimum_qty INTEGER NOT NULL DEFAULT 1,
            minimum_cost REAL NOT NULL DEFAULT 0,
            maximum_cost REAL NOT NULL DEFAULT 0,
            sku_prefix TEXT DEFAULT '',
            settings_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(seller_id, price_list_id, marketplace, storefront)
        );
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER REFERENCES sellers(id),
            marketplace_account_id INTEGER REFERENCES marketplace_accounts(id),
            price_list_id INTEGER REFERENCES price_lists(id),
            marketplace TEXT NOT NULL,
            storefront TEXT DEFAULT '',
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            success_rows INTEGER NOT NULL DEFAULT 0,
            failed_rows INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS saved_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            snapshot_path TEXT NOT NULL,
            snapshot_storage_key TEXT NOT NULL DEFAULT '',
            snapshot_storage_backend TEXT NOT NULL DEFAULT '',
            snapshot_sha256 TEXT NOT NULL DEFAULT '',
            snapshot_size_bytes INTEGER NOT NULL DEFAULT 0,
            filters_json TEXT NOT NULL DEFAULT '{}',
            row_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(seller_id, name)
        );
        CREATE TABLE IF NOT EXISTS saved_view_marketplaces (
            saved_view_id INTEGER NOT NULL REFERENCES saved_views(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            PRIMARY KEY(saved_view_id, marketplace_account_id)
        );
        CREATE TABLE IF NOT EXISTS buybox_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            storefront TEXT NOT NULL,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            ean TEXT NOT NULL DEFAULT '',
            sku TEXT NOT NULL,
            original_sku TEXT NOT NULL DEFAULT '',
            id_product INTEGER,
            id_unit INTEGER,
            status TEXT NOT NULL,
            our_rank INTEGER,
            winner_seller TEXT NOT NULL DEFAULT '',
            winner_price REAL,
            winner_shipping REAL,
            winner_total REAL,
            our_price REAL,
            our_shipping REAL,
            our_total REAL,
            target_price REAL,
            currency TEXT NOT NULL DEFAULT '',
            delivery_min INTEGER,
            delivery_max INTEGER,
            own_delivery_min INTEGER,
            own_delivery_max INTEGER,
            own_handling_time INTEGER,
            logistics_status TEXT NOT NULL DEFAULT '',
            offer_count INTEGER NOT NULL DEFAULT 0,
            error_type TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            purchase_cost_eur REAL,
            shipping_cost_eur REAL,
            total_cost_eur REAL,
            commission_pct REAL,
            commission_fixed_eur REAL NOT NULL DEFAULT 0,
            commission_source TEXT NOT NULL DEFAULT '',
            target_sales_price REAL,
            target_sales_price_eur REAL,
            target_source TEXT NOT NULL DEFAULT '',
            target_commission_eur REAL,
            profit_eur REAL,
            profit_pct REAL,
            profit_status TEXT NOT NULL DEFAULT '',
            minimum_price REAL,
            minimum_price_source TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL,
            UNIQUE(marketplace_account_id, price_list_id, storefront, environment, sku)
        );
        CREATE INDEX IF NOT EXISTS idx_buybox_checks_scope
        ON buybox_checks(seller_id,marketplace_account_id,price_list_id,environment,storefront);
        CREATE TABLE IF NOT EXISTS buybox_price_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            storefront TEXT NOT NULL,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            ean TEXT NOT NULL DEFAULT '',
            sku TEXT NOT NULL,
            id_unit INTEGER,
            source TEXT NOT NULL,
            previous_price REAL,
            new_price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT '',
            purchase_cost_eur REAL,
            shipping_cost_eur REAL,
            total_cost_eur REAL,
            commission_pct REAL,
            commission_fixed_eur REAL NOT NULL DEFAULT 0,
            commission_source TEXT NOT NULL DEFAULT '',
            commission_eur REAL,
            profit_eur REAL,
            profit_pct REAL,
            margin_status TEXT NOT NULL DEFAULT '',
            price_field TEXT NOT NULL DEFAULT 'listing_price',
            api_result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_buybox_price_updates_scope
        ON buybox_price_updates(
            seller_id,marketplace_account_id,price_list_id,environment,storefront,sku
        );
        CREATE TABLE IF NOT EXISTS worten_buybox_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            channel_code TEXT NOT NULL DEFAULT 'WRT_PT_ONLINE',
            ean TEXT NOT NULL DEFAULT '',
            sku TEXT NOT NULL,
            original_sku TEXT NOT NULL DEFAULT '',
            product_sku TEXT NOT NULL DEFAULT '',
            category_code TEXT NOT NULL DEFAULT '',
            category_label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            our_rank INTEGER,
            winner_shop_id TEXT NOT NULL DEFAULT '',
            winner_shop_name TEXT NOT NULL DEFAULT '',
            winner_price REAL,
            winner_shipping REAL,
            winner_total REAL,
            our_price REAL,
            our_shipping REAL,
            our_total REAL,
            currency TEXT NOT NULL DEFAULT 'EUR',
            offer_count INTEGER NOT NULL DEFAULT 0,
            competitor_visible INTEGER NOT NULL DEFAULT 0,
            purchase_cost_eur REAL,
            shipping_cost_eur REAL,
            total_cost_eur REAL,
            commission_pct REAL,
            commission_source TEXT NOT NULL DEFAULT '',
            profit_at_buybox_eur REAL,
            margin_at_buybox_pct REAL,
            economic_status TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL,
            UNIQUE(
                marketplace_account_id,price_list_id,channel_code,sku
            )
        );
        CREATE INDEX IF NOT EXISTS idx_worten_buybox_checks_scope
        ON worten_buybox_checks(
            seller_id,marketplace_account_id,price_list_id,channel_code,status
        );
        CREATE TABLE IF NOT EXISTS worten_buybox_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            channel_code TEXT NOT NULL DEFAULT 'WRT_PT_ONLINE',
            name TEXT NOT NULL DEFAULT '',
            rows_json TEXT NOT NULL DEFAULT '[]',
            row_count INTEGER NOT NULL DEFAULT 0,
            latest_checked_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_worten_buybox_views_scope
        ON worten_buybox_views(
            seller_id,marketplace_account_id,price_list_id,channel_code,created_at
        );
        CREATE TABLE IF NOT EXISTS kaufland_support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            id_ticket TEXT NOT NULL,
            ids_order_units_json TEXT NOT NULL DEFAULT '[]',
            id_buyer TEXT NOT NULL DEFAULT '',
            ts_created_iso TEXT NOT NULL DEFAULT '',
            ts_updated_iso TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            open_reason TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '',
            is_seller_responsible INTEGER NOT NULL DEFAULT 0,
            fulfillment_type TEXT NOT NULL DEFAULT '',
            order_ids_json TEXT NOT NULL DEFAULT '[]',
            storefronts_json TEXT NOT NULL DEFAULT '[]',
            buyer_label TEXT NOT NULL DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,
            last_message_at TEXT NOT NULL DEFAULT '',
            last_message_author TEXT NOT NULL DEFAULT '',
            last_message_preview TEXT NOT NULL DEFAULT '',
            is_read_local INTEGER NOT NULL DEFAULT 0,
            read_at TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            synced_at TEXT NOT NULL,
            UNIQUE(marketplace_account_id,environment,id_ticket)
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_support_ticket_scope
        ON kaufland_support_tickets(
            seller_id,marketplace_account_id,environment,status,
            is_seller_responsible,ts_updated_iso
        );
        CREATE TABLE IF NOT EXISTS kaufland_support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            id_ticket TEXT NOT NULL,
            id_ticket_message TEXT NOT NULL,
            author_type TEXT NOT NULL DEFAULT '',
            author_name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            ts_created_iso TEXT NOT NULL DEFAULT '',
            attachments_json TEXT NOT NULL DEFAULT '[]',
            raw_json TEXT NOT NULL DEFAULT '{}',
            synced_at TEXT NOT NULL,
            UNIQUE(
                marketplace_account_id,environment,id_ticket,id_ticket_message
            )
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_support_message_ticket
        ON kaufland_support_messages(
            marketplace_account_id,environment,id_ticket,ts_created_iso
        );
        CREATE TABLE IF NOT EXISTS kaufland_support_order_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            id_order_unit TEXT NOT NULL,
            id_order TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            storefront TEXT NOT NULL DEFAULT '',
            id_offer TEXT NOT NULL DEFAULT '',
            ean TEXT NOT NULL DEFAULT '',
            product_title TEXT NOT NULL DEFAULT '',
            product_url TEXT NOT NULL DEFAULT '',
            product_image TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT '',
            price REAL,
            shipping_rate REAL,
            buyer_id TEXT NOT NULL DEFAULT '',
            buyer_email TEXT NOT NULL DEFAULT '',
            buyer_pseudonym TEXT NOT NULL DEFAULT '',
            ts_created_iso TEXT NOT NULL DEFAULT '',
            ts_updated_iso TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            synced_at TEXT NOT NULL,
            UNIQUE(marketplace_account_id,environment,id_order_unit)
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_support_order
        ON kaufland_support_order_units(
            marketplace_account_id,environment,id_order,id_order_unit
        );
        CREATE TABLE IF NOT EXISTS kaufland_support_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            status TEXT NOT NULL,
            tickets_seen INTEGER NOT NULL DEFAULT 0,
            tickets_saved INTEGER NOT NULL DEFAULT 0,
            messages_saved INTEGER NOT NULL DEFAULT 0,
            order_units_saved INTEGER NOT NULL DEFAULT 0,
            errors_json TEXT NOT NULL DEFAULT '[]',
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS kaufland_support_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            id_ticket TEXT NOT NULL DEFAULT '',
            order_unit_ids_json TEXT NOT NULL DEFAULT '[]',
            action_type TEXT NOT NULL,
            status TEXT NOT NULL,
            request_summary_json TEXT NOT NULL DEFAULT '{}',
            response_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_support_actions_scope
        ON kaufland_support_actions(
            seller_id,marketplace_account_id,environment,id_ticket,created_at
        );
        CREATE TABLE IF NOT EXISTS kaufland_order_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            id_order_unit TEXT NOT NULL,
            id_order TEXT NOT NULL DEFAULT '',
            storefront TEXT NOT NULL DEFAULT '',
            country_code TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT '',
            ts_created_iso TEXT NOT NULL DEFAULT '',
            ts_updated_iso TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            cancel_reason TEXT NOT NULL DEFAULT '',
            sku TEXT NOT NULL DEFAULT '',
            ean TEXT NOT NULL DEFAULT '',
            product_name TEXT NOT NULL DEFAULT '',
            product_url TEXT NOT NULL DEFAULT '',
            product_price_local REAL,
            shipping_local REAL,
            sold_total_local REAL,
            revenue_gross_local REAL,
            revenue_net_local REAL,
            commission_local REAL,
            commission_pct REAL,
            commission_source TEXT NOT NULL DEFAULT '',
            payout_local REAL,
            received_at TEXT NOT NULL DEFAULT '',
            received_source TEXT NOT NULL DEFAULT '',
            payment_due_at TEXT NOT NULL DEFAULT '',
            payment_source TEXT NOT NULL DEFAULT '',
            vat_pct REAL,
            carrier_code TEXT NOT NULL DEFAULT '',
            tracking_numbers TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            detail_checked_at TEXT NOT NULL DEFAULT '',
            synced_at TEXT NOT NULL,
            UNIQUE(marketplace_account_id,environment,id_order_unit)
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_orders_scope
        ON kaufland_order_units(
            seller_id,marketplace_account_id,environment,ts_created_iso
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_orders_speed_v303
        ON kaufland_order_units(
            seller_id,marketplace_account_id,environment,ts_created_iso DESC,id DESC
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_orders_filters
        ON kaufland_order_units(
            marketplace_account_id,environment,storefront,status
        );
        CREATE TABLE IF NOT EXISTS kaufland_order_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            status TEXT NOT NULL,
            units_seen INTEGER NOT NULL DEFAULT 0,
            units_saved INTEGER NOT NULL DEFAULT 0,
            details_checked INTEGER NOT NULL DEFAULT 0,
            errors_json TEXT NOT NULL DEFAULT '[]',
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS kaufland_buybox_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL
                REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            storefronts TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            rows_json TEXT NOT NULL DEFAULT '[]',
            row_count INTEGER NOT NULL DEFAULT 0,
            latest_checked_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_buybox_views_scope
        ON kaufland_buybox_views(
            seller_id,marketplace_account_id,price_list_id,environment,created_at
        );
        CREATE TABLE IF NOT EXISTS kaufland_live_units (
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            storefront TEXT NOT NULL,
            id_unit INTEGER NOT NULL,
            id_offer TEXT NOT NULL DEFAULT '',
            id_product INTEGER,
            ean TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            manufacturer TEXT NOT NULL DEFAULT '',
            listing_price_cents INTEGER,
            minimum_price_cents INTEGER,
            amount INTEGER,
            status TEXT NOT NULL DEFAULT '',
            condition_code TEXT NOT NULL DEFAULT '',
            handling_time INTEGER,
            warehouse_id TEXT NOT NULL DEFAULT '',
            shipping_group_id TEXT NOT NULL DEFAULT '',
            date_lastchange_iso TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT '',
            is_present INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            removed_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(marketplace_account_id,environment,storefront,id_unit)
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_live_units_scope
        ON kaufland_live_units(
            seller_id,marketplace_account_id,environment,storefront,is_present
        );
        CREATE TABLE IF NOT EXISTS kaufland_inventory_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
            environment TEXT NOT NULL CHECK(environment IN ('test','live')),
            storefront TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'incremental',
            status TEXT NOT NULL,
            seen INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            missing INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_kaufland_inventory_syncs_scope
        ON kaufland_inventory_syncs(
            marketplace_account_id,environment,storefront,started_at
        );
        """)
        existing_saved_view_columns={
            str(item["name"]) for item in con.execute("PRAGMA table_info(saved_views)").fetchall()
        }
        saved_view_migrations={
            "snapshot_storage_key":"TEXT NOT NULL DEFAULT ''",
            "snapshot_storage_backend":"TEXT NOT NULL DEFAULT ''",
            "snapshot_sha256":"TEXT NOT NULL DEFAULT ''",
            "snapshot_size_bytes":"INTEGER NOT NULL DEFAULT 0",
        }
        for column,declaration in saved_view_migrations.items():
            if column not in existing_saved_view_columns:
                con.execute(f"ALTER TABLE saved_views ADD COLUMN {column} {declaration}")

        existing_seller_columns={
            str(item["name"]) for item in con.execute("PRAGMA table_info(sellers)").fetchall()
        }
        seller_migrations={
            "our_profit_pct":"REAL NOT NULL DEFAULT 0",
            "partner_profit_pct":"REAL NOT NULL DEFAULT 100",
        }
        for column,declaration in seller_migrations.items():
            if column not in existing_seller_columns:
                con.execute(f"ALTER TABLE sellers ADD COLUMN {column} {declaration}")

        existing_kaufland_order_columns={
            str(item["name"])
            for item in con.execute(
                "PRAGMA table_info(kaufland_order_units)"
            ).fetchall()
        }
        kaufland_order_migrations={
            "commission_pct":"REAL",
            "commission_source":"TEXT NOT NULL DEFAULT ''",
            "received_at":"TEXT NOT NULL DEFAULT ''",
            "received_source":"TEXT NOT NULL DEFAULT ''",
            "payment_due_at":"TEXT NOT NULL DEFAULT ''",
            "payment_source":"TEXT NOT NULL DEFAULT ''",
        }
        for column,declaration in kaufland_order_migrations.items():
            if column not in existing_kaufland_order_columns:
                con.execute(
                    f"ALTER TABLE kaufland_order_units "
                    f"ADD COLUMN {column} {declaration}"
                )
        existing_price_list_columns={
            str(item["name"]) for item in con.execute("PRAGMA table_info(price_lists)").fetchall()
        }
        price_list_storage_migrations={
            "storage_key":"TEXT NOT NULL DEFAULT ''",
            "storage_backend":"TEXT NOT NULL DEFAULT ''",
            "storage_sha256":"TEXT NOT NULL DEFAULT ''",
            "storage_size_bytes":"INTEGER NOT NULL DEFAULT 0",
        }
        for column,declaration in price_list_storage_migrations.items():
            if column not in existing_price_list_columns:
                con.execute(f"ALTER TABLE price_lists ADD COLUMN {column} {declaration}")

        existing_buybox_columns={
            str(item["name"]) for item in con.execute("PRAGMA table_info(buybox_checks)").fetchall()
        }
        buybox_migrations={
            "error_type":"TEXT NOT NULL DEFAULT ''",
            "id_unit":"INTEGER",
            "purchase_cost_eur":"REAL","shipping_cost_eur":"REAL","total_cost_eur":"REAL",
            "commission_pct":"REAL","commission_fixed_eur":"REAL NOT NULL DEFAULT 0",
            "commission_source":"TEXT NOT NULL DEFAULT ''","target_sales_price":"REAL",
            "target_sales_price_eur":"REAL","target_source":"TEXT NOT NULL DEFAULT ''",
            "target_commission_eur":"REAL","profit_eur":"REAL","profit_pct":"REAL",
            "profit_status":"TEXT NOT NULL DEFAULT ''",
            "minimum_price":"REAL",
            "minimum_price_source":"TEXT NOT NULL DEFAULT ''",
            "own_delivery_min":"INTEGER","own_delivery_max":"INTEGER",
            "own_handling_time":"INTEGER",
            "logistics_status":"TEXT NOT NULL DEFAULT ''",
        }
        for column,declaration in buybox_migrations.items():
            if column not in existing_buybox_columns:
                con.execute(f"ALTER TABLE buybox_checks ADD COLUMN {column} {declaration}")
        existing_price_update_columns={
            str(item["name"])
            for item in con.execute("PRAGMA table_info(buybox_price_updates)").fetchall()
        }
        price_update_migrations={
            "commission_fixed_eur":"REAL NOT NULL DEFAULT 0",
            "commission_source":"TEXT NOT NULL DEFAULT ''",
            "price_field":"TEXT NOT NULL DEFAULT 'listing_price'",
        }
        for column,declaration in price_update_migrations.items():
            if column not in existing_price_update_columns:
                con.execute(
                    f"ALTER TABLE buybox_price_updates ADD COLUMN {column} {declaration}"
                )
        existing_support_columns={
            str(item["name"])
            for item in con.execute(
                "PRAGMA table_info(kaufland_support_tickets)"
            ).fetchall()
        }
        support_migrations={
            "is_read_local":"INTEGER NOT NULL DEFAULT 0",
            "read_at":"TEXT NOT NULL DEFAULT ''",
        }
        for column,declaration in support_migrations.items():
            if column not in existing_support_columns:
                con.execute(
                    f"ALTER TABLE kaufland_support_tickets "
                    f"ADD COLUMN {column} {declaration}"
                )
        existing_worten_buybox_columns={
            str(item["name"])
            for item in con.execute(
                "PRAGMA table_info(worten_buybox_checks)"
            ).fetchall()
        }
        worten_buybox_migrations={
            "commission_source":"TEXT NOT NULL DEFAULT ''",
            "category_code":"TEXT NOT NULL DEFAULT ''",
            "category_label":"TEXT NOT NULL DEFAULT ''",
        }
        for column,declaration in worten_buybox_migrations.items():
            if column not in existing_worten_buybox_columns:
                con.execute(
                    f"ALTER TABLE worten_buybox_checks "
                    f"ADD COLUMN {column} {declaration}"
                )
    # A Windows process can briefly keep an imported XML/Excel file open.
    # Retry queued removals after the database connection and page resources
    # from the previous Streamlit run have been released.
    cleanup_pending_deletions()
    cleanup_orphan_price_list_storage()
    # Catalog Intelligence v246 is an additive schema.  Importing here keeps
    # every page and every database backend aligned without requiring a manual
    # migration step.
    from services.catalog_intelligence.schema import ensure_schema as ensure_catalog_schema

    ensure_catalog_schema()
    # v305: persistent job queue shared by Streamlit today and dedicated SaaS workers later.
    from services.background_jobs import ensure_job_schema

    ensure_job_schema()



def _database_runtime_key() -> tuple:
    """Stable identity for the active DB used by the process-level init cache."""
    engine = database_engine()
    if engine == "postgresql":
        public = database_config_public()
        return (
            "postgresql",
            str(public.get("postgresql_host") or ""),
            str(public.get("postgresql_port") or ""),
            str(public.get("postgresql_database") or ""),
            str(public.get("postgresql_user") or ""),
        )
    return ("sqlite", str(Path(DB_PATH).resolve()))


def _reset_init_cache_for_tests() -> None:
    """Clear process-scoped DB initialization state (test/migration utility)."""
    with _DB_INIT_LOCK:
        _DB_INITIALIZED_KEYS.clear()


def init_db() -> None:
    """Initialize the active database once per process/database target.

    Streamlit executes page modules again on every interaction.  Before v251
    that meant re-running the whole DDL migration chain on every rerun.  A
    catalogue normalization can legitimately hold SQLite's single writer lock,
    so a concurrent rerun could fail inside ``ensure_catalog_schema`` with
    ``sqlite3.OperationalError: database is locked``.  Once a process has
    successfully initialized a specific database, subsequent bootstraps are
    read-only no-ops.
    """
    key = _database_runtime_key()
    if key in _DB_INITIALIZED_KEYS:
        return
    with _DB_INIT_LOCK:
        if key in _DB_INITIALIZED_KEYS:
            return
        _retry_locked(_init_db_once, attempts=10, base_delay=0.15)
        _DB_INITIALIZED_KEYS.add(key)

def _postgres_retryable(error: BaseException) -> bool:
    name = error.__class__.__name__.lower()
    message = str(error).lower()
    sqlstate = str(getattr(error, "sqlstate", "") or "")
    return (
        sqlstate in {"40001", "40P01", "55P03", "08000", "08003", "08006", "08001"}
        or any(marker in name for marker in ("operationalerror", "serializationfailure", "deadlockdetected", "locknotavailable"))
        or any(marker in message for marker in ("deadlock detected", "could not obtain lock", "connection is closed"))
    )


def _retry_locked(
    operation: Callable,
    *,
    attempts: int = 6,
    base_delay: float = 0.1,
):
    """Retry transient SQLite locks or PostgreSQL transaction/connection failures."""
    readonly_repaired = False
    for attempt in range(max(1, attempts)):
        try:
            return operation()
        except Exception as error:
            if database_engine() == "postgresql":
                if not _postgres_retryable(error) or attempt + 1 >= attempts:
                    raise
                time.sleep(base_delay * (attempt + 1))
                continue

            if not isinstance(error, sqlite3.OperationalError):
                raise
            message = str(error).lower()
            if _is_readonly_error(error) and not readonly_repaired:
                repair_database_permissions(force=True)
                readonly_repaired = True
                if attempt + 1 >= attempts:
                    raise
                time.sleep(max(base_delay, 0.1))
                continue
            if "locked" not in message and "busy" not in message:
                raise
            if attempt + 1 >= attempts:
                raise
            time.sleep(base_delay * (attempt + 1))


def rows(sql: str, params=()) -> list[dict]:
    def read():
        with connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    return _retry_locked(read)


def row(sql: str, params=()) -> dict | None:
    result = rows(sql, params)
    return result[0] if result else None


def execute(sql: str, params=()) -> int:
    def write():
        with connect() as con:
            if database_engine() == "postgresql":
                if str(sql).lstrip().upper().startswith("INSERT"):
                    return postgresql_backend.insert_returning_id(con, sql, params)
                con.execute(sql, params)
                return 0
            cur = con.execute(sql, params)
            return int(cur.lastrowid or 0)

    result = _retry_locked(write)
    try:
        from services.cache_invalidation import invalidate_for_sql
        invalidate_for_sql(sql)
    except Exception:
        pass
    return result


def execute_many(sql: str, parameter_rows: Iterable[tuple]) -> int:
    values = list(parameter_rows)
    if not values:
        return 0

    def write_many():
        with connect() as con:
            cur = con.executemany(sql, values)
            return max(0, int(cur.rowcount or 0))

    result = _retry_locked(write_many)
    try:
        from services.cache_invalidation import invalidate_for_sql
        invalidate_for_sql(sql)
    except Exception:
        pass
    return result


def database_write_probe() -> bool:
    """Verify that the active database can perform a real transactional write."""
    def probe() -> bool:
        with connect() as con:
            con.execute("SAVEPOINT marketplace_hub_write_probe")
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS __marketplace_hub_write_probe "
                    "(id INTEGER PRIMARY KEY)"
                )
                con.execute(
                    "INSERT INTO __marketplace_hub_write_probe DEFAULT VALUES"
                )
                con.execute("ROLLBACK TO marketplace_hub_write_probe")
            finally:
                con.execute("RELEASE marketplace_hub_write_probe")
        return True

    return bool(_retry_locked(probe))


def _read_pending_deletions() -> list[str]:
    try:
        content=json.loads(PENDING_DELETIONS_PATH.read_text(encoding="utf-8"))
        return [str(value) for value in content if value]
    except (FileNotFoundError,TypeError,ValueError,json.JSONDecodeError):
        return []


def _write_pending_deletions(paths: list[str]) -> None:
    unique=list(dict.fromkeys(str(Path(value)) for value in paths if value))
    try:
        if not unique:
            PENDING_DELETIONS_PATH.unlink(missing_ok=True)
            return
        DATA_DIR.mkdir(parents=True,exist_ok=True)
        temporary=PENDING_DELETIONS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(unique,ensure_ascii=False),encoding="utf-8")
        temporary.replace(PENDING_DELETIONS_PATH)
    except OSError:
        # Storage cleanup must never prevent normal use of the application.
        pass


def _try_remove_path(path: Path,retries: int=4) -> bool:
    for attempt in range(max(1,retries)):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except (PermissionError,OSError):
            if attempt+1<retries:
                gc.collect()
                time.sleep(0.15*(attempt+1))
    return False


def _remove_paths(paths: list[str]) -> list[str]:
    """Remove application-owned paths and queue Windows-locked files."""
    queued=_read_pending_deletions()
    candidates=list(dict.fromkeys([str(value) for value in queued+paths if value]))
    # Files/snapshots are normally listed before their containing directory.
    remaining=[value for value in candidates if not _try_remove_path(Path(value))]
    _write_pending_deletions(remaining)
    return remaining


def cleanup_pending_deletions() -> list[str]:
    """Retry files that Windows kept open during an earlier deletion."""
    return _remove_paths([])


def pending_deletion_paths() -> list[str]:
    return _read_pending_deletions()


def cleanup_orphan_price_list_storage() -> list[str]:
    """Remove folders left by lists whose database row is already gone."""
    base=DATA_DIR/"price_lists"
    if not base.is_dir():
        return pending_deletion_paths()
    valid={str(item["id"]) for item in rows("SELECT id FROM price_lists")}
    orphan_paths=[str(child) for child in base.iterdir()
                  if child.is_dir() and child.name.isdigit() and child.name not in valid]
    return _remove_paths(orphan_paths)


def delete_marketplace_account(account_id: int, seller_id: int) -> bool:
    with connect() as con:
        found = con.execute(
            "SELECT id FROM marketplace_accounts WHERE id=? AND seller_id=?",
            (account_id, seller_id),
        ).fetchone()
        if not found:
            return False
        con.execute("DELETE FROM operations WHERE marketplace_account_id=?", (account_id,))
        con.execute("DELETE FROM marketplace_accounts WHERE id=?", (account_id,))
    return True


def delete_saved_view(view_id: int, seller_id: int) -> bool:
    snapshot = ""
    storage_key = ""
    with connect() as con:
        found = con.execute(
            "SELECT snapshot_path,snapshot_storage_key FROM saved_views WHERE id=? AND seller_id=?",
            (view_id, seller_id),
        ).fetchone()
        if not found:
            return False
        snapshot = found["snapshot_path"] or ""
        storage_key = found["snapshot_storage_key"] or ""
        con.execute("DELETE FROM saved_views WHERE id=?", (view_id,))
    _remove_paths([snapshot])
    if storage_key:
        try:
            from services.saved_view_storage import delete_saved_view_object
            delete_saved_view_object(storage_key)
        except Exception:
            pass
    return True


def delete_price_list(price_list_id: int, owner_seller_id: int) -> bool:
    paths: list[str] = []
    with connect() as con:
        found = con.execute(
            "SELECT local_path,storage_key FROM price_lists WHERE id=? AND owner_seller_id=?",
            (price_list_id, owner_seller_id),
        ).fetchone()
        if not found:
            return False
        if found["local_path"]:
            paths.append(found["local_path"])
        list_storage_key=found["storage_key"] or ""
        saved_view_rows = con.execute(
            "SELECT snapshot_path,snapshot_storage_key FROM saved_views WHERE price_list_id=?",
            (price_list_id,),
        ).fetchall()
        paths.extend(r["snapshot_path"] for r in saved_view_rows if r["snapshot_path"])
        storage_keys=[r["snapshot_storage_key"] for r in saved_view_rows if r["snapshot_storage_key"]]
        con.execute("DELETE FROM operations WHERE price_list_id=?", (price_list_id,))
        con.execute("DELETE FROM price_lists WHERE id=?", (price_list_id,))
    # The importer stores every feed under a directory named with the list ID.
    paths.append(str(DATA_DIR / "price_lists" / str(price_list_id)))
    _remove_paths(paths)
    if storage_keys:
        try:
            from services.saved_view_storage import delete_saved_view_object
            for storage_key in storage_keys:
                delete_saved_view_object(storage_key)
        except Exception:
            pass
    if list_storage_key:
        try:
            from services.durable_files import delete as delete_durable_file
            delete_durable_file(list_storage_key)
        except Exception:
            pass
    return True


def delete_supplier(supplier_id: int, owner_seller_id: int) -> bool:
    owned = rows(
        "SELECT id FROM price_lists WHERE supplier_id=? AND owner_seller_id=?",
        (supplier_id, owner_seller_id),
    )
    found = row(
        "SELECT id FROM suppliers WHERE id=? AND owner_seller_id=?",
        (supplier_id, owner_seller_id),
    )
    if not found:
        return False
    for item in owned:
        delete_price_list(item["id"], owner_seller_id)
    with connect() as con:
        con.execute(
            "DELETE FROM suppliers WHERE id=? AND owner_seller_id=?",
            (supplier_id, owner_seller_id),
        )
    return True


def delete_seller(seller_id: int) -> bool:
    found = row("SELECT id FROM sellers WHERE id=?", (seller_id,))
    if not found:
        return False
    supplier_ids = rows("SELECT id FROM suppliers WHERE owner_seller_id=?", (seller_id,))
    # Delete owned list files and all dependent records first.
    for supplier in supplier_ids:
        delete_supplier(supplier["id"], seller_id)
    orphan_lists = rows("SELECT id FROM price_lists WHERE owner_seller_id=?", (seller_id,))
    for item in orphan_lists:
        delete_price_list(item["id"], seller_id)
    with connect() as con:
        con.execute("DELETE FROM operations WHERE seller_id=?", (seller_id,))
        con.execute("DELETE FROM sellers WHERE id=?", (seller_id,))
    return True


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _authenticated_seller_scope() -> set[int] | None:
    """Return seller IDs allowed for the current Streamlit user.

    ``None`` means unrestricted (administrator, legacy session, or non-UI context).
    The helper intentionally reads only session_state to avoid an import cycle
    db -> auth -> user_access -> db.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx() is None:
            return None
        import streamlit as st
        session = st.session_state.get("_marketplace_hub_user_session")
    except Exception:
        return None
    if not isinstance(session, dict):
        return None
    if bool(session.get("is_admin")):
        return None
    values = session.get("seller_ids")
    # v291 legacy users have seller_ids=None and keep all sellers until the admin
    # saves an explicit multiple selection in Gestione Utenti.
    if values is None:
        return None
    result: set[int] = set()
    for value in values or []:
        try:
            seller_id = int(value)
        except (TypeError, ValueError):
            continue
        if seller_id > 0:
            result.add(seller_id)
    return result


def sellers(active_only=True) -> list[dict]:
    scope = _authenticated_seller_scope()
    if scope is not None and not scope:
        return []
    ordered_scope = tuple(sorted(scope)) if scope is not None else None

    def load() -> list[dict]:
        clauses: list[str] = []
        params: list[int] = []
        if active_only:
            clauses.append("active=1")
        if ordered_scope is not None:
            clauses.append(f"id IN ({','.join('?' for _ in ordered_scope)})")
            params.extend(ordered_scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return rows(f"SELECT * FROM sellers {where} ORDER BY name", tuple(params))

    cache_key = (bool(active_only), ordered_scope or "all")
    return cache_get_or_set("sellers", cache_key, load, ttl_seconds=30)


def accessible_lists(seller_id: int) -> list[dict]:
    """Tenant-aware catalogue access with a legacy-compatible public signature."""
    seller_id = int(seller_id)

    def load() -> list[dict]:
        from services.catalog_sharing import accessible_price_lists
        return accessible_price_lists(seller_id)

    # Include tenant identity in the cache key because the same seller catalogue
    # helper can be called by API workers running under different tenant scopes.
    try:
        from services.catalog_sharing import tenant_for_seller
        tenant_id = tenant_for_seller(seller_id)
    except Exception:
        tenant_id = 0
    return cache_get_or_set("accessible_lists", (tenant_id, seller_id), load, ttl_seconds=30)
