import json
import shutil
import threading
import time

import pymupdf
import pytest

from app import db
from app.config import settings
from app.services import chat, documents, ollama
from app.services.gate import gate


def make_pdf(pages, encrypted=False):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), text, fontsize=10)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="a", owner_pw="b") if encrypted else doc.tobytes()
    doc.close()
    return data


def upload(client, headers, pages, name="contratto.pdf", data=None):
    return client.post("/api/documents", headers=headers,
                       files={"file": (name, data if data is not None else make_pdf(pages), "application/pdf")})


def wait_doc(client, headers, doc_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        doc = client.get(f"/api/documents/{doc_id}", headers=headers).json()
        if doc["status"] != "processing":
            return doc
        time.sleep(0.05)
    raise AssertionError("documento non elaborato")


def ready_doc(client, headers, pages, name="contratto.pdf"):
    r = upload(client, headers, pages, name)
    assert r.status_code == 200, r.text
    doc = wait_doc(client, headers, r.json()["id"])
    assert doc["status"] == "ready", doc
    return doc


def ask(client, headers, conv_id, text):
    events = []
    with client.stream("POST", f"/api/conversations/{conv_id}/messages", headers=headers, json={"content": text}) as r:
        assert r.status_code == 200, r.read()
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.reply = "Il canone mensile è di **750 euro** [p. 2]."

    def __call__(self, messages, model=None, options=None):
        self.calls.append(messages)
        for i in range(0, len(self.reply), 7):
            yield self.reply[i:i + 7]


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(ollama, "chat_stream", fake)
    ollama.select("test-llm", "", 4096)
    yield fake
    ollama.select("", "", 4096)


CONTRATTO = ["CONTRATTO DI LOCAZIONE. Il contratto scade il 30 giugno 2028.",
             "Il canone mensile è di 750 euro, da pagare entro il 5 di ogni mese.",
             "Alla scadenza il contratto si rinnova per altri quattro anni."]


# ------------------------------------------------------------------ caricamento

def test_upload_index_and_pages(client, login):
    h = login("mario")
    doc = ready_doc(client, h, CONTRATTO)
    assert doc["pages"] == 3 and doc["title"] == "contratto" and doc["conversation_count"] == 0
    assert not doc["semantic"]
    assert documents.pdf_path(doc["id"]).is_file() and documents.load_index(doc["id"]) is not None
    page = client.get(f"/api/documents/{doc['id']}/pages/2?w=500", headers=h)
    assert page.status_code == 200 and page.content[:4] == b"\x89PNG"
    assert client.get(f"/api/documents/{doc['id']}/pages/9", headers=h).status_code == 404
    assert "750 euro" in client.get(f"/api/documents/{doc['id']}/text", headers=h).text
    assert client.get(f"/api/documents/{doc['id']}/file", headers=h).content[:4] == b"%PDF"
    # I lavori interni non compaiono tra i lavori delle sezioni.
    assert client.get("/api/jobs?section=video", headers=h).json() == []


def test_invalid_uploads(client, login):
    h = login("mario")
    assert upload(client, h, [], name="note.txt", data=b"ciao").status_code == 400
    r = upload(client, h, [], data=b"non sono un pdf")
    assert r.status_code == 400 and "PDF valido" in r.json()["detail"]
    r = upload(client, h, [], data=make_pdf(["x"], encrypted=True))
    assert r.status_code == 400 and "password" in r.json()["detail"]
    assert not list(settings.documents_dir.glob(".upload-*"))


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract non installato")
def test_scanned_pdf_is_read_with_ocr(client, login):
    from PIL import Image, ImageDraw, ImageFont
    import io

    img = Image.new("RGB", (1240, 400), "white")
    ImageDraw.Draw(img).text((60, 120), "CANONE MENSILE 750 EURO", fill="black",
                             font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56))
    buf = io.BytesIO(); img.save(buf, "PNG")
    pdf = pymupdf.open(); pg = pdf.new_page(); pg.insert_image(pg.rect, stream=buf.getvalue())
    h = login("mario")
    r = upload(client, h, [], name="scansione.pdf", data=pdf.tobytes())
    doc = wait_doc(client, h, r.json()["id"])
    assert doc["status"] == "ready" and doc["ocr_pages"] == 1
    assert "750" in client.get(f"/api/documents/{doc['id']}/text", headers=h).text


# ------------------------------------------------------------------ conversazioni

def test_conversation_and_history(client, login, llm):
    h = login("mario")
    doc = ready_doc(client, h, CONTRATTO)
    conv = client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()
    assert conv["title"] == chat.DEFAULT_TITLE
    # Senza messaggi non compare nell'elenco.
    assert client.get(f"/api/documents/{doc['id']}/conversations", headers=h).json() == []

    events = ask(client, h, conv["id"], "Quanto è il canone mensile?")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "user_message" and kinds[-1] == "done" and "token" in kinds
    assert events[0]["title"] == "Quanto è il canone mensile?"
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed == llm.reply == events[-1]["message"]["content"]
    # Documento breve: letto per intero, senza elenco di fonti.
    assert events[-1]["message"]["sources"] == []
    assert "scade il 30 giugno 2028" in llm.calls[0][-1]["content"]

    # Il secondo messaggio porta con sé la storia della conversazione.
    ask(client, h, conv["id"], "E quando scade?")
    prompt = llm.calls[1]
    assert [m["role"] for m in prompt] == ["system", "user", "assistant", "user"]
    assert prompt[1]["content"] == "Quanto è il canone mensile?" and "750 euro" in prompt[2]["content"]

    detail = client.get(f"/api/conversations/{conv['id']}", headers=h).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    listing = client.get(f"/api/documents/{doc['id']}/conversations", headers=h).json()
    assert len(listing) == 1 and listing[0]["message_count"] == 4 and listing[0]["last_question"] == "E quando scade?"
    assert client.get("/api/conversations", headers=h).json()[0]["document_title"] == "contratto"
    assert client.get(f"/api/documents/{doc['id']}", headers=h).json()["conversation_count"] == 1


def test_no_model_chosen_and_bad_requests(client, login):
    h = login("mario")
    doc = ready_doc(client, h, CONTRATTO)
    conv = client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()
    events = ask(client, h, conv["id"], "Ciao?")
    assert events == [{"type": "error", "detail": events[0]["detail"]}] and "Nessun modello" in events[0]["detail"]
    assert client.get(f"/api/conversations/{conv['id']}", headers=h).json()["messages"] == []
    url = f"/api/conversations/{conv['id']}/messages"
    assert client.post(url, headers=h, json={"content": "   "}).status_code == 400
    assert client.post(url, headers=h, json={"content": "x" * 5000}).status_code == 400


def test_model_failure_is_saved(client, login, llm, monkeypatch):
    h = login("mario")
    doc = ready_doc(client, h, CONTRATTO)
    conv = client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()

    def broken(messages, model=None, options=None):
        yield "Risposta parz"
        raise ollama.OllamaError("Ollama non è raggiungibile")

    monkeypatch.setattr(ollama, "chat_stream", broken)
    events = ask(client, h, conv["id"], "Domanda?")
    assert events[-1]["type"] == "error" and "raggiungibile" in events[-1]["detail"]
    last = client.get(f"/api/conversations/{conv['id']}", headers=h).json()["messages"][-1]
    assert last["status"] == "error" and last["content"] == "Risposta parz"


def test_rename_and_delete(client, login, llm):
    h = login("mario")
    doc = ready_doc(client, h, CONTRATTO)
    conv = client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()
    ask(client, h, conv["id"], "Domanda?")
    assert client.patch(f"/api/conversations/{conv['id']}", headers=h, json={"title": "Scadenze"}).json()["title"] == "Scadenze"
    assert client.patch(f"/api/conversations/{conv['id']}", headers=h, json={"title": "  "}).status_code == 400
    assert client.patch(f"/api/documents/{doc['id']}", headers=h, json={"title": "Contratto Rossi"}).json()["title"] == "Contratto Rossi"
    assert client.delete(f"/api/conversations/{conv['id']}", headers=h).status_code == 200
    assert client.get(f"/api/conversations/{conv['id']}", headers=h).status_code == 404

    conv2 = client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()
    ask(client, h, conv2["id"], "Altra domanda?")
    assert client.delete(f"/api/documents/{doc['id']}", headers=h).status_code == 200
    assert not documents.doc_dir(doc["id"]).exists()
    assert client.get(f"/api/conversations/{conv2['id']}", headers=h).status_code == 404
    assert db.list_messages(conv2["id"]) == []


def test_documents_are_private(client, login, llm):
    mario, luigi, admin = login("mario"), login("luigi"), login("admin")
    doc = ready_doc(client, mario, CONTRATTO)
    conv = client.post(f"/api/documents/{doc['id']}/conversations", headers=mario).json()
    ask(client, mario, conv["id"], "Domanda?")
    for url in (f"/api/documents/{doc['id']}", f"/api/documents/{doc['id']}/pages/1", f"/api/documents/{doc['id']}/file",
                f"/api/documents/{doc['id']}/conversations", f"/api/conversations/{conv['id']}"):
        assert client.get(url, headers=luigi).status_code == 404, url
    assert client.post(f"/api/conversations/{conv['id']}/messages", headers=luigi, json={"content": "x"}).status_code == 404
    assert client.delete(f"/api/documents/{doc['id']}", headers=luigi).status_code == 404
    assert all(d["id"] != doc["id"] for d in client.get("/api/documents?all=true", headers=luigi).json())
    assert any(d["id"] == doc["id"] for d in client.get("/api/documents?all=true", headers=admin).json())
    assert client.get(f"/api/conversations/{conv['id']}", headers=admin).status_code == 200


# ------------------------------------------------------------------ ricerca

def test_long_document_retrieval_and_summary(client, login, llm):
    filler = "Questo paragrafo parla di argomenti generali senza importanza. " * 30
    pages = [filler] * 12
    pages[7] = filler[:500] + " La password del magazzino è girasole42. " + filler[:500]
    h = login("mario")
    doc = ready_doc(client, h, pages, name="lungo.pdf")
    conv = client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()

    events = ask(client, h, conv["id"], "Qual è la password del magazzino?")
    prompt = llm.calls[-1][-1]["content"]
    assert "girasole42" in prompt
    sources = events[-1]["message"]["sources"]
    assert any(s["page"] == 8 for s in sources) and len(sources) <= 6

    # Un riassunto di un documento lungo usa parti distribuite su tutto il testo.
    events = ask(client, h, conv["id"], "Riassumi il documento")
    sources = events[-1]["message"]["sources"]
    assert len({s["page"] for s in sources}) >= 4
    assert any("distribuite" in n for n in next(e for e in events if e["type"] == "sources")["notes"])

    # Una domanda breve di approfondimento si appoggia a quella precedente per la ricerca.
    ask(client, h, conv["id"], "Qual è la password del magazzino?")
    ask(client, h, conv["id"], "e la sua?")
    assert chat.retrieval_query("e la sua?", [{"role": "user", "content": "Qual è la password"}]).startswith("Qual è")


def test_semantic_index_and_reindex(client, login, monkeypatch):
    h = login("mario")
    filler = "Testo di riempimento qualsiasi senza interesse. " * 40
    doc = ready_doc(client, h, [filler] * 8 + ["Il magazzino è chiuso la domenica."], name="semantico.pdf")
    assert not doc["semantic"]

    calls = []

    def fake_embed(texts, model=None, **kw):
        calls.append(len(texts))
        return [[1.0 if "magazzino" in t else 0.0, 1.0] for t in texts]

    monkeypatch.setattr(ollama, "embed", fake_embed)
    ollama.select("", "nomic-embed-text", 4096)
    try:
        assert client.get(f"/api/documents/{doc['id']}", headers=h).json()["semantic_outdated"] is True
        assert client.post(f"/api/documents/{doc['id']}/reindex", headers=h).status_code == 200
        doc = wait_doc(client, h, doc["id"])
        assert doc["status"] == "ready" and doc["semantic"] and doc["embed_model"] == "nomic-embed-text"
        assert doc["semantic_outdated"] is False and calls
        index = documents.load_index(doc["id"])
        found, mode = documents.retrieve(index, "quando è aperto il magazzino?", 2, [])
        assert mode == "semantica" and any("magazzino" in c["text"] for c in found)
        # Cambiando modello, la ricerca ripiega sulle parole chiave e lo segnala.
        ollama.select("", "altro-modello", 4096)
        notes = []
        _, mode = documents.retrieve(index, "magazzino", 2, notes)
        assert mode == "parole chiave" and "Reindicizza" in notes[0]
    finally:
        ollama.select("", "", 4096)


def test_embedding_failure_falls_back_to_keywords(client, login, monkeypatch):
    def broken(*a, **k):
        raise ollama.OllamaError("modello mancante")

    monkeypatch.setattr(ollama, "embed", broken)
    ollama.select("", "nomic-embed-text", 4096)
    try:
        h = login("mario")
        doc = ready_doc(client, h, CONTRATTO, name="senza-embedding.pdf")
        assert not doc["semantic"] and any("parole chiave" in n for n in doc["notes"])
    finally:
        ollama.select("", "", 4096)


def test_history_respects_budget():
    msgs = []
    for i in range(10):
        msgs += [{"role": "user", "content": f"domanda {i}", "status": "ok"},
                 {"role": "assistant", "content": "risposta " * 50, "status": "ok"},]
    msgs.append({"role": "user", "content": "ultima", "status": "ok"})
    out = chat.history_messages(msgs, budget=1500)
    assert 2 <= len(out) < 20 and out[-2]["content"] == "domanda 9" and out[0]["role"] == "user"
    assert chat.history_messages(msgs[:1], budget=1500) == []


# ------------------------------------------------------------------ turni, interruzioni, filtro

def test_closing_the_stream_keeps_partial_answer(client, login, llm):
    h = login("mario")
    doc = ready_doc(client, h, CONTRATTO)
    conv = db.get_conversation(client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()["id"])
    gen = chat.run_turn(conv, db.get_document(doc["id"]), "Quanto è il canone?")
    seen = []
    for event in gen:
        seen.append(event["type"])
        if seen.count("token") == 2:
            break
    gen.close()
    last = db.list_messages(conv["id"])[-1]
    assert last["role"] == "assistant" and last["status"] == "stopped" and last["content"] == llm.reply[:14]
    assert gate.holder is None and gate.waiting == 0


def test_chat_waits_for_other_work(client, login, llm):
    h = login("mario")
    doc = ready_doc(client, h, CONTRATTO)
    conv = db.get_conversation(client.post(f"/api/documents/{doc['id']}/conversations", headers=h).json()["id"])
    busy = gate.enter("job", "un'analisi video")
    assert gate.wait(busy, 1)
    threading.Timer(1.2, gate.leave, [busy]).start()
    events = list(chat.run_turn(conv, db.get_document(doc["id"]), "Domanda?"))
    waiting = [e for e in events if e["type"] == "status" and e["state"] == "waiting"]
    assert waiting and "un'analisi video" in waiting[0]["text"]
    assert events[-1]["type"] == "done"


def test_gate_is_fifo_and_can_be_disabled():
    from app.services.gate import Gate

    g = Gate()
    a, b, c = g.enter("job"), g.enter("chat"), g.enter("job")
    assert g.wait(a, 0.1) and not g.wait(b, 0.05)
    g.leave(a)
    assert g.wait(b, 0.1) and not g.wait(c, 0.05) and g.holder is b
    g.leave(b); g.leave(c)
    assert g.holder is None and g.waiting == 0
    free = Gate(enabled=False)
    x, y = free.enter("job"), free.enter("chat")
    assert free.wait(x, 0.01) and free.wait(y, 0.01)


def test_think_filter_in_stream():
    f = ollama.ThinkFilter()
    out = "".join(f.feed(p) for p in ["Ci", "ao <th", "ink>segreto</thi", "nk>  mondo"]) + f.flush()
    assert out == "Ciao mondo"
    f = ollama.ThinkFilter()
    assert f.feed("<think>solo ragionamento") + f.flush() == ""


def test_unfinished_documents_are_requeued(client, login):
    from app.worker import requeue_unfinished_documents

    h = login("mario")
    user = db.get_user_by_name("mario")
    doc_id = db.create_document(user["id"], "Interrotto", "interrotto.pdf", 10, 3)
    documents.doc_dir(doc_id).mkdir(parents=True, exist_ok=True)
    documents.pdf_path(doc_id).write_bytes(make_pdf(CONTRATTO))
    requeue_unfinished_documents()
    doc = wait_doc(client, h, doc_id)
    assert doc["status"] == "ready" and doc["pages"] == 3
