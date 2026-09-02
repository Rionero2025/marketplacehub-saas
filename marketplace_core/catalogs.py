from __future__ import annotations

import csv
import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from marketplace_core.contracts import JobRequest


_CACHE_LOCK = threading.RLock()
_FRAME_CACHE: "OrderedDict[tuple[int, str], pd.DataFrame]" = OrderedDict()
_FRAME_CACHE_MAX_ENTRIES = 2


@dataclass(frozen=True, slots=True)
class CatalogStatus:
    price_list_id: int
    ready: bool
    row_count: int
    source_fingerprint: str
    materialized_at: str
    source_path: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class CatalogPage:
    rows: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


class CatalogCore:
    """Normalized product-catalog boundary, independent from Streamlit.

    A source file is normalized once by a worker and materialized in PostgreSQL/SQLite.
    Subsequent previews and SaaS queries read only the requested rows instead of reparsing
    XML/Excel/CSV on every UI rerun.
    """

    def ensure_schema(self) -> None:
        from services.db import connect
        with connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_materializations (
                    price_list_id INTEGER PRIMARY KEY,
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ready',
                    message TEXT NOT NULL DEFAULT '',
                    materialized_at TEXT NOT NULL DEFAULT '',
                    schema_version INTEGER NOT NULL DEFAULT 2
                );
                CREATE TABLE IF NOT EXISTS catalog_products (
                    price_list_id INTEGER NOT NULL,
                    row_no INTEGER NOT NULL,
                    ean TEXT NOT NULL DEFAULT '',
                    sku TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    cost REAL NOT NULL DEFAULT 0,
                    shipping_cost REAL NOT NULL DEFAULT 0,
                    cost_pt REAL NOT NULL DEFAULT 0,
                    cost_it REAL NOT NULL DEFAULT 0,
                    cost_fr REAL NOT NULL DEFAULT 0,
                    cost_de REAL NOT NULL DEFAULT 0,
                    cost_zone3 REAL NOT NULL DEFAULT 0,
                    cost_zone4 REAL NOT NULL DEFAULT 0,
                    cost_zone5 REAL NOT NULL DEFAULT 0,
                    cost_zone6 REAL NOT NULL DEFAULT 0,
                    quantity REAL NOT NULL DEFAULT 0,
                    weight_kg REAL NOT NULL DEFAULT 0,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(price_list_id,row_no)
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_products_ean
                ON catalog_products(price_list_id,ean);
                CREATE INDEX IF NOT EXISTS idx_catalog_products_sku
                ON catalog_products(price_list_id,sku);
                CREATE INDEX IF NOT EXISTS idx_catalog_products_cost
                ON catalog_products(price_list_id,cost);
                CREATE INDEX IF NOT EXISTS idx_catalog_products_qty
                ON catalog_products(price_list_id,quantity);
                CREATE INDEX IF NOT EXISTS idx_catalog_products_weight
                ON catalog_products(price_list_id,weight_kg);
                """
            )
            mat_columns={str(item['name']) for item in con.execute('PRAGMA table_info(catalog_materializations)').fetchall()}
            if 'schema_version' not in mat_columns:
                con.execute("ALTER TABLE catalog_materializations ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
            existing={str(item['name']) for item in con.execute('PRAGMA table_info(catalog_products)').fetchall()}
            migrations={
                'shipping_cost':'REAL NOT NULL DEFAULT 0',
                'cost_pt':'REAL NOT NULL DEFAULT 0','cost_it':'REAL NOT NULL DEFAULT 0',
                'cost_fr':'REAL NOT NULL DEFAULT 0','cost_de':'REAL NOT NULL DEFAULT 0',
                'cost_zone3':'REAL NOT NULL DEFAULT 0','cost_zone4':'REAL NOT NULL DEFAULT 0',
                'cost_zone5':'REAL NOT NULL DEFAULT 0','cost_zone6':'REAL NOT NULL DEFAULT 0',
            }
            for column,declaration in migrations.items():
                if column not in existing:
                    con.execute(f'ALTER TABLE catalog_products ADD COLUMN {column} {declaration}')

    @staticmethod
    def source_fingerprint(path: str | Path) -> str:
        source = Path(path)
        stat = source.stat()
        digest = hashlib.sha256()
        digest.update(str(stat.st_size).encode())
        digest.update(str(stat.st_mtime_ns).encode())
        with source.open('rb') as handle:
            head = handle.read(65536)
            digest.update(head)
            if stat.st_size > 65536:
                handle.seek(max(0, stat.st_size - 65536))
                digest.update(handle.read(65536))
        return digest.hexdigest()

    def status(self, price_list_id: int, source_path: str | Path | None = None) -> CatalogStatus:
        from services.db import row
        self.ensure_schema()
        item = row(
            "SELECT * FROM catalog_materializations WHERE price_list_id=?",
            (int(price_list_id),),
        ) or {}
        current = ''
        if source_path and Path(source_path).exists():
            try:
                current = self.source_fingerprint(source_path)
            except OSError:
                current = ''
        stored = str(item.get('source_fingerprint') or '')
        ready = bool(
            item and item.get('status') == 'ready' and stored
            and int(item.get('schema_version') or 1) >= 2
            and (not current or stored == current)
        )
        return CatalogStatus(
            price_list_id=int(price_list_id),
            ready=ready,
            row_count=int(item.get('row_count') or 0),
            source_fingerprint=stored,
            materialized_at=str(item.get('materialized_at') or ''),
            source_path=str(item.get('source_path') or source_path or ''),
            message=str(item.get('message') or ''),
        )

    @staticmethod
    def _safe_value(value: Any) -> Any:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        if isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        item = getattr(value, 'item', None)
        if callable(item):
            try:
                return CatalogCore._safe_value(item())
            except Exception:
                pass
        return str(value)

    @staticmethod
    def _csv_separator(path: Path) -> str:
        sample = path.read_bytes()[:65536].decode('utf-8', errors='replace')
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=';,\t|,')
            return dialect.delimiter
        except csv.Error:
            return ';'

    def _source_chunks(self, path: Path, chunk_size: int) -> Iterable[pd.DataFrame]:
        from services.lists import read_list
        suffix = path.suffix.lower()
        if suffix in {'.csv', '.txt', '.tsv'}:
            sep = self._csv_separator(path)
            try:
                for chunk in pd.read_csv(
                    path, sep=sep, chunksize=max(1000, int(chunk_size)),
                    encoding_errors='replace', low_memory=True,
                ):
                    yield chunk
                return
            except Exception:
                pass
        # Supplier-specific XML/Excel/PKL parsers remain authoritative. They are run
        # in the background worker, then the normalized result is persisted once.
        yield read_list(path)

    def materialize(
        self,
        price_list_id: int,
        source_path: str | Path,
        *,
        chunk_size: int = 10000,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        from services.db import connect, execute_many, now_iso
        from services.lists import normalize

        self.ensure_schema()
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f'Listino non disponibile: {source}')
        fingerprint = self.source_fingerprint(source)

        with connect() as con:
            con.execute("DELETE FROM catalog_products WHERE price_list_id=?", (int(price_list_id),))
            con.execute(
                """INSERT INTO catalog_materializations(
                    price_list_id,source_fingerprint,source_path,row_count,status,message,materialized_at,schema_version
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(price_list_id) DO UPDATE SET
                    source_fingerprint=excluded.source_fingerprint,
                    source_path=excluded.source_path,row_count=0,status='running',
                    message='',materialized_at=excluded.materialized_at,schema_version=excluded.schema_version""",
                (int(price_list_id), fingerprint, str(source), 0, 'running', '', now_iso(), 2),
            )

        row_no = 0
        chunks_done = 0
        try:
            for raw_chunk in self._source_chunks(source, chunk_size):
                normalized = normalize(raw_chunk)
                payloads = []
                for record in normalized.to_dict('records'):
                    safe = {str(k): self._safe_value(v) for k, v in record.items()}
                    payloads.append((
                        int(price_list_id), row_no,
                        str(safe.get('ean') or ''), str(safe.get('sku') or ''),
                        str(safe.get('name') or ''), float(safe.get('cost') or 0),
                        float(safe.get('shipping_cost') or 0),
                        float(safe.get('cost_pt') or 0), float(safe.get('cost_it') or 0),
                        float(safe.get('cost_fr') or 0), float(safe.get('cost_de') or 0),
                        float(safe.get('cost_zone3') or 0), float(safe.get('cost_zone4') or 0),
                        float(safe.get('cost_zone5') or 0), float(safe.get('cost_zone6') or 0),
                        float(safe.get('quantity') or 0), float(safe.get('weight_kg') or 0),
                        json.dumps(safe, ensure_ascii=False, separators=(',', ':')),
                    ))
                    row_no += 1
                execute_many(
                    """INSERT INTO catalog_products(
                        price_list_id,row_no,ean,sku,name,cost,shipping_cost,
                        cost_pt,cost_it,cost_fr,cost_de,cost_zone3,cost_zone4,cost_zone5,cost_zone6,
                        quantity,weight_kg,data_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    payloads,
                )
                chunks_done += 1
                if progress:
                    progress(row_no, 0, f'Catalogo normalizzato: {row_no:,} prodotti')

            with connect() as con:
                con.execute(
                    """UPDATE catalog_materializations SET row_count=?,status='ready',message='',
                       materialized_at=?,schema_version=2 WHERE price_list_id=?""",
                    (row_no, now_iso(), int(price_list_id)),
                )
            self.invalidate_memory(price_list_id)
            return {
                'price_list_id': int(price_list_id), 'rows': row_no,
                'chunks': chunks_done, 'fingerprint': fingerprint,
            }
        except Exception as exc:
            with connect() as con:
                con.execute(
                    "UPDATE catalog_materializations SET status='error',message=?,materialized_at=? WHERE price_list_id=?",
                    (str(exc), now_iso(), int(price_list_id)),
                )
            raise

    def preview(self, price_list_id: int, limit: int = 200) -> CatalogPage:
        return self.query(price_list_id, limit=limit, offset=0)

    @staticmethod
    def _country_cost_column(country: str) -> str:
        return {
            'pt':'cost_pt','it':'cost_it','fr':'cost_fr','de':'cost_de',
            'at':'cost_zone3','be':'cost_zone3','lu':'cost_zone3','nl':'cost_zone3','gb':'cost_zone3',
            'sk':'cost_zone4','si':'cost_zone4','pl':'cost_zone4','cz':'cost_zone4','bg':'cost_zone4',
            'hr':'cost_zone4','ee':'cost_zone4','gr':'cost_zone4','hu':'cost_zone4','lv':'cost_zone4',
            'lt':'cost_zone4','ro':'cost_zone4','dk':'cost_zone5','fi':'cost_zone5','ie':'cost_zone5',
            'se':'cost_zone5','ch':'cost_zone6','no':'cost_zone6','sm':'cost_zone6',
        }.get(str(country or '').lower(), '')

    def _query_parts(
        self, price_list_id: int, *, search: str = '', min_qty: float = 0,
        min_cost: float = 0, max_cost: float = 0, weight_mode: str = 'none',
        weight_from: float = 0, weight_to: float = 0, destination_country: str = '',
        positive_cost_only: bool = False, row_nos: Iterable[int] | None = None,
    ) -> tuple[str, list[Any], str]:
        country_column=self._country_cost_column(destination_country)
        cost_expr=(f"CASE WHEN {country_column}>0 THEN {country_column} ELSE cost END"
                   if country_column else 'cost')
        where=['price_list_id=?']; params: list[Any]=[int(price_list_id)]
        if search.strip():
            token=f"%{search.strip().lower()}%"
            where.append('(LOWER(ean) LIKE ? OR LOWER(sku) LIKE ? OR LOWER(name) LIKE ?)')
            params.extend([token,token,token])
        if min_qty>0:
            where.append('quantity>=?'); params.append(float(min_qty))
        if min_cost>0:
            where.append(f'{cost_expr}>=?'); params.append(float(min_cost))
        if max_cost>0:
            where.append(f'{cost_expr}<=?'); params.append(float(max_cost))
        if positive_cost_only:
            where.append(f'{cost_expr}>0')
        mode=str(weight_mode or 'none').lower()
        lower=max(0.0,float(weight_from or 0)); upper=max(0.0,float(weight_to or 0))
        if mode=='between' and lower>upper:
            raise ValueError('Nel filtro peso il valore Da non può superare il valore A.')
        if mode=='above':
            where.append('(weight_kg<=0 OR weight_kg<=?)'); params.append(lower)
        elif mode=='below':
            where.append('(weight_kg<=0 OR weight_kg>=?)'); params.append(lower)
        elif mode=='between':
            where.append('(weight_kg<=0 OR weight_kg<? OR weight_kg>?)'); params.extend([lower,upper])
        elif mode!='none':
            raise ValueError('Modalità di esclusione peso non valida.')
        if row_nos is not None:
            ids=sorted({int(value) for value in row_nos})
            if not ids:
                where.append('1=0')
            else:
                where.append('row_no IN ('+','.join('?' for _ in ids)+')'); params.extend(ids)
        return ' AND '.join(where),params,cost_expr

    def query(
        self,
        price_list_id: int,
        *,
        search: str = '', min_qty: float = 0, min_cost: float = 0,
        max_cost: float = 0, weight_mode: str = 'none', weight_from: float = 0,
        weight_to: float = 0, destination_country: str = '', positive_cost_only: bool = False,
        row_nos: Iterable[int] | None = None, offset: int = 0, limit: int = 250,
    ) -> CatalogPage:
        from services.db import row, rows
        self.ensure_schema()
        clause,params,cost_expr=self._query_parts(
            price_list_id,search=search,min_qty=min_qty,min_cost=min_cost,max_cost=max_cost,
            weight_mode=weight_mode,weight_from=weight_from,weight_to=weight_to,
            destination_country=destination_country,positive_cost_only=positive_cost_only,row_nos=row_nos,
        )
        total=int((row(f'SELECT COUNT(*) AS n FROM catalog_products WHERE {clause}',tuple(params)) or {}).get('n') or 0)
        page_params=list(params)+[max(1,int(limit)),max(0,int(offset))]
        selected=rows(
            f"SELECT row_no,data_json,{cost_expr} AS resolved_cost,shipping_cost "
            f"FROM catalog_products WHERE {clause} ORDER BY row_no LIMIT ? OFFSET ?",
            tuple(page_params),
        )
        records=[]
        for item in selected:
            try: record=json.loads(item.get('data_json') or '{}')
            except (TypeError,ValueError,json.JSONDecodeError): record={}
            record['_row_no']=int(item.get('row_no') or 0)
            record['cost']=float(item.get('resolved_cost') or 0)
            record['shipping_cost']=float(item.get('shipping_cost') or record.get('shipping_cost') or 0)
            records.append(record)
        return CatalogPage(records,total,max(0,int(offset)),max(1,int(limit)))

    def weight_stats(self, price_list_id: int) -> tuple[int,int]:
        from services.db import row
        self.ensure_schema()
        item=row(
            "SELECT SUM(CASE WHEN weight_kg>0 THEN 1 ELSE 0 END) known,"
            "SUM(CASE WHEN weight_kg<=0 THEN 1 ELSE 0 END) unknown "
            "FROM catalog_products WHERE price_list_id=?",(int(price_list_id),),
        ) or {}
        return int(item.get('known') or 0),int(item.get('unknown') or 0)

    def export_filtered_frame(self, price_list_id: int, *, excluded_row_nos: Iterable[int] = (),
                              selected_row_nos: Iterable[int] | None = None,
                              page_size: int = 5000, **filters: Any) -> pd.DataFrame:
        """Materialize only on explicit save, never on ordinary UI reruns."""
        excluded={int(v) for v in excluded_row_nos}
        selected=None if selected_row_nos is None else {int(v) for v in selected_row_nos}
        records: list[dict[str,Any]]=[]
        if selected is not None:
            ids=sorted(selected)
            for start in range(0,len(ids),500):
                page=self.query(price_list_id,offset=0,limit=max(1,len(ids[start:start+500])),
                                row_nos=ids[start:start+500],**filters)
                records.extend(item for item in page.rows if int(item.get('_row_no',-1)) not in excluded)
            return pd.DataFrame.from_records(records)
        offset=0
        while True:
            page=self.query(price_list_id,offset=offset,limit=page_size,**filters)
            if not page.rows: break
            records.extend(item for item in page.rows if int(item.get('_row_no',-1)) not in excluded)
            offset+=len(page.rows)
            if offset>=page.total: break
        return pd.DataFrame.from_records(records)

    def load_working_frame(self, price_list_id: int, fingerprint: str = '') -> pd.DataFrame:
        """Compatibility bridge for existing Streamlit pages.

        The normalized DataFrame is cached in-process across Streamlit reruns. This
        removes repeated XML/Excel parsing while v311 moves the editor itself to
        server-side pagination.
        """
        from services.db import rows
        key = (int(price_list_id), str(fingerprint or ''))
        with _CACHE_LOCK:
            cached = _FRAME_CACHE.get(key)
            if cached is not None:
                _FRAME_CACHE.move_to_end(key)
                return cached.copy(deep=False)
        result = []
        for item in rows(
            "SELECT data_json FROM catalog_products WHERE price_list_id=? ORDER BY row_no",
            (int(price_list_id),),
        ):
            try:
                result.append(json.loads(item.get('data_json') or '{}'))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        frame = pd.DataFrame.from_records(result)
        with _CACHE_LOCK:
            _FRAME_CACHE[key] = frame
            _FRAME_CACHE.move_to_end(key)
            while len(_FRAME_CACHE) > _FRAME_CACHE_MAX_ENTRIES:
                _FRAME_CACHE.popitem(last=False)
        return frame.copy(deep=False)

    @staticmethod
    def invalidate_memory(price_list_id: int) -> None:
        with _CACHE_LOCK:
            for key in list(_FRAME_CACHE):
                if key[0] == int(price_list_id):
                    _FRAME_CACHE.pop(key, None)

    def build_materialize_job(self, seller_id: int, price_list_id: int) -> JobRequest:
        return JobRequest(
            kind='catalog.materialize', seller_id=int(seller_id),
            payload={'price_list_id': int(price_list_id)},
        )
