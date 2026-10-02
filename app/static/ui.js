"use strict";

/* Funzioni comuni a tutta l'interfaccia: elementi, API, formattazione, moduli. */

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

/** Come el.replaceChildren(...), ma ignora null/undefined/false e appiattisce gli array. */
function setKids(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Riferimenti a pagina come [p. 3], (p. 3), [pp. 3, 5] o [p. 3-5]: diventano pulsanti cliccabili. */
function citeHtml(html) {
  return html.replace(/[\[(]\s*(?:p|pp|pag|pagg)\.?\s*(\d+(?:\s*(?:[,;]|e|–|-)\s*\d+)*)\s*[\])]/gi,
    (m, nums) => nums.match(/\d+/g)
      .map((n) => `<button type="button" class="cite" data-page="${n}" title="Apri la pagina ${n}">p. ${n}</button>`)
      .join(" "));
}

/** Markdown essenziale (titoli, elenchi, grassetto, corsivo, codice, tabelle, citazioni) per le risposte dell'LLM.
 *  Con { cite: true } i riferimenti a pagina diventano pulsanti. Tutto il testo viene escapato. */
function markdown(text, opts = {}) {
  const inline = (s) => {
    let out = escapeHtml(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return opts.cite ? citeHtml(out) : out;
  };
  const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const isTableSep = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(line);

  const lines = String(text).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let list = null;
  let para = [];
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flush = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const t = line.trim();
    let m;
    if (t.startsWith("```")) {
      flush(); closeList();
      const code = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith("```"); i++) code.push(lines[i]);
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (!t) { flush(); closeList(); continue; }
    if (t.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flush(); closeList();
      const head = cells(t);
      const rows = [];
      for (i += 2; i < lines.length && lines[i].trim().includes("|"); i++) rows.push(cells(lines[i]));
      i--;
      out.push('<div class="md-table"><table><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join("")
        + "</tr></thead><tbody>" + rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("")
        + "</tbody></table></div>");
      continue;
    }
    if ((m = t.match(/^(#{1,4})\s+(.*)$/))) { flush(); closeList(); out.push(`<h4>${inline(m[2])}</h4>`); continue; }
    if (/^(-{3,}|\*{3,})$/.test(t)) { flush(); closeList(); out.push("<hr>"); continue; }
    if ((m = t.match(/^>\s?(.*)$/))) { flush(); closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
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

/** "5 min fa", "ieri", "12/09": per elenchi di conversazioni e documenti. */
function fmtAgo(ts) {
  if (!ts) return "";
  const d = (Date.now() / 1000 - ts);
  if (d < 45) return "adesso";
  if (d < 3600) return `${Math.round(d / 60)} min fa`;
  if (d < 86400) return `${Math.round(d / 3600)} h fa`;
  if (d < 172800) return "ieri";
  return new Date(ts * 1000).toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit", year: d > 31536000 ? "2-digit" : undefined });
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

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

// Chiede conferma prima di lasciare una pagina con modifiche non salvate.
let leaveGuard = null; // funzione che restituisce true se ci sono modifiche non salvate
let currentHash = location.hash;

function errorBox(msg) { return h("div", { class: "alert error" }, msg); }

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
      // Scelta obbligatoria senza valore: nessuna opzione preimpostata.
      input = h("select", { name: p.name, required: p.required },
        p.required && (value === null || value === undefined) ? h("option", { value: "", selected: true, disabled: true }, "— scegli —") : null,
        p.choices.map((c) => {
          const v = typeof c === "object" ? c.value : c;
          return h("option", { value: v, selected: String(v) === String(value) }, typeof c === "object" ? c.label : c);
        }));
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

function uploadWithProgress(form, onProgress, url = "/api/jobs") {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
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

