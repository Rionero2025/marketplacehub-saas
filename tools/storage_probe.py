"""Verify a shared object from two independent service processes, without DB access."""
from __future__ import annotations

import argparse
import json
import secrets
import uuid

from services.object_storage import object_store, sha256_bytes, storage_config


def write_probe() -> dict:
    config = storage_config()
    if config.backend != 's3':
        raise ValueError('La verifica condivisa richiede storage S3/R2, non local')
    key = f'_probes/{uuid.uuid4().hex}.bin'
    payload = secrets.token_bytes(256)
    object_store().put_bytes(key, payload, content_type='application/octet-stream')
    return {'key':key, 'sha256':sha256_bytes(payload), 'size_bytes':len(payload)}


def restore_probe(key: str, expected_sha256: str, *, cleanup: bool = False) -> dict:
    if not key.startswith('_probes/') or len(expected_sha256) != 64:
        raise ValueError('Riferimento probe non valido')
    if storage_config().backend != 's3':
        raise ValueError('La verifica condivisa richiede storage S3/R2, non local')
    store = object_store()
    payload = store.get_bytes(key)
    if sha256_bytes(payload) != expected_sha256:
        raise ValueError('Ripristino fallito: SHA-256 differente')
    if cleanup:
        store.delete(key)
        if store.exists(key):
            raise RuntimeError('Pulizia probe non riuscita')
    return {'verified':True, 'size_bytes':len(payload), 'cleanup':cleanup}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('write')
    restore = commands.add_parser('restore')
    restore.add_argument('--key', required=True)
    restore.add_argument('--sha256', required=True)
    restore.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    try:
        result = write_probe() if args.command == 'write' else restore_probe(args.key, args.sha256, cleanup=args.cleanup)
    except Exception as error:
        # Provider errors may contain endpoint/account details. Keep CLI output safe.
        parser.exit(1, f'Storage probe fallita ({type(error).__name__}); verificare configurazione e permessi.\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
