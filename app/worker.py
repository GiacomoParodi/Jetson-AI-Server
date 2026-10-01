"""Esecutore dei lavori: un thread che prende i lavori dalla coda uno alla volta.

Uno alla volta perché sul Jetson Orin Nano la memoria (8 GB, condivisa tra CPU e
GPU) basta per un modello pesante per volta.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import traceback

from . import db
from .config import settings
from .tasks import JobCancelled, JobContext, TaskError, registry

log = logging.getLogger(__name__)


def job_dir(job_id: str):
    return settings.jobs_dir / job_id


def input_dir(job_id: str):
    return job_dir(job_id) / "input"


def output_dir(job_id: str):
    return job_dir(job_id) / "output"


class Worker:
    def __init__(self) -> None:
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel_lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._thread: threading.Thread | None = None
        self.current_job: str | None = None

    def start(self) -> None:
        db.fail_interrupted_jobs()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def notify(self) -> None:
        self._wake.set()

    def cancel(self, job_id: str) -> None:
        with self._cancel_lock:
            self._cancelled.add(job_id)

    def _is_cancelled(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancelled

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = db.claim_next_job()
            if job is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job: dict) -> None:
        job_id = job["id"]
        self.current_job = job_id
        task = registry.get(job["task"])
        out = output_dir(job_id)
        out.mkdir(parents=True, exist_ok=True)
        inputs = sorted(input_dir(job_id).glob("*")) if input_dir(job_id).is_dir() else []

        def report(fraction: float, message: str) -> None:
            db.update_job(job_id, progress=fraction, message=message)

        ctx = JobContext(
            job_id=job_id,
            input_path=inputs[0] if inputs else None,
            params=job["params"],
            output_dir=out,
            cache_dir=settings.cache_dir,
            _report=report,
            _is_cancelled=lambda: self._is_cancelled(job_id),
        )
        log.info("Avvio lavoro %s (%s)", job_id, job["task"])
        try:
            if task is None:
                raise TaskError(f"Compito '{job['task']}' non più disponibile")
            result = task.run(ctx) or {}
            db.update_job(job_id, status="done", progress=1.0, message="Completato",
                          result=result, finished_at=time.time())
            log.info("Lavoro %s completato", job_id)
        except JobCancelled:
            db.update_job(job_id, status="cancelled", message="Annullato", finished_at=time.time())
            shutil.rmtree(out, ignore_errors=True)
        except TaskError as e:
            db.update_job(job_id, status="failed", error=str(e), message="Errore", finished_at=time.time())
        except Exception as e:  # errore inatteso: salviamo il dettaglio nel log
            log.error("Lavoro %s fallito:\n%s", job_id, traceback.format_exc())
            db.update_job(job_id, status="failed", error=f"Errore interno: {type(e).__name__}: {e}",
                          message="Errore", finished_at=time.time())
        finally:
            self.current_job = None
            with self._cancel_lock:
                self._cancelled.discard(job_id)


worker = Worker()


def queue_optimization(model_name: str, user_id: int | None = None) -> str | None:
    """Mette in coda l'ottimizzazione TensorRT di un modello (se non è già in coda)."""
    for job in db.list_jobs(None, limit=200):
        if (job["task"] == "optimize_model" and job["params"].get("model") == model_name
                and job["status"] in ("queued", "uploading")):
            return job["id"]
    if user_id is None:
        admin = next((u for u in db.list_users() if u["is_admin"]), None)
        if admin is None:
            log.warning("Nessun amministratore: impossibile ottimizzare %s", model_name)
            return None
        user_id = admin["id"]
    job_id = db.create_job(user_id, "optimize_model", {"model": model_name}, None)
    worker.notify()
    return job_id


def check_models_on_startup() -> None:
    """Riprende le ottimizzazioni interrotte e rifà quelle diventate inutilizzabili
    (es. dopo un aggiornamento di JetPack/TensorRT). Gira in un thread a parte
    perché importare PyTorch richiede qualche secondo."""
    from .services import yolo_models

    try:
        for m in yolo_models.list_models():
            if yolo_models.read_meta(m["name"]).get("status") == yolo_models.OPTIMIZING:
                yolo_models.update_meta(m["name"], status=yolo_models.PENDING)
            if yolo_models.needs_optimization(m["name"]):
                log.info("Modello %s da ottimizzare: messo in coda", m["name"])
                queue_optimization(m["name"])
    except Exception:
        log.exception("Controllo dei modelli all'avvio non riuscito")
