"""Modelli YOLO: archivio, metadati, ottimizzazione TensorRT e caricamento.

Ogni modello è un file `<nome>.pt` in data/models/yolo con accanto:
  <nome>.json    metadati (tipo, classi, stato dell'ottimizzazione, velocità)
  <nome>.engine  versione ottimizzata TensorRT (creata sul Jetson stesso)

Un .engine funziona solo sul tipo di GPU e sulla versione di TensorRT con cui è
stato creato: per questo l'ottimizzazione avviene sul Jetson e viene rifatta da
sola se TensorRT cambia (es. dopo un aggiornamento di JetPack).
"""
from __future__ import annotations

import functools
import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import settings

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.pt$")
_meta_lock = threading.RLock()

SUPPORTED_TASKS = {"detect": "Detection", "segment": "Segmentazione", "classify": "Classificazione"}

# Stati dell'ottimizzazione
PENDING, OPTIMIZING, READY, FAILED = "pending", "optimizing", "ready", "failed"


def safe_model_name(filename: str) -> str | None:
    name = Path(filename).name.replace(" ", "_")
    return name if _NAME_RE.match(name) else None


def pt_path(name: str) -> Path:
    return settings.models_dir / name


def _meta_path(name: str) -> Path:
    return pt_path(name).with_suffix(".json")


def engine_path(name: str) -> Path:
    return pt_path(name).with_suffix(".engine")


def read_meta(name: str) -> dict[str, Any]:
    with _meta_lock:
        try:
            return json.loads(_meta_path(name).read_text())
        except (OSError, ValueError):
            return {}


def update_meta(name: str, **fields: Any) -> dict[str, Any]:
    with _meta_lock:
        meta = {**read_meta(name), **fields}
        tmp = _meta_path(name).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(_meta_path(name))
        return meta


def exists(name: str) -> bool:
    return bool(safe_model_name(name)) and pt_path(name).is_file()


def _public(name: str, meta: dict[str, Any]) -> dict[str, Any]:
    pt = pt_path(name)
    status = meta.get("status", PENDING)
    if status == READY and meta.get("backend") == "tensorrt" and not engine_usable(name, meta):
        status = PENDING  # engine mancante o creato con un altro TensorRT
    return {
        "name": name,
        "task": meta.get("task", "detect"),
        "task_label": SUPPORTED_TASKS.get(meta.get("task", "detect"), meta.get("task")),
        "classes": meta.get("classes", []),
        "imgsz": meta.get("imgsz"),
        "size_mb": round(pt.stat().st_size / 1e6, 1) if pt.is_file() else None,
        "status": status,
        "backend": meta.get("backend"),
        "precision": meta.get("precision"),
        "speed": meta.get("speed"),
        "error": meta.get("error"),
        "note": meta.get("note"),
        "uploaded_at": meta.get("uploaded_at"),
        "optimized_at": meta.get("optimized_at"),
    }


def list_models() -> list[dict[str, Any]]:
    out = []
    for pt in sorted(settings.models_dir.glob("*.pt")):
        if pt.name.startswith("."):  # caricamento in corso
            continue
        out.append(_public(pt.name, read_meta(pt.name)))
    return out


def get_model(name: str) -> dict[str, Any] | None:
    return _public(name, read_meta(name)) if exists(name) else None


def inspect_model(path: Path) -> dict[str, Any]:
    """Apre il .pt per verificarne la validità e leggerne tipo, classi e risoluzione."""
    from ultralytics import YOLO

    model = YOLO(str(path))
    task = getattr(model, "task", "detect")
    if task not in SUPPORTED_TASKS:
        raise ValueError(f"tipo di modello '{task}' non supportato (ammessi: detection, segmentazione, classificazione)")
    names = model.names
    classes = [names[i] for i in sorted(names)] if isinstance(names, dict) else list(names)
    return {"task": task, "classes": classes, "imgsz": _train_imgsz(model, task)}


def _train_imgsz(model, task: str) -> int:
    """Risoluzione usata in addestramento: è quella con cui conviene ottimizzare."""
    candidates = []
    ckpt = getattr(model, "ckpt", None) or {}
    if isinstance(ckpt, dict):
        candidates.append((ckpt.get("train_args") or {}).get("imgsz"))
    candidates.append((getattr(model, "overrides", None) or {}).get("imgsz"))
    for c in candidates:
        if isinstance(c, (list, tuple)) and c:
            c = max(c)
        if isinstance(c, int) and 32 <= c <= 2048:
            return c
    return 224 if task == "classify" else 640


def install_model(tmp_file: Path, name: str) -> dict[str, Any]:
    """Verifica un .pt appena caricato e lo mette in archivio (sostituendo quello
    con lo stesso nome). Il modello resta 'da ottimizzare'."""
    info = inspect_model(tmp_file)
    delete_model(name)
    tmp_file.replace(pt_path(name))
    update_meta(name, **info, status=PENDING, uploaded_at=time.time(), backend=None,
                precision=None, speed=None, error=None, note=None)
    return get_model(name)  # type: ignore[return-value]


def delete_model(name: str) -> bool:
    pt = pt_path(name)
    if not pt.is_file():
        return False
    for p in (pt, pt.with_suffix(".engine"), pt.with_suffix(".json"), pt.with_suffix(".onnx")):
        p.unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------- GPU / TensorRT

@functools.lru_cache(maxsize=1)
def cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def tensorrt_version() -> str | None:
    try:
        import tensorrt

        return str(tensorrt.__version__)
    except Exception:
        return None


def tensorrt_enabled() -> bool:
    if settings.tensorrt == "off" or not cuda_available():
        return False
    return settings.tensorrt == "on" or tensorrt_version() is not None


def engine_usable(name: str, meta: dict[str, Any] | None = None) -> bool:
    meta = meta if meta is not None else read_meta(name)
    return (
        engine_path(name).is_file()
        and tensorrt_enabled()
        and meta.get("trt_version") == tensorrt_version()
    )


def needs_optimization(name: str) -> bool:
    meta = read_meta(name)
    status = meta.get("status", PENDING)
    if status in (PENDING, OPTIMIZING):
        return True
    if status == READY and tensorrt_enabled() and not engine_usable(name, meta):
        return True  # nuovo TensorRT o engine cancellato: va rifatto
    return False


def _benchmark(model, imgsz: int, runs: int = 30) -> float:
    """Millisecondi medi per immagine (pre/post-elaborazione comprese)."""
    import numpy as np

    img = (np.random.default_rng(0).random((imgsz, imgsz, 3)) * 255).astype("uint8")
    for _ in range(5):
        model.predict(img, imgsz=imgsz, verbose=False)
    start = time.perf_counter()
    for _ in range(runs):
        model.predict(img, imgsz=imgsz, verbose=False)
    return round((time.perf_counter() - start) * 1000 / runs, 2)


def optimize(name: str, status: Callable[[float, str], None] = lambda p, m: None,
             check_cancelled: Callable[[], None] = lambda: None) -> dict[str, Any]:
    """Ricompila il modello per la GPU del Jetson con TensorRT (FP16) e misura il
    guadagno di velocità. Senza GPU il modello resta in PyTorch."""
    from ultralytics import YOLO

    if not exists(name):
        raise FileNotFoundError(name)
    meta = read_meta(name)
    task = meta.get("task", "detect")
    imgsz = int(meta.get("imgsz") or 640)
    update_meta(name, status=OPTIMIZING, error=None)
    try:
        if not tensorrt_enabled():
            reason = "GPU NVIDIA non disponibile" if not cuda_available() else "TensorRT non installato o disattivato"
            return update_meta(name, status=READY, backend="pytorch", precision="fp32",
                               note=f"{reason}: il modello usa PyTorch senza ottimizzazione.",
                               optimized_at=time.time(), speed=None)

        device = 0
        status(0.05, "Misura della velocità del modello originale…")
        ms_before = _benchmark(YOLO(str(pt_path(name)), task=task), imgsz)
        check_cancelled()

        status(0.15, "Compilazione TensorRT FP16 (può richiedere diversi minuti)…")
        engine = engine_path(name)
        engine.unlink(missing_ok=True)
        exported = YOLO(str(pt_path(name)), task=task).export(
            format="engine", half=True, imgsz=imgsz, device=device, batch=1,
            simplify=True, verbose=False,
        )
        exported = Path(exported)
        if exported != engine:
            shutil.move(str(exported), engine)
        pt_path(name).with_suffix(".onnx").unlink(missing_ok=True)  # file intermedio
        check_cancelled()

        status(0.9, "Misura della velocità del modello ottimizzato…")
        ms_after = _benchmark(YOLO(str(engine), task=task), imgsz)
        release_gpu_memory()
        return update_meta(
            name, status=READY, backend="tensorrt", precision="fp16", trt_version=tensorrt_version(),
            optimized_at=time.time(), note=None,
            speed={"before_ms": ms_before, "after_ms": ms_after,
                   "speedup": round(ms_before / ms_after, 2) if ms_after else None, "imgsz": imgsz},
        )
    except Exception as e:
        update_meta(name, status=FAILED, error=f"{type(e).__name__}: {e}")
        raise


def load(name: str):
    """Modello pronto per l'inferenza: l'engine TensorRT se disponibile, altrimenti il .pt."""
    from ultralytics import YOLO

    if not exists(name):
        raise FileNotFoundError(name)
    meta = read_meta(name)
    task = meta.get("task", "detect")
    if engine_usable(name, meta):
        return YOLO(str(engine_path(name)), task=task)
    return YOLO(str(pt_path(name)), task=task)


def release_gpu_memory() -> None:
    try:
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

