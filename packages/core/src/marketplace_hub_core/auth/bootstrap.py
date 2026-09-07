from __future__ import annotations

from argparse import ArgumentParser
from datetime import UTC, datetime
from getpass import getpass

from marketplace_hub_core.auth.passwords import hash_password
from marketplace_hub_core.auth.service import normalize_login
from marketplace_hub_core.database import create_database_engine
from marketplace_hub_core.settings import get_settings
from marketplace_hub_core.tenancy.bootstrap import create_platform_owner


def main() -> None:
    parser = ArgumentParser(description="Crea un account interno Platform Admin.")
    parser.add_argument("login", help="Email o nome utente del Platform Admin")
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args()
    password = getpass("Password: ")
    confirmation = getpass("Conferma password: ")
    if password != confirmation:
        raise SystemExit("Le password non coincidono.")

    engine = create_database_engine(get_settings())
    try:
        user = create_platform_owner(
            engine,
            login=normalize_login(args.login),
            display_name=args.display_name.strip(),
            password_hash=hash_password(password),
            now=datetime.now(UTC),
        )
        print(f"Platform Admin creato: {user.login}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
