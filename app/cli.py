"""Comandi di amministrazione da terminale.

  python -m app.cli create-user NOME [--admin]
  python -m app.cli reset-password NOME
  python -m app.cli list-users
  python -m app.cli add-model percorso/modello.pt [--name NOME.pt]
  python -m app.cli optimize NOME.pt        # ricompila con TensorRT per il Jetson
  python -m app.cli list-models
  python -m app.cli set-llm NOME [--context 4096]   # LLM Ollama in uso
"""
from __future__ import annotations

import argparse
import getpass
import shutil
import sys
from pathlib import Path

from . import db
from .config import settings
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
    a = sub.add_parser("add-model", help="Aggiunge un modello YOLO .pt (verrà ottimizzato dal server)")
    a.add_argument("path")
    a.add_argument("--name", help="Nome con cui salvarlo (default: nome del file)")
    o = sub.add_parser("optimize", help="Ricompila subito un modello con TensorRT FP16")
    o.add_argument("name")
    sub.add_parser("list-models", help="Elenca i modelli e il loro stato")
    s = sub.add_parser("set-llm", help="Imposta l'LLM Ollama in uso (già scaricato)")
    s.add_argument("name")
    s.add_argument("--context", type=int, default=None)
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
    elif args.cmd in ("add-model", "optimize", "list-models"):
        return _models_cmd(args)
    elif args.cmd == "set-llm":
        from .services import ollama

        installed = ollama.installed_models()
        if installed is not None and not ollama.has_model(args.name, installed):
            print(f"{args.name} non è scaricato in Ollama (ollama pull {args.name})", file=sys.stderr)
            return 1
        context = args.context or ollama.selected_context()
        if context not in ollama.CONTEXT_CHOICES:
            print(f"Contesto non valido: scegli tra {ollama.CONTEXT_CHOICES}", file=sys.stderr)
            return 1
        ollama.select(args.name, ollama.selected_embed(), context)
        print(f"LLM in uso: {args.name} (contesto {context})")
    return 0


def _models_cmd(args: argparse.Namespace) -> int:
    from .services import yolo_models

    settings.ensure_dirs()
    if args.cmd == "add-model":
        src = Path(args.path)
        name = yolo_models.safe_model_name(args.name or src.name)
        if not src.is_file() or not name:
            print("File non trovato o nome non valido (serve un .pt)", file=sys.stderr)
            return 1
        tmp = settings.models_dir / f".upload-{name}"
        shutil.copy2(src, tmp)
        try:
            m = yolo_models.install_model(tmp, name)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"Modello non valido: {e}", file=sys.stderr)
            return 1
        print(f"Aggiunto {name} ({m['task_label']}, {len(m['classes'])} classi). "
              "Il server lo ottimizzerà al prossimo avvio, oppure: python -m app.cli optimize " + name)
    elif args.cmd == "optimize":
        if not yolo_models.exists(args.name):
            print(f"Modello {args.name} non trovato in {settings.models_dir}", file=sys.stderr)
            return 1
        print("Suggerimento: ferma il server durante l'ottimizzazione (sudo systemctl stop jetson-ai-server)")
        meta = yolo_models.optimize(args.name, status=lambda p, m: print(f"[{p * 100:3.0f}%] {m}"))
        if meta.get("backend") == "tensorrt":
            sp = meta["speed"]
            print(f"Fatto: {sp['before_ms']} ms -> {sp['after_ms']} ms per immagine ({sp['speedup']}x)")
        else:
            print(meta.get("note"))
    else:
        for m in yolo_models.list_models():
            speed = f"  {m['speed']['after_ms']} ms" if m.get("speed") else ""
            print(f"{m['name']:30} {m['task_label']:16} {m['status']:10} {m['backend'] or '-'}{speed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
