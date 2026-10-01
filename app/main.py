"""API HTTP e interfaccia web del server.

Avvio in sviluppo:  python -m app   (oppure uvicorn app.main:app)
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, db
from .config import settings
from .security import hash_password, hash_token, login_limiter, new_token, validate_new_password, verify_password
from .services import ollama, pipelines, system, yolo_models
from .tasks import TaskError, registry
from .worker import check_models_on_startup, input_dir, job_dir, output_dir, queue_optimization, worker

log = logging.getLogger("app")
STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "jas_session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    pipelines.init()
    if not registry.tasks:
        registry.discover(settings.plugin_dirs)
    if db.count_users() == 0:
        log.warning("Nessun utente: crealo con  python -m app.cli create-user NOME --admin")
    worker.start()
    threading.Thread(target=check_models_on_startup, name="model-check", daemon=True).start()
    yield
    worker.stop()


app = FastAPI(title="Jetson AI Server", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------- autenticazione

def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE)


def current_user(request: Request) -> dict[str, Any]:
    token = _token_from(request)
    user = db.get_session_user(hash_token(token)) if token else None
    if not user:
        raise HTTPException(401, "Accesso richiesto")
    return dict(user)


def admin_user(user: dict = Depends(current_user)) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(403, "Solo gli amministratori possono farlo")
    return user


def _public_user(u: dict[str, Any]) -> dict[str, Any]:
    return {"id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"])}


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    key = request.client.host if request.client else "?"
    if login_limiter.blocked(key):
        raise HTTPException(429, "Troppi tentativi falliti: riprova tra qualche minuto")
    user = db.get_user_by_name(body.username.strip())
    if not user or not verify_password(body.password, user["password_hash"]):
        login_limiter.fail(key)
        raise HTTPException(401, "Nome utente o password errati")
    login_limiter.reset(key)
    token = new_token()
    max_age = settings.session_hours * 3600
    db.create_session(hash_token(token), user["id"], time.time() + max_age)
    response.set_cookie(COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return {"token": token, "user": _public_user(dict(user))}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = _token_from(request)
    if token:
        db.delete_session(hash_token(token))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return _public_user(user)


class PasswordIn(BaseModel):
    current_password: str | None = None
    new_password: str


@app.post("/api/me/password")
def change_my_password(body: PasswordIn, user: dict = Depends(current_user)):
    if not verify_password(body.current_password or "", user["password_hash"]):
        raise HTTPException(400, "La password attuale non è corretta")
    if err := validate_new_password(body.new_password):
        raise HTTPException(400, err)
    db.set_password(user["id"], hash_password(body.new_password))
    return {"ok": True}


# ---------------------------------------------------------------- utenti (admin)

class UserIn(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@app.get("/api/users")
def list_users(_: dict = Depends(admin_user)):
    return [_public_user(dict(u)) for u in db.list_users()]


@app.post("/api/users")
def create_user(body: UserIn, _: dict = Depends(admin_user)):
    name = body.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", name):
        raise HTTPException(400, "Nome utente: 3-32 caratteri tra lettere, numeri, . _ -")
    if err := validate_new_password(body.password):
        raise HTTPException(400, err)
    if db.get_user_by_name(name):
        raise HTTPException(400, "Nome utente già esistente")
    uid = db.create_user(name, hash_password(body.password), body.is_admin)
    return _public_user(dict(db.get_user(uid)))


@app.post("/api/users/{user_id}/password")
def reset_password(user_id: int, body: PasswordIn, _: dict = Depends(admin_user)):
    if not db.get_user(user_id):
        raise HTTPException(404, "Utente non trovato")
    if err := validate_new_password(body.new_password):
        raise HTTPException(400, err)
    db.set_password(user_id, hash_password(body.new_password))
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(admin_user)):
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(404, "Utente non trovato")
    if target["id"] == admin["id"]:
        raise HTTPException(400, "Non puoi eliminare il tuo stesso account")
    for job in db.list_jobs(user_id, limit=100000):
        if job["status"] == "running":
            raise HTTPException(400, "L'utente ha un lavoro in esecuzione: annullalo prima")
    for job in db.list_jobs(user_id, limit=100000):
        shutil.rmtree(job_dir(job["id"]), ignore_errors=True)
    db.delete_user(user_id)
    return {"ok": True}


# ---------------------------------------------------------------- compiti e lavori

@app.get("/api/tasks")
def list_tasks(_: dict = Depends(current_user)):
    return [t.describe() for t in registry.all() if not t.hidden]


def _safe_filename(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^\w.\- ]", "_", name).strip(" .") or "file"
    return name[-120:]


def _job_for(job_id: str, user: dict) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job or (job["user_id"] != user["id"] and not user["is_admin"]):
        raise HTTPException(404, "Lavoro non trovato")
    return job


def _job_out(job: dict[str, Any]) -> dict[str, Any]:
    task = registry.get(job["task"])
    out = {k: job[k] for k in ("id", "task", "params", "input_name", "status", "progress", "message",
                               "result", "error", "created_at", "started_at", "finished_at", "username")}
    out["task_title"] = task.title if task else job["task"]
    out["rerun_label"] = task.rerun_label if task else None
    out["queue_position"] = db.queue_position(job["id"]) if job["status"] == "queued" else None
    return out


async def _save_upload(upload: UploadFile, dest: Path) -> None:
    limit = settings.max_upload_mb * 1024 * 1024
    written = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"File troppo grande (massimo {settings.max_upload_mb} MB)")
            f.write(chunk)


@app.post("/api/jobs")
async def create_job(
    task: str = Form(...),
    params: str = Form("{}"),
    file: UploadFile | None = File(None),
    user: dict = Depends(current_user),
):
    t = registry.get(task)
    if not t or t.hidden:
        raise HTTPException(404, "Compito sconosciuto")
    ok, reason = t.available()
    if not ok:
        raise HTTPException(503, f"Compito non disponibile: {reason}")
    try:
        clean = t.validate_params(json.loads(params or "{}"))
    except (TaskError, ValueError) as e:
        raise HTTPException(400, str(e)) from None

    filename = None
    if t.needs_file:
        if file is None or not file.filename:
            raise HTTPException(400, "Seleziona un file")
        filename = _safe_filename(file.filename)
        if t.accept and Path(filename).suffix.lower() not in t.accept:
            raise HTTPException(400, f"Formato non supportato. Accettati: {', '.join(t.accept)}")

    # Stato 'uploading' finché il file non è salvato, così il worker non lo prende troppo presto.
    job_id = db.create_job(user["id"], task, clean, filename, status="uploading")
    try:
        if file is not None and filename:
            await _save_upload(file, input_dir(job_id) / filename)
    except BaseException:
        shutil.rmtree(job_dir(job_id), ignore_errors=True)
        db.delete_job(job_id)
        raise
    db.update_job(job_id, status="queued")
    worker.notify()
    return _job_out(db.get_job(job_id))


@app.get("/api/jobs")
def list_jobs(all: bool = False, limit: int = 100, user: dict = Depends(current_user)):
    owner = None if (all and user["is_admin"]) else user["id"]
    return [_job_out(j) for j in db.list_jobs(owner, limit=min(limit, 500))]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(current_user)):
    return _job_out(_job_for(job_id, user))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: dict = Depends(current_user)):
    job = _job_for(job_id, user)
    if db.cancel_if_queued(job_id):
        return {"ok": True}
    if job["status"] == "running":
        worker.cancel(job_id)
        return {"ok": True}
    raise HTTPException(400, "Il lavoro è già terminato")


class RerunIn(BaseModel):
    params: dict[str, Any] = {}


@app.post("/api/jobs/{job_id}/rerun")
def rerun_job(job_id: str, body: RerunIn, user: dict = Depends(current_user)):
    """Nuovo lavoro sullo stesso file con parametri diversi (es. un'altra domanda sul PDF)."""
    old = _job_for(job_id, user)
    t = registry.get(old["task"])
    if not t or t.hidden:
        raise HTTPException(404 if not t else 400, "Questo lavoro non si può rieseguire")
    try:
        clean = t.validate_params({**old["params"], **body.params})
    except TaskError as e:
        raise HTTPException(400, str(e)) from None
    old_inputs = sorted(input_dir(job_id).glob("*")) if input_dir(job_id).is_dir() else []
    if t.needs_file and not old_inputs:
        raise HTTPException(400, "Il file originale non è più disponibile")

    new_id = db.create_job(user["id"], old["task"], clean, old["input_name"], status="uploading")
    for src in old_inputs:
        dest = input_dir(new_id) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.hardlink_to(src)  # nessuna copia: stesso file sul disco
        except OSError:
            shutil.copy2(src, dest)
    db.update_job(new_id, status="queued")
    worker.notify()
    return _job_out(db.get_job(new_id))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, user: dict = Depends(current_user)):
    job = _job_for(job_id, user)
    if job["status"] in ("running", "uploading"):
        raise HTTPException(400, "Annulla il lavoro prima di eliminarlo")
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    db.delete_job(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str, download: bool = False, user: dict = Depends(current_user)):
    _job_for(job_id, user)
    base = output_dir(job_id).resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(404, "File non trovato")
    return FileResponse(path, filename=path.name if download else None)


@app.get("/api/jobs/{job_id}/input")
def job_input(job_id: str, user: dict = Depends(current_user)):
    _job_for(job_id, user)
    files = sorted(input_dir(job_id).glob("*")) if input_dir(job_id).is_dir() else []
    if not files:
        raise HTTPException(404, "File non trovato")
    return FileResponse(files[0], filename=files[0].name)


# ---------------------------------------------------------------- modelli YOLO

@app.get("/api/models")
def list_models(_: dict = Depends(current_user)):
    return yolo_models.list_models()


@app.post("/api/models")
async def upload_model(
    file: UploadFile = File(...),
    replace: str | None = Form(None),
    admin: dict = Depends(admin_user),
):
    """Carica un modello .pt (o ne sostituisce uno esistente con `replace`) e ne
    mette in coda l'ottimizzazione per il Jetson."""
    # Solo gli admin: un file .pt può eseguire codice quando viene aperto.
    if replace:
        if not yolo_models.exists(replace):
            raise HTTPException(404, "Modello da sostituire non trovato")
        if not (file.filename or "").lower().endswith(".pt"):
            raise HTTPException(400, "Serve un file .pt")
        name = replace
    else:
        name = yolo_models.safe_model_name(file.filename or "")
        if not name:
            raise HTTPException(400, "Serve un file .pt con un nome semplice (lettere, numeri, _ . -)")
    old_classes = (yolo_models.get_model(name) or {}).get("classes")
    tmp = settings.models_dir / f".upload-{name}"
    await _save_upload(file, tmp)
    try:
        model = await run_in_threadpool(yolo_models.install_model, tmp, name)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"Il file non è un modello YOLO valido: {e}") from None
    queue_optimization(name, admin["id"])
    warnings = []
    if old_classes is not None and old_classes != model["classes"]:
        used = pipelines.using_model(name)
        if used:
            warnings.append("Le classi del modello sono cambiate: controlla le pipeline che lo usano ("
                            + ", ".join(used) + ").")
    return {**model, "warnings": warnings}


@app.post("/api/models/{name}/optimize")
def optimize_model(name: str, admin: dict = Depends(admin_user)):
    if not yolo_models.exists(name):
        raise HTTPException(404, "Modello non trovato")
    yolo_models.update_meta(name, status=yolo_models.PENDING, error=None)
    return {"job_id": queue_optimization(name, admin["id"])}


@app.delete("/api/models/{name}")
def delete_model(name: str, _: dict = Depends(admin_user)):
    if not yolo_models.exists(name):
        raise HTTPException(404, "Modello non trovato")
    used = pipelines.using_model(name)
    if used:
        raise HTTPException(400, f"Il modello è usato dalle pipeline: {', '.join(used)}. Toglilo prima dai nodi.")
    yolo_models.delete_model(name)
    return {"ok": True}


# ---------------------------------------------------------------- pipeline YOLO

class PipelineIn(BaseModel):
    name: str
    description: str = ""
    tree: list[dict[str, Any]] = []
    version: int | None = None


@app.get("/api/pipelines")
def list_pipelines(_: dict = Depends(current_user)):
    return pipelines.list_all()


@app.get("/api/pipelines/{pipeline_id}")
def get_pipeline(pipeline_id: int, _: dict = Depends(current_user)):
    p = pipelines.get(pipeline_id)
    if not p:
        raise HTTPException(404, "Pipeline non trovata")
    return p


@app.post("/api/pipelines")
def create_pipeline(body: PipelineIn, admin: dict = Depends(admin_user)):
    try:
        return pipelines.create(body.name, body.description, body.tree, admin["id"])
    except pipelines.PipelineError as e:
        raise HTTPException(400, str(e)) from None


@app.put("/api/pipelines/{pipeline_id}")
def update_pipeline(pipeline_id: int, body: PipelineIn, _: dict = Depends(admin_user)):
    if body.version is None:
        raise HTTPException(400, "Versione mancante")
    try:
        return pipelines.update(pipeline_id, body.name, body.description, body.tree, body.version)
    except pipelines.PipelineError as e:
        raise HTTPException(400, str(e)) from None
    except pipelines.VersionConflict:
        raise HTTPException(409, "Qualcun altro ha modificato questa pipeline nel frattempo: ricarica la pagina") from None
    except KeyError:
        raise HTTPException(404, "Pipeline non trovata") from None


@app.delete("/api/pipelines/{pipeline_id}")
def delete_pipeline(pipeline_id: int, _: dict = Depends(admin_user)):
    if not pipelines.delete(pipeline_id):
        raise HTTPException(404, "Pipeline non trovata")
    return {"ok": True}


# ---------------------------------------------------------------- stato del sistema

@app.get("/api/system")
def system_status(_: dict = Depends(current_user)):
    models = ollama.installed_models()
    return {
        **system.status(),
        "version": __version__,
        "queued": db.count_queued(),
        "running": worker.current_job is not None,
        "ollama": {"running": models is not None, "models": models or [],
                   "llm_model": settings.llm_model, "embed_model": settings.embed_model},
        "cuda": yolo_models.cuda_available(),
        "tensorrt": yolo_models.tensorrt_version() if yolo_models.tensorrt_enabled() else None,
    }


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- interfaccia web

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
