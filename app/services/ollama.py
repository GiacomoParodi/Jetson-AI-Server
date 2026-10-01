"""Client minimo per l'API HTTP di Ollama (LLM ed embedding locali)."""
from __future__ import annotations

import json
import logging
from typing import Callable, Iterable

import httpx

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


def has_model(name: str, models: list[str] | None) -> bool:
    if not models:
        return False
    wanted = name if ":" in name else f"{name}:latest"
    return wanted in models or name in models


def embed(texts: list[str], model: str | None = None, batch_size: int = 32) -> list[list[float]]:
    model = model or settings.embed_model
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
    model = model or settings.llm_model
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": settings.ollama_keep_alive,
        "options": {"num_ctx": settings.llm_context, "temperature": 0.2, **(options or {})},
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
