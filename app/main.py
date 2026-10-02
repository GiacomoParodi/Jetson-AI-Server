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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from . import __version__, db
from .config import settings
from .security import hash_password, hash_token, login_limiter, new_token, validate_new_password, verify_password
from .services import chat, documents, ollama, pipelines, system, yolo_models
from .services.gate import gate
from .tasks import TaskError, registry
from .worker import (check_models_on_startup, input_dir, job_dir, output_dir, queue_document_index,
                     queue_optimization, worker)

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


app = FastAPI(
    title="Jetson AI Server", version=__version__, lifespan=lifespan,
    docs_url="/docs" if settings.api_docs else None, redoc_url=None,
    openapi_url="/openapi.json" if settings.api_docs else None,
)


# ---------------------------------------------------------------- intestazioni di sicurezza

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; media-src 'self' data: blob:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}


class SecurityHeaders:
    """Aggiunge a ogni risposta le intestazioni che chiedono al browser di difendersi: niente
    pagina incorporata in altri siti, niente script esterni, niente tipi di file indovinati.
    È un middleware ASGI puro, così non interferisce con le risposte in streaming."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        https = scope.get("scheme") == "https"
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                defaults = {
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                    "Cross-Origin-Opener-Policy": "same-origin",
                }
                if path not in DOCS_PATHS:
                    defaults["Content-Security-Policy"] = CSP
                if https:
                    defaults["Strict-Transport-Security"] = "max-age=15552000"
                if path.startswith("/api/"):
                    defaults["Cache-Control"] = "no-store"
                for name, value in defaults.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


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
    key = f"{request.client.host if request.client else '?'}|{body.username.strip().lower()}"
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
    for doc in db.list_documents(user_id):
        documents.delete_files(doc["id"])
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
    out["section"] = task.section if task else ""
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
def list_jobs(all: bool = False, limit: int = 100, section: str | None = None,
              user: dict = Depends(current_user)):
    """Lavori dell'utente (di tutti per un amministratore con all=true). Con `section` solo quelli
    di un'area (es. "video"), senza i lavori interni."""
    owner = None if (all and user["is_admin"]) else user["id"]
    tasks = None
    if section is not None:
        tasks = [t.id for t in registry.all() if t.section == section and not t.hidden]
    return [_job_out(j) for j in db.list_jobs(owner, limit=min(limit, 500), tasks=tasks)]


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
    """Nuova analisi sullo stesso file con parametri diversi (es. un'altra pipeline)."""
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


# ---------------------------------------------------------------- modelli LLM (Ollama)

@app.get("/api/llm")
def llm_status(_: dict = Depends(current_user)):
    installed = ollama.installed_details()
    return {
        "running": installed is not None,
        "installed": installed or [],
        "llm_model": ollama.selected_llm(),
        "embed_model": ollama.selected_embed(),
        "context": ollama.selected_context(),
        "context_choices": ollama.CONTEXT_CHOICES,
        "pulls": ollama.pull_status(),
    }


class LlmPullIn(BaseModel):
    name: str


@app.post("/api/llm/pull")
def llm_pull(body: LlmPullIn, _: dict = Depends(admin_user)):
    name = body.name.strip()
    if not ollama.valid_name(name):
        raise HTTPException(400, "Nome del modello non valido (es. qwen2.5:3b)")
    if ollama.installed_models() is None:
        raise HTTPException(503, "Ollama non è in esecuzione")
    ollama.start_pull(name)
    return {"ok": True}


class LlmSelectIn(BaseModel):
    llm_model: str = ""
    embed_model: str = ""
    context: int = 4096


@app.post("/api/llm/select")
def llm_select(body: LlmSelectIn, _: dict = Depends(admin_user)):
    installed = ollama.installed_models()
    if installed is None:
        raise HTTPException(503, "Ollama non è in esecuzione")
    for value, label in ((body.llm_model, "LLM"), (body.embed_model, "di embedding")):
        if value and not ollama.has_model(value, installed):
            raise HTTPException(400, f"Il modello {label} '{value}' non è scaricato")
    if body.context not in ollama.CONTEXT_CHOICES:
        raise HTTPException(400, "Dimensione del contesto non valida")
    ollama.select(body.llm_model, body.embed_model, body.context)
    return {"ok": True}


@app.delete("/api/llm/models/{name:path}")
def llm_delete(name: str, _: dict = Depends(admin_user)):
    if not ollama.valid_name(name):
        raise HTTPException(400, "Nome non valido")
    if name in (ollama.selected_llm(), ollama.selected_embed()):
        raise HTTPException(400, "Il modello è quello scelto in uso: scegline un altro prima di eliminarlo")
    try:
        ollama.delete_model(name)
    except ollama.OllamaError as e:
        raise HTTPException(400, str(e)) from None
    except Exception:
        raise HTTPException(503, "Ollama non è in esecuzione") from None
    return {"ok": True}


# ---------------------------------------------------------------- lettore documenti

DOC_OCR_MODES = ("automatica", "sempre", "mai")


def _doc_for(doc_id: int, user: dict) -> dict[str, Any]:
    doc = db.get_document(doc_id)
    if not doc or (doc["user_id"] != user["id"] and not user["is_admin"]):
        raise HTTPException(404, "Documento non trovato")
    return doc


def _doc_out(doc: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "filename", "size_bytes", "pages", "ocr_pages", "status", "error", "notes",
            "embed_model", "created_at", "updated_at", "username", "conversation_count", "last_activity")
    out = {k: doc[k] for k in keys}
    out["semantic"] = bool(doc["embed_model"])
    # L'indice è stato creato con un modello di embedding diverso da quello scelto ora.
    out["semantic_outdated"] = (doc["embed_model"] or "") != ollama.selected_embed()
    if doc["status"] == "processing" and doc["job_id"]:
        job = db.get_job(doc["job_id"])
        if job:
            out["progress"] = job["progress"] if job["status"] == "running" else 0
            out["message"] = job["message"] if job["status"] == "running" else "In coda"
    return out


@app.get("/api/documents")
def list_documents(all: bool = False, user: dict = Depends(current_user)):
    owner = None if (all and user["is_admin"]) else user["id"]
    return [_doc_out(d) for d in db.list_documents(owner)]


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...), ocr: str = Form("automatica"),
                          user: dict = Depends(current_user)):
    name = _safe_filename(file.filename or "")
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Carica un file PDF")
    if ocr not in DOC_OCR_MODES:
        ocr = "automatica"
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.documents_dir / f".upload-{time.time_ns()}.pdf"
    try:
        await _save_upload(file, tmp)
        pages = await run_in_threadpool(documents.inspect_pdf, tmp)
    except documents.DocumentError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    title = Path(name).stem.strip()[:120] or "Documento"
    doc_id = db.create_document(user["id"], title, name, tmp.stat().st_size, pages)
    documents.doc_dir(doc_id).mkdir(parents=True, exist_ok=True)
    tmp.replace(documents.pdf_path(doc_id))
    queue_document_index(doc_id, user["id"], ocr)
    return _doc_out(db.get_document(doc_id))


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, user: dict = Depends(current_user)):
    return _doc_out(_doc_for(doc_id, user))


class TitleIn(BaseModel):
    title: str


@app.patch("/api/documents/{doc_id}")
def rename_document(doc_id: int, body: TitleIn, user: dict = Depends(current_user)):
    _doc_for(doc_id, user)
    title = " ".join(body.title.split())[:120]
    if not title:
        raise HTTPException(400, "Il nome non può essere vuoto")
    db.update_document(doc_id, title=title)
    return _doc_out(db.get_document(doc_id))


def _cancel_document_job(doc: dict[str, Any]) -> None:
    if doc["status"] == "processing" and doc["job_id"]:
        if not db.cancel_if_queued(doc["job_id"]):
            worker.cancel(doc["job_id"])


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, user: dict = Depends(current_user)):
    doc = _doc_for(doc_id, user)
    _cancel_document_job(doc)
    db.delete_document(doc_id)
    documents.delete_files(doc_id)
    return {"ok": True}


@app.post("/api/documents/{doc_id}/reindex")
def reindex_document(doc_id: int, user: dict = Depends(current_user)):
    doc = _doc_for(doc_id, user)
    if doc["status"] == "processing":
        raise HTTPException(400, "Il documento è già in elaborazione")
    if not documents.pdf_path(doc_id).is_file():
        raise HTTPException(400, "Il file originale non è più disponibile")
    db.update_document(doc_id, status="processing", error=None)
    queue_document_index(doc_id, doc["user_id"])
    return _doc_out(db.get_document(doc_id))


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int, user: dict = Depends(current_user)):
    doc = _doc_for(doc_id, user)
    return FileResponse(documents.pdf_path(doc_id), media_type="application/pdf", filename=doc["filename"])


@app.get("/api/documents/{doc_id}/text")
def document_text(doc_id: int, user: dict = Depends(current_user)):
    doc = _doc_for(doc_id, user)
    path = documents.full_text_path(doc_id)
    if not path.is_file():
        raise HTTPException(404, "Il testo non è ancora disponibile")
    return FileResponse(path, media_type="text/plain", filename=f"{doc['title']}.txt")


@app.get("/api/documents/{doc_id}/pages/{page}")
async def document_page(doc_id: int, page: int, w: int = 900, user: dict = Depends(current_user)):
    _doc_for(doc_id, user)
    try:
        data = await run_in_threadpool(documents.render_page, doc_id, page, w)
    except documents.DocumentError as e:
        raise HTTPException(404, str(e)) from None
    except Exception:
        raise HTTPException(404, "Pagina non disponibile") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------- conversazioni

def _conv_for(conv_id: int, user: dict) -> dict[str, Any]:
    conv = db.get_conversation(conv_id)
    if not conv or (conv["user_id"] != user["id"] and not user["is_admin"]):
        raise HTTPException(404, "Conversazione non trovata")
    return conv


def _conv_out(conv: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "document_id", "document_title", "title", "created_at", "updated_at", "message_count",
            "last_question", "username")
    return {k: conv.get(k) for k in keys}


@app.get("/api/documents/{doc_id}/conversations")
def document_conversations(doc_id: int, user: dict = Depends(current_user)):
    _doc_for(doc_id, user)
    owner = None if user["is_admin"] else user["id"]
    return [_conv_out(c) for c in db.list_conversations(document_id=doc_id, user_id=owner)]


@app.post("/api/documents/{doc_id}/conversations")
def create_conversation(doc_id: int, user: dict = Depends(current_user)):
    _doc_for(doc_id, user)
    conv_id = db.create_conversation(doc_id, user["id"], chat.DEFAULT_TITLE)
    return _conv_out(db.get_conversation(conv_id))


@app.get("/api/conversations")
def list_conversations(all: bool = False, limit: int = 300, user: dict = Depends(current_user)):
    owner = None if (all and user["is_admin"]) else user["id"]
    return [_conv_out(c) for c in db.list_conversations(user_id=owner, limit=min(limit, 1000))]


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: int, user: dict = Depends(current_user)):
    conv = _conv_for(conv_id, user)
    return {**_conv_out(conv), "messages": db.list_messages(conv_id)}


@app.patch("/api/conversations/{conv_id}")
def rename_conversation(conv_id: int, body: TitleIn, user: dict = Depends(current_user)):
    _conv_for(conv_id, user)
    title = " ".join(body.title.split())[:120]
    if not title:
        raise HTTPException(400, "Il titolo non può essere vuoto")
    db.update_conversation(conv_id, title=title, updated_at=db.get_conversation(conv_id)["updated_at"])
    return _conv_out(db.get_conversation(conv_id))


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: dict = Depends(current_user)):
    _conv_for(conv_id, user)
    db.delete_conversation(conv_id)
    return {"ok": True}


_streaming: set[int] = set()
_streaming_lock = threading.Lock()


class MessageIn(BaseModel):
    content: str


@app.post("/api/conversations/{conv_id}/messages")
def send_message(conv_id: int, body: MessageIn, user: dict = Depends(current_user)):
    """Invia una domanda e riceve la risposta in streaming (Server-Sent Events, una riga `data: {json}` per evento)."""
    conv = _conv_for(conv_id, user)
    doc = db.get_document(conv["document_id"])
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "Scrivi una domanda")
    if len(content) > chat.MAX_QUESTION_CHARS:
        raise HTTPException(400, f"Domanda troppo lunga (massimo {chat.MAX_QUESTION_CHARS} caratteri)")
    if not doc or doc["status"] != "ready":
        raise HTTPException(409, "Il documento non è ancora pronto: attendi la fine dell'elaborazione")

    def events():
        with _streaming_lock:
            busy = conv_id in _streaming
            _streaming.add(conv_id)
        if busy:
            yield f"data: {json.dumps({'type': 'error', 'detail': 'In questa conversazione è già in corso una risposta'}, ensure_ascii=False)}\n\n"
            return
        try:
            for event in chat.run_turn(conv, doc, content):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            with _streaming_lock:
                _streaming.discard(conv_id)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- panoramica

@app.get("/api/overview")
def overview(user: dict = Depends(current_user)):
    uid = user["id"]
    docs = db.list_documents(uid)
    video_tasks = [t.id for t in registry.all() if t.section == "video" and not t.hidden]
    analyses = db.list_jobs(uid, limit=100000, tasks=video_tasks)
    others = [t.describe() for t in registry.all() if not t.section and not t.hidden]
    return {
        "documents": {
            "count": len(docs),
            "processing": sum(1 for d in docs if d["status"] == "processing"),
            "conversations": db.count_conversations(uid),
            "llm": ollama.selected_llm(),
        },
        "video": {
            "analyses": len(analyses),
            "active": sum(1 for j in analyses if j["status"] in ("queued", "running")),
            "pipelines": len(pipelines.list_all()),
            "models": len(yolo_models.list_models()),
        },
        "other_tools": others,
    }


# ---------------------------------------------------------------- stato del sistema

@app.get("/api/system")
def system_status(_: dict = Depends(current_user)):
    models = ollama.installed_models()
    return {
        **system.status(),
        "version": __version__,
        "queued": db.count_queued(),
        "running": worker.current_job is not None,
        "busy_with": (gate.holder.label if gate.holder else None),
        "waiting": gate.waiting,
        "ollama": {"running": models is not None, "models": models or [],
                   "llm_model": ollama.selected_llm(), "embed_model": ollama.selected_embed()},
        "cuda": yolo_models.cuda_available(),
        "tensorrt": yolo_models.tensorrt_version() if yolo_models.tensorrt_enabled() else None,
    }


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- interfaccia web

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)  # l'icona è nel <head> della pagina


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
