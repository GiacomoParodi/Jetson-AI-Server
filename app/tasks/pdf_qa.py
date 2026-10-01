"""Domande e risposte su un documento PDF con un LLM locale (Ollama).

Il testo viene estratto dal PDF (con OCR per le pagine scansionate), diviso in
pezzi e indicizzato una sola volta per file. A ogni domanda si cercano i pezzi
più pertinenti e si passano all'LLM, che risponde citando le pagine.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import settings
from ..services import ollama
from .base import JobContext, Param, Task, TaskError

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 200
TOP_K = 6
# Margine per domanda e risposta: il documento intero va all'LLM solo se occupa
# al massimo ~60% del contesto (circa 2,5 caratteri per token in italiano, stima prudente).


def full_doc_chars() -> int:
    """Sotto questa soglia (in caratteri) l'intero documento entra nel contesto dell'LLM."""
    return int(ollama.selected_context() * 1.5)
MIN_TEXT_FOR_NO_OCR = 50

SYSTEM_PROMPT = (
    "Sei un assistente che risponde a domande su un documento. "
    "Usa solo le informazioni negli estratti forniti. Se la risposta non c'è, dillo chiaramente "
    "invece di inventare. Cita le pagine da cui prendi le informazioni nel formato (p. N). "
    "Rispondi nella stessa lingua della domanda, in modo chiaro e conciso."
)


class PdfQA(Task):
    id = "pdf_qa"
    title = "Domande su un documento PDF"
    description = (
        "Carica un PDF (anche scansionato) e fai una domanda: un modello linguistico locale "
        "risponde basandosi sul contenuto e cita le pagine. Il documento non lascia il server."
    )
    accept = [".pdf"]
    params = [
        Param("question", "Domanda", "textarea", required=True,
              help="Es. 'Qual è la scadenza del contratto?' oppure 'Riassumi il documento'"),
        Param("ocr", "Lettura del testo (OCR)", "select", default="automatica",
              choices=["automatica", "sempre", "mai"],
              help="Automatica: usa l'OCR solo sulle pagine senza testo (scansioni)"),
    ]
    rerun_label = "Fai un'altra domanda"

    def available(self) -> tuple[bool, str]:
        try:
            import pymupdf  # noqa: F401
        except ImportError:
            return False, "Libreria mancante: pymupdf"
        models = ollama.installed_models()
        if models is None:
            return False, "Ollama non è in esecuzione"
        llm = ollama.selected_llm()
        if not llm:
            return False, "Nessun modello LLM scelto: un amministratore deve sceglierlo nella pagina Modelli"
        if not ollama.has_model(llm, models):
            return False, f"Il modello LLM scelto ({llm}) non è scaricato: scaricalo dalla pagina Modelli"
        return True, ""

    def run(self, ctx: JobContext) -> dict[str, Any]:
        assert ctx.input_path is not None
        question = ctx.params["question"].strip()
        notes: list[str] = []

        index = load_or_build_index(ctx, ctx.input_path, ctx.params.get("ocr", "automatica"), notes)
        chunks = index["chunks"]
        if not chunks:
            raise TaskError("Non ho trovato testo nel documento (nemmeno con l'OCR)")
        shutil.copy(index["text_path"], ctx.output_dir / "testo_estratto.txt")

        ctx.check_cancelled()
        ctx.progress(0.7, "Ricerca delle parti pertinenti…", force=True)
        total_chars = sum(len(c["text"]) for c in chunks)
        full_doc = total_chars <= full_doc_chars()
        if full_doc:
            selected = chunks
            notes.append("Documento breve: il modello lo ha letto per intero.")
        else:
            selected = retrieve(question, index, notes)

        context = "\n\n".join(f"[Estratto {i + 1} — pagina {c['page']}]\n{c['text']}" for i, c in enumerate(selected))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Estratti dal documento:\n\n{context}\n\nDomanda: {question}"},
        ]

        ctx.progress(0.8, "Il modello sta scrivendo la risposta…", force=True)
        produced = 0

        def on_token(piece: str) -> None:
            nonlocal produced
            ctx.check_cancelled()
            produced += len(piece)
            ctx.progress(min(0.99, 0.8 + produced / 8000), "Il modello sta scrivendo la risposta…")

        try:
            answer = ollama.chat(messages, on_token=on_token)
            # Alcuni modelli (es. Qwen3 "thinking") scrivono il ragionamento tra <think>…</think>.
            answer = re.sub(r"<think>.*?(</think>|$)", "", answer, flags=re.S)
        except ollama.OllamaError as e:
            raise TaskError(str(e)) from None

        return {
            "summary": answer.strip() or "(Il modello non ha prodotto una risposta)",
            "sources": [] if full_doc else [{"label": f"Pagina {c['page']}", "text": c["text"]} for c in selected],
            "outputs": [{"file": "testo_estratto.txt", "kind": "file", "label": "Testo estratto dal PDF"}],
            "notes": notes,
            "meta": {"pages": index["pages"], "ocr_pages": index["ocr_pages"], "model": ollama.selected_llm()},
        }


# ---------------------------------------------------------------- estrazione

def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _ocr_page(page) -> str:
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=200)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return pytesseract.image_to_string(image, lang=settings.ocr_languages)


def extract_pages(ctx: JobContext, pdf: Path, ocr_mode: str, notes: list[str]) -> tuple[list[str], int]:
    import pymupdf

    try:
        doc = pymupdf.open(pdf)
    except Exception:
        raise TaskError("Il file non è un PDF valido") from None
    if doc.needs_pass:
        raise TaskError("Il PDF è protetto da password")

    ocr_ok = shutil.which("tesseract") is not None
    if ocr_mode != "mai" and not ocr_ok:
        notes.append("Tesseract non è installato: le pagine scansionate non possono essere lette.")

    pages: list[str] = []
    ocr_pages = 0
    n = len(doc)
    for i, page in enumerate(doc):
        ctx.check_cancelled()
        text = page.get_text("text")
        use_ocr = ocr_ok and (ocr_mode == "sempre" or (ocr_mode == "automatica" and len(text.strip()) < MIN_TEXT_FOR_NO_OCR))
        if use_ocr:
            ctx.progress(0.6 * i / n, f"OCR della pagina {i + 1} di {n}…")
            try:
                ocr_text = _ocr_page(page)
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
                    ocr_pages += 1
            except Exception as e:
                notes.append(f"OCR non riuscito a pagina {i + 1}: {e}")
        else:
            ctx.progress(0.6 * i / n, f"Lettura della pagina {i + 1} di {n}…")
        pages.append(text)
    doc.close()
    return pages, ocr_pages


def chunk_pages(pages: list[str]) -> list[dict[str, Any]]:
    chunks = []
    for page_no, text in enumerate(pages, start=1):
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        start = 0
        while start < len(text):
            end = min(len(text), start + CHUNK_CHARS)
            if end < len(text):
                # Preferisci tagliare a fine paragrafo o frase.
                cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
                if cut > start + CHUNK_CHARS // 2:
                    end = cut + 1
            piece = text[start:end].strip()
            if piece:
                chunks.append({"page": page_no, "text": piece})
            if end >= len(text):
                break
            start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _doc_prefix(model: str) -> tuple[str, str]:
    # nomic-embed-text rende meglio con questi prefissi di compito.
    if "nomic" in model:
        return "search_document: ", "search_query: "
    return "", ""


def load_or_build_index(ctx: JobContext, pdf: Path, ocr_mode: str, notes: list[str]) -> dict[str, Any]:
    """Indice del documento, salvato in cache: la seconda domanda sullo stesso PDF è immediata."""
    # Anche il modello di embedding fa parte della chiave: se lo cambi, l'indice si rifà.
    embed_key = re.sub(r"[^A-Za-z0-9._-]", "_", ollama.selected_embed() or "nessuno")
    key = f"{_file_hash(pdf)}-{ocr_mode}-{embed_key}"
    cache = ctx.cache_dir / "pdf" / key
    meta_path = cache / "index.json"
    text_path = cache / "testo.txt"

    if meta_path.is_file():
        index = json.loads(meta_path.read_text())
        notes.extend(index.get("build_notes", []))
    else:
        build_notes: list[str] = []
        pages, ocr_pages = extract_pages(ctx, pdf, ocr_mode, build_notes)
        chunks = chunk_pages(pages)
        index = {"pages": len(pages), "ocr_pages": ocr_pages, "chunks": chunks,
                 "embed_model": None, "build_notes": build_notes}
        cache.mkdir(parents=True, exist_ok=True)
        text_path.write_text(
            "\n\n".join(f"===== Pagina {i} =====\n{t.strip()}" for i, t in enumerate(pages, start=1)),
            encoding="utf-8",
        )
        embed_model = ollama.selected_embed()
        if chunks and sum(len(c["text"]) for c in chunks) > full_doc_chars() and not embed_model:
            build_notes.append("Nessun modello di embedding scelto: ricerca solo per parole chiave.")
        elif chunks and sum(len(c["text"]) for c in chunks) > full_doc_chars():
            ctx.progress(0.62, "Indicizzazione del documento…", force=True)
            try:
                import numpy as np

                doc_prefix, _ = _doc_prefix(embed_model)
                vectors = np.array(ollama.embed([doc_prefix + c["text"] for c in chunks], model=embed_model),
                                   dtype=np.float32)
                vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
                np.save(cache / "embeddings.npy", vectors)
                index["embed_model"] = embed_model
            except Exception as e:
                build_notes.append(
                    f"Ricerca semantica non disponibile ({e}); uso la ricerca per parole chiave."
                )
        meta_path.write_text(json.dumps(index))
        notes.extend(build_notes)

    index["text_path"] = str(text_path)
    index["cache"] = str(cache)
    return index


# ---------------------------------------------------------------- ricerca

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if len(w) > 2]


def bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    tokenized = [_tokens(d) for d in docs]
    avg_len = sum(len(t) for t in tokenized) / max(1, len(tokenized))
    df: Counter[str] = Counter()
    for t in tokenized:
        df.update(set(t))
    n = len(docs)
    scores = []
    q_terms = set(_tokens(query))
    for t in tokenized:
        tf = Counter(t)
        s = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(t) / (avg_len or 1)))
        scores.append(s)
    return scores


def _normalize(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def retrieve(question: str, index: dict[str, Any], notes: list[str], k: int = TOP_K) -> list[dict[str, Any]]:
    """Ricerca ibrida: somiglianza semantica (embedding) + parole chiave (BM25)."""
    chunks = index["chunks"]
    keyword = _normalize(bm25_scores(question, [c["text"] for c in chunks]))
    combined = keyword
    emb_file = Path(index["cache"]) / "embeddings.npy"
    if index.get("embed_model") and emb_file.is_file():
        try:
            import numpy as np

            _, query_prefix = _doc_prefix(index["embed_model"])
            q = np.array(ollama.embed([query_prefix + question], model=index["embed_model"])[0], dtype=np.float32)
            q /= np.linalg.norm(q) + 1e-9
            semantic = _normalize((np.load(emb_file) @ q).tolist())
            combined = [0.7 * s + 0.3 * kw for s, kw in zip(semantic, keyword)]
        except Exception as e:
            notes.append(f"Ricerca semantica non riuscita ({e}); uso le parole chiave.")
    best = sorted(range(len(chunks)), key=lambda i: -combined[i])[:k]
    # Presenta gli estratti nell'ordine del documento: l'LLM li legge meglio.
    return [chunks[i] for i in sorted(best)]
