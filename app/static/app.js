"use strict";

/* Struttura dell'applicazione: accesso, navigazione, pagina iniziale, utenti e account. */

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
      setKids(err);
      try {
        const r = await api("/api/login", { json: { username: user.value, password: pass.value } });
        me = r.user;
        renderShell();
      } catch (ex) {
        setKids(err, h("div", { class: "alert error" }, ex.message));
      }
    },
  },
    h("div", { class: "row", style: "margin-bottom:16px" }, h("span", { class: "brand-dot" }), h("h1", { style: "margin:0" }, "Jetson AI Server")),
    err,
    h("div", { class: "field" }, h("label", {}, "Nome utente"), user),
    h("div", { class: "field" }, h("label", {}, "Password"), pass),
    h("button", { class: "primary", type: "submit", style: "width:100%;justify-content:center" }, "Accedi"));
  setKids($app, h("div", { class: "login-wrap" }, h("div", { class: "card" }, form)));
}

// ------------------------------------------------------------------ struttura

let $main, $nav, $subnav, $status;

/** Le due aree dell'applicazione, ognuna con il proprio sottomenu. */
const SECTIONS = [
  { key: "docs", label: "Lettore documenti", href: "#/docs", subs: [
    ["docs", "Documenti", "#/docs"], ["history", "Cronologia", "#/docs/history"], ["models", "Modelli linguistici", "#/docs/models"]] },
  { key: "video", label: "Analisi video", href: "#/video", subs: [
    ["new", "Nuova analisi", "#/video/new"], ["history", "Cronologia", "#/video/history"],
    ["pipelines", "Pipeline", "#/video/pipelines"], ["models", "Modelli di visione", "#/video/models"]] },
];

function renderShell() {
  $nav = h("nav", { "aria-label": "Aree" });
  $subnav = h("nav", { class: "subnav", "aria-label": "Sezioni" });
  $status = h("div", { class: "statusbar" });
  $main = h("main");
  setKids($app,
    h("header", { class: "topbar" },
      h("div", { class: "topbar-inner" },
        h("a", { class: "brand", href: "#/", style: "text-decoration:none;color:inherit" }, h("span", { class: "brand-dot" }), "Jetson AI"),
        $nav,
        h("div", { class: "user-box" },
          h("a", { href: "#/account", class: "username" }, me.username),
          h("button", { class: "link", onclick: logout }, "Esci"))),
      $subnav,
      $status),
    $main);
  refreshStatus();
  if (statusTimer) clearInterval(statusTimer);
  statusTimer = setInterval(refreshStatus, 5000);
  route();
}

async function logout() {
  stopChatStream();
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
    if (s.busy_with) parts.push(["In corso", s.busy_with]);
    if (s.queued) parts.push(["In coda", String(s.queued)]);
    if (!s.ollama.running) parts.push(["Modelli linguistici", "Ollama spento"]);
    setKids($status, ...parts.map(([k, v]) => h("span", {}, `${k} `, h("b", {}, v))));
  } catch { /* ignora */ }
}

/** Evidenzia l'area attiva e mostra il suo sottomenu. `area` è "docs", "video", "users" o null. */
function setNav(area, sub) {
  const top = SECTIONS.map((s) => h("a", { href: s.href, class: s.key === area ? "active" : null, "aria-current": s.key === area ? "page" : null }, s.label));
  if (me.is_admin) top.push(h("a", { href: "#/users", class: area === "users" ? "active" : null }, "Utenti"));
  setKids($nav, ...top);
  const section = SECTIONS.find((s) => s.key === area);
  $subnav.hidden = !section;
  setKids($subnav, ...(section ? section.subs.map(([key, label, href]) =>
    h("a", { href, class: key === sub ? "active" : null }, label)) : []));
}

/** Indirizzi usati nelle versioni precedenti dell'interfaccia. */
const OLD_ROUTES = { jobs: "#/video/history", models: "#/video/models", pipelines: "#/video/pipelines" };

function route() {
  if (!me) return;
  if (leaveGuard && location.hash !== currentHash) {
    if (leaveGuard() && !confirm("Ci sono modifiche non salvate. Uscire comunque?")) {
      history.replaceState(null, "", currentHash || "#/");
      return;
    }
  }
  leaveGuard = null;
  currentHash = location.hash;
  stopPolling();
  stopChatStream();
  document.documentElement.classList.remove("wide");
  const [area, a, b, c] = (location.hash || "#/").slice(2).split("/");
  window.scrollTo(0, 0);

  if (OLD_ROUTES[area]) { location.replace(OLD_ROUTES[area]); return; }
  if (area === "docs") {
    if (!a) return viewDocLibrary();
    if (a === "history") return viewDocHistory();
    if (a === "models") return viewLlmModels();
    if (/^\d+$/.test(a)) return viewDocument(Number(a), b === "c" && /^\d+$/.test(c || "") ? Number(c) : b === "new" ? "new" : null);
  } else if (area === "video") {
    if (!a || a === "new") return viewNewAnalysis();
    if (a === "history") return viewVideoHistory();
    if (a === "job" && b) return viewJob(b, "video");
    if (a === "pipelines") return viewPipelines();
    if (a === "pipeline" && /^\d+$/.test(b || "")) return viewPipeline(Number(b));
    if (a === "models") return viewVisionModels();
  } else if (area === "tool" && a) {
    return viewNewJob(a);
  } else if (area === "job" && a) {
    return viewJob(a, "");
  } else if (area === "pipeline" && /^\d+$/.test(a || "")) {
    location.replace(`#/video/pipeline/${a}`); return;
  } else if (area === "users" && me.is_admin) {
    return viewUsers();
  } else if (area === "account") {
    return viewAccount();
  }
  return viewHome();
}

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => { if (leaveGuard && leaveGuard()) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------------------ pagina iniziale

async function viewHome() {
  setNav(null);
  setKids($main, h("h1", {}, "Area di lavoro"), h("p", { class: "muted" }, "Caricamento…"));
  let o;
  try { o = await api("/api/overview"); } catch (e) { return setKids($main, h("h1", {}, "Area di lavoro"), errorBox(e.message)); }

  const stat = (n, one, many) => h("li", {}, h("b", {}, String(n)), ` ${n === 1 ? one : many}`);
  const docs = o.documents, video = o.video;
  const areaCard = (href, title, text, stats, note) => h("a", { class: "card area-card", href },
    h("h2", {}, title), h("p", { class: "muted" }, text),
    h("ul", { class: "stats" }, ...stats), note,
    h("span", { class: "area-go" }, "Apri"));

  setKids($main,
    h("h1", {}, "Area di lavoro"),
    h("p", { class: "muted" }, "Scegli cosa vuoi fare."),
    h("div", { class: "areas" },
      areaCard("#/docs", "Lettore documenti",
        "Carica i PDF, anche scansionati, e dialoga con il loro contenuto. Le risposte indicano le pagine e le conversazioni restano consultabili.",
        [stat(docs.count, "documento", "documenti"), stat(docs.conversations, "conversazione", "conversazioni")],
        docs.processing ? h("div", { class: "small muted" }, `${plural(docs.processing, "documento in elaborazione", "documenti in elaborazione")}`)
          : !docs.llm ? h("div", { class: "small", style: "color:var(--warn)" }, "Nessun modello linguistico scelto") : null),
      areaCard("#/video", "Analisi video",
        "Esegui pipeline di modelli di visione su video e immagini: rilevamento, segmentazione e classificazione, anche a più livelli.",
        [stat(video.analyses, "analisi", "analisi"), stat(video.pipelines, "pipeline", "pipeline"), stat(video.models, "modello", "modelli")],
        video.active ? h("div", { class: "small muted" }, `${plural(video.active, "analisi in corso o in coda", "analisi in corso o in coda")}`) : null)),
    o.other_tools.length ? h("div", {},
      h("h2", { style: "margin-top:28px" }, "Altri strumenti"),
      h("div", { class: "grid" }, o.other_tools.map((t) => h("a", {
        class: `card task-card ${t.available ? "" : "disabled"}`, href: t.available ? `#/tool/${t.id}` : null, style: "text-decoration:none;color:inherit",
      }, h("h2", {}, t.title), h("p", {}, t.description),
      t.available ? null : h("div", { class: "alert warn small", style: "margin:0" }, t.unavailable_reason))))) : null);
}

// ------------------------------------------------------------------ utenti

async function viewUsers() {
  setNav("users");
  const list = h("div", { class: "card" });
  const username = h("input", { type: "text", autocomplete: "off" });
  const password = h("input", { type: "password", autocomplete: "new-password" });
  const isAdmin = h("input", { type: "checkbox" });
  const msg = h("div");
  setKids($main, h("h1", {}, "Utenti"), list,
    h("div", { class: "card" }, h("h2", {}, "Nuovo utente"),
      h("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          try {
            await api("/api/users", { json: { username: username.value, password: password.value, is_admin: isAdmin.checked } });
            username.value = ""; password.value = ""; isAdmin.checked = false;
            setKids(msg);
            toast("Utente creato");
            load();
          } catch (ex) { setKids(msg, errorBox(ex.message)); }
        },
      }, msg,
        h("div", { class: "field" }, h("label", {}, "Nome utente"), username),
        h("div", { class: "field" }, h("label", {}, "Password (almeno 8 caratteri)"), password),
        h("div", { class: "field" }, h("label", { class: "check" }, isAdmin, "Amministratore"),
          h("div", { class: "help" }, "Può gestire utenti e modelli e vedere i lavori di tutti")),
        h("button", { class: "primary", type: "submit" }, "Crea utente"))));

  async function load() {
    let users;
    try { users = await api("/api/users"); } catch (e) { return setKids(list, errorBox(e.message)); }
    setKids(list, h("div", { class: "table-wrap" }, h("table", {},
      h("tbody", {}, users.map((u) => h("tr", {},
        h("td", {}, h("b", {}, u.username), u.is_admin ? h("span", { class: "badge done", style: "margin-left:8px" }, "admin") : null),
        h("td", { style: "text-align:right" },
          h("button", { class: "link", onclick: async () => {
            const pw = prompt(`Nuova password per ${u.username} (almeno 8 caratteri):`);
            if (!pw) return;
            try { await api(`/api/users/${u.id}/password`, { json: { new_password: pw } }); toast("Password aggiornata"); } catch (e) { toast(e.message); }
          } }, "Cambia password"),
          u.id !== me.id ? h("button", { class: "link", style: "color:var(--danger)", onclick: async () => {
            if (!confirm(`Eliminare ${u.username} e tutti i suoi lavori, documenti e conversazioni?`)) return;
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
  setKids($main, h("h1", {}, `Account: ${me.username}`),
    h("div", { class: "card" }, h("h2", {}, "Cambia password"),
      h("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          try {
            await api("/api/me/password", { json: { current_password: cur.value, new_password: nw.value } });
            toast("Password cambiata: accedi di nuovo");
            me = null;
            renderLogin();
          } catch (ex) { setKids(msg, errorBox(ex.message)); }
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

