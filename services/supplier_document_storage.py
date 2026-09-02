from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from services.cecotec_orders import clean_text
from services.db import connect, execute, now_iso, rows
from services.durable_files import put_bytes as put_durable_bytes, read_bytes as read_durable_bytes


def ensure_supplier_document_file_schema() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS supplier_document_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                storage_key TEXT NOT NULL DEFAULT '',
                storage_backend TEXT NOT NULL DEFAULT '',
                storage_sha256 TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(seller_id,marketplace_account_id,marketplace,file_sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_supplier_document_files_scope
            ON supplier_document_files(seller_id,marketplace_account_id,marketplace,created_at);
            """
        )


def archive_supplier_documents(
    *, seller_id: int, account_id: int, marketplace: str,
    documents: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    ensure_supplier_document_file_schema()
    archived: list[dict[str, Any]]=[]
    for document in documents:
        payload=bytes(document.get("content") or b"")
        if not payload:
            continue
        digest=hashlib.sha256(payload).hexdigest()
        existing=rows(
            """SELECT * FROM supplier_document_files
               WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND file_sha256=? LIMIT 1""",
            (int(seller_id),int(account_id),clean_text(marketplace).lower(),digest),
        )
        if existing:
            archived.append(dict(existing[0])); continue
        filename=clean_text(document.get("file_name")) or f"documento_{digest[:12]}.bin"
        mime_type=clean_text(document.get("mime_type")) or "application/octet-stream"
        stored=put_durable_bytes(
            namespace="supplier_documents",
            identity=f"seller_{int(seller_id)}_account_{int(account_id)}",
            filename=filename,content=payload,content_type=mime_type,
        )
        file_id=execute(
            """INSERT INTO supplier_document_files(
                seller_id,marketplace_account_id,marketplace,file_name,file_sha256,source,source_url,
                mime_type,size_bytes,storage_key,storage_backend,storage_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(seller_id),int(account_id),clean_text(marketplace).lower(),filename,digest,
             clean_text(document.get("source")),clean_text(document.get("input_url") or document.get("source_url")),
             mime_type,len(payload),stored["storage_key"],stored["storage_backend"],stored["sha256"],now_iso()),
        )
        archived.append({"id":file_id,"file_name":filename,"file_sha256":digest,**stored})
    return archived


def supplier_document_bytes(file_id: int) -> bytes:
    ensure_supplier_document_file_schema()
    found=rows("SELECT * FROM supplier_document_files WHERE id=? LIMIT 1",(int(file_id),))
    if not found:
        raise KeyError(f"Documento fornitore non trovato: {file_id}")
    item=found[0]
    return read_durable_bytes(storage_key=clean_text(item.get("storage_key")),expected_sha256=clean_text(item.get("storage_sha256")))
