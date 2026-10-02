"""Compito interno: legge un PDF caricato (con OCR se serve) e ne prepara l'indice per la chat."""
from __future__ import annotations

import shutil
from typing import Any

from .. import db
from ..services import documents
from .base import JobCancelled, JobContext, Param, Task, TaskError


class IndexDocument(Task):
    id = "index_document"
    title = "Elaborazione documento"
    description = "Legge il documento e prepara la ricerca nel testo."
    hidden = True
    needs_file = False
    section = "docs"
    activity = "l'elaborazione di un documento"
    params = [
        Param("document_id", "Documento", "number", required=True),
        Param("ocr", "Lettura delle scansioni (OCR)", "select", default="automatica",
              choices=["automatica", "sempre", "mai"]),
    ]

    def run(self, ctx: JobContext) -> dict[str, Any]:
        doc_id = int(ctx.params["document_id"])
        if db.get_document(doc_id) is None:
            raise TaskError("Il documento non esiste più")
        try:
            meta = documents.build_index(doc_id, ctx.params.get("ocr") or "automatica",
                                         lambda p, m: ctx.progress(p, m), ctx.check_cancelled)
        except JobCancelled:
            if db.get_document(doc_id) is not None:
                db.update_document(doc_id, status="failed", error="Elaborazione annullata")
            raise
        except documents.DocumentError as e:
            db.update_document(doc_id, status="failed", error=str(e))
            raise TaskError(str(e)) from None
        except Exception as e:
            db.update_document(doc_id, status="failed", error=f"{type(e).__name__}: {e}")
            raise
        if db.get_document(doc_id) is None:  # eliminato mentre veniva elaborato
            shutil.rmtree(documents.doc_dir(doc_id), ignore_errors=True)
            raise JobCancelled()
        db.update_document(doc_id, status="ready", error=None, pages=meta["pages"], ocr_pages=meta["ocr_pages"],
                           notes=meta["notes"], embed_model=meta["embed_model"])
        return {"summary": f"Documento elaborato: {meta['pages']} pagine, {meta['chunks']} parti.", "notes": meta["notes"]}
