"""Comandi di amministrazione da terminale.

  python -m app.cli create-user NOME [--admin]
  python -m app.cli reset-password NOME
  python -m app.cli list-users
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import db
from .security import hash_password, validate_new_password


def _ask_password() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        if err := validate_new_password(pw):
            print(err)
            continue
        if getpass.getpass("Ripeti la password: ") != pw:
            print("Le password non coincidono")
            continue
        return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="Crea un utente")
    c.add_argument("username")
    c.add_argument("--admin", action="store_true", help="Utente amministratore")
    r = sub.add_parser("reset-password", help="Imposta una nuova password")
    r.add_argument("username")
    sub.add_parser("list-users", help="Elenca gli utenti")
    sub.add_parser("count-users", help="Stampa il numero di utenti")
    args = parser.parse_args(argv)

    db.init()
    if args.cmd == "create-user":
        if db.get_user_by_name(args.username):
            print(f"L'utente {args.username} esiste già", file=sys.stderr)
            return 1
        db.create_user(args.username, hash_password(_ask_password()), args.admin)
        print(f"Utente {args.username} creato{' (amministratore)' if args.admin else ''}")
    elif args.cmd == "reset-password":
        user = db.get_user_by_name(args.username)
        if not user:
            print(f"Utente {args.username} non trovato", file=sys.stderr)
            return 1
        db.set_password(user["id"], hash_password(_ask_password()))
        print("Password aggiornata")
    elif args.cmd == "list-users":
        for u in db.list_users():
            print(f"{u['username']}{'  (admin)' if u['is_admin'] else ''}")
    elif args.cmd == "count-users":
        print(db.count_users())
    return 0


if __name__ == "__main__":
    sys.exit(main())
