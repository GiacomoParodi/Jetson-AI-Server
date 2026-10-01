"""Gestione dei modelli YOLO: elenco, caricamento dei file .pt e ottimizzazione TensorRT."""
from __future__ import annotations

import functools
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable

from ..config import settings

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.pt$")
_lock = threading.Lock()


def safe_model_name(filename: str) -> str | None:
    name = Path(filename).name.replace(" ", "_")
    return name if _NAME_RE.match(name) else None


def _meta_path(pt: Path) -> Path:
    return pt.with_suffix(".json")


def list_models() -> list[dict[str, Any]]:
    """Modelli caricati dagli utenti più quello predefinito di Ultralytics."""
    models = []
    for pt in sorted(settings.models_dir.glob("*.pt")):
        if pt.name.startswith("."):  # caricamento in corso
            continue
        meta: dict[str, Any] = {}
        if _meta_path(pt).is_file():
            try:
                meta = json.loads(_meta_path(pt).read_text())
            except ValueError:
                pass
        models.append({
            "name": pt.name,
            "classes": meta.get("classes", []),
            "size_mb": round(pt.stat().st_size / 1e6, 1),
            "tensorrt": pt.with_suffix(".engine").is_file(),
            "builtin": False,
        })
    if settings.yolo_default_model and not any(m["name"] == settings.yolo_default_model for m in models):
        models.append({
            "name": settings.yolo_default_model,
            "classes": [],
            "size_mb": None,
            "tensorrt": False,
            "builtin": True,
        })
    return models


def model_names() -> list[str]:
    return [m["name"] for m in list_models()]


def resolve_path(name: str) -> str:
    """Percorso del modello sul disco. Il modello predefinito sta in data/models/
    e, se manca, Ultralytics lo scarica lì da solo (serve internet la prima volta)."""
    pt = settings.models_dir / name
    if pt.is_file():
        return str(pt)
    if name == settings.yolo_default_model:
        return str(settings.models_dir.parent / name)
    raise FileNotFoundError(name)


def inspect_model(path: Path) -> dict[str, Any]:
    """Apre il modello per verificarne la validità e leggerne le classi."""
    from ultralytics import YOLO

    model = YOLO(str(path))
    names = model.names
    classes = [names[i] for i in sorted(names)] if isinstance(names, dict) else list(names)
    meta = {"classes": classes, "task": getattr(model, "task", "detect")}
    _meta_path(path).write_text(json.dumps(meta))
    return meta


def delete_model(name: str) -> bool:
    pt = settings.models_dir / name
    if not pt.is_file():
        return False
    for p in (pt, pt.with_suffix(".engine"), pt.with_suffix(".json"), pt.with_suffix(".onnx")):
        p.unlink(missing_ok=True)
    return True


@functools.lru_cache(maxsize=1)
def cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _want_tensorrt() -> bool:
    if settings.tensorrt == "off":
        return False
    if not cuda_available():
        return False
    if settings.tensorrt == "on":
        return True
    try:
        import tensorrt  # noqa: F401
    except Exception:
        return False
    return True


def load(name: str, status: Callable[[str], None] = lambda m: None):
    """Carica un modello per l'inferenza. Sul Jetson, la prima volta converte il
    modello in TensorRT (molto più veloce) e riusa poi il file .engine."""
    from ultralytics import YOLO

    path = resolve_path(name)
    if _want_tensorrt() and path.endswith(".pt"):
        engine = Path(path).with_suffix(".engine")
        with _lock:
            if not engine.is_file():
                status("Ottimizzazione TensorRT del modello (solo la prima volta, alcuni minuti)…")
                try:
                    exported = YOLO(path).export(format="engine", half=True, device=0, verbose=False)
                    if Path(exported) != engine and Path(exported).is_file():
                        Path(exported).replace(engine)
                except Exception:
                    log.exception("Esportazione TensorRT fallita, uso il modello PyTorch")
        if engine.is_file():
            return YOLO(str(engine), task="detect")
    return YOLO(path)


def release_gpu_memory() -> None:
    try:
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
