"use strict";

/* Area «Lettore documenti»: archivio, chat con i documenti e modelli linguistici. */

// ------------------------------------------------------------------ archivio dei documenti

let showAllDocs = false;

function docStatusLine(d) {
  if (d.status === "processing") {
    const pct = d.progress ? ` ${Math.round(d.progress * 100)}%` : "";
    return h("div", { class: "doc-state" }, h("span", { class: "badge running" }, `Elaborazione${pct}`),
      h("span", { class: "small muted" }, d.message || ""));
  }
  if (d.status === "failed") return h("div", { class: "doc-state" }, h("span", { class: "badge failed" }, "Errore"),
    h("span", { class: "small", style: "color:var(--danger)" }, d.error || ""));
  return null;
}

async function viewDocLibrary() {
  setNav("docs", "docs");
  const uploads = h("div", { class: "uploads" });
  const grid = h("div", { class: "doc-grid" });
  const input = h("input", { type: "file", accept: ".pdf,application/pdf", multiple: true, style: "display:none" });
  input.addEventListener("change", () => { startUploads([...input.files]); input.value = ""; });
  const drop = h("div", { class: "dropzone slim", tabindex: 0, onclick: () => input.click(),
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") input.click(); } },
  h("b", {}, "Trascina qui uno o più PDF"), " oppure tocca per sceglierli",
  h("div", { class: "small" }, "Restano in archivio: puoi farci tutte le domande che vuoi, anche in giorni diversi."));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); startUploads([...e.dataTransfer.files]); });

  setKids($main,
    h("div", { class: "row spread" }, h("h1", {}, "Documenti"),
      me.is_admin ? h("label", { class: "check small" },
        h("input", { type: "checkbox", checked: showAllDocs, onchange: (e) => { showAllDocs = e.target.checked; viewDocLibrary(); } }),
        "Tutti gli utenti") : null),
    h("p", { class: "muted" }, "Carica un PDF, anche scansionato, e dialoga con il suo contenuto: le risposte indicano le pagine "
      + "da cui provengono. Per ogni documento puoi tenere più conversazioni e ritrovarle quando vuoi."),
    drop, input, uploads, grid);

  async function load() {
    let docs;
    try { docs = await api(`/api/documents?all=${showAllDocs}`); } catch (e) { return setKids(grid, errorBox(e.message)); }
    if (!docs.length) {
      setKids(grid, h("div", { class: "card muted", style: "grid-column:1/-1" }, "Nessun documento ancora. Carica il primo PDF qui sopra."));
      return;
    }
    setKids(grid, docs.map((d) => h("article", { class: "doc-card" },
      h("a", { class: "doc-thumb", href: `#/docs/${d.id}`, "aria-label": `Apri ${d.title}` },
        d.status === "failed" ? h("div", { class: "thumb-empty" }, "Non disponibile")
          : h("img", { src: `/api/documents/${d.id}/pages/1?w=360`, alt: "", loading: "lazy" })),
      h("div", { class: "doc-body" },
        h("a", { class: "doc-title", href: `#/docs/${d.id}` }, d.title),
        h("div", { class: "small muted" }, [plural(d.pages, "pagina", "pagine"),
          plural(d.conversation_count, "conversazione", "conversazioni"),
          showAllDocs ? d.username : null].filter(Boolean).join(" · ")),
        h("div", { class: "small muted" }, d.last_activity ? `Ultima domanda ${fmtAgo(d.last_activity)}` : `Caricato ${fmtAgo(d.created_at)}`),
        docStatusLine(d),
        h("div", { class: "row doc-actions" },
          h("a", { class: "btn small-btn", href: `#/docs/${d.id}` }, d.conversation_count ? "Apri" : "Fai una domanda"),
          d.status === "failed" ? h("button", { class: "small-btn", onclick: async () => {
            try { await api(`/api/documents/${d.id}/reindex`, { method: "POST" }); load(); } catch (e) { toast(e.message); }
          } }, "Riprova") : null,
          h("button", { class: "link danger-link", onclick: async () => {
            const extra = d.conversation_count ? ` e le sue ${plural(d.conversation_count, "conversazione", "conversazioni")}` : "";
            if (!confirm(`Eliminare «${d.title}»${extra}?`)) return;
            try { await api(`/api/documents/${d.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
          } }, "Elimina"))))));
    if (docs.some((d) => d.status === "processing") && location.hash === "#/docs") pollTimer = setTimeout(load, 1800);
  }

  async function startUploads(files) {
    files = files.filter((f) => /\.pdf$/i.test(f.name) || f.type === "application/pdf");
    if (!files.length) return toast("Scegli dei file PDF");
    for (const file of files) {
      const bar = progressBar(0);
      const label = h("span", { class: "small" }, `${file.name} · caricamento…`);
      const row = h("div", { class: "upload-row" }, label, bar);
      uploads.append(row);
      const fd = new FormData();
      fd.append("file", file);
      try {
        await uploadWithProgress(fd, (f) => { bar.firstChild.style.width = `${Math.round(f * 100)}%`; }, "/api/documents");
        row.remove();
      } catch (e) {
        setKids(row, h("span", { class: "small", style: "color:var(--danger)" }, `${file.name}: ${e.message}`),
          h("button", { class: "link", onclick: () => row.remove() }, "Chiudi"));
      }
      load();
    }
  }
  load();
}

// ------------------------------------------------------------------ spazio di lavoro: documento + chat

const STARTERS = ["Riassumi il documento", "Quali sono i punti principali?", "Elenca date, importi e scadenze"];
const viewerPage = {};       // ultima pagina vista per ogni documento
let activeStream = null;     // risposta in corso: si può interrompere

function stopChatStream() {
  if (activeStream) { activeStream.abort(); activeStream = null; }
}

function shortModel(name) { return String(name || "").split("/").pop().split(":")[0]; }

/** Legge una risposta in streaming (Server-Sent Events) e chiama `onEvent` per ogni evento. */
async function streamAnswer(convId, text, onEvent, signal) {
  const res = await fetch(`/api/conversations/${convId}/messages`, {
    method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: text }),
  });
  if (res.status === 401) { me = null; renderLogin(); throw new Error("Sessione scaduta"); }
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch { /* vuoto */ }
    throw new Error((data && typeof data.detail === "string" && data.detail) || `Errore ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

async function viewDocument(docId, which) {
  setNav("docs", "docs");
  document.documentElement.classList.add("wide");
  setKids($main, h("p", { class: "muted" }, "Caricamento…"));

  let doc, convs, llm;
  try {
    [doc, convs, llm] = await Promise.all([
      api(`/api/documents/${docId}`), api(`/api/documents/${docId}/conversations`),
      api("/api/llm").catch(() => null),
    ]);
  } catch (e) { return setKids($main, errorBox(e.message)); }

  // Conversazione da mostrare: quella indicata, altrimenti la più recente, altrimenti una nuova.
  let conv = null, messages = [];
  const wanted = typeof which === "number" ? which : which === "new" ? null : (convs[0] && convs[0].id);
  if (wanted) {
    try {
      const detail = await api(`/api/conversations/${wanted}`);
      if (detail.document_id === docId) { conv = detail; messages = detail.messages; }
    } catch { /* conversazione non trovata: si parte da una nuova */ }
  }

  const ws = { doc, convs, conv, messages, busy: false, pane: "chat", llm, stick: true };
  let page = viewerPage[docId] || 1;
  const llmReady = () => !ws.llm || !ws.llm.running || !!ws.llm.llm_model;

  // ---- elementi
  const head = h("div", { class: "ws-head" });
  const banner = h("div");
  const tabs = h("div", { class: "ws-tabs", role: "tablist" });
  const convList = h("aside", { class: "ws-convs" });
  const feed = h("div", { class: "feed", "aria-live": "polite" });
  const input = h("textarea", { class: "composer-input", rows: 1, placeholder: "Scrivi una domanda sul documento…", "aria-label": "Domanda" });
  const sendBtn = h("button", { class: "primary", type: "submit" }, "Invia");
  const stopBtn = h("button", { type: "button", class: "danger", style: "display:none" }, "Interrompi");
  const composer = h("form", { class: "composer" }, input, h("div", { class: "composer-btns" }, stopBtn, sendBtn));
  const chatPane = h("section", { class: "ws-chat" }, banner, feed, composer);
  const viewerBody = h("div", { class: "viewer-body" });
  const pageNum = h("input", { type: "number", min: 1, max: doc.pages, value: page, class: "page-num", "aria-label": "Numero di pagina" });
  const viewer = h("aside", { class: "ws-viewer" },
    h("div", { class: "viewer-bar" },
      h("button", { class: "small-btn", onclick: () => openPage(page - 1), "aria-label": "Pagina precedente" }, "‹"),
      pageNum, h("span", { class: "small muted" }, `di ${doc.pages}`),
      h("button", { class: "small-btn", onclick: () => openPage(page + 1), "aria-label": "Pagina successiva" }, "›"),
      h("a", { class: "small", href: `/api/documents/${doc.id}/file`, target: "_blank", rel: "noopener", style: "margin-left:auto" }, "Apri il PDF")),
    viewerBody);
  const root = h("div", { class: "ws", "data-pane": "chat" }, head, tabs,
    h("div", { class: "ws-grid" }, convList, chatPane, viewer));

  // ---- visualizzatore
  function openPage(n, switchPane = true) {
    n = Math.max(1, Math.min(doc.pages, Math.round(Number(n) || 1)));
    page = n;
    viewerPage[docId] = n;
    pageNum.value = n;
    const img = h("img", { class: "page-img", alt: `Pagina ${n} di ${doc.pages}`, src: `/api/documents/${doc.id}/pages/${n}?w=1000` });
    img.addEventListener("error", () => setKids(viewerBody, h("p", { class: "muted small" }, "Pagina non disponibile.")));
    setKids(viewerBody, img);
    viewerBody.scrollTop = 0;
    if (switchPane) showPane("viewer", true);
  }
  pageNum.addEventListener("change", () => openPage(pageNum.value, false));

  function showPane(name, onlyIfNarrow) {
    if (onlyIfNarrow && window.innerWidth > 900) return;
    ws.pane = name;
    root.dataset.pane = name;
    renderTabs();
  }
  function renderTabs() {
    const items = [["convs", "Cronologia"], ["chat", "Chat"], ["viewer", "Documento"]];
    setKids(tabs, items.map(([key, label]) => h("button", { role: "tab", "aria-selected": ws.pane === key,
      class: ws.pane === key ? "active" : null, onclick: () => showPane(key) }, label)));
  }

  // ---- intestazione del documento
  function renderHead() {
    const d = ws.doc;
    const info = [plural(d.pages, "pagina", "pagine")];
    if (d.ocr_pages) info.push(`${plural(d.ocr_pages, "pagina letta", "pagine lette")} con OCR`);
    if (d.status === "ready") info.push(d.semantic ? "ricerca semantica" : "ricerca per parole chiave");
    const title = h("h1", { class: "ws-title", tabindex: 0, title: "Clicca per rinominare" }, d.title);
    title.addEventListener("click", () => editText(title, d.title, async (v) => {
      const r = await api(`/api/documents/${d.id}`, { method: "PATCH", json: { title: v } });
      ws.doc = { ...ws.doc, ...r }; renderHead();
    }));
    setKids(head,
      h("a", { class: "small", href: "#/docs" }, "← Documenti"),
      h("div", { class: "ws-headrow" }, title,
        h("div", { class: "row ws-actions" },
          h("a", { class: "btn small-btn", href: `/api/documents/${d.id}/text`, target: "_blank", rel: "noopener" }, "Testo estratto"),
          d.status !== "processing" && (d.semantic_outdated && llm && llm.embed_model || d.status === "failed")
            ? h("button", { class: "small-btn", onclick: reindex }, "Reindicizza") : null,
          h("button", { class: "small-btn danger", onclick: async () => {
            const extra = ws.convs.length ? ` e le sue ${plural(ws.convs.length, "conversazione", "conversazioni")}` : "";
            if (!confirm(`Eliminare «${d.title}»${extra}?`)) return;
            try { await api(`/api/documents/${d.id}`, { method: "DELETE" }); location.hash = "#/docs"; } catch (e) { toast(e.message); }
          } }, "Elimina"))),
      h("div", { class: "small muted" }, info.join(" · ")));
  }
  async function reindex() {
    try { ws.doc = { ...ws.doc, ...(await api(`/api/documents/${doc.id}/reindex`, { method: "POST" })) }; renderBanner(); renderHead(); watchProcessing(); } catch (e) { toast(e.message); }
  }

  /** Cambia un testo in un campo di modifica; Invio salva, Esc annulla. */
  function editText(el, current, save) {
    const field = h("input", { type: "text", value: current, class: "inline-edit", maxlength: 120, "aria-label": "Nuovo nome" });
    el.replaceWith(field);
    field.focus(); field.select();
    let finished = false;
    const done = async (commit) => {
      if (finished) return; finished = true;
      const v = field.value.trim();
      if (commit && v && v !== current) { try { await save(v); return; } catch (e) { toast(e.message); } }
      field.replaceWith(el);
    };
    field.addEventListener("keydown", (e) => { if (e.key === "Enter") done(true); else if (e.key === "Escape") done(false); });
    field.addEventListener("blur", () => done(true));
  }

  // ---- elenco delle conversazioni
  function convHref(id) { return id ? `#/docs/${docId}/c/${id}` : `#/docs/${docId}/new`; }
  function renderConvs() {
    setKids(convList,
      h("a", { class: "btn new-conv", href: convHref(null), onclick: (e) => { if (!ws.conv && !ws.messages.length) { e.preventDefault(); showPane("chat", true); input.focus(); } } }, "+ Nuova conversazione"),
      ws.convs.length ? h("ul", { class: "conv-list" }, ws.convs.map((c) => {
        const active = ws.conv && ws.conv.id === c.id;
        const titleEl = h("span", { class: "conv-title" }, c.title);
        return h("li", { class: active ? "active" : null },
          h("a", { href: convHref(c.id), class: "conv-link" }, titleEl,
            h("span", { class: "small muted" }, `${fmtAgo(c.updated_at)} · ${plural(Math.ceil(c.message_count / 2), "domanda", "domande")}`)),
          h("div", { class: "conv-actions" },
            h("button", { class: "link", onclick: () => editText(titleEl, c.title, async (v) => {
              await api(`/api/conversations/${c.id}`, { method: "PATCH", json: { title: v } });
              c.title = v; if (ws.conv && ws.conv.id === c.id) ws.conv.title = v; renderConvs();
            }) }, "Rinomina"),
            h("button", { class: "link danger-link", onclick: async () => {
              if (!confirm(`Eliminare la conversazione «${c.title}»?`)) return;
              try {
                await api(`/api/conversations/${c.id}`, { method: "DELETE" });
                if (active) location.hash = `#/docs/${docId}/new`; else { ws.convs = ws.convs.filter((x) => x.id !== c.id); renderConvs(); renderTabs(); }
              } catch (e) { toast(e.message); }
            } }, "Elimina")));
      })) : h("p", { class: "small muted", style: "padding:6px 4px" }, "Le tue conversazioni su questo documento compariranno qui."));
  }

  // ---- messaggi
  function copyText(text, btn) {
    const done = () => { btn.textContent = "Copiato"; setTimeout(() => { btn.textContent = "Copia"; }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast("Copia non riuscita"));
    else toast("Copia non disponibile in questo browser");
  }

  function messageEl(m) {
    if (m.role === "user") return h("div", { class: "msg user" }, h("div", { class: "bubble" }, m.content));
    if (m.status === "error" && !m.sources.length) {
      return h("div", { class: "msg assistant" }, h("div", { class: "alert error small" }, m.content));
    }
    const copy = h("button", { class: "link small", onclick: () => copyText(m.content, copy) }, "Copia");
    return h("div", { class: "msg assistant" },
      h("div", { class: "md", html: markdown(m.content, { cite: true }) }),
      m.status === "stopped" ? h("div", { class: "small muted" }, "Risposta interrotta") : null,
      m.status === "error" ? h("div", { class: "alert error small" }, "La risposta si è interrotta per un errore.") : null,
      m.sources && m.sources.length ? h("details", { class: "sources" }, h("summary", {}, `Fonti usate (${m.sources.length})`),
        m.sources.map((s) => h("div", { class: "source" },
          h("button", { type: "button", class: "cite", "data-page": s.page }, `p. ${s.page}`),
          h("span", { class: "small muted" }, s.text)))) : null,
      h("div", { class: "msg-foot small muted" }, copy, m.model ? h("span", {}, shortModel(m.model)) : null));
  }

  function renderFeed() {
    if (!ws.messages.length) {
      setKids(feed, h("div", { class: "empty-chat" },
        h("h2", {}, doc.title),
        h("p", { class: "muted" }, ws.doc.status === "ready" ? "Scrivi una domanda oppure parti da uno di questi spunti." : "Il documento è in elaborazione: potrai fare domande appena è pronto."),
        ws.doc.status === "ready" ? h("div", { class: "starters" }, STARTERS.map((q) => h("button", { type: "button", class: "chip-btn", onclick: () => send(q) }, q))) : null));
      return;
    }
    setKids(feed, ws.messages.map(messageEl));
    scrollDown(true);
  }
  function scrollDown(force) {
    if (force || ws.stick) feed.scrollTop = feed.scrollHeight;
  }
  feed.addEventListener("scroll", () => { ws.stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80; });
  chatPane.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".cite");
    if (b) openPage(Number(b.dataset.page));
  });

  // ---- stato del documento e del modello
  function renderBanner() {
    const d = ws.doc, items = [];
    if (d.status === "processing") {
      items.push(h("div", { class: "alert info" }, `Elaborazione del documento in corso${d.progress ? ` (${Math.round(d.progress * 100)}%)` : ""}`
        + `${d.message ? `: ${d.message.replace(/[….]+$/, "")}` : ""}. Potrai fare domande appena è pronto.`, progressBar(d.progress || 0)));
    } else if (d.status === "failed") {
      items.push(h("div", { class: "alert error" }, `Elaborazione non riuscita: ${d.error || "errore sconosciuto"} `,
        h("button", { class: "small-btn", onclick: reindex }, "Riprova")));
    } else if (!llmReady()) {
      items.push(h("div", { class: "alert warn" }, "Nessun modello linguistico scelto: la chat è disattivata. ",
        me.is_admin ? h("a", { href: "#/docs/models" }, "Scegli un modello") : "Chiedi a un amministratore di sceglierne uno."));
    } else if (ws.llm && !ws.llm.running) {
      items.push(h("div", { class: "alert warn" }, "Ollama non è in esecuzione: avvialo sul Jetson con  sudo systemctl start ollama"));
    }
    for (const n of d.notes || []) items.push(h("div", { class: "alert info small" }, n));
    setKids(banner, items);
    const canAsk = d.status === "ready" && llmReady();
    input.disabled = !canAsk || ws.busy;
    sendBtn.disabled = !canAsk || ws.busy;
  }

  function watchProcessing() {
    if (ws.doc.status !== "processing") return;
    const tick = async () => {
      if (!root.isConnected) return;
      try {
        ws.doc = { ...ws.doc, ...(await api(`/api/documents/${doc.id}`)) };
      } catch { return; }
      renderBanner(); renderHead();
      if (ws.doc.status === "processing") pollTimer = setTimeout(tick, 1500);
      else if (!ws.messages.length) renderFeed();
    };
    pollTimer = setTimeout(tick, 1200);
  }

  // ---- invio di una domanda
  async function send(text) {
    text = String(text || "").trim();
    if (!text || ws.busy || ws.doc.status !== "ready") return;
    ws.busy = true; ws.stick = true;
    input.value = ""; autoGrow();
    stopBtn.style.display = ""; sendBtn.style.display = "none";
    renderBanner();

    if (!ws.conv) {
      try {
        ws.conv = await api(`/api/documents/${docId}/conversations`, { method: "POST" });
        history.replaceState(null, "", convHref(ws.conv.id));
        currentHash = location.hash;
      } catch (e) { ws.busy = false; finishUi(); toast(e.message); return; }
    }
    if (!ws.messages.length) setKids(feed);
    feed.append(messageEl({ role: "user", content: text }));
    const status = h("div", { class: "typing small muted" }, "Invio…");
    const md = h("div", { class: "md" });
    const live = h("div", { class: "msg assistant" }, md, status);
    feed.append(live);
    scrollDown(true);

    let buffer = "", sources = [], saved = null, queued = false, failed = null;
    const paint = () => { queued = false; md.innerHTML = markdown(buffer, { cite: true }); scrollDown(); };
    const ctrl = new AbortController();
    activeStream = ctrl;
    try {
      await streamAnswer(ws.conv.id, text, (ev) => {
        if (ev.type === "user_message") {
          ws.messages.push(ev.message);
          ws.conv.title = ev.title;
          if (!ws.convs.some((c) => c.id === ws.conv.id)) ws.convs.unshift({ ...ws.conv, message_count: 1, updated_at: Date.now() / 1000 });
          else ws.convs.find((c) => c.id === ws.conv.id).updated_at = Date.now() / 1000;
          renderConvs(); renderTabs();
        } else if (ev.type === "status") {
          status.textContent = ev.text;
        } else if (ev.type === "sources") {
          sources = ev.sources || [];
        } else if (ev.type === "token") {
          buffer += ev.text;
          status.textContent = "";
          if (!queued) { queued = true; requestAnimationFrame(paint); }
        } else if (ev.type === "done") {
          saved = ev.message;
        } else if (ev.type === "error") {
          failed = ev;
        }
      }, ctrl.signal);
    } catch (e) {
      if (e.name !== "AbortError") failed = { detail: e.message };
    }
    activeStream = null;
    if (saved) {
      ws.messages.push(saved);
      live.replaceWith(messageEl(saved));
    } else {
      const aborted = ctrl.signal.aborted;
      if (failed && failed.message) ws.messages.push(failed.message);
      if (buffer.trim()) {
        live.replaceWith(messageEl({ role: "assistant", content: buffer, sources, status: aborted ? "stopped" : failed ? "error" : "ok", model: null }));
      } else if (failed) {
        live.replaceWith(h("div", { class: "msg assistant" }, h("div", { class: "alert error small" }, failed.detail || "Errore")));
      } else {
        live.remove();
      }
      if (failed && !buffer.trim()) { /* l'errore resta visibile nella conversazione */ }
    }
    ws.busy = false;
    finishUi();
    scrollDown(true);
    // L'elenco delle conversazioni si aggiorna con i dati del server (conteggi, ordine).
    api(`/api/documents/${docId}/conversations`).then((list) => { ws.convs = list; renderConvs(); renderTabs(); }).catch(() => {});
  }
  function finishUi() {
    stopBtn.style.display = "none"; sendBtn.style.display = "";
    renderBanner();
    if (!input.disabled) input.focus();
  }

  function autoGrow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); }
  });
  composer.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  stopBtn.addEventListener("click", stopChatStream);

  // ---- avvio
  setKids($main, root);
  renderHead(); renderBanner(); renderConvs(); renderTabs(); renderFeed(); openPage(page, false);
  watchProcessing();
  if (ws.doc.status === "ready" && !ws.messages.length && window.innerWidth > 900) input.focus();
}

// ------------------------------------------------------------------ cronologia delle conversazioni

async function viewDocHistory() {
  setNav("docs", "history");
  const list = h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0" }, "Caricamento…"));
  const search = h("input", { type: "search", placeholder: "Cerca tra titoli, domande e documenti…", "aria-label": "Cerca" });
  setKids($main,
    h("div", { class: "row spread" }, h("h1", {}, "Cronologia conversazioni"),
      me.is_admin ? h("label", { class: "check small" },
        h("input", { type: "checkbox", checked: showAllDocs, onchange: (e) => { showAllDocs = e.target.checked; viewDocHistory(); } }),
        "Tutti gli utenti") : null),
    h("p", { class: "muted" }, "Tutte le conversazioni avute con i tuoi documenti, dalla più recente."),
    h("div", { class: "field" }, search), list);
  let all;
  try { all = await api(`/api/conversations?all=${showAllDocs}`); } catch (e) { return setKids(list, errorBox(e.message)); }
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const shown = all.filter((c) => !q || [c.title, c.last_question, c.document_title].some((t) => String(t || "").toLowerCase().includes(q)));
    if (!shown.length) {
      setKids(list, h("p", { class: "muted", style: "margin:0" }, all.length ? "Nessun risultato." : "Nessuna conversazione ancora. Apri un documento e fai una domanda."));
      return;
    }
    setKids(list, shown.map((c) => h("a", { class: "job-row", href: `#/docs/${c.document_id}/c/${c.id}` },
      h("div", { class: "job-main" },
        h("div", { class: "job-title" }, c.title),
        h("div", { class: "job-sub" }, [c.document_title, plural(Math.ceil(c.message_count / 2), "domanda", "domande"),
          fmtAgo(c.updated_at), showAllDocs ? c.username : null].filter(Boolean).join(" · "))),
      h("span", { class: "small muted" }, "Apri"))));
  };
  search.addEventListener("input", render);
  render();
}

// ------------------------------------------------------------------ modelli linguistici

function viewLlmModels() {
  setNav("docs", "models");
  const card = h("div", { class: "card" });
  setKids($main, h("h1", {}, "Modelli linguistici"),
    h("p", { class: "muted" }, "I modelli che rispondono alle domande nella chat con i documenti (eseguiti in locale con Ollama). "
      + "Li scarichi e li scegli tu."), card);
  renderLlmSection(card);
}

// ------------------------------------------------------------------ gestione dei modelli (Ollama)

const CONTEXT_LABEL = { 2048: "2048 (più veloce)", 4096: "4096 (consigliato)", 8192: "8192 (documenti lunghi)", 16384: "16384 (lento, molta memoria)" };

/** Avvisi se un modello non rispetta i parametri consigliati per il Jetson Orin Nano. */
function llmWarnings(m) {
  const w = [];
  const q = (m.quantization || "").toUpperCase();
  // Gli embedding sono piccoli e su Ollama sono quasi sempre F16: l'avviso vale solo per gli LLM.
  if (!m.embedding && q && /^(F16|F32|BF16)$/.test(q)) w.push("non quantizzato: lento e pesante");
  if (!m.embedding && m.size_gb > 3.5) w.push("grande per il Jetson: lento, poca memoria per YOLO");
  if (m.embedding && m.size_gb > 1.3) w.push("embedding pesante");
  return w;
}

function llmGuide() {
  return h("details", { class: "guide" }, h("summary", {}, "Quali modelli scegliere per il Jetson Orin Nano (8 GB)"),
    h("h3", {}, "LLM (risponde alle domande)"),
    h("ul", {},
      h("li", {}, h("b", {}, "Dimensione: "), "1–4 miliardi di parametri (tag ", h("code", {}, ":1.5b"), ", ", h("code", {}, ":3b"), ", ", h("code", {}, ":4b"),
        "). I modelli da 7–8 miliardi girano, ma sono molto più lenti e lasciano poca memoria a YOLO."),
      h("li", {}, h("b", {}, "Quantizzazione: "), h("code", {}, "Q4_K_M"), " (è quella dei tag normali di Ollama). Evita le varianti ",
        h("code", {}, "fp16"), " e ", h("code", {}, "q8"), ": occupano 2–4 volte la memoria e sono più lente."),
      h("li", {}, h("b", {}, "Peso del file: "), "sotto i ~3 GB."),
      h("li", {}, h("b", {}, "Contesto: "), "4096 va bene quasi sempre; 8192 se vuoi che documenti più lunghi vengano letti per intero (più memoria e più lento); 2048 è il più veloce."),
      h("li", {}, h("b", {}, "Lingua: "), "per l'italiano funzionano bene le famiglie Qwen, Gemma e Llama recenti.")),
    h("h3", {}, "Embedding (trova le pagine giuste nei PDF lunghi, facoltativo)"),
    h("ul", {},
      h("li", {}, "Modelli piccoli (sotto ~600 milioni di parametri, file sotto ~1,2 GB)."),
      h("li", {}, "Per documenti in italiano scegli un modello ", h("b", {}, "multilingue"), "; molti modelli di embedding sono addestrati soprattutto sull'inglese."),
      h("li", {}, "Senza embedding la ricerca nel PDF usa solo le parole chiave: funziona, ma capisce meno i sinonimi.")),
    h("p", { class: "small muted" }, "Per scaricare un modello scrivi il nome esatto dalla libreria su ollama.com (es. ",
      h("code", {}, "famiglia:3b"), "). Dopo il download, sceglilo qui sopra e premi Salva. ",
      "Puoi provare più modelli e tenere quello che ti dà il miglior compromesso tra velocità e qualità sui tuoi documenti."));
}

async function renderLlmSection(card) {
  let timer = null;
  async function load() {
    clearTimeout(timer);
    let s;
    try { s = await api("/api/llm"); } catch (e) { return setKids(card, errorBox(e.message)); }
    const pulls = Object.entries(s.pulls || {});
    const llms = s.installed.filter((m) => !m.embedding);
    const embeds = s.installed.filter((m) => m.embedding);
    const opt = (m, selected) => h("option", { value: m.name, selected: m.name === selected },
      `${m.name}${m.parameter_size ? ` · ${m.parameter_size}` : ""}${m.quantization ? ` · ${m.quantization}` : ""}`);
    const dis = !me.is_admin;

    const llmSel = h("select", { disabled: dis },
      h("option", { value: "", selected: !s.llm_model }, "— nessuno —"),
      llms.length ? h("optgroup", { label: "Modelli linguistici" }, llms.map((m) => opt(m, s.llm_model))) : null,
      embeds.length ? h("optgroup", { label: "Altri" }, embeds.map((m) => opt(m, s.llm_model))) : null);
    const embSel = h("select", { disabled: dis },
      h("option", { value: "", selected: !s.embed_model }, "Nessuno (solo parole chiave)"),
      embeds.length ? h("optgroup", { label: "Modelli di embedding" }, embeds.map((m) => opt(m, s.embed_model))) : null,
      llms.length ? h("optgroup", { label: "Altri" }, llms.map((m) => opt(m, s.embed_model))) : null);
    const ctxSel = h("select", { disabled: dis }, s.context_choices.map((c) => h("option", { value: c, selected: c === s.context }, CONTEXT_LABEL[c] || c)));
    const msg = h("div");

    const pullName = h("input", { type: "text", placeholder: "nome:tag dalla libreria Ollama", autocomplete: "off" });

    setKids(card,
      !s.running ? h("div", { class: "alert warn" }, "Ollama non è in esecuzione: avvialo sul Jetson con  sudo systemctl start ollama") : null,
      s.running && !s.llm_model ? h("div", { class: "alert info small" }, "Nessun modello linguistico scelto: la chat con i documenti è disattivata finché non ne scegli uno.") : null,
      h("div", { class: "llm-form" },
        h("div", { class: "field" }, h("label", {}, "LLM in uso"), llmSel),
        h("div", { class: "field" }, h("label", {}, "Embedding in uso"), embSel),
        h("div", { class: "field" }, h("label", {}, "Contesto (token)"), ctxSel)),
      msg,
      me.is_admin ? h("button", { class: "primary", onclick: async () => {
        try {
          await api("/api/llm/select", { json: { llm_model: llmSel.value, embed_model: embSel.value, context: Number(ctxSel.value) } });
          toast("Scelta salvata");
          load();
        } catch (e) { setKids(msg, errorBox(e.message)); }
      } }, "Salva scelta") : null,

      h("h3", { style: "margin-top:20px" }, "Scaricati sul Jetson"),
      s.installed.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Modello"), h("th", {}, "Parametri"), h("th", {}, "Quantizzazione"), h("th", { class: "num" }, "Peso"), h("th", {}))),
        h("tbody", {}, s.installed.map((m) => {
          const inUse = m.name === s.llm_model ? "LLM in uso" : m.name === s.embed_model ? "embedding in uso" : null;
          const warns = llmWarnings(m);
          return h("tr", {},
            h("td", {}, h("b", {}, m.name), " ", m.embedding ? h("span", { class: "badge cancelled" }, "embedding") : null,
              inUse ? h("span", { class: "badge done", style: "margin-left:6px" }, inUse) : null,
              warns.map((w) => h("div", { class: "small", style: "color:var(--warn)" }, `⚠ ${w}`))),
            h("td", {}, m.parameter_size || "—"),
            h("td", {}, m.quantization || "—"),
            h("td", { class: "num" }, `${m.size_gb} GB`),
            h("td", { style: "text-align:right" }, me.is_admin && !inUse ? h("button", { class: "link", style: "color:var(--danger)", onclick: async () => {
              if (!confirm(`Eliminare ${m.name} dal Jetson?`)) return;
              try { await api(`/api/llm/models/${encodeURIComponent(m.name)}`, { method: "DELETE" }); load(); } catch (e) { alert(e.message); }
            } }, "Elimina") : null));
        })))) : h("p", { class: "muted small" }, s.running ? "Nessun modello scaricato." : "—"),

      me.is_admin && s.running ? h("form", { class: "row", style: "margin-top:12px", onsubmit: async (e) => {
        e.preventDefault();
        if (!pullName.value.trim()) return;
        try { await api("/api/llm/pull", { json: { name: pullName.value.trim() } }); pullName.value = ""; load(); } catch (ex) { alert(ex.message); }
      } }, h("div", { style: "flex:1;min-width:200px" }, pullName), h("button", { type: "submit" }, "Scarica modello")) : null,
      pulls.map(([name, p]) => h("div", { class: "pull" },
        h("div", { class: "row spread small" }, h("b", {}, name),
          h("span", { class: p.error ? "" : "muted", style: p.error ? "color:var(--danger)" : "" },
            p.error ? `Errore: ${p.error}` : p.total ? `${p.status} · ${(p.completed / 1e9).toFixed(2)} / ${(p.total / 1e9).toFixed(2)} GB` : p.status)),
        !p.error && !p.finished_at ? progressBar(p.total ? p.completed / p.total : 0) : null)),
      llmGuide());

    if (pulls.some(([, p]) => !p.finished_at) && location.hash === "#/docs/models") timer = setTimeout(load, 1500);
  }
  load();
}

