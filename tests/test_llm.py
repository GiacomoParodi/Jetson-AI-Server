from app.services import ollama


def test_llm_choice_is_manual(client, login, monkeypatch):
    admin, mario = login("admin"), login("mario")
    s = client.get("/api/llm", headers=mario).json()
    # Nessun modello preimpostato.
    assert s["llm_model"] == "" and s["embed_model"] == ""
    assert s["running"] is False

    monkeypatch.setattr(ollama, "installed_models", lambda: ["qwen2.5:3b", "nomic-embed-text:latest"])
    body = {"llm_model": "qwen2.5:3b", "embed_model": "nomic-embed-text", "context": 4096}
    assert client.post("/api/llm/select", headers=mario, json=body).status_code == 403
    assert client.post("/api/llm/select", headers=admin, json={**body, "llm_model": "llama3.1:70b"}).status_code == 400
    assert client.post("/api/llm/select", headers=admin, json={**body, "context": 1234}).status_code == 400
    assert client.post("/api/llm/select", headers=admin, json=body).status_code == 200
    assert ollama.selected_llm() == "qwen2.5:3b"
    assert ollama.selected_embed() == "nomic-embed-text"
    # Il modello in uso non si può eliminare.
    assert client.delete("/api/llm/models/qwen2.5:3b", headers=admin).status_code == 400
    # Si può anche togliere l'embedding (ricerca solo per parole chiave).
    assert client.post("/api/llm/select", headers=admin, json={**body, "embed_model": ""}).status_code == 200
    assert ollama.selected_embed() == ""
    ollama.select("", "", 4096)


def test_pull_validation(client, login, monkeypatch):
    admin = login("admin")
    assert client.post("/api/llm/pull", headers=login("mario"), json={"name": "qwen2.5:3b"}).status_code == 403
    assert client.post("/api/llm/pull", headers=admin, json={"name": "rm -rf /"}).status_code == 400
    # Ollama spento.
    assert client.post("/api/llm/pull", headers=admin, json={"name": "qwen2.5:3b"}).status_code == 503
    started = []
    monkeypatch.setattr(ollama, "installed_models", lambda: [])
    monkeypatch.setattr(ollama, "start_pull", started.append)
    assert client.post("/api/llm/pull", headers=admin, json={"name": "qwen2.5:3b"}).status_code == 200
    assert started == ["qwen2.5:3b"]


def test_pdf_task_needs_a_chosen_model(client, login, monkeypatch):
    from app.tasks.pdf_qa import PdfQA

    monkeypatch.setattr(ollama, "installed_models", lambda: ["qwen2.5:3b"])
    ok, reason = PdfQA().available()
    assert not ok and "Nessun modello LLM scelto" in reason
    monkeypatch.setattr(ollama, "selected_llm", lambda: "qwen2.5:3b")
    assert PdfQA().available() == (True, "")
