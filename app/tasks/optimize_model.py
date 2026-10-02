"""Compito interno: ricompila un modello YOLO con TensorRT per la GPU del Jetson.

Parte da solo quando un amministratore carica o sostituisce un modello (e
all'avvio, se TensorRT è cambiato). Si può lanciare anche da terminale:
    python -m app.cli optimize NOME.pt
"""
from __future__ import annotations

from typing import Any

from ..services import ollama, yolo_models
from .base import JobCancelled, JobContext, Param, Task, TaskError


class OptimizeModel(Task):
    id = "optimize_model"
    title = "Ottimizzazione modello (TensorRT)"
    description = "Ricompila un modello YOLO per la GPU del Jetson."
    hidden = True
    section = "video"
    activity = "l'ottimizzazione di un modello"
    needs_file = False
    params = [Param("model", "Modello", "text", required=True)]
    rerun_label = "Ottimizza di nuovo"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        name = ctx.params["model"]
        if not yolo_models.exists(name):
            raise TaskError(f"Il modello {name} non esiste più")
        ollama.unload_all()  # serve tutta la memoria possibile per la compilazione
        try:
            meta = yolo_models.optimize(
                name,
                status=lambda p, m: ctx.progress(p, m, force=True),
                check_cancelled=ctx.check_cancelled,
            )
        except JobCancelled:
            yolo_models.update_meta(name, status=yolo_models.PENDING, error=None)
            raise
        except Exception as e:
            raise TaskError(f"Ottimizzazione non riuscita: {e}") from None
        finally:
            yolo_models.release_gpu_memory()

        if meta.get("backend") != "tensorrt":
            return {"summary": meta.get("note") or "Modello pronto (PyTorch).", "notes": []}
        speed = meta["speed"]

        def fps(ms: float) -> str:
            return f"{1000 / ms:.1f}" if ms else "—"

        return {
            "summary": (
                f"**{name}** è ottimizzato con TensorRT FP16: "
                f"**{speed['speedup']}x** più veloce ({speed['before_ms']} → {speed['after_ms']} ms per immagine)."
            ),
            "table": {
                "columns": ["Versione", "ms per immagine", "Immagini al secondo"],
                "rows": [
                    ["Originale (PyTorch)", speed["before_ms"], fps(speed["before_ms"])],
                    ["Ottimizzata (TensorRT FP16)", speed["after_ms"], fps(speed["after_ms"])],
                ],
            },
            "notes": [f"Risoluzione di ingresso: {speed['imgsz']} px (quella dell'addestramento)."],
        }
