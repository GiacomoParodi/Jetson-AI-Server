"use strict";

/* Area «Analisi video»: nuova analisi, cronologia, pipeline e modelli di visione. */

let tasksCache = null;

async function loadTasks(force) {
  if (!tasksCache || force) tasksCache = await api("/api/tasks");
  return tasksCache;
}


/** Indirizzo della pagina di un lavoro: le analisi video stanno nella loro area. */
function jobHref(job) {
  return job.section === "video" ? `#/video/job/${job.id}` : `#/job/${job.id}`;
}

function viewNewAnalysis() { return viewNewJob("yolo_pipeline"); }

/** Modulo per avviare un compito: l'analisi video o uno strumento aggiunto con un plugin. */
async function viewNewJob(taskId) {
  let task;
  try {
    task = (await loadTasks(true)).find((t) => t.id === taskId);
  } catch (e) { return setKids($main, errorBox(e.message)); }
  if (!task) return setKids($main, errorBox("Strumento non trovato"));
  const isVideo = task.section === "video";
  setNav(isVideo ? "video" : null, "new");

  let file = null;
  const fileInput = h("input", { type: "file", accept: task.accept.join(","), style: "display:none" });
  const dzText = h("div", {}, h("b", {}, "Trascina qui il file"), " oppure tocca per sceglierlo",
    h("div", { class: "small" }, task.accept.join("  ")));
  const dz = h("div", { class: "dropzone", tabindex: 0, onclick: () => fileInput.click() }, dzText);
  const setFile = (f) => {
    file = f;
    setKids(dzText, h("b", {}, f.name), ` · ${fmtSize(f.size)}`, h("div", { class: "small" }, "Tocca per cambiare"));
  };
  fileInput.addEventListener("change", () => fileInput.files[0] && setFile(fileInput.files[0]));
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });

  const { fields, read } = paramFields(task.params);
  const msg = h("div");
  const bar = h("div", { style: "display:none;margin-bottom:12px" });
  const submit = h("button", { class: "primary", type: "submit" }, "Avvia");

  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      setKids(msg);
      if (task.needs_file && !file) return setKids(msg, errorBox("Seleziona un file"));
      const fd = new FormData();
      fd.append("task", task.id);
      fd.append("params", JSON.stringify(read()));
      if (file) fd.append("file", file);
      submit.disabled = true;
      bar.style.display = "block";
      const label = h("div", { class: "small muted" }, "Caricamento del file…");
      const pb = progressBar(0);
      setKids(bar, label, pb);
      try {
        const job = await uploadWithProgress(fd, (f) => {
          pb.firstChild.style.width = `${Math.round(f * 100)}%`;
          label.textContent = `Caricamento del file… ${Math.round(f * 100)}%`;
        });
        location.hash = jobHref(job);
      } catch (ex) {
        setKids(msg, errorBox(ex.message));
        submit.disabled = false;
        bar.style.display = "none";
      }
    },
  },
    msg,
    task.needs_file ? h("div", { class: "field" }, h("label", {}, "File"), dz, fileInput) : null,
    fields, bar, submit);

  setKids($main, 
    isVideo ? null : h("p", {}, h("a", { href: "#/" }, "← Pagina iniziale")),
    h("h1", {}, isVideo ? "Nuova analisi video" : task.title),
    h("p", { class: "muted" }, task.description),
    h("div", { class: "card" }, form));
}


// ------------------------------------------------------------------ lavori

let showAllJobs = false;

async function viewVideoHistory() {
  setNav("video", "history");
  const list = h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0" }, "Caricamento…"));
  const head = h("div", { class: "row spread" }, h("h1", {}, "Cronologia analisi"),
    me.is_admin ? h("label", { class: "check small" },
      h("input", { type: "checkbox", checked: showAllJobs, onchange: (e) => { showAllJobs = e.target.checked; viewVideoHistory(); } }),
      "Tutti gli utenti") : null);
  setKids($main, head, list);

  const load = async () => {
    let jobs;
    try { jobs = await api(`/api/jobs?section=video&all=${showAllJobs}`); } catch (e) { return setKids(list, errorBox(e.message)); }
    if (!jobs.length) {
      setKids(list, h("p", { class: "muted", style: "margin:0" }, "Nessuna analisi ancora. ", h("a", { href: "#/video/new" }, "Avvia la prima")));
      return;
    }
    setKids(list, ...jobs.map((j) => h("a", { class: "job-row", href: jobHref(j) },
      h("div", { class: "job-main" },
        h("div", { class: "job-title" }, j.task_title),
        h("div", { class: "job-sub" }, [j.input_name, fmtDate(j.created_at), showAllJobs ? j.username : null].filter(Boolean).join(" · "))),
      j.status === "running" ? h("div", { class: "job-progress" }, progressBar(j.progress)) : null,
      badge(j.status))));
    if (jobs.some((j) => ACTIVE.has(j.status)) && location.hash === "#/video/history") pollTimer = setTimeout(load, 2000);
  };
  load();
}

function renderResult(job) {
  const r = job.result || {};
  const nodes = [];
  const fileUrl = (f, dl) => `/api/jobs/${job.id}/files/${encodeURIComponent(f)}${dl ? "?download=true" : ""}`;

  if (r.summary) nodes.push(h("div", { class: "card answer", html: markdown(r.summary) }));
  for (const n of r.notes || []) nodes.push(h("div", { class: "alert info small" }, n));

  for (const o of (r.outputs || []).filter((o) => o.kind === "video" || o.kind === "image")) {
    nodes.push(h("div", { class: "card" },
      h("h3", {}, o.label || o.file),
      o.kind === "video"
        ? h("video", { class: "media", src: fileUrl(o.file), controls: true, playsinline: true, preload: "metadata" })
        : h("a", { href: fileUrl(o.file), target: "_blank" }, h("img", { class: "media", src: fileUrl(o.file), alt: o.label || "" }))));
  }

  if (r.table && r.table.rows && r.table.rows.length) {
    const isNum = (v) => typeof v === "number";
    nodes.push(h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, r.table.columns.map((c, i) => h("th", { class: r.table.rows.every((row) => isNum(row[i])) ? "num" : null }, c)))),
      h("tbody", {}, r.table.rows.map((row) => h("tr", {}, row.map((v) => h("td", { class: isNum(v) ? "num" : null }, v)))))))));
  }

  if (r.pipeline) {
    nodes.push(h("div", { class: "card" }, h("h3", {}, `Pipeline usata: ${r.pipeline.name}`),
      h("p", { class: "small muted" }, "I colori corrispondono a quelli dei riquadri nel video."),
      treeSummary(r.pipeline.tree)));
  }

  if (r.sources && r.sources.length) {
    nodes.push(h("div", { class: "card" }, h("h3", {}, "Parti del documento usate"),
      r.sources.map((s) => h("details", {}, h("summary", {}, s.label), h("pre", {}, s.text)))));
  }

  if (r.outputs && r.outputs.length) {
    nodes.push(h("div", { class: "card" }, h("h3", {}, "Scarica"),
      h("div", { class: "row" }, r.outputs.map((o) => h("a", { class: "btn", href: fileUrl(o.file, true) }, `⬇ ${o.label || o.file}`)))));
  }
  return nodes;
}

function rerunForm(job, task) {
  if (!task || !task.params.length) return null;
  const { fields, read } = paramFields(task.params, job.params);
  const msg = h("div");
  const submit = h("button", { class: "primary", type: "submit" }, task.rerun_label);
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      submit.disabled = true;
      try {
        const nj = await api(`/api/jobs/${job.id}/rerun`, { json: { params: read() } });
        location.hash = jobHref(nj);
      } catch (ex) { setKids(msg, errorBox(ex.message)); submit.disabled = false; }
    },
  }, msg, fields, submit);
  // Per i PDF la domanda va svuotata: si vuole scriverne una nuova.
  const q = form.querySelector("textarea");
  if (q) q.value = "";
  return h("div", { class: "card" }, h("h2", {}, task.rerun_label), h("p", { class: "small muted" }, `Stesso file: ${job.input_name || "—"}`), form);
}

async function viewJob(id, area) {
  setNav(area === "video" ? "video" : null, "history");
  const backHref = area === "video" ? "#/video/history" : "#/";
  const container = h("div");
  setKids($main, h("p", {}, h("a", { href: backHref }, area === "video" ? "← Cronologia analisi" : "← Pagina iniziale")), container);
  let lastStatus = null;
  let tasks = [];
  try { tasks = await loadTasks(); } catch { /* opzionale */ }

  const load = async () => {
    let job;
    try { job = await api(`/api/jobs/${id}`); } catch (e) { return setKids(container, errorBox(e.message)); }
    const active = ACTIVE.has(job.status);
    if (!active && lastStatus === job.status) return; // niente da aggiornare
    lastStatus = job.status;

    const task = tasks.find((t) => t.id === job.task);
    const elapsed = job.started_at ? (job.finished_at || Date.now() / 1000) - job.started_at : null;
    const params = Object.entries(job.params || {}).filter(([k, v]) => v !== "" && v !== null && !k.startsWith("_"));
    const paramDef = (k) => (task && task.params.find((p) => p.name === k)) || {};
    const paramLabel = (k) => paramDef(k).label || k;
    const paramValue = (k, v) => {
      if (typeof v === "boolean") return v ? "sì" : "no";
      if (k === "pipeline" && job.params._pipeline) return job.params._pipeline.name;
      const choice = (paramDef(k).choices || []).find((c) => typeof c === "object" && String(c.value) === String(v));
      return choice ? choice.label : String(v);
    };

    const header = h("div", { class: "card" },
      h("div", { class: "row spread" }, h("h1", { style: "margin:0" }, job.task_title), badge(job.status)),
      h("dl", { class: "kv", style: "margin-top:12px" },
        job.input_name ? [h("dt", {}, "File"), h("dd", {}, h("a", { href: `/api/jobs/${job.id}/input` }, job.input_name))] : null,
        params.map(([k, v]) => [h("dt", {}, paramLabel(k)), h("dd", {}, paramValue(k, v))]),
        h("dt", {}, "Creato"), h("dd", {}, `${fmtDate(job.created_at)}${job.username !== me.username ? ` da ${job.username}` : ""}`),
        elapsed != null ? [h("dt", {}, "Durata"), h("dd", {}, fmtDuration(elapsed))] : null),
      active ? h("div", { style: "margin-top:14px" },
        h("div", { class: "small muted", style: "margin-bottom:6px" },
          job.status === "queued"
            ? (job.queue_position ? `In coda: ${job.queue_position} lavori prima di questo` : "In coda: è il prossimo")
            : `${job.message || ""} ${job.status === "running" ? `(${Math.round(job.progress * 100)}%)` : ""}`),
        progressBar(job.status === "running" ? job.progress : 0)) : null,
      h("div", { class: "row", style: "margin-top:14px" },
        active ? h("button", { class: "danger", onclick: async () => {
          try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); toast("Annullamento richiesto"); } catch (e) { toast(e.message); }
        } }, "Annulla") : null,
        !active ? h("button", { class: "danger", onclick: async () => {
          if (!confirm("Eliminare il lavoro e i suoi file?")) return;
          try { await api(`/api/jobs/${id}`, { method: "DELETE" }); location.hash = backHref; } catch (e) { toast(e.message); }
        } }, "Elimina") : null));

    const body = [];
    if (job.status === "failed") body.push(errorBox(job.error || "Errore sconosciuto"));
    if (job.status === "done") body.push(...renderResult(job));
    if (!active && job.status !== "cancelled") body.push(rerunForm(job, task));

    setKids(container, header, ...body.filter(Boolean));
    if (active && location.hash.endsWith(`/job/${id}`)) pollTimer = setTimeout(load, 1500);
  };
  load();
}


// ------------------------------------------------------------------ modelli

const TASK_LABEL = { detect: "Detection", segment: "Segmentazione", classify: "Classificazione" };
// Stessi colori dei riquadri disegnati nei video (app/tasks/yolo_pipeline.py, convertiti da BGR).
const NODE_COLORS = ["#ff9f1a", "#2e86ff", "#3cb44b", "#e040fb", "#00c8c8", "#ff4d4d", "#9c6bff", "#c8d400", "#ff7fbf", "#8d6e63"];

function modelStatus(m, chosen = true) {
  if (!chosen) return h("span", { class: "badge failed" }, "scegli un modello");
  if (!m) return h("span", { class: "badge failed" }, "modello mancante");
  if (m.status === "ready" && m.backend === "tensorrt") return h("span", { class: "badge done", title: m.speed ? `${m.speed.after_ms} ms per immagine` : "" }, "⚡ TensorRT");
  if (m.status === "ready") return h("span", { class: "badge cancelled", title: m.note || "" }, "PyTorch");
  if (m.status === "optimizing") return h("span", { class: "badge running" }, "in ottimizzazione…");
  if (m.status === "failed") return h("span", { class: "badge failed", title: m.error || "" }, "ottimizzazione fallita");
  return h("span", { class: "badge queued" }, "da ottimizzare");
}

function taskBadge(task) { return h("span", { class: `badge type-${task}` }, TASK_LABEL[task] || task); }

/** Carica (o sostituisce) un file .pt. Restituisce il modello creato. */
function pickAndUploadModel(replace) {
  return new Promise((resolve, reject) => {
    const input = h("input", { type: "file", accept: ".pt", style: "display:none" });
    document.body.append(input);
    input.addEventListener("change", async () => {
      const f = input.files[0];
      input.remove();
      if (!f) return resolve(null);
      const fd = new FormData();
      fd.append("file", f);
      if (replace) fd.append("replace", replace);
      toast("Caricamento e verifica del modello…");
      try {
        const m = await api("/api/models", { method: "POST", body: fd });
        for (const w of m.warnings || []) alert(w);
        toast(`${m.name} caricato: ottimizzazione per il Jetson in coda`);
        resolve(m);
      } catch (e) { reject(e); }
    });
    input.click();
  });
}

async function viewVisionModels() {
  setNav("video", "models");
  const list = h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0" }, "Caricamento…"));
  setKids($main,
    h("div", { class: "row spread" }, h("h1", {}, "Modelli di visione"),
      me.is_admin ? h("button", { class: "primary", onclick: async () => {
        try { if (await pickAndUploadModel()) loadList(); } catch (e) { toast(e.message); }
      } }, "+ Carica modello (.pt)") : null),
    h("p", { class: "muted" }, "Modelli di rilevamento, segmentazione o classificazione addestrati con Ultralytics (YOLO). "
      + "Dopo il caricamento ogni modello viene ricompilato automaticamente con TensorRT FP16 per la GPU del Jetson "
      + "(qualche minuto, una volta sola): lo stato e il guadagno di velocità compaiono qui."),
    list);

  let timer = null;
  async function loadList() {
    clearTimeout(timer);
    let models;
    try { models = await api("/api/models"); } catch (e) { return setKids(list, errorBox(e.message)); }
    if (!models.length) {
      setKids(list, h("p", { class: "muted", style: "margin:0" }, "Nessun modello. ",
        me.is_admin ? "Carica il primo con il pulsante in alto." : "Chiedi a un amministratore di caricarne uno."));
      return;
    }
    setKids(list, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Modello"), h("th", {}, "Tipo"), h("th", {}, "Classi"), h("th", {}, "Ottimizzazione"), h("th", {}))),
      h("tbody", {}, models.map((m) => h("tr", {},
        h("td", {}, h("b", {}, m.name), h("div", { class: "small muted" }, `${m.size_mb} MB · ${m.imgsz || "?"} px`)),
        h("td", {}, taskBadge(m.task)),
        h("td", { class: "small" }, m.classes.slice(0, 10).join(", ") + (m.classes.length > 10 ? ` … (+${m.classes.length - 10})` : "")),
        h("td", {}, modelStatus(m),
          m.speed ? h("div", { class: "small muted" }, `${m.speed.before_ms} → ${m.speed.after_ms} ms (${m.speed.speedup}x)`) : null,
          m.status === "failed" && m.error ? h("div", { class: "small", style: "color:var(--danger)" }, m.error.slice(0, 200)) : null,
          m.note ? h("div", { class: "small muted" }, m.note) : null),
        h("td", { style: "text-align:right;white-space:nowrap" }, me.is_admin ? [
          h("button", { class: "link", onclick: async () => {
            try { if (await pickAndUploadModel(m.name)) loadList(); } catch (e) { toast(e.message); }
          } }, "Sostituisci"),
          h("button", { class: "link", onclick: async () => {
            try { await api(`/api/models/${encodeURIComponent(m.name)}/optimize`, { method: "POST" }); toast("Ottimizzazione in coda"); loadList(); } catch (e) { toast(e.message); }
          } }, "Ri-ottimizza"),
          h("button", { class: "link", style: "color:var(--danger)", onclick: async () => {
            if (!confirm(`Eliminare ${m.name}?`)) return;
            try { await api(`/api/models/${encodeURIComponent(m.name)}`, { method: "DELETE" }); loadList(); } catch (e) { alert(e.message); }
          } }, "Elimina")] : null)))))));
    if (models.some((m) => m.status === "pending" || m.status === "optimizing") && location.hash === "#/video/models") {
      timer = setTimeout(loadList, 4000);
    }
  }
  loadList();
}


// ------------------------------------------------------------------ pipeline

/** Albero in sola lettura (risultati dei lavori, elenco pipeline). */
function treeSummary(tree) {
  let idx = 0;
  const render = (nodes) => h("ul", { class: "tree compact" }, nodes.map((n) => {
    const color = NODE_COLORS[idx++ % NODE_COLORS.length];
    return h("li", {},
      h("div", { class: "tnode-mini" }, h("span", { class: "dot", style: `background:${color}` }), h("b", {}, n.name),
        h("span", { class: "muted small" }, ` ${n.model}`),
        n.parent_classes && n.parent_classes.length ? h("span", { class: "muted small" }, ` · se ${n.parent_classes.join(", ")}`) : null),
      n.children && n.children.length ? render(n.children) : null);
  }));
  return render(tree || []);
}

async function viewPipelines() {
  setNav("video", "pipelines");
  const list = h("div", {}, h("p", { class: "muted" }, "Caricamento…"));
  setKids($main, 
    h("div", { class: "row spread" }, h("h1", {}, "Pipeline di analisi"),
      me.is_admin ? h("button", { class: "primary", onclick: async () => {
        const name = prompt("Nome della nuova pipeline:");
        if (!name) return;
        try { const p = await api("/api/pipelines", { json: { name, tree: [] } }); location.hash = `#/video/pipeline/${p.id}`; } catch (e) { alert(e.message); }
      } }, "+ Nuova pipeline") : null),
    h("p", { class: "muted" }, "Una pipeline è un albero di modelli: i nodi radice guardano tutto il fotogramma, "
      + "i figli guardano gli oggetti trovati dal padre. Si usa dalla sezione «Nuova analisi»."),
    list);
  let items;
  try { items = await api("/api/pipelines"); } catch (e) { return setKids(list, errorBox(e.message)); }
  if (!items.length) {
    return setKids(list, h("div", { class: "card muted" }, me.is_admin ? "Nessuna pipeline: creane una." : "Nessuna pipeline ancora."));
  }
  setKids(list, h("div", { class: "grid" }, items.map((p) => h("a", { class: "card task-card", href: `#/video/pipeline/${p.id}`, style: "text-decoration:none;color:inherit" },
    h("h2", {}, p.name),
    p.description ? h("p", {}, p.description) : null,
    p.node_count ? treeSummary(p.tree) : h("p", {}, "Vuota"),
    h("div", { class: "small muted", style: "margin-top:8px" }, `${p.node_count} nodi · aggiornata ${fmtDate(p.updated_at)}`)))));
}

function newNodeId() { return Math.random().toString(36).slice(2, 12); }

function findNode(tree, id, parent = null) {
  for (let i = 0; i < tree.length; i++) {
    const n = tree[i];
    if (n.id === id) return { node: n, parent, siblings: tree, index: i };
    const r = findNode(n.children || [], id, n);
    if (r) return r;
  }
  return null;
}

function cloneNode(n) {
  return { ...JSON.parse(JSON.stringify(n)), id: newNodeId(), children: (n.children || []).map(cloneNode) };
}

/** Elenco di caselle per scegliere classi (nessuna spuntata = tutte). */
function classPicker(all, selected, onChange, disabled) {
  const chosen = new Set(selected);
  const filter = h("input", { type: "text", placeholder: "Cerca classe…", style: all.length > 12 ? "margin-bottom:6px" : "display:none" });
  const box = h("div", { class: "class-picker" });
  const summary = h("div", { class: "help" });
  const update = () => {
    summary.textContent = chosen.size ? `${chosen.size} selezionate: ${[...chosen].join(", ")}` : "Nessuna selezionata = tutte le classi";
  };
  const render = () => {
    const q = filter.value.toLowerCase();
    setKids(box, ...all.filter((c) => c.toLowerCase().includes(q)).map((c) => {
      const cb = h("input", { type: "checkbox", checked: chosen.has(c), disabled });
      cb.addEventListener("change", () => { cb.checked ? chosen.add(c) : chosen.delete(c); update(); onChange(all.filter((x) => chosen.has(x))); });
      return h("label", { class: "chip" }, cb, c);
    }));
  };
  filter.addEventListener("input", render);
  render(); update();
  return h("div", {}, filter, box, summary);
}

async function viewPipeline(id) {
  setNav("video", "pipelines");
  setKids($main, h("p", { class: "muted" }, "Caricamento…"));
  let p, models;
  try {
    [p, models] = await Promise.all([api(`/api/pipelines/${id}`), api("/api/models")]);
  } catch (e) { return setKids($main, errorBox(e.message)); }

  const editable = me.is_admin;
  const state = { name: p.name, description: p.description || "", tree: p.tree, version: p.version, selected: null, dirty: false };
  let modelsByName = Object.fromEntries(models.map((m) => [m.name, m]));
  leaveGuard = () => state.dirty;

  const nameInput = h("input", { type: "text", value: state.name, disabled: !editable, class: "title-input", "aria-label": "Nome della pipeline" });
  const descInput = h("input", { type: "text", value: state.description, disabled: !editable, placeholder: "Descrizione (facoltativa)" });
  nameInput.addEventListener("input", () => { state.name = nameInput.value; markDirty(); });
  descInput.addEventListener("input", () => { state.description = descInput.value; markDirty(); });

  const dirtyLabel = h("span", { class: "small muted" });
  const msg = h("div");
  const saveBtn = h("button", { class: "primary", disabled: true, onclick: save }, "Salva");
  const treeBox = h("div", { class: "card tree-card" });
  const panel = h("div", { class: "card node-panel" });

  function markDirty() {
    state.dirty = true;
    saveBtn.disabled = false;
    dirtyLabel.textContent = "Modifiche non salvate";
  }

  async function save() {
    setKids(msg);
    // Prima di salvare porta l'utente sul primo nodo senza modello.
    const missing = (function find(ns) { for (const x of ns) { if (!x.model) return x; const c = find(x.children || []); if (c) return c; } return null; })(state.tree);
    if (missing) {
      select(missing.id);
      setKids(msg, errorBox(`Scegli il modello del nodo «${missing.name}» prima di salvare.`));
      return;
    }
    saveBtn.disabled = true;
    try {
      const r = await api(`/api/pipelines/${id}`, { method: "PUT", json: { name: state.name, description: state.description, tree: state.tree, version: state.version } });
      state.tree = r.tree;
      state.version = r.version;
      state.dirty = false;
      dirtyLabel.textContent = "Salvata";
      renderTree();
      renderPanel();
      toast("Pipeline salvata");
    } catch (e) {
      setKids(msg, errorBox(e.message));
      saveBtn.disabled = false;
    }
  }

  // ---- operazioni sui nodi
  function defaultNode(parent) {
    // Nessun modello preimpostato: lo sceglie l'utente.
    return { id: newNodeId(), name: parent ? "Nuovo figlio" : "Nuovo nodo", model: "", conf: 0.35,
      classes: [], parent_classes: [], padding: 0.05, children: [] };
  }
  function addRoot() { const n = defaultNode(null); state.tree.push(n); select(n.id); markDirty(); }
  function addChild(id) {
    const f = findNode(state.tree, id);
    const n = defaultNode(f.node);
    (f.node.children = f.node.children || []).push(n);
    select(n.id); markDirty();
  }
  function addSibling(id) {
    const f = findNode(state.tree, id);
    const n = defaultNode(f.parent);
    f.siblings.splice(f.index + 1, 0, n);
    select(n.id); markDirty();
  }
  function duplicate(id) {
    const f = findNode(state.tree, id);
    const n = cloneNode(f.node);
    n.name = `${n.name} (copia)`;
    f.siblings.splice(f.index + 1, 0, n);
    select(n.id); markDirty();
  }
  function move(id, delta) {
    const f = findNode(state.tree, id);
    const j = f.index + delta;
    if (j < 0 || j >= f.siblings.length) return;
    [f.siblings[f.index], f.siblings[j]] = [f.siblings[j], f.siblings[f.index]];
    markDirty(); renderTree();
  }
  function remove(id) {
    const f = findNode(state.tree, id);
    const nChildren = JSON.stringify(f.node.children || []).split('"id"').length - 1;
    if (!confirm(nChildren ? `Eliminare "${f.node.name}" e i suoi ${nChildren} nodi discendenti?` : `Eliminare "${f.node.name}"?`)) return;
    f.siblings.splice(f.index, 1);
    if (state.selected === id) state.selected = null;
    markDirty(); renderTree(); renderPanel();
  }
  function select(nodeId) {
    state.selected = nodeId;
    renderTree();
    renderPanel();
    if (nodeId && window.innerWidth <= 860) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---- disegno dell'albero
  function renderTree() {
    let idx = 0;
    const renderNodes = (nodes, parent) => h("ul", { class: "tree" }, nodes.map((n) => {
      const color = NODE_COLORS[idx++ % NODE_COLORS.length];
      const m = modelsByName[n.model];
      const parentModel = parent && modelsByName[parent.model];
      const scope = !parent ? "tutto il frame"
        : parentModel && parentModel.task === "classify"
          ? `se «${parent.name}» = ${n.parent_classes.length ? n.parent_classes.join("/") : "qualsiasi"}`
          : `ritagli di ${n.parent_classes.length ? n.parent_classes.join(", ") : "ogni oggetto"}`;
      const tb = (label, title, fn, cls) => h("button", { class: `tbtn ${cls || ""}`, title, onclick: (e) => { e.stopPropagation(); fn(); } }, label);
      return h("li", {},
        h("div", { class: `tnode ${state.selected === n.id ? "selected" : ""}`, style: `--node-color:${color}`, tabindex: 0,
          onclick: () => select(n.id), onkeydown: (e) => { if (e.key === "Enter") select(n.id); } },
          h("div", { class: "tnode-head" },
            h("span", { class: "dot" }), h("b", { class: "tnode-name" }, n.name),
            m ? taskBadge(m.task) : null, modelStatus(m, !!n.model)),
          h("div", { class: "small muted tnode-sub" }, `${n.model || "nessun modello"} · ${scope}`
            + (n.classes.length ? ` · tiene: ${n.classes.join(", ")}` : "")),
          editable ? h("div", { class: "tnode-actions" },
            m && m.task !== undefined ? tb("+ Figlio", "Aggiungi un nodo che lavora sui risultati di questo", () => addChild(n.id)) : null,
            tb("+ Fratello", "Aggiungi un nodo allo stesso livello", () => addSibling(n.id)),
            tb("↑", "Sposta su", () => move(n.id, -1)),
            tb("↓", "Sposta giù", () => move(n.id, 1)),
            tb("Duplica", "Duplica il nodo con i suoi figli", () => duplicate(n.id)),
            tb("Elimina", "Elimina il nodo e i suoi figli", () => remove(n.id), "danger")) : null),
        n.children && n.children.length ? renderNodes(n.children, n) : null);
    }));
    setKids(treeBox, 
      h("div", { class: "row spread", style: "margin-bottom:8px" }, h("h2", { style: "margin:0" }, "Struttura"),
        editable ? h("button", { onclick: addRoot }, "+ Nodo radice") : null),
      state.tree.length ? renderNodes(state.tree, null)
        : h("p", { class: "muted" }, editable ? "La pipeline è vuota: aggiungi il primo nodo radice." : "La pipeline è vuota."),
      !models.length && editable ? h("div", { class: "alert warn small" }, "Non ci sono ancora modelli: caricane uno dalla sezione Modelli di visione o dal pannello del nodo.") : "");
  }

  // ---- pannello del nodo selezionato
  function renderPanel() {
    const f = state.selected && findNode(state.tree, state.selected);
    if (!f) {
      setKids(panel, h("h2", {}, "Come funziona"),
        h("ul", { class: "help-list" },
          h("li", {}, h("b", {}, "Nodi radice: "), "girano sull'intero fotogramma del video o sull'immagine."),
          h("li", {}, h("b", {}, "Figli di una detection o segmentazione: "), "girano sul ritaglio di ogni oggetto trovato dal padre, solo per le classi del padre che scegli (es. padre “scrivanie” → figlio “oggetti sulla scrivania”)."),
          h("li", {}, h("b", {}, "Figli di una classificazione: "), "girano sulla stessa immagine del padre quando la classe predetta è tra quelle scelte."),
          h("li", {}, h("b", {}, "Fratelli: "), "modelli indipendenti che lavorano sulla stessa immagine.")),
        h("p", { class: "muted small" }, editable ? "Seleziona un nodo per modificarlo." : "Solo gli amministratori possono modificare le pipeline."));
      return;
    }
    const n = f.node;
    const parent = f.parent;
    const m = modelsByName[n.model];
    const parentModel = parent && modelsByName[parent.model];
    const dis = !editable;
    const field = (label, input, help) => h("div", { class: "field" }, h("label", {}, label), input, help ? h("div", { class: "help" }, help) : null);
    const changed = () => { markDirty(); renderTree(); };

    const name = h("input", { type: "text", value: n.name, disabled: dis, maxlength: 60 });
    name.addEventListener("input", () => { n.name = name.value; changed(); });

    const modelSel = h("select", { disabled: dis },
      h("option", { value: "", selected: !n.model }, "— scegli —"),
      ["detect", "segment", "classify"].map((t) => {
        const ms = models.filter((x) => x.task === t);
        return ms.length ? h("optgroup", { label: TASK_LABEL[t] }, ms.map((x) =>
          h("option", { value: x.name, selected: x.name === n.model }, x.name))) : null;
      }));
    modelSel.addEventListener("change", () => setModel(modelSel.value));

    function setModel(name) {
      n.model = name;
      const nm = modelsByName[name];
      const cls = nm ? nm.classes : [];
      n.classes = n.classes.filter((c) => cls.includes(c));
      for (const c of n.children || []) c.parent_classes = c.parent_classes.filter((x) => cls.includes(x));
      changed(); renderPanel();
    }

    const conf = h("input", { type: "number", min: 0.01, max: 0.99, step: 0.05, value: n.conf, disabled: dis });
    conf.addEventListener("input", () => { n.conf = Number(conf.value); changed(); });

    const parentIsCrop = parentModel && parentModel.task !== "classify";
    const padding = h("input", { type: "number", min: 0, max: 50, step: 5, value: Math.round((n.padding ?? 0.05) * 100), disabled: dis });
    padding.addEventListener("input", () => { n.padding = Number(padding.value) / 100; changed(); });

    const describe = !parent ? "Questo nodo gira sull'intero fotogramma."
      : parentIsCrop ? `Questo nodo gira sul ritaglio di ogni oggetto trovato da «${parent.name}».`
        : `Questo nodo gira sulla stessa immagine di «${parent.name}», quando la classificazione corrisponde.`;

    setKids(panel, 
      h("div", { class: "row spread" }, h("h2", { style: "margin:0" }, "Nodo"), h("button", { class: "link", onclick: () => select(null) }, "Chiudi")),
      h("p", { class: "small muted" }, describe),
      field("Nome", name),
      field("Modello", h("div", {}, modelSel,
        m ? h("div", { class: "row", style: "margin-top:6px" }, taskBadge(m.task), modelStatus(m),
          m.speed ? h("span", { class: "small muted" }, `${m.speed.after_ms} ms/immagine`) : null) : null,
        editable ? h("div", { class: "row", style: "margin-top:6px" },
          h("button", { type: "button", class: "small-btn", onclick: async () => {
            try {
              const nm = await pickAndUploadModel();
              if (!nm) return;
              models = await api("/api/models");
              modelsByName = Object.fromEntries(models.map((x) => [x.name, x]));
              setModel(nm.name);
            } catch (e) { alert(e.message); }
          } }, "Carica nuovo modello…"),
          n.model ? h("button", { type: "button", class: "small-btn", onclick: async () => {
            const users = [];
            (function walk(ns) { for (const x of ns) { if (x.model === n.model) users.push(x.name); walk(x.children || []); } })(state.tree);
            if (!confirm(`Sostituire il file di ${n.model}? Cambierà per tutti i nodi e le pipeline che lo usano (${users.join(", ")}).`)) return;
            try {
              await pickAndUploadModel(n.model);
              models = await api("/api/models");
              modelsByName = Object.fromEntries(models.map((x) => [x.name, x]));
              setModel(n.model);
            } catch (e) { alert(e.message); }
          } }, "Sostituisci file del modello…") : null) : null),
        "Il tipo (detection, segmentazione, classificazione) dipende dal modello."),
      field(m && m.task === "classify" ? "Confidenza minima della classe predetta" : "Confidenza minima", conf,
        "Risultati sotto questa soglia vengono scartati"),
      m ? field(m.task === "classify" ? "Classi da accettare" : "Classi da tenere",
        classPicker(m.classes, n.classes, (v) => { n.classes = v; changed(); }, dis)) : null,
      parent && parentModel ? field(parentIsCrop ? `Lavora sugli oggetti di «${parent.name}» di classe` : `Attiva quando «${parent.name}» predice`,
        classPicker(parent.classes.length ? parent.classes : parentModel.classes, n.parent_classes, (v) => { n.parent_classes = v; changed(); }, dis)) : null,
      parentIsCrop ? field("Margine del ritaglio (%)", padding, "Allarga il ritaglio dell'oggetto del padre per dare più contesto al modello") : null,
      editable ? h("div", { class: "row", style: "margin-top:8px" },
        h("button", { onclick: () => addChild(n.id) }, "+ Figlio"),
        h("button", { onclick: () => addSibling(n.id) }, "+ Fratello"),
        h("button", { class: "danger", onclick: () => remove(n.id) }, "Elimina")) : null);
  }

  setKids($main, 
    h("p", {}, h("a", { href: "#/video/pipelines" }, "← Tutte le pipeline")),
    h("div", { class: "card editor-head" },
      nameInput, descInput,
      h("div", { class: "row spread", style: "margin-top:10px" },
        h("div", { class: "small muted" }, `Versione ${state.version}${p.created_by_name ? ` · creata da ${p.created_by_name}` : ""}`),
        editable ? h("div", { class: "row" }, dirtyLabel,
          h("button", { class: "danger", onclick: async () => {
            if (!confirm(`Eliminare la pipeline "${state.name}"?`)) return;
            try { await api(`/api/pipelines/${id}`, { method: "DELETE" }); state.dirty = false; location.hash = "#/video/pipelines"; } catch (e) { alert(e.message); }
          } }, "Elimina pipeline"),
          saveBtn) : null),
      msg),
    h("div", { class: "editor" }, treeBox, panel));
  renderTree();
  renderPanel();
}

