"""Documenti PDF: archivio su disco, indicizzazione (testo, OCR, embedding) e ricerca.

Ogni documento ha una cartella data/documents/<id>/ con:
  original.pdf          il file caricato
  index/                testo a pezzi (chunks.json), testo intero (text.txt),
                        vettori per la ricerca semantica (embeddings.npy, facoltativi), meta.json
  pages/                immagini delle pagine già generate (cache del visualizzatore)
"""
from __future__ import annotations

import json
import math
import re
import shutil
import threading
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from . import ollama

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 200
MIN_TEXT_FOR_NO_OCR = 50
MIN_EXCERPT_CHARS = 1500

Progress = Callable[[float, str], None]


class DocumentError(Exception):
    """Errore da mostrare all'utente così com'è."""


# ---------------------------------------------------------------- percorsi

def doc_dir(doc_id: int) -> Path:
    return settings.documents_dir / str(doc_id)


def pdf_path(doc_id: int) -> Path:
    return doc_dir(doc_id) / "original.pdf"


def index_dir(doc_id: int) -> Path:
    return doc_dir(doc_id) / "index"


def pages_dir(doc_id: int) -> Path:
    return doc_dir(doc_id) / "pages"


def delete_files(doc_id: int) -> None:
    _cache.pop(doc_id, None)
    shutil.rmtree(doc_dir(doc_id), ignore_errors=True)


# MuPDF non è sicuro da usare da più thread insieme.
_pdf_lock = threading.Lock()


def inspect_pdf(path: Path) -> int:
    """Controlla che il file sia un PDF leggibile e restituisce il numero di pagine."""
    import pymupdf

    with _pdf_lock:
        try:
            doc = pymupdf.open(path)
        except Exception:
            raise DocumentError("Il file non è un PDF valido") from None
        try:
            if doc.needs_pass:
                raise DocumentError("Il PDF è protetto da password")
            if len(doc) == 0:
                raise DocumentError("Il PDF non contiene pagine")
            return len(doc)
        finally:
            doc.close()


# ---------------------------------------------------------------- estrazione e indicizzazione

def _ocr_page(page) -> str:
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=200)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return pytesseract.image_to_string(image, lang=settings.ocr_languages)


def extract_pages(pdf: Path, ocr_mode: str, progress: Progress, check_cancelled: Callable[[], None],
                  notes: list[str]) -> tuple[list[str], int]:
    """Testo di ogni pagina; per le pagine senza testo (scansioni) usa l'OCR."""
    import pymupdf

    ocr_ok = shutil.which("tesseract") is not None
    if ocr_mode != "mai" and not ocr_ok:
        notes.append("Tesseract non è installato: le pagine scansionate non possono essere lette.")
    pages: list[str] = []
    ocr_pages = 0
    with _pdf_lock:
        try:
            doc = pymupdf.open(pdf)
        except Exception:
            raise DocumentError("Il file non è un PDF valido") from None
        try:
            n = len(doc)
            for i, page in enumerate(doc):
                check_cancelled()
                text = page.get_text("text")
                use_ocr = ocr_ok and (ocr_mode == "sempre" or (ocr_mode == "automatica" and len(text.strip()) < MIN_TEXT_FOR_NO_OCR))
                if use_ocr:
                    progress(0.8 * i / n, f"Lettura con OCR della pagina {i + 1} di {n}…")
                    try:
                        ocr_text = _ocr_page(page)
                        if len(ocr_text.strip()) > len(text.strip()):
                            text, ocr_pages = ocr_text, ocr_pages + 1
                    except Exception as e:
                        notes.append(f"OCR non riuscito a pagina {i + 1}: {e}")
                else:
                    progress(0.8 * i / n, f"Lettura della pagina {i + 1} di {n}…")
                pages.append(text)
        finally:
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


def doc_prefixes(model: str) -> tuple[str, str]:
    """Alcuni modelli di embedding rendono meglio con un prefisso diverso per documenti e domande."""
    if "nomic" in model:
        return "search_document: ", "search_query: "
    if "e5" in model:
        return "passage: ", "query: "
    return "", ""


def build_index(doc_id: int, ocr_mode: str, progress: Progress, check_cancelled: Callable[[], None]) -> dict[str, Any]:
    """Legge il PDF e prepara l'indice per la ricerca. Sostituisce quello precedente solo a lavoro finito."""
    notes: list[str] = []
    pages, ocr_pages = extract_pages(pdf_path(doc_id), ocr_mode, progress, check_cancelled, notes)
    chunks = chunk_pages(pages)
    if not chunks:
        raise DocumentError("Non ho trovato testo nel documento, nemmeno con l'OCR")

    tmp = doc_dir(doc_id) / "index.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    (tmp / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    (tmp / "text.txt").write_text(
        "\n\n".join(f"===== Pagina {i} =====\n{t.strip()}" for i, t in enumerate(pages, start=1)), encoding="utf-8")

    embed_model = ollama.selected_embed()
    used_embed: str | None = None
    if embed_model:
        progress(0.82, "Indicizzazione semantica…")
        try:
            import numpy as np

            prefix, _ = doc_prefixes(embed_model)
            texts = [prefix + c["text"] for c in chunks]
            vectors: list[list[float]] = []
            step = 32
            for i in range(0, len(texts), step):
                check_cancelled()
                vectors.extend(ollama.embed(texts[i:i + step], model=embed_model))
                progress(0.82 + 0.16 * min(1.0, (i + step) / len(texts)), "Indicizzazione semantica…")
            matrix = np.array(vectors, dtype=np.float32)
            matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
            np.save(tmp / "embeddings.npy", matrix)
            used_embed = embed_model
        except ollama.OllamaError as e:
            notes.append(f"Ricerca semantica non disponibile ({e}): uso le parole chiave.")
        except Exception as e:  # numpy mancante, memoria, ecc.
            notes.append(f"Ricerca semantica non disponibile ({type(e).__name__}): uso le parole chiave.")

    meta = {"pages": len(pages), "ocr_pages": ocr_pages, "chars": sum(len(c["text"]) for c in chunks),
            "chunks": len(chunks), "embed_model": used_embed, "notes": notes}
    (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    out = index_dir(doc_id)
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    _cache.pop(doc_id, None)
    return meta


# ---------------------------------------------------------------- indice in memoria e ricerca

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if len(w) > 2]


class Index:
    def __init__(self, doc_id: int, meta: dict[str, Any], chunks: list[dict[str, Any]], vectors) -> None:
        self.doc_id = doc_id
        self.meta = meta
        self.chunks = chunks
        self.vectors = vectors
        self._tf = [Counter(_tokens(c["text"])) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = sum(self._len) / max(1, len(self._len))
        self._df: Counter[str] = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())

    @property
    def total_chars(self) -> int:
        return int(self.meta.get("chars") or sum(len(c["text"]) for c in self.chunks))

    def bm25(self, query: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
        n = len(self.chunks)
        terms = set(_tokens(query))
        scores = []
        for tf, length in zip(self._tf, self._len):
            s = 0.0
            for term in terms:
                f = tf.get(term)
                if not f:
                    continue
                df = self._df[term]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / (self._avg or 1)))
            scores.append(s)
        return scores


_cache: "OrderedDict[int, tuple[float, Index]]" = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 6


def load_index(doc_id: int) -> Index | None:
    meta_file = index_dir(doc_id) / "meta.json"
    try:
        mtime = meta_file.stat().st_mtime
    except OSError:
        return None
    with _cache_lock:
        hit = _cache.get(doc_id)
        if hit and hit[0] == mtime:
            _cache.move_to_end(doc_id)
            return hit[1]
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    chunks = json.loads((index_dir(doc_id) / "chunks.json").read_text(encoding="utf-8"))
    vectors = None
    emb = index_dir(doc_id) / "embeddings.npy"
    if meta.get("embed_model") and emb.is_file():
        import numpy as np

        vectors = np.load(emb)
    index = Index(doc_id, meta, chunks, vectors)
    with _cache_lock:
        _cache[doc_id] = (mtime, index)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return index


def full_text_path(doc_id: int) -> Path:
    return index_dir(doc_id) / "text.txt"


def _normalize(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def excerpt_budget(context_tokens: int) -> int:
    """Caratteri di estratti da dare all'LLM: circa metà della finestra di contesto
    (stima prudente di 3 caratteri per token). Il resto serve a storia, domanda e risposta."""
    return max(MIN_EXCERPT_CHARS, int(context_tokens * 3 * 0.5))


def retrieve(index: Index, question: str, k: int, notes: list[str]) -> tuple[list[dict[str, Any]], str]:
    """Pezzi più pertinenti, nell'ordine del documento, e il metodo usato ("semantica" o "parole chiave")."""
    chunks = index.chunks
    keyword = _normalize(index.bm25(question))
    combined, mode = keyword, "parole chiave"
    embed_model = index.meta.get("embed_model")
    if embed_model and index.vectors is not None:
        if ollama.selected_embed() == embed_model:
            try:
                import numpy as np

                _, query_prefix = doc_prefixes(embed_model)
                q = np.array(ollama.embed([query_prefix + question], model=embed_model)[0], dtype=np.float32)
                q /= np.linalg.norm(q) + 1e-9
                semantic = _normalize((index.vectors @ q).tolist())
                combined = [0.7 * s + 0.3 * kw for s, kw in zip(semantic, keyword)]
                mode = "semantica"
            except Exception as e:
                notes.append(f"Ricerca semantica non riuscita ({e}): uso le parole chiave.")
        else:
            notes.append("Il documento è indicizzato con un altro modello di embedding: uso le parole chiave. "
                         "Usa «Reindicizza» per aggiornarlo.")
    best = sorted(range(len(chunks)), key=lambda i: -combined[i])[:k]
    return [chunks[i] for i in sorted(best)], mode


def spread(index: Index, k: int) -> list[dict[str, Any]]:
    """Pezzi distribuiti in modo uniforme lungo il documento (per i riassunti)."""
    n = len(index.chunks)
    if k >= n:
        return list(index.chunks)
    picks = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)}) if k > 1 else [0]
    return [index.chunks[i] for i in picks]


_SUMMARY_RE = re.compile(
    r"\b(riassum\w*|riassunto|sintesi|sintetizz\w*|panoramica|di cosa (parla|tratta)|punti principali|"
    r"argomenti principali|in breve|summar\w*)\b", re.IGNORECASE)


def is_summary_request(question: str) -> bool:
    return bool(_SUMMARY_RE.search(question))


# ---------------------------------------------------------------- immagini delle pagine

def render_page(doc_id: int, page: int, width: int = 900) -> bytes:
    """Immagine PNG di una pagina (per il visualizzatore); si conserva su disco."""
    import pymupdf

    width = max(160, min(1800, int(width)))
    cache_file = pages_dir(doc_id) / f"{page}-{width}.png"
    if cache_file.is_file():
        return cache_file.read_bytes()
    with _pdf_lock:
        doc = pymupdf.open(pdf_path(doc_id))
        try:
            if not 1 <= page <= len(doc):
                raise DocumentError("Pagina inesistente")
            pg = doc[page - 1]
            zoom = width / pg.rect.width
            data = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
        finally:
            doc.close()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(data)
    return data
