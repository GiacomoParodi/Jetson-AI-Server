import pymupdf
import pytest

from app import db
from app.services import ollama
from app.tasks import JobContext, TaskError
from app.tasks import pdf_qa


@pytest.fixture(autouse=True)
def database():
    db.init()


def use_embedding(monkeypatch):
    monkeypatch.setattr(ollama, "selected_embed", lambda: "nomic-embed-text")


def make_pdf(path, pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), text, fontsize=10)
    doc.save(path)


def ctx_for(tmp_path, pdf, question, ocr="mai"):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return JobContext(job_id="t", input_path=pdf, params={"question": question, "ocr": ocr},
                      output_dir=out, cache_dir=tmp_path)


def test_chunking_keeps_pages():
    chunks = pdf_qa.chunk_pages(["a " * 800, "pagina due"])
    assert {c["page"] for c in chunks} == {1, 2}
    assert all(len(c["text"]) <= pdf_qa.CHUNK_CHARS for c in chunks)


def test_bm25_prefers_matching_chunk():
    scores = pdf_qa.bm25_scores("scadenza contratto", [
        "Il pagamento avviene ogni mese.",
        "La scadenza del contratto è fissata al 31 dicembre 2027.",
        "Le parti si impegnano alla riservatezza.",
    ])
    assert max(range(3), key=lambda i: scores[i]) == 1


def test_short_pdf_full_document(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    make_pdf(pdf, ["Il contratto scade il 31 dicembre 2027.", "Il canone è di 500 euro al mese."])
    seen = {}

    def fake_chat(messages, on_token=None, **kw):
        seen["prompt"] = messages[-1]["content"]
        on_token and on_token("Scade il 31 dicembre 2027 (p. 1).")
        return "Scade il 31 dicembre 2027 (p. 1)."

    monkeypatch.setattr(ollama, "chat", fake_chat)
    result = pdf_qa.PdfQA().run(ctx_for(tmp_path, pdf, "Quando scade il contratto?"))
    assert "2027" in result["summary"]
    assert "pagina 2" in seen["prompt"] and "500 euro" in seen["prompt"]
    assert result["meta"]["pages"] == 2
    assert (tmp_path / "out" / "testo_estratto.txt").is_file()


def test_long_pdf_uses_retrieval_and_cache(tmp_path, monkeypatch):
    filler = "Questo paragrafo parla di argomenti generali senza importanza. " * 30
    pages = [filler for _ in range(12)]
    pages[7] = filler[:500] + " La password del magazzino è girasole42. " + filler[:500]
    pdf = tmp_path / "lungo.pdf"
    make_pdf(pdf, pages)

    def fake_embed(texts, model=None, **kw):
        # Vettori finti: la dimensione 'magazzino' fa da segnale semantico.
        return [[1.0 if "magazzino" in t else 0.0, 1.0] for t in texts]

    prompts = []
    use_embedding(monkeypatch)
    monkeypatch.setattr(ollama, "embed", fake_embed)
    monkeypatch.setattr(ollama, "chat", lambda m, **kw: prompts.append(m[-1]["content"]) or "girasole42 (p. 8)")

    result = pdf_qa.PdfQA().run(ctx_for(tmp_path, pdf, "Qual è la password del magazzino?"))
    assert "girasole42" in prompts[0]
    assert any(s["label"] == "Pagina 8" for s in result["sources"])
    assert len(result["sources"]) <= pdf_qa.TOP_K

    # Seconda domanda: l'indice arriva dalla cache, niente nuova estrazione.
    monkeypatch.setattr(pdf_qa, "extract_pages", lambda *a, **k: pytest.fail("non doveva rileggere il PDF"))
    pdf_qa.PdfQA().run(ctx_for(tmp_path, pdf, "E la password?"))


def test_embedding_failure_falls_back_to_keywords(tmp_path, monkeypatch):
    filler = "Testo di riempimento qualsiasi. " * 60
    pages = [filler] * 10 + ["Il codice segreto è ornitorinco."]
    pdf = tmp_path / "kw.pdf"
    make_pdf(pdf, pages)

    def broken_embed(*a, **k):
        raise ollama.OllamaError("modello mancante")

    use_embedding(monkeypatch)
    monkeypatch.setattr(ollama, "embed", broken_embed)
    monkeypatch.setattr(ollama, "chat", lambda m, **kw: "ok")
    result = pdf_qa.PdfQA().run(ctx_for(tmp_path, pdf, "Qual è il codice segreto?"))
    assert any("ornitorinco" in s["text"] for s in result["sources"])
    assert any("parole chiave" in n for n in result["notes"])


def test_invalid_pdf(tmp_path):
    bad = tmp_path / "rotto.pdf"
    bad.write_bytes(b"non sono un pdf")
    with pytest.raises(TaskError):
        pdf_qa.PdfQA().run(ctx_for(tmp_path, bad, "?"))


def test_without_embedding_model_uses_keywords(tmp_path, monkeypatch):
    filler = "Testo di riempimento qualsiasi. " * 60
    pdf = tmp_path / "kw2.pdf"
    make_pdf(pdf, [filler] * 10 + ["La chiave è sotto il vaso di gerani."])
    monkeypatch.setattr(ollama, "selected_embed", lambda: "")
    monkeypatch.setattr(ollama, "embed", lambda *a, **k: pytest.fail("nessun embedding doveva essere usato"))
    monkeypatch.setattr(ollama, "chat", lambda m, **kw: "ok")
    result = pdf_qa.PdfQA().run(ctx_for(tmp_path, pdf, "Dove si trova la chiave? gerani"))
    assert any("gerani" in s["text"] for s in result["sources"])
    assert any("Nessun modello di embedding" in n for n in result["notes"])


def test_full_document_threshold_follows_context(monkeypatch):
    monkeypatch.setattr(ollama, "selected_context", lambda: 4096)
    small = pdf_qa.full_doc_chars()
    monkeypatch.setattr(ollama, "selected_context", lambda: 8192)
    assert pdf_qa.full_doc_chars() == 2 * small
