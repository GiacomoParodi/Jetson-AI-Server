"""Configurazione del server, letta da variabili d'ambiente (prefisso JAS_).

In produzione le variabili arrivano dal file .env tramite systemd (EnvironmentFile).
In sviluppo il file .env nella cartella del progetto viene letto automaticamente.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT_DIR / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(_env("JAS_DATA_DIR", str(ROOT_DIR / "data"))).resolve()
        # Di norma il server ascolta solo sul dispositivo stesso (127.0.0.1) e viene reso
        # raggiungibile da fuori solo tramite Tailscale. 0.0.0.0 lo apre a tutta la rete locale.
        self.host = _env("JAS_HOST", "127.0.0.1")
        self.port = int(_env("JAS_PORT", "8000"))
        self.max_upload_mb = int(_env("JAS_MAX_UPLOAD_MB", "4096"))
        self.session_hours = int(_env("JAS_SESSION_HOURS", "168"))
        # Documentazione interattiva dell'API (/docs): spenta di default, è visibile anche senza accesso.
        self.api_docs = _env("JAS_API_DOCS", "off").lower() in ("on", "1", "true", "yes", "si", "sì")

        # LLM locale tramite Ollama
        self.ollama_url = _env("JAS_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        # I modelli si scelgono da Lettore documenti → Modelli linguistici; questi valori servono solo
        # come scelta iniziale se impostati nel file .env.
        self.llm_model = _env("JAS_LLM_MODEL", "")
        self.embed_model = _env("JAS_EMBED_MODEL", "")
        self.llm_context = int(_env("JAS_LLM_CONTEXT", "4096"))
        self.ollama_keep_alive = _env("JAS_OLLAMA_KEEP_ALIVE", "5m")

        # YOLO: uso di TensorRT (auto = se disponibile)
        self.tensorrt = _env("JAS_TENSORRT", "auto").lower()  # auto | on | off

        # Limiti delle pipeline YOLO (solo protezione da alberi enormi per errore)
        self.pipeline_max_depth = int(_env("JAS_PIPELINE_MAX_DEPTH", "20"))
        self.pipeline_max_nodes = int(_env("JAS_PIPELINE_MAX_NODES", "200"))

        # Chat, analisi video e indicizzazione di norma si mettono in fila per non esaurire
        # la memoria (8 GB condivisi). Con "on" possono girare insieme.
        self.concurrent_ai = _env("JAS_CONCURRENT_AI", "off").lower() in ("on", "1", "true", "yes", "si", "sì")

        # OCR (lingue Tesseract)
        self.ocr_languages = _env("JAS_OCR_LANG", "ita+eng")

        # Cartelle aggiuntive da cui caricare plugin (separate da ':')
        self.plugin_dirs = [Path(p) for p in _env("JAS_PLUGIN_DIRS", str(ROOT_DIR / "plugins")).split(":") if p]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "server.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models" / "yolo"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.jobs_dir, self.models_dir, self.cache_dir, self.documents_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
