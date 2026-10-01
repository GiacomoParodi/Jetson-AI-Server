"use strict";

// ------------------------------------------------------------------ utilità

const $app = document.getElementById("app");
let me = null;
let pollTimer = null;
let statusTimer = null;

/** Crea un elemento DOM: h("div", {class: "x", onclick: fn}, figli...). Il testo viene sempre escapato. */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v; // solo per HTML già sanificato
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Markdown minimo (grassetto, corsivo, codice, titoli, elenchi) per le risposte dell'LLM. */
function markdown(text) {
  const inline = (s) => escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  let para = [];
  const flush = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  for (const line of String(text).split("\n")) {
    const t = line.trim();
    let m;
    if (!t) { flush(); closeList(); continue; }
    if ((m = t.match(/^(#{1,4})\s+(.*)$/))) { flush(); closeList(); out.push(`<h3>${inline(m[2])}</h3>`); continue; }
    if ((m = t.match(/^[-*•]\s+(.*)$/))) { flush(); if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    if ((m = t.match(/^\d+[.)]\s+(.*)$/))) { flush(); if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    closeList();
    para.push(t);
  }
  flush(); closeList();
  return out.join("");
}

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  if (res.status === 401 && path !== "/api/login") {
    me = null;
    renderLogin();
    throw new Error("Sessione scaduta");
  }
  let data = null;
  try { data = await res.json(); } catch { /* risposta vuota */ }
  if (!res.ok) throw new Error((data && data.detail && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || `Errore ${res.status}`);
  return data;
}

function toast(msg) {
  const t = h("div", { class: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 3500);
}

function fmtDate(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("it-IT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function fmtDuration(s) {
  if (s == null) return "";
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

const STATUS = {
  uploading: "Caricamento", queued: "In coda", running: "In esecuzione",
  done: "Completato", failed: "Errore", cancelled: "Annullato",
};
const ACTIVE = new Set(["uploading", "queued", "running"]);

function badge(status) { return h("span", { class: `badge ${status}` }, STATUS[status] || status); }

function progressBar(fraction) {
  return h("div", { class: "progress", role: "progressbar", "aria-valuenow": Math.round(fraction * 100) },
    h("div", { style: `width:${Math.round(fraction * 100)}%` }));
}

function stopPolling() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }

// ------------------------------------------------------------------ login

function renderLogin() {
  stopPolling();
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  const err = h("div");
  const user = h("input", { type: "text", autocomplete: "username", autofocus: true });
  const pass = h("input", { type: "password", autocomplete: "current-password" });
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      err.replaceChildren();
      try {
        const r = await api("/api/login", { json: { username: user.value, password: pass.value } });
        me = r.user;
        renderShell();
      } catch (ex) {
        err.replaceChildren(h("div", { class: "alert error" }, ex.message));
      }
    },
  },
    h("div", { class: "row", style: "margin-bottom:16px" }, h("span", { class: "brand-dot" }), h("h1", { style: "margin:0" }, "Jetson AI Server")),
    err,
    h("div", { class: "field" }, h("label", {}, "Nome utente"), user),
    h("div", { class: "field" }, h("label", {}, "Password"), pass),
    h("button", { class: "primary", type: "submit", style: "width:100%;justify-content:center" }, "Accedi"));
  $app.replaceChildren(h("div", { class: "login-wrap" }, h("div", { class: "card" }, form)));
}

// ------------------------------------------------------------------ struttura

let $main, $nav, $status;

function renderShell() {
  $nav = h("nav");
  $status = h("div", { class: "statusbar" });
  $main = h("main");
  $app.replaceChildren(
    h("header", { class: "topbar" },
      h("div", { class: "topbar-inner" },
        h("a", { class: "brand", href: "#/", style: "text-decoration:none;color:inherit" }, h("span", { class: "brand-dot" }), "Jetson AI"),
        $nav,
        h("div", { class: "user-box" },
          h("a", { href: "#/account", class: "username" }, me.username),
          h("button", { class: "link", onclick: logout }, "Esci"))),
      $status),
    $main);
  refreshStatus();
  if (statusTimer) clearInterval(statusTimer);
  statusTimer = setInterval(refreshStatus, 5000);
  route();
}

async function logout() {
  try { await api("/api/logout", { method: "POST" }); } catch { /* ignora */ }
  me = null;
  renderLogin();
}

async function refreshStatus() {
  if (!me || document.hidden) return;
  try {
    const s = await api("/api/system");
    const temps = Object.values(s.temperatures || {});
    const parts = [
      ["RAM", `${s.ram_used_gb.toFixed(1)} / ${s.ram_total_gb.toFixed(1)} GB`],
      ["CPU", `${Math.round(s.cpu_percent)}%`],
    ];
    if (s.gpu_percent != null) parts.push(["GPU", `${Math.round(s.gpu_percent)}%`]);
    if (temps.length) parts.push(["Temp", `${Math.max(...temps).toFixed(0)} °C`]);
    parts.push(["Disco libero", `${s.disk_free_gb} GB`]);
    parts.push(["Coda", `${s.queued}${s.running ? " + 1 in corso" : ""}`]);
    if (!s.ollama.running) parts.push(["LLM", "Ollama spento"]);
    $status.replaceChildren(...parts.map(([k, v]) => h("span", {}, `${k} `, h("b", {}, v))));
  } catch { /* ignora */ }
}

function setNav(active) {
  const links = [["#/", "Nuovo lavoro", "new"], ["#/jobs", "Lavori", "jobs"], ["#/models", "Modelli", "models"]];
  if (me.is_admin) links.push(["#/users", "Utenti", "users"]);
  $nav.replaceChildren(...links.map(([href, label, key]) => h("a", { href, class: key === active ? "active" : null }, label)));
}

function route() {
  if (!me) return;
  stopPolling();
  const hash = location.hash || "#/";
  const parts = hash.slice(2).split("/");
  window.scrollTo(0, 0);
  if (parts[0] === "jobs") return viewJobs();
  if (parts[0] === "job" && parts[1]) return viewJob(parts[1]);
  if (parts[0] === "models") return viewModels();
  if (parts[0] === "users" && me.is_admin) return viewUsers();
  if (parts[0] === "account") return viewAccount();
  if (parts[0] === "new" && parts[1]) return viewNewJob(parts[1]);
  return viewTasks();
}

window.addEventListener("hashchange", route);

function errorBox(msg) { return h("div", { class: "alert error" }, msg); }

// ------------------------------------------------------------------ compiti

let tasksCache = null;

async function loadTasks(force) {
  if (!tasksCache || force) tasksCache = await api("/api/tasks");
  return tasksCache;
}

async function viewTasks() {
  setNav("new");
  $main.replaceChildren(h("h1", {}, "Cosa vuoi fare?"), h("p", { class: "muted" }, "Caricamento…"));
  try {
    const tasks = await loadTasks(true);
    $main.replaceChildren(
      h("h1", {}, "Cosa vuoi fare?"),
      h("div", { class: "grid" }, tasks.map((t) => h("div", {
        class: `card task-card ${t.available ? "" : "disabled"}`,
        tabindex: 0,
        onclick: () => { if (t.available) location.hash = `#/new/${t.id}`; },
        onkeydown: (e) => { if (e.key === "Enter" && t.available) location.hash = `#/new/${t.id}`; },
      },
        h("h2", {}, t.title),
        h("p", {}, t.description),
        t.available
          ? h("div", { class: "small muted" }, t.accept.length ? `File: ${t.accept.join(" ")}` : "")
          : h("div", { class: "alert warn small", style: "margin:0" }, t.unavailable_reason)))));
  } catch (e) { $main.append(errorBox(e.message)); }
}

/** Costruisce i campi del modulo dai parametri dichiarati dal compito. */
function paramFields(params, values) {
  const getters = {};
  const fields = params.map((p) => {
    const value = values && values[p.name] !== undefined ? values[p.name] : p.default;
    let input;
    if (p.type === "textarea") {
      input = h("textarea", { name: p.name, required: p.required });
      input.value = value ?? "";
      getters[p.name] = () => input.value;
    } else if (p.type === "number") {
      input = h("input", { type: "number", name: p.name, min: p.min, max: p.max, step: p.step ?? "any" });
      input.value = value ?? "";
      getters[p.name] = () => input.value;
    } else if (p.type === "bool") {
      input = h("input", { type: "checkbox", name: p.name });
      input.checked = !!value;
      getters[p.name] = () => input.checked;
      return h("div", { class: "field" }, h("label", { class: "check" }, input, p.label),
        p.help ? h("div", { class: "help" }, p.help) : null);
    } else if (p.choices) {
      input = h("select", { name: p.name },
        p.choices.map((c) => h("option", { value: c, selected: c === value }, c)));
      getters[p.name] = () => input.value;
    } else {
      input = h("input", { type: "text", name: p.name });
      input.value = value ?? "";
      getters[p.name] = () => input.value;
    }
    return h("div", { class: "field" }, h("label", {}, p.label, p.required ? " *" : ""), input,
      p.help ? h("div", { class: "help" }, p.help) : null);
  });
  const read = () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()]));
  return { fields, read };
}

function uploadWithProgress(form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* vuoto */ }
      if (xhr.status === 401) { me = null; renderLogin(); return reject(new Error("Sessione scaduta")); }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && data.detail) || `Errore ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Connessione interrotta durante il caricamento"));
    xhr.send(form);
  });
}

function fmtSize(bytes) {
  if (bytes < 1e6) return `${(bytes / 1e3).toFixed(0)} KB`;
  if (bytes < 1e9) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

async function viewNewJob(taskId) {
  setNav("new");
  let task;
  try {
    task = (await loadTasks(true)).find((t) => t.id === taskId);
  } catch (e) { return $main.replaceChildren(errorBox(e.message)); }
  if (!task) return $main.replaceChildren(errorBox("Compito non trovato"));

  let file = null;
  const fileInput = h("input", { type: "file", accept: task.accept.join(","), style: "display:none" });
  const dzText = h("div", {}, h("b", {}, "Trascina qui il file"), " oppure tocca per sceglierlo",
    h("div", { class: "small" }, task.accept.join("  ")));
  const dz = h("div", { class: "dropzone", tabindex: 0, onclick: () => fileInput.click() }, dzText);
  const setFile = (f) => {
    file = f;
    dzText.replaceChildren(h("b", {}, f.name), ` · ${fmtSize(f.size)}`, h("div", { class: "small" }, "Tocca per cambiare"));
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
      msg.replaceChildren();
      if (task.needs_file && !file) return msg.replaceChildren(errorBox("Seleziona un file"));
      const fd = new FormData();
      fd.append("task", task.id);
      fd.append("params", JSON.stringify(read()));
      if (file) fd.append("file", file);
      submit.disabled = true;
      bar.style.display = "block";
      const label = h("div", { class: "small muted" }, "Caricamento del file…");
      const pb = progressBar(0);
      bar.replaceChildren(label, pb);
      try {
        const job = await uploadWithProgress(fd, (f) => {
          pb.firstChild.style.width = `${Math.round(f * 100)}%`;
          label.textContent = `Caricamento del file… ${Math.round(f * 100)}%`;
        });
        location.hash = `#/job/${job.id}`;
      } catch (ex) {
        msg.replaceChildren(errorBox(ex.message));
        submit.disabled = false;
        bar.style.display = "none";
      }
    },
  },
    msg,
    task.needs_file ? h("div", { class: "field" }, h("label", {}, "File"), dz, fileInput) : null,
    fields, bar, submit);

  $main.replaceChildren(
    h("p", {}, h("a", { href: "#/" }, "← Tutti i compiti")),
    h("h1", {}, task.title),
    h("p", { class: "muted" }, task.description),
    h("div", { class: "card" }, form));
}

// ------------------------------------------------------------------ lavori

let showAllJobs = false;

async function viewJobs() {
  setNav("jobs");
  const list = h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0" }, "Caricamento…"));
  const head = h("div", { class: "row spread" }, h("h1", {}, "Lavori"),
    me.is_admin ? h("label", { class: "check small" },
      h("input", { type: "checkbox", checked: showAllJobs, onchange: (e) => { showAllJobs = e.target.checked; viewJobs(); } }),
      "Tutti gli utenti") : null);
  $main.replaceChildren(head, list);

  const load = async () => {
    let jobs;
    try { jobs = await api(`/api/jobs?all=${showAllJobs}`); } catch (e) { return list.replaceChildren(errorBox(e.message)); }
    if (!jobs.length) {
      list.replaceChildren(h("p", { class: "muted", style: "margin:0" }, "Nessun lavoro ancora. ", h("a", { href: "#/" }, "Avviane uno")));
      return;
    }
    list.replaceChildren(...jobs.map((j) => h("a", { class: "job-row", href: `#/job/${j.id}` },
      h("div", { class: "job-main" },
        h("div", { class: "job-title" }, j.task_title),
        h("div", { class: "job-sub" }, [j.input_name, fmtDate(j.created_at), showAllJobs ? j.username : null].filter(Boolean).join(" · "))),
      j.status === "running" ? h("div", { class: "job-progress" }, progressBar(j.progress)) : null,
      badge(j.status))));
    if (jobs.some((j) => ACTIVE.has(j.status)) && location.hash === "#/jobs") pollTimer = setTimeout(load, 2000);
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
        location.hash = `#/job/${nj.id}`;
      } catch (ex) { msg.replaceChildren(errorBox(ex.message)); submit.disabled = false; }
    },
  }, msg, fields, submit);
  // Per i PDF la domanda va svuotata: si vuole scriverne una nuova.
  const q = form.querySelector("textarea");
  if (q) q.value = "";
  return h("div", { class: "card" }, h("h2", {}, task.rerun_label), h("p", { class: "small muted" }, `Stesso file: ${job.input_name || "—"}`), form);
}

async function viewJob(id) {
  setNav("jobs");
  const container = h("div");
  $main.replaceChildren(h("p", {}, h("a", { href: "#/jobs" }, "← Tutti i lavori")), container);
  let lastStatus = null;
  let tasks = [];
  try { tasks = await loadTasks(); } catch { /* opzionale */ }

  const load = async () => {
    let job;
    try { job = await api(`/api/jobs/${id}`); } catch (e) { return container.replaceChildren(errorBox(e.message)); }
    const active = ACTIVE.has(job.status);
    if (!active && lastStatus === job.status) return; // niente da aggiornare
    lastStatus = job.status;

    const task = tasks.find((t) => t.id === job.task);
    const elapsed = job.started_at ? (job.finished_at || Date.now() / 1000) - job.started_at : null;
    const params = Object.entries(job.params || {}).filter(([, v]) => v !== "" && v !== null);
    const paramLabel = (k) => (task && (task.params.find((p) => p.name === k) || {}).label) || k;

    const header = h("div", { class: "card" },
      h("div", { class: "row spread" }, h("h1", { style: "margin:0" }, job.task_title), badge(job.status)),
      h("dl", { class: "kv", style: "margin-top:12px" },
        job.input_name ? [h("dt", {}, "File"), h("dd", {}, h("a", { href: `/api/jobs/${job.id}/input` }, job.input_name))] : null,
        params.map(([k, v]) => [h("dt", {}, paramLabel(k)), h("dd", {}, typeof v === "boolean" ? (v ? "sì" : "no") : String(v))]),
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
          try { await api(`/api/jobs/${id}`, { method: "DELETE" }); location.hash = "#/jobs"; } catch (e) { toast(e.message); }
        } }, "Elimina") : null));

    const body = [];
    if (job.status === "failed") body.push(errorBox(job.error || "Errore sconosciuto"));
    if (job.status === "done") body.push(...renderResult(job));
    if (!active && job.status !== "cancelled") body.push(rerunForm(job, task));

    container.replaceChildren(header, ...body.filter(Boolean));
    if (active && location.hash === `#/job/${id}`) pollTimer = setTimeout(load, 1500);
  };
  load();
}

// ------------------------------------------------------------------ modelli

async function viewModels() {
  setNav("models");
  const list = h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0" }, "Caricamento…"));
  $main.replaceChildren(h("h1", {}, "Modelli YOLO"),
    h("p", { class: "muted" }, "I modelli caricati qui compaiono nel compito di rilevamento oggetti. Sul Jetson, al primo utilizzo vengono ottimizzati con TensorRT (qualche minuto, una volta sola)."),
    list);

  if (me.is_admin) {
    const input = h("input", { type: "file", accept: ".pt" });
    const msg = h("div");
    const btn = h("button", { class: "primary", type: "submit" }, "Carica modello");
    $main.append(h("div", { class: "card" }, h("h2", {}, "Carica un modello (.pt)"),
      h("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          if (!input.files[0]) return;
          const fd = new FormData();
          fd.append("file", input.files[0]);
          btn.disabled = true;
          msg.replaceChildren(h("div", { class: "alert info" }, "Caricamento e verifica del modello…"));
          try {
            await api("/api/models", { method: "POST", body: fd });
            msg.replaceChildren();
            input.value = "";
            toast("Modello caricato");
            loadList();
          } catch (ex) { msg.replaceChildren(errorBox(ex.message)); }
          btn.disabled = false;
        },
      }, msg, h("div", { class: "field" }, input,
        h("div", { class: "help" }, "File .pt addestrato con Ultralytics (YOLOv8, YOLO11…). Il nome del file diventa il nome del modello.")), btn)));
  }

  async function loadList() {
    let models;
    try { models = await api("/api/models"); } catch (e) { return list.replaceChildren(errorBox(e.message)); }
    list.replaceChildren(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Nome"), h("th", {}, "Classi"), h("th", {}, "TensorRT"), h("th", {}))),
      h("tbody", {}, models.map((m) => h("tr", {},
        h("td", {}, h("b", {}, m.name), m.builtin ? h("div", { class: "small muted" }, "Predefinito (oggetti comuni COCO)") : m.size_mb ? h("div", { class: "small muted" }, `${m.size_mb} MB`) : null),
        h("td", { class: "small" }, m.classes.length ? m.classes.slice(0, 12).join(", ") + (m.classes.length > 12 ? ` … (+${m.classes.length - 12})` : "") : "—"),
        h("td", {}, m.tensorrt ? "pronto" : "—"),
        h("td", {}, me.is_admin && !m.builtin ? h("button", { class: "danger", onclick: async () => {
          if (!confirm(`Eliminare ${m.name}?`)) return;
          try { await api(`/api/models/${encodeURIComponent(m.name)}`, { method: "DELETE" }); loadList(); } catch (e) { toast(e.message); }
        } }, "Elimina") : null)))))));
  }
  loadList();
}

// ------------------------------------------------------------------ utenti

async function viewUsers() {
  setNav("users");
  const list = h("div", { class: "card" });
  const username = h("input", { type: "text", autocomplete: "off" });
  const password = h("input", { type: "password", autocomplete: "new-password" });
  const isAdmin = h("input", { type: "checkbox" });
  const msg = h("div");
  $main.replaceChildren(h("h1", {}, "Utenti"), list,
    h("div", { class: "card" }, h("h2", {}, "Nuovo utente"),
      h("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          try {
            await api("/api/users", { json: { username: username.value, password: password.value, is_admin: isAdmin.checked } });
            username.value = ""; password.value = ""; isAdmin.checked = false;
            msg.replaceChildren();
            toast("Utente creato");
            load();
          } catch (ex) { msg.replaceChildren(errorBox(ex.message)); }
        },
      }, msg,
        h("div", { class: "field" }, h("label", {}, "Nome utente"), username),
        h("div", { class: "field" }, h("label", {}, "Password (almeno 8 caratteri)"), password),
        h("div", { class: "field" }, h("label", { class: "check" }, isAdmin, "Amministratore"),
          h("div", { class: "help" }, "Può gestire utenti e modelli e vedere i lavori di tutti")),
        h("button", { class: "primary", type: "submit" }, "Crea utente"))));

  async function load() {
    let users;
    try { users = await api("/api/users"); } catch (e) { return list.replaceChildren(errorBox(e.message)); }
    list.replaceChildren(h("div", { class: "table-wrap" }, h("table", {},
      h("tbody", {}, users.map((u) => h("tr", {},
        h("td", {}, h("b", {}, u.username), u.is_admin ? h("span", { class: "badge done", style: "margin-left:8px" }, "admin") : null),
        h("td", { style: "text-align:right" },
          h("button", { class: "link", onclick: async () => {
            const pw = prompt(`Nuova password per ${u.username} (almeno 8 caratteri):`);
            if (!pw) return;
            try { await api(`/api/users/${u.id}/password`, { json: { new_password: pw } }); toast("Password aggiornata"); } catch (e) { toast(e.message); }
          } }, "Cambia password"),
          u.id !== me.id ? h("button", { class: "link", style: "color:var(--danger)", onclick: async () => {
            if (!confirm(`Eliminare ${u.username} e tutti i suoi lavori?`)) return;
            try { await api(`/api/users/${u.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
          } }, "Elimina") : null)))))));
  }
  load();
}

// ------------------------------------------------------------------ account

function viewAccount() {
  setNav(null);
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password" });
  const msg = h("div");
  $main.replaceChildren(h("h1", {}, `Account: ${me.username}`),
    h("div", { class: "card" }, h("h2", {}, "Cambia password"),
      h("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          try {
            await api("/api/me/password", { json: { current_password: cur.value, new_password: nw.value } });
            toast("Password cambiata: accedi di nuovo");
            me = null;
            renderLogin();
          } catch (ex) { msg.replaceChildren(errorBox(ex.message)); }
        },
      }, msg,
        h("div", { class: "field" }, h("label", {}, "Password attuale"), cur),
        h("div", { class: "field" }, h("label", {}, "Nuova password"), nw),
        h("button", { class: "primary", type: "submit" }, "Salva"))));
}

// ------------------------------------------------------------------ avvio

(async () => {
  try {
    me = await api("/api/me");
    renderShell();
  } catch {
    renderLogin();
  }
})();
