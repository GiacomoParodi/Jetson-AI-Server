"""Client minimo per l'API HTTP di Ollama (LLM ed embedding locali)."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Iterable

import httpx

from .. import db
from ..config import settings

log = logging.getLogger(__name__)


class OllamaError(Exception):
    pass


def _client(timeout: float = 600) -> httpx.Client:
    return httpx.Client(base_url=settings.ollama_url, timeout=httpx.Timeout(timeout, connect=5))


def installed_models() -> list[str] | None:
    """Nomi dei modelli scaricati, oppure None se Ollama non risponde."""
    try:
        with _client(5) as c:
            r = c.get("/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return None


# ---------------------------------------------------------------- modelli scelti

CONTEXT_CHOICES = [2048, 4096, 8192, 16384]


def selected_llm() -> str:
    """Modello LLM scelto dall'amministratore ('' = nessuno)."""
    value = db.get_setting("llm_model")
    return value if value is not None else settings.llm_model


def selected_embed() -> str:
    """Modello di embedding scelto ('' = nessuno: ricerca solo per parole chiave)."""
    value = db.get_setting("embed_model")
    return value if value is not None else settings.embed_model


def selected_context() -> int:
    value = db.get_setting("llm_context")
    return int(value) if value else settings.llm_context


def select(llm_model: str, embed_model: str, context: int) -> None:
    db.set_setting("llm_model", llm_model)
    db.set_setting("embed_model", embed_model)
    db.set_setting("llm_context", str(context))


_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}(:[A-Za-z0-9._-]{1,60})?$")


def valid_name(name: str) -> bool:
    return bool(_MODEL_NAME_RE.match(name or ""))


def installed_details() -> list[dict[str, Any]] | None:
    """Modelli scaricati con dimensione, parametri e quantizzazione (None se Ollama è spento)."""
    try:
        with _client(5) as c:
            r = c.get("/api/tags")
            r.raise_for_status()
            raw = r.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return None
    out = []
    for m in raw:
        d = m.get("details") or {}
        families = " ".join([d.get("family") or ""] + list(d.get("families") or [])).lower()
        out.append({
            "name": m["name"],
            "size_gb": round(m.get("size", 0) / 1e9, 2),
            "parameter_size": d.get("parameter_size"),
            "quantization": d.get("quantization_level"),
            "family": d.get("family"),
            # Euristica solo per ordinare le scelte nell'interfaccia.
            "embedding": "bert" in families or "embed" in m["name"].lower(),
        })
    return sorted(out, key=lambda m: m["name"])


# ---------------------------------------------------------------- download

_pulls: dict[str, dict[str, Any]] = {}
_pulls_lock = threading.Lock()


def pull_status() -> dict[str, dict[str, Any]]:
    with _pulls_lock:
        now = time.time()
        # I download finiti restano visibili per 10 minuti.
        for name in [n for n, p in _pulls.items() if p.get("finished_at") and now - p["finished_at"] > 600]:
            del _pulls[name]
        return {n: dict(p) for n, p in _pulls.items()}


def start_pull(name: str) -> None:
    """Scarica un modello dalla libreria Ollama in un thread a parte (non blocca la coda dei lavori)."""
    with _pulls_lock:
        if name in _pulls and not _pulls[name].get("finished_at"):
            return
        _pulls[name] = {"status": "avvio…", "completed": 0, "total": 0, "error": None, "finished_at": None}
    threading.Thread(target=_pull, args=(name,), name=f"pull-{name}", daemon=True).start()


def _pull(name: str) -> None:
    def update(**kw: Any) -> None:
        with _pulls_lock:
            _pulls[name].update(kw)

    try:
        with httpx.Client(base_url=settings.ollama_url, timeout=httpx.Timeout(None, connect=5)) as c, \
                c.stream("POST", "/api/pull", json={"model": name, "stream": True}) as r:
            if r.status_code != 200:
                r.read()
                raise OllamaError(r.text[:200] or f"errore {r.status_code}")
            for line in _lines(r.iter_lines()):
                data = json.loads(line)
                if data.get("error"):
                    raise OllamaError(data["error"])
                fields: dict[str, Any] = {"status": data.get("status", "")}
                if data.get("total"):
                    fields.update(total=data["total"], completed=data.get("completed", 0))
                update(**fields)
        update(status="completato", finished_at=time.time())
    except httpx.ConnectError:
        update(error="Ollama non è raggiungibile", finished_at=time.time())
    except Exception as e:
        update(error=str(e), finished_at=time.time())


def delete_model(name: str) -> None:
    with _client(30) as c:
        r = c.request("DELETE", "/api/delete", json={"model": name})
        if r.status_code not in (200, 404):
            raise OllamaError(r.text[:200])


def has_model(name: str, models: list[str] | None) -> bool:
    if not models:
        return False
    wanted = name if ":" in name else f"{name}:latest"
    return wanted in models or name in models


def embed(texts: list[str], model: str | None = None, batch_size: int = 32) -> list[list[float]]:
    model = model or selected_embed()
    if not model:
        raise OllamaError("nessun modello di embedding scelto")
    vectors: list[list[float]] = []
    with _client() as c:
        for i in range(0, len(texts), batch_size):
            r = c.post("/api/embed", json={
                "model": model,
                "input": texts[i:i + batch_size],
                "keep_alive": settings.ollama_keep_alive,
            })
            if r.status_code != 200:
                raise OllamaError(f"Embedding non riuscito ({r.status_code}): {r.text[:200]}")
            vectors.extend(r.json()["embeddings"])
    return vectors


def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    on_token: Callable[[str], None] | None = None,
    options: dict | None = None,
) -> str:
    """Chat in streaming; on_token viene chiamata a ogni pezzo di testo generato
    (e può sollevare un'eccezione per interrompere la generazione)."""
    model = model or selected_llm()
    if not model:
        raise OllamaError("Nessun modello LLM scelto: sceglilo nella pagina Modelli")
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": settings.ollama_keep_alive,
        "options": {"num_ctx": selected_context(), "temperature": 0.2, **(options or {})},
    }
    parts: list[str] = []
    try:
        with _client() as c, c.stream("POST", "/api/chat", json=body) as r:
            if r.status_code != 200:
                r.read()
                raise OllamaError(f"LLM non disponibile ({r.status_code}): {r.text[:200]}")
            for line in _lines(r.iter_lines()):
                data = json.loads(line)
                if data.get("error"):
                    raise OllamaError(data["error"])
                piece = data.get("message", {}).get("content", "")
                if piece:
                    parts.append(piece)
                    if on_token:
                        on_token(piece)
                if data.get("done"):
                    break
    except httpx.ConnectError:
        raise OllamaError("Ollama non è raggiungibile: il servizio è avviato?") from None
    return "".join(parts)


def _lines(it: Iterable[str]) -> Iterable[str]:
    for line in it:
        if line.strip():
            yield line


def unload_all() -> None:
    """Libera la memoria occupata dai modelli LLM (utile prima di un lavoro YOLO,
    perché sul Jetson CPU e GPU condividono la stessa RAM)."""
    try:
        with _client(10) as c:
            r = c.get("/api/ps")
            r.raise_for_status()
            for m in r.json().get("models", []):
                c.post("/api/generate", json={"model": m["name"], "keep_alive": 0})
    except (httpx.HTTPError, ValueError):
        pass
