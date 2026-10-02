"""Chat con un documento: prepara il prompt, aspetta il proprio turno e risponde in streaming."""
from __future__ import annotations

import time
from typing import Any, Iterator

from .. import db
from . import documents, ollama
from .gate import gate

DEFAULT_TITLE = "Nuova conversazione"
MAX_QUESTION_CHARS = 4000
MAX_ANSWER_IN_HISTORY = 1500
SOURCE_PREVIEW_CHARS = 400

SYSTEM_PROMPT = (
    "Sei un assistente per la lettura di documenti. Rispondi alle domande dell'utente usando esclusivamente "
    "gli estratti del documento forniti. Se l'informazione non è presente negli estratti, dillo chiaramente "
    "senza inventare. Indica sempre le pagine da cui provengono le informazioni nel formato [p. N] "
    "(più pagine: [p. 3, 5]). Rispondi nella lingua della domanda, in modo chiaro, preciso e ben strutturato; "
    "usa elenchi puntati quando servono."
)


def title_from(question: str) -> str:
    text = " ".join(question.split())
    if len(text) <= 60:
        return text
    return (text[:60].rsplit(" ", 1)[0] or text[:60]) + "…"


def history_messages(messages: list[dict[str, Any]], budget: int) -> list[dict[str, str]]:
    """Gli ultimi scambi domanda/risposta che stanno nel budget, in ordine cronologico."""
    pairs: list[tuple[str, str]] = []
    for i, m in enumerate(messages[:-1]):
        nxt = messages[i + 1]
        if (m["role"] == "user" and nxt["role"] == "assistant" and nxt["status"] in ("ok", "stopped")
                and nxt["content"].strip()):
            pairs.append((m["content"], nxt["content"][:MAX_ANSWER_IN_HISTORY]))
    chosen: list[tuple[str, str]] = []
    used = 0
    for q, a in reversed(pairs):
        cost = len(q) + len(a)
        if chosen and used + cost > budget:
            break
        if not chosen and cost > budget:
            break
        chosen.append((q, a))
        used += cost
    out: list[dict[str, str]] = []
    for q, a in reversed(chosen):
        out += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return out


def retrieval_query(question: str, messages: list[dict[str, Any]]) -> str:
    """Una domanda breve di approfondimento ("e il canone?") si completa con la domanda precedente."""
    if len(question.split()) <= 6:
        for m in reversed(messages):
            if m["role"] == "user":
                return f"{m['content']} {question}"
    return question


def build_messages(index: documents.Index, question: str, past: list[dict[str, Any]],
                   notes: list[str]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Prompt per l'LLM e fonti usate. `past` sono i messaggi precedenti della conversazione."""
    ctx = ollama.selected_context()
    budget = documents.excerpt_budget(ctx)
    k = max(2, budget // documents.CHUNK_CHARS)

    if index.total_chars <= budget:
        chunks, whole = index.chunks, True
    elif documents.is_summary_request(question):
        chunks, whole = documents.spread(index, k), False
        notes.append("Documento lungo: il riassunto si basa su una selezione di parti distribuite lungo il documento.")
    else:
        chunks, _ = documents.retrieve(index, retrieval_query(question, past), k, notes)
        whole = False

    context = "\n\n".join(f"[Estratto {i + 1} · pagina {c['page']}]\n{c['text']}" for i, c in enumerate(chunks))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history_messages(past, int(ctx * 3 * 0.2)),
        {"role": "user", "content": f"Estratti del documento:\n\n{context}\n\nDomanda: {question}"},
    ]
    sources: list[dict[str, Any]] = []
    if not whole:
        sources = [{"page": c["page"], "text": c["text"][:SOURCE_PREVIEW_CHARS]} for c in chunks]
    return messages, sources


def _waiting_text(ticket) -> str:
    holder = gate.holder
    if holder is None:
        return "In attesa del proprio turno…"
    what = "un'altra conversazione" if holder.kind == "chat" else (holder.label or "un'elaborazione")
    return f"Il server sta eseguendo {what}: la risposta parte appena finisce."


def run_turn(conv: dict[str, Any], doc: dict[str, Any], content: str) -> Iterator[dict[str, Any]]:
    """Gestisce un messaggio dell'utente. Produce eventi: user_message, status, sources, token, done, error."""
    llm = ollama.selected_llm()
    if not llm:
        yield {"type": "error", "detail": "Nessun modello linguistico scelto: un amministratore deve sceglierlo "
                                          "nella pagina Modelli linguistici."}
        return
    index = documents.load_index(doc["id"])
    if index is None:
        yield {"type": "error", "detail": "Il documento non è ancora pronto: attendi la fine dell'elaborazione."}
        return

    past = db.list_messages(conv["id"])
    user_msg = db.add_message(conv["id"], "user", content)
    title = conv["title"]
    if title == DEFAULT_TITLE and not past:
        title = title_from(content)
        db.update_conversation(conv["id"], title=title)
    yield {"type": "user_message", "message": user_msg, "title": title}

    ticket = gate.enter("chat", "una conversazione")
    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    try:
        announced = 0.0
        while not gate.wait(ticket, timeout=0.5):
            if time.time() - announced >= 1.0:
                announced = time.time()
                yield {"type": "status", "state": "waiting", "text": _waiting_text(ticket)}

        yield {"type": "status", "state": "searching", "text": "Ricerca nel documento…"}
        notes: list[str] = []
        messages, sources = build_messages(index, content, past, notes)
        yield {"type": "sources", "sources": sources, "notes": notes}
        yield {"type": "status", "state": "writing", "text": "Scrittura della risposta…"}

        think = ollama.ThinkFilter()
        try:
            for piece in ollama.chat_stream(messages, model=llm):
                text = think.feed(piece)
                if text:
                    parts.append(text)
                    yield {"type": "token", "text": text}
            tail = think.flush()
            if tail:
                parts.append(tail)
                yield {"type": "token", "text": tail}
        except ollama.OllamaError as e:
            partial = "".join(parts).strip()
            saved = db.add_message(conv["id"], "assistant", partial or str(e), sources if partial else None,
                                   model=llm, status="error")
            yield {"type": "error", "detail": str(e), "message": saved}
            return

        answer = "".join(parts).strip() or "Il modello non ha prodotto una risposta."
        saved = db.add_message(conv["id"], "assistant", answer, sources, model=llm)
        yield {"type": "done", "message": saved}
    except GeneratorExit:
        # L'utente ha interrotto: si conserva quanto scritto fin qui.
        partial = "".join(parts).strip()
        if partial:
            db.add_message(conv["id"], "assistant", partial, sources, model=llm, status="stopped")
        raise
    finally:
        gate.leave(ticket)
