"""Pipeline YOLO: alberi di nodi, ognuno con un modello.

Struttura di un nodo:
  id              identificativo univoco (stringa)
  name            nome mostrato
  model           file .pt del modello (detection, segmentazione o classificazione)
  conf            confidenza minima (0.05-0.95)
  classes         classi del modello da tenere (vuoto = tutte)
  parent_classes  classi del padre che attivano il nodo (vuoto = tutte). Non per le radici.
  padding         margine aggiunto al ritaglio dell'oggetto del padre (0-0.5, frazione del lato)
  children        nodi figli

Come si esegue (vedi app/tasks/yolo_pipeline.py):
  - i nodi radice girano sull'intero fotogramma;
  - un figlio di un nodo detection/segmentazione gira sul ritaglio di ogni oggetto
    trovato dal padre (solo per le classi in parent_classes);
  - un figlio di un nodo di classificazione gira sulla stessa immagine del padre,
    solo se la classe predetta è in parent_classes.
I fratelli sono indipendenti: girano tutti sulla stessa immagine.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Iterator

from .. import db
from ..config import settings
from . import yolo_models

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    tree TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at REAL NOT NULL
);
"""


class PipelineError(ValueError):
    pass


class VersionConflict(Exception):
    pass


# ---------------------------------------------------------------- validazione

def iter_nodes(tree: list[dict[str, Any]], depth: int = 0, parent: dict | None = None) -> Iterator[tuple[dict, int, dict | None]]:
    for node in tree:
        yield node, depth, parent
        yield from iter_nodes(node.get("children") or [], depth + 1, node)


def normalize(tree: Any) -> list[dict[str, Any]]:
    """Valida l'albero ricevuto dal client e lo riporta a una forma pulita."""
    if not isinstance(tree, list):
        raise PipelineError("Formato dell'albero non valido")
    models = {m["name"]: m for m in yolo_models.list_models()}
    seen_ids: set[str] = set()
    count = 0

    def clean(node: Any, depth: int, parent: dict | None) -> dict[str, Any]:
        nonlocal count
        if not isinstance(node, dict):
            raise PipelineError("Nodo non valido")
        count += 1
        if count > settings.pipeline_max_nodes:
            raise PipelineError(f"Troppi nodi (massimo {settings.pipeline_max_nodes}, modificabile con "
                                "JAS_PIPELINE_MAX_NODES nel file .env)")
        if depth >= settings.pipeline_max_depth:
            raise PipelineError(f"Albero troppo profondo (massimo {settings.pipeline_max_depth} livelli, "
                                "modificabile con JAS_PIPELINE_MAX_DEPTH nel file .env)")

        name = str(node.get("name") or "").strip()[:60]
        if not name:
            raise PipelineError("Ogni nodo deve avere un nome")
        node_id = str(node.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", node_id) or node_id in seen_ids:
            node_id = uuid.uuid4().hex[:12]
        seen_ids.add(node_id)

        model_name = str(node.get("model") or "")
        model = models.get(model_name)
        if not model:
            raise PipelineError(f"Nodo '{name}': scegli un modello" if not model_name
                                else f"Nodo '{name}': il modello '{model_name}' non esiste")

        try:
            conf = float(node.get("conf", 0.35))
            padding = float(node.get("padding", 0.05))
        except (TypeError, ValueError):
            raise PipelineError(f"Nodo '{name}': valori numerici non validi") from None
        if not 0.01 <= conf <= 0.99:
            raise PipelineError(f"Nodo '{name}': la confidenza deve essere tra 0.01 e 0.99")
        if not 0 <= padding <= 0.5:
            raise PipelineError(f"Nodo '{name}': il margine deve essere tra 0 e 50%")

        classes = _class_list(node.get("classes"), model["classes"], f"Nodo '{name}'")
        parent_classes: list[str] = []
        if parent is not None:
            parent_model = models[parent["model"]]
            allowed = parent["classes"] or parent_model["classes"]
            parent_classes = _class_list(node.get("parent_classes"), allowed,
                                         f"Nodo '{name}' (classi del padre)")

        out = {
            "id": node_id, "name": name, "model": model_name, "conf": round(conf, 3),
            "classes": classes, "parent_classes": parent_classes, "padding": round(padding, 3),
            "children": [],
        }
        children = node.get("children") or []
        if not isinstance(children, list):
            raise PipelineError(f"Nodo '{name}': figli non validi")
        out["children"] = [clean(c, depth + 1, out) for c in children]
        return out

    cleaned = [clean(n, 0, None) for n in tree]
    if not cleaned:
        raise PipelineError("La pipeline deve avere almeno un nodo")
    return cleaned


def _class_list(value: Any, allowed: list[str], where: str) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list):
        raise PipelineError(f"{where}: elenco di classi non valido")
    out = []
    for c in value:
        c = str(c)
        if c not in allowed:
            raise PipelineError(f"{where}: la classe '{c}' non esiste")
        if c not in out:
            out.append(c)
    return out


def models_used(tree: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for node, _, _ in iter_nodes(tree):
        if node["model"] not in seen:
            seen.append(node["model"])
    return seen


# ---------------------------------------------------------------- archivio

def init() -> None:
    with db.connection() as conn:
        conn.executescript(SCHEMA)


def _row(r) -> dict[str, Any]:
    d = dict(r)
    d["tree"] = json.loads(d["tree"])
    d["node_count"] = sum(1 for _ in iter_nodes(d["tree"]))
    d["models"] = models_used(d["tree"])
    return d


def list_all() -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT p.*, u.username AS created_by_name FROM pipelines p "
            "LEFT JOIN users u ON u.id = p.created_by ORDER BY p.name"
        ).fetchall()
    return [_row(r) for r in rows]


def get(pipeline_id: int) -> dict[str, Any] | None:
    with db.connection() as conn:
        r = conn.execute(
            "SELECT p.*, u.username AS created_by_name FROM pipelines p "
            "LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?", (pipeline_id,)
        ).fetchone()
    return _row(r) if r else None


def _clean_name(name: str) -> str:
    name = (name or "").strip()[:80]
    if not name:
        raise PipelineError("Dai un nome alla pipeline")
    return name


def create(name: str, description: str, tree: Any, user_id: int) -> dict[str, Any]:
    name = _clean_name(name)
    cleaned = normalize(tree) if tree else []
    with db.connection() as conn:
        if conn.execute("SELECT 1 FROM pipelines WHERE name = ?", (name,)).fetchone():
            raise PipelineError("Esiste già una pipeline con questo nome")
        cur = conn.execute(
            "INSERT INTO pipelines (name, description, tree, created_by, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, description.strip()[:500], json.dumps(cleaned), user_id, time.time()),
        )
        new_id = int(cur.lastrowid)
    return get(new_id)  # type: ignore[return-value]


def update(pipeline_id: int, name: str, description: str, tree: Any, version: int) -> dict[str, Any]:
    name = _clean_name(name)
    cleaned = normalize(tree)
    with db.connection() as conn:
        if conn.execute("SELECT 1 FROM pipelines WHERE name = ? AND id != ?", (name, pipeline_id)).fetchone():
            raise PipelineError("Esiste già una pipeline con questo nome")
        cur = conn.execute(
            "UPDATE pipelines SET name = ?, description = ?, tree = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (name, description.strip()[:500], json.dumps(cleaned), time.time(), pipeline_id, version),
        )
        if cur.rowcount == 0:
            if not conn.execute("SELECT 1 FROM pipelines WHERE id = ?", (pipeline_id,)).fetchone():
                raise KeyError(pipeline_id)
            raise VersionConflict()
    return get(pipeline_id)  # type: ignore[return-value]


def delete(pipeline_id: int) -> bool:
    with db.connection() as conn:
        return conn.execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,)).rowcount > 0


def using_model(model_name: str) -> list[str]:
    return [p["name"] for p in list_all() if model_name in p["models"]]
