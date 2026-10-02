/* Finto server per la demo: risponde alle stesse API del server vero, ma tutto
 * resta in memoria nel browser. Niente viene salvato né inviato da nessuna parte.
 * Va caricato PRIMA di app.js. Le risorse (video, immagine, risultato di una
 * vera esecuzione) arrivano da window.DEMO_ASSETS e window.DEMO_RESULT. */
(() => {
  "use strict";

  const ASSETS = window.DEMO_ASSETS || {};
  const REAL = window.DEMO_RESULT;
  const NOW = Date.now() / 1000;
  const COCO = window.DEMO_COCO || [];
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const toastMsg = (m) => (typeof toast === "function" ? toast(m) : console.log(m));

  // ------------------------------------------------------------------ dati di esempio

  const state = {
    loggedIn: true,
    me: { id: 1, username: "giacomo", is_admin: true },
    users: [
      { id: 1, username: "giacomo", is_admin: true },
      { id: 2, username: "mario", is_admin: false },
      { id: 3, username: "elena", is_admin: false },
    ],
    nextUser: 4, nextPipeline: 3, nextJob: 1,
    models: [], pipelines: [], jobs: [], pulls: {},
    llm: { llm_model: "", embed_model: "", context: 4096, installed: [] },
  };

  function model(name, task, classes, imgsz, sizeMb, speed, extra = {}) {
    const labels = { detect: "Detection", segment: "Segmentazione", classify: "Classificazione" };
    return {
      name, task, task_label: labels[task], classes, imgsz, size_mb: sizeMb,
      status: "ready", backend: "tensorrt", precision: "fp16",
      speed: speed ? { before_ms: speed[0], after_ms: speed[1], speedup: Math.round((speed[0] / speed[1]) * 100) / 100, imgsz } : null,
      error: null, note: null, uploaded_at: NOW - 86400 * 3, optimized_at: NOW - 86400 * 3 + 600, ...extra,
    };
  }

  state.models = [
    model("rilevatore_generale.pt", "detect", COCO, 640, 5.6, [18.4, 6.2]),
    model("sagome_generale.pt", "segment", COCO, 640, 6.0, [31.5, 11.9]),
    model("tipo_veicolo.pt", "classify", ["minibus", "trolleybus", "school_bus", "police_van", "passenger_car", "taxi"], 224, 3.4, [8.1, 2.4]),
    model("scrivanie_v3.pt", "detect", ["scrivania", "sedia", "monitor", "tastiera", "lavagna", "cestino"], 640, 22.4, [41.2, 12.1]),
    model("stato_scrivania.pt", "classify", ["ordinata", "disordinata"], 224, 3.1, [8.5, 2.2]),
    model("oggetti_scrivania.pt", "segment", ["laptop", "tazza", "telefono", "quaderno", "bottiglia", "occhiali"], 640, 22.0, null, {
      status: "optimizing", backend: null, precision: null, uploaded_at: NOW - 300, optimized_at: null,
      sim: { t0: NOW - 15, pending: 0, optimize: 45, final: { speed: [58.7, 19.3] } },
    }),
    model("persone_n.pt", "detect", ["person"], 640, 5.4, null, {
      status: "pending", backend: null, precision: null, uploaded_at: NOW - 120, optimized_at: null,
    }),
  ];

  const node = (id, name, model_, kw = {}, children = []) => ({
    id, name, model: model_, conf: 0.35, classes: [], parent_classes: [], padding: 0.05, ...kw, children,
  });

  state.pipelines = [
    { id: 1, name: "Persone e veicoli", description: "Persone e bus in strada, con sagome e tipo di bus",
      tree: clone(REAL.pipeline.tree), version: 1, created_by: 1, created_by_name: "giacomo", updated_at: NOW - 86400 * 2 },
    { id: 2, name: "Scrivanie e oggetti", description: "Trova le scrivanie, ne valuta l'ordine e, se disordinate, riconosce gli oggetti",
      tree: [
        node("scrivanie", "Scrivanie", "scrivanie_v3.pt", { conf: 0.4, classes: ["scrivania"] }, [
          node("stato", "Stato della scrivania", "stato_scrivania.pt", { conf: 0.5, parent_classes: ["scrivania"], padding: 0.1 }, [
            node("oggetti", "Oggetti sulla scrivania", "oggetti_scrivania.pt", { parent_classes: ["disordinata"], padding: 0 }),
          ]),
        ]),
        node("persone", "Persone presenti", "persone_n.pt", { conf: 0.45, classes: ["person"] }),
      ], version: 1, created_by: 1, created_by_name: "giacomo", updated_at: NOW - 86400 },
  ];

  const GB = 1e9;
  state.llm.installed = [
    { name: "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M", size_gb: 2.5, parameter_size: "4.0B", quantization: "Q4_K_M", family: "qwen3", embedding: false },
    { name: "bge-m3:latest", size_gb: 1.16, parameter_size: "566.70M", quantization: "F16", family: "bert", embedding: true },
    { name: "llama3.2:3b", size_gb: 2.0, parameter_size: "3.2B", quantization: "Q4_K_M", family: "llama", embedding: false },
    { name: "llama3.1:8b-instruct-fp16", size_gb: 16.07, parameter_size: "8.0B", quantization: "F16", family: "llama", embedding: false },
  ];
  state.llm.llm_model = state.llm.installed[0].name;

  const OPT_RESULT = {
    summary: "**scrivanie_v3.pt** è ottimizzato con TensorRT FP16: **3.4x** più veloce (41.2 → 12.1 ms per immagine).",
    table: { columns: ["Versione", "ms per immagine", "Immagini al secondo"], rows: [["Originale (PyTorch)", 41.2, "24.3"], ["Ottimizzata (TensorRT FP16)", 12.1, "82.6"]] },
    notes: ["Risoluzione di ingresso: 640 px (quella dell'addestramento)."],
  };

  const TASK_TITLES = { yolo_pipeline: "Analisi video", optimize_model: "Ottimizzazione modello" };
  const RERUN = { yolo_pipeline: "Riesegui con altri parametri", optimize_model: null };
  const SECTION = { yolo_pipeline: "video", optimize_model: "video" };

  function seedJob(id, task, user, input, params, ago, dur, status, extra = {}) {
    return {
      id, task, username: user, input_name: input, params, status, progress: status === "done" ? 1 : 0,
      message: status === "done" ? "Completato" : status === "failed" ? "Errore" : "",
      result: null, error: null, created_at: NOW - ago, started_at: NOW - ago + 1, finished_at: NOW - ago + 1 + dur, ...extra,
    };
  }
  const snap = (p) => ({ id: p.id, name: p.name, version: p.version, tree: clone(p.tree) });

  state.jobs = [
    seedJob("demo-video", "yolo_pipeline", "giacomo", "piazza_mattina.mp4",
      { pipeline: "1", track: true, frame_step: 1, _pipeline: snap(state.pipelines[0]) }, 1500, 16, "done", { result: clone(REAL) }),
    seedJob("demo-opt", "optimize_model", "giacomo", null, { model: "scrivanie_v3.pt" }, 86400 * 3 + 700, 318, "done", { result: clone(OPT_RESULT) }),
    seedJob("demo-fail", "yolo_pipeline", "elena", "registrazione_corrotta.avi",
      { pipeline: "2", track: true, frame_step: 2, _pipeline: snap(state.pipelines[1]) }, 86400, 1, "failed",
      { error: "Impossibile aprire il video (formato non supportato?)" }),
    seedJob("demo-mario", "yolo_pipeline", "mario", "ingresso_ufficio.mp4",
      { pipeline: "1", track: true, frame_step: 1, _pipeline: snap(state.pipelines[0]) }, 5400, 12, "done", { result: clone(REAL) }),
    seedJob("demo-run", "yolo_pipeline", "giacomo", "parcheggio_notte.mp4",
      { pipeline: "1", track: true, frame_step: 1, _pipeline: snap(state.pipelines[0]) }, 22, 0, "running",
      { sim: { t0: NOW - 22, queued: 0, run: 70, result: clone(REAL) } }),
  ];

  // ------------------------------------------------------------------ lavori

  function jobMessage(job, frac) {
    if (frac < 0.06) return "Caricamento dei modelli…";
    return `Frame ${Math.min(45, Math.round(frac * 45))} di 45`;
  }

  function settle(job) {
    if (!job.sim) return job;
    const el = Date.now() / 1000 - job.sim.t0;
    const { queued, run } = job.sim;
    if (el < queued) {
      job.status = "queued"; job.progress = 0; job.message = "";
    } else if (el < queued + run) {
      const frac = (el - queued) / run;
      job.status = "running"; job.progress = frac; job.message = jobMessage(job, frac);
      job.started_at = job.sim.t0 + queued;
    } else {
      job.status = "done"; job.progress = 1; job.message = "Completato";
      job.started_at = job.sim.t0 + queued; job.finished_at = job.sim.t0 + queued + run;
      job.result = job.sim.result; job.sim = null;
    }
    return job;
  }

  function jobOut(job) {
    settle(job);
    const out = { ...job };
    delete out.sim;
    out.task_title = TASK_TITLES[job.task] || job.task;
    out.section = SECTION[job.task] || "";
    out.rerun_label = RERUN[job.task] || null;
    out.queue_position = job.status === "queued" ? 0 : null;
    return out;
  }

  function visibleJob(id) {
    const job = state.jobs.find((j) => j.id === id);
    if (!job || (job.username !== state.me.username && !state.me.is_admin)) return null;
    return job;
  }

  function fail(status, detail) { return [status, { detail }]; }

  function pipelineResultFor(p, ext) {
    const r = clone(REAL);
    const isImage = /\.(jpe?g|png|bmp|webp)$/i.test(ext || "");
    if (isImage) {
      r.outputs = [{ file: "annotata.jpg", kind: "image", label: "Immagine annotata" }, ...r.outputs.filter((o) => o.kind === "file")];
      r.summary = `Pipeline **${p.name}**: 7 risultati.`;
    }
    r.pipeline = { name: p.name, version: p.version, tree: clone(p.tree) };
    r.notes = [...(p.id === 1 ? [] : ["Demo: il video e i conteggi sono sempre quelli di esempio, qualunque pipeline scegli."]), ...(r.notes || [])];
    return r;
  }

  function createJob(task, params, inputName) {
    const t = TASK_TITLES[task];
    if (!t || task === "optimize_model") return fail(404, "Compito sconosciuto");
    const clean = {};
    let result;
    {
      const p = state.pipelines.find((x) => String(x.id) === String(params.pipeline));
      if (!params.pipeline) return fail(400, "Il campo 'Pipeline' è obbligatorio");
      if (!p) return fail(400, `Valore non valido per 'Pipeline': ${params.pipeline}`);
      if (!p.tree.length) return fail(400, `La pipeline '${p.name}' è vuota`);
      const step = Math.min(30, Math.max(1, Number(params.frame_step) || 1));
      Object.assign(clean, { pipeline: String(p.id), track: params.track === undefined ? true : !!params.track, frame_step: step, _pipeline: snap(p) });
      result = pipelineResultFor(p, inputName);
    }
    const now = Date.now() / 1000;
    const job = {
      id: `demo-${now.toString(36)}-${state.nextJob++}`, task, username: state.me.username, input_name: inputName,
      params: clean, status: "queued", progress: 0, message: "", result: null, error: null,
      created_at: now, started_at: null, finished_at: null,
      sim: { t0: now, queued: 2, run: 14, result },
    };
    state.jobs.unshift(job);
    return [200, jobOut(job)];
  }

  // ------------------------------------------------------------------ modelli

  function settleModel(m) {
    if (!m.sim) return m;
    const el = Date.now() / 1000 - m.sim.t0;
    if (el < m.sim.pending) {
      m.status = "pending";
    } else if (el < m.sim.pending + m.sim.optimize) {
      m.status = "optimizing";
    } else {
      const [before, after] = m.sim.final.speed;
      Object.assign(m, {
        status: "ready", backend: "tensorrt", precision: "fp16", note: null, optimized_at: Date.now() / 1000,
        speed: { before_ms: before, after_ms: after, speedup: Math.round((before / after) * 100) / 100, imgsz: m.imgsz },
      });
      m.sim = null;
    }
    return m;
  }

  function startOptimization(m, optimizeFor = 9, pendingFor = 3) {
    const before = Math.round((15 + Math.random() * 40) * 10) / 10;
    Object.assign(m, { status: "pending", backend: null, precision: null, speed: null, note: null, optimized_at: null });
    m.sim = { t0: Date.now() / 1000, pending: pendingFor, optimize: optimizeFor, final: { speed: [before, Math.round((before / (2.6 + Math.random() * 1.2)) * 10) / 10] } };
  }

  const modelOut = (m) => { settleModel(m); const o = { ...m }; delete o.sim; return o; };
  const modelsUsedIn = (tree, acc = new Set()) => { tree.forEach((n) => { acc.add(n.model); modelsUsedIn(n.children || [], acc); }); return acc; };
  const pipelinesUsing = (name) => state.pipelines.filter((p) => modelsUsedIn(p.tree).has(name)).map((p) => p.name);

  // ------------------------------------------------------------------ pipeline (stessa validazione del server)

  class PipelineError extends Error {}

  function normalizeTree(tree) {
    if (!Array.isArray(tree)) throw new PipelineError("Formato dell'albero non valido");
    const seen = new Set();
    let count = 0;
    const MAX_DEPTH = 20, MAX_NODES = 200;
    const classList = (value, allowed, where) => {
      if (!value || !value.length) return [];
      const out = [];
      for (const c of value) {
        if (!allowed.includes(c)) throw new PipelineError(`${where}: la classe '${c}' non esiste`);
        if (!out.includes(c)) out.push(c);
      }
      return out;
    };
    const clean = (n, depth, parent) => {
      count++;
      if (count > MAX_NODES) throw new PipelineError(`Troppi nodi (massimo ${MAX_NODES})`);
      if (depth >= MAX_DEPTH) throw new PipelineError(`Albero troppo profondo (massimo ${MAX_DEPTH} livelli)`);
      const name = String(n.name || "").trim().slice(0, 60);
      if (!name) throw new PipelineError("Ogni nodo deve avere un nome");
      let id = String(n.id || "");
      if (!/^[A-Za-z0-9_-]{1,40}$/.test(id) || seen.has(id)) id = Math.random().toString(36).slice(2, 14);
      seen.add(id);
      const m = state.models.find((x) => x.name === n.model);
      if (!m) throw new PipelineError(n.model ? `Nodo '${name}': il modello '${n.model}' non esiste` : `Nodo '${name}': scegli un modello`);
      const conf = Number(n.conf ?? 0.35), padding = Number(n.padding ?? 0.05);
      if (!(conf >= 0.01 && conf <= 0.99)) throw new PipelineError(`Nodo '${name}': la confidenza deve essere tra 0.01 e 0.99`);
      if (!(padding >= 0 && padding <= 0.5)) throw new PipelineError(`Nodo '${name}': il margine deve essere tra 0 e 50%`);
      const classes = classList(n.classes, m.classes, `Nodo '${name}'`);
      let parent_classes = [];
      if (parent) {
        const pm = state.models.find((x) => x.name === parent.model);
        parent_classes = classList(n.parent_classes, parent.classes.length ? parent.classes : pm.classes, `Nodo '${name}' (classi del padre)`);
      }
      const out = { id, name, model: n.model, conf: Math.round(conf * 1000) / 1000, classes, parent_classes, padding: Math.round(padding * 1000) / 1000, children: [] };
      out.children = (n.children || []).map((c) => clean(c, depth + 1, out));
      return out;
    };
    const cleaned = tree.map((n) => clean(n, 0, null));
    if (!cleaned.length) throw new PipelineError("La pipeline deve avere almeno un nodo");
    return cleaned;
  }

  function countNodes(tree) { return tree.reduce((a, n) => a + 1 + countNodes(n.children || []), 0); }
  function pipelineOut(p) {
    return { ...clone(p), node_count: countNodes(p.tree), models: [...modelsUsedIn(p.tree)] };
  }

  // ------------------------------------------------------------------ Ollama

  function llmStatus() {
    const pulls = {};
    const now = Date.now() / 1000;
    for (const [name, p] of Object.entries(state.pulls)) {
      const frac = Math.min(1, (now - p.t0) / p.duration);
      if (frac >= 1) {
        if (!p.done) {
          p.done = true; p.finished_at = now;
          if (!state.llm.installed.some((m) => m.name === name)) {
            const emb = /embed|bge|minilm|e5/i.test(name);
            state.llm.installed.push({ name, size_gb: p.total / GB, parameter_size: emb ? "300M" : "3.0B", quantization: emb ? "F16" : "Q4_K_M", family: emb ? "bert" : "llama", embedding: emb });
          }
        }
        pulls[name] = { status: "completato", completed: p.total, total: p.total, error: null, finished_at: p.finished_at };
      } else {
        pulls[name] = { status: frac < 0.05 ? "pulling manifest" : "pulling", completed: p.total * frac, total: p.total, error: null, finished_at: null };
      }
    }
    return {
      running: true, installed: [...state.llm.installed].sort((a, b) => a.name.localeCompare(b.name)),
      llm_model: state.llm.llm_model, embed_model: state.llm.embed_model, context: state.llm.context,
      context_choices: [2048, 4096, 8192, 16384], pulls,
    };
  }

  // ------------------------------------------------------------------ sistema

  function systemStatus() {
    const t = Date.now() / 1000;
    const jobs = state.jobs.map(settle);
    const running = jobs.some((j) => j.status === "running");
    const wave = (period, amp) => Math.sin((t * 2 * Math.PI) / period) * amp;
    return {
      device: "NVIDIA Jetson Orin Nano Developer Kit",
      cpu_percent: Math.round((running ? 58 : 14) + wave(7, 8) + Math.random() * 6),
      ram_used_gb: Math.round(((running ? 5.2 : 3.4) + wave(31, 0.15)) * 100) / 100,
      ram_total_gb: 7.6, disk_free_gb: 187.4, disk_total_gb: 233.9,
      gpu_percent: Math.round(running ? 74 + wave(5, 12) + Math.random() * 5 : 2 + Math.random() * 3),
      temperatures: { "cpu-thermal": Math.round((running ? 61 : 47) + wave(40, 2)), "gpu-thermal": Math.round((running ? 63 : 46) + wave(36, 2)) },
      version: "0.1.0", queued: jobs.filter((j) => j.status === "queued").length, running,
      busy_with: running ? "un'analisi video" : null, waiting: 0,
      ollama: { running: true, models: state.llm.installed.map((m) => m.name), llm_model: state.llm.llm_model, embed_model: state.llm.embed_model },
      cuda: true, tensorrt: "10.3.0",
    };
  }

  // ------------------------------------------------------------------ tasks

  function tasksOut() {
    const choices = state.pipelines.filter((p) => p.tree.length).map((p) => ({ value: String(p.id), label: p.name }));
    const P = (o) => ({ default: null, required: false, help: "", choices: null, min: null, max: null, step: null, ...o });
    return [
      {
        id: "yolo_pipeline", title: TASK_TITLES.yolo_pipeline, section: "video", hidden: false, needs_file: true,
        available: choices.length > 0, unavailable_reason: choices.length ? "" : "Nessuna pipeline pronta: un amministratore deve crearne una nella sezione Pipeline",
        description: "Esegue una pipeline di modelli di visione (rilevamento, segmentazione, classificazione) su un video o un'immagine. Restituisce il file annotato, i conteggi per fase e un CSV con tutti i risultati.",
        accept: [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".jpg", ".jpeg", ".png", ".bmp", ".webp"],
        params: [
          P({ name: "pipeline", label: "Pipeline", type: "select", required: true, choices, help: "Gli amministratori creano e modificano le pipeline nella sezione Pipeline" }),
          P({ name: "track", label: "Conta gli oggetti unici (tracking)", type: "bool", default: true, help: "Solo video, per i nodi radice di detection/segmentazione" }),
          P({ name: "frame_step", label: "Analizza un frame ogni", type: "number", default: 1, min: 1, max: 30, step: 1, help: "Valori più alti = più veloce ma meno fluido (es. 3 = un frame su tre)" }),
        ],
        rerun_label: RERUN.yolo_pipeline,
      },
    ];
  }

  // ------------------------------------------------------------------ lettore documenti: dati di esempio

  const DEFAULT_TITLE = "Nuova conversazione";
  const ago = (seconds) => NOW - seconds;

  /** Pagina segnaposto per i documenti caricati nella demo (di cui non si può mostrare il contenuto). */
  const placeholderCache = {};
  function placeholderPage(title, n, total) {
    const key = `${title}|${n}`;
    if (placeholderCache[key]) return placeholderCache[key];
    const c = document.createElement("canvas");
    c.width = 760; c.height = 1075;
    const g = c.getContext("2d");
    g.fillStyle = "#ffffff"; g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = "#14213d"; g.font = "bold 26px system-ui, sans-serif"; g.fillText(String(title).slice(0, 42), 70, 110);
    g.fillStyle = "#d9dde6";
    for (let i = 0; i < 22; i++) g.fillRect(70, 170 + i * 30, i % 5 === 4 ? 360 : 620, 10);
    g.fillStyle = "#6b7280"; g.font = "18px system-ui, sans-serif";
    g.fillText("Anteprima non disponibile nella demo", 70, 900);
    g.fillText(`Pagina ${n} di ${total}`, 70, 930);
    return (placeholderCache[key] = c.toDataURL("image/jpeg", 0.7));
  }
  function pageImage(doc, n) {
    const set = ASSETS.pages && ASSETS.pages[doc.kind];
    return (set && set[n - 1]) || placeholderPage(doc.title, n, doc.pages);
  }

  const doc = (id, user, title, filename, kind, pages, extra = {}) => ({
    id, user, title, filename, kind, pages, size_bytes: pages * 52000, ocr_pages: 0, status: "ready", error: null,
    notes: [], embed_model: null, created_at: ago(86400 * 2), updated_at: ago(86400 * 2), ...extra,
  });

  state.documents = [
    doc(1, "giacomo", "Contratto di locazione", "contratto_locazione.pdf", "contratto", 4, { created_at: ago(86400 * 3) }),
    doc(2, "giacomo", "Manuale di sicurezza sul lavoro", "manuale_sicurezza_ed3.pdf", "manuale", 24, { created_at: ago(86400 * 2), size_bytes: 3400000 }),
    doc(3, "giacomo", "Relazione trimestrale Q3", "relazione_q3.pdf", "relazione", 3, { created_at: ago(7200) }),
    doc(4, "giacomo", "Verbale riunione (scansione)", "verbale_scansionato.pdf", "generico", 5, {
      created_at: ago(8), status: "processing", size_bytes: 6528461,
      sim: { t0: NOW - 8, run: 50, pages: 5, ocr: 5 },
    }),
    doc(5, "mario", "Capitolato d'appalto", "capitolato.pdf", "generico", 12, { created_at: ago(86400 * 4) }),
  ];
  state.convs = [];
  state.messages = {};
  state.nextDoc = 6; state.nextConv = 1; state.nextMsg = 1;

  const docOf = (id) => state.documents.find((d) => d.id === id);

  function addMessage(convId, role, content, sources = [], model = null, status = "ok", at = Date.now() / 1000) {
    const msg = { id: state.nextMsg++, conversation_id: convId, role, content, sources, model, status, created_at: at };
    (state.messages[convId] = state.messages[convId] || []).push(msg);
    const conv = state.convs.find((c) => c.id === convId);
    if (conv) conv.updated_at = at;
    return msg;
  }

  // ---- risposte di esempio: poche domande tipiche per ogni documento, il resto riceve una risposta generica
  const SRC = {
    c1: { page: 1, text: "Il contratto ha durata di anni 4 (quattro), a decorrere dal 1° luglio 2024 e fino al 30 giugno 2028, salvo rinnovo come previsto dall'articolo 7." },
    c2: { page: 2, text: "Il canone annuo di locazione è convenuto in euro 9.000,00, da pagarsi in rate mensili anticipate di euro 750,00 ciascuna, entro il giorno 5 di ogni mese." },
    m2: { page: 2, text: "I dispositivi di protezione individuale (DPI) sono obbligatori nelle aree indicate dalla segnaletica. Reparto presse: elmetto, occhiali, guanti antitaglio, scarpe S3." },
    m2b: { page: 2, text: "I DPI vengono controllati ogni trimestre dal preposto. Il lavoratore che riscontra un difetto lo segnala subito e ritira un dispositivo sostitutivo presso il magazzino sicurezza." },
    m3: { page: 3, text: "Ogni lavoratore riceve una formazione generale di 4 ore e una formazione specifica di 8 ore prima di accedere al reparto assegnato. L'aggiornamento è di 6 ore ogni cinque anni." },
    m4: { page: 4, text: "Al segnale di allarme interrompere le attività, mettere in sicurezza le macchine e raggiungere il punto di raccolta indicato nelle planimetrie, senza usare gli ascensori." },
    m4b: { page: 4, text: "In caso di infortunio chiamare il 112 e avvisare l'addetto al primo soccorso del turno. Le cassette di medicazione si trovano in ogni reparto." },
    m5: { page: 5, text: "Ogni infortunio, anche lieve, e ogni quasi incidente vanno segnalati al preposto entro la fine del turno, compilando il modulo MS-12." },
  };

  const ANSWERS = {
    contratto: [
      [/riassum|sintesi|punti principali|di cosa|principali/i, () => ({
        text: "Il documento è un **contratto di locazione ad uso abitativo** tra Mario Rossi (Locatore) ed Elena Bianchi (Conduttore). In sintesi:\n\n"
          + "- **Oggetto:** appartamento di tre vani in via Roma 12, Milano, con cantina [p. 1]\n"
          + "- **Durata:** 4 anni, dal 1° luglio 2024 al 30 giugno 2028 [p. 1]\n"
          + "- **Canone:** 750 € al mese, da pagare entro il 5 di ogni mese [p. 2]\n"
          + "- **Deposito cauzionale:** tre mensilità [p. 2]\n"
          + "- **Manutenzione:** ordinaria al Conduttore, straordinaria al Locatore [p. 3]\n"
          + "- **Rinnovo:** per altri 4 anni, salvo disdetta con sei mesi di preavviso [p. 4]", sources: [] })],
      [/scad|durata|quando (finisce|termina)|fino a/i, () => ({
        text: "Il contratto scade il **30 giugno 2028** [p. 1]. Ha durata di quattro anni, a partire dal 1° luglio 2024 [p. 1].\n\n"
          + "Alla scadenza si rinnova automaticamente per altri quattro anni, a meno che una delle parti invii la disdetta con almeno sei mesi di preavviso [p. 4].", sources: [] })],
      [/canone|affitto|pagament|quanto|costo|import/i, () => ({
        text: "Il canone è di **750 € al mese**, pari a 9.000 € all'anno, da pagare entro il giorno 5 di ogni mese con bonifico bancario [p. 2].\n\n"
          + "| Voce | Importo | Scadenza |\n|---|---|---|\n| Canone mensile | 750,00 € | entro il 5 di ogni mese |\n"
          + "| Spese condominiali (acconto) | 60,00 € | con il canone |\n| Deposito cauzionale | 2.250,00 € | alla firma |\n\n"
          + "Dal secondo anno il canone si aggiorna al 75% della variazione ISTAT [p. 2].", sources: [] })],
      [/disdett|preavviso|rinnov|recede|recesso/i, () => ({
        text: "- La **disdetta** va inviata con lettera raccomandata con ricevuta di ritorno almeno **sei mesi prima** della scadenza [p. 4]\n"
          + "- Se nessuna delle parti disdice, il contratto si **rinnova per altri quattro anni** [p. 4]\n"
          + "- Il Conduttore può recedere in qualsiasi momento, per gravi motivi, con preavviso di sei mesi [p. 1]", sources: [] })],
      [/deposit|cauzion/i, () => ({
        text: "Il deposito cauzionale è pari a **tre mensilità**, cioè 2.250 € [p. 2]. Viene restituito alla riconsegna dell'immobile, dedotti eventuali danni accertati [p. 2].", sources: [] })],
      [/manutenzion|riparaz|guast/i, () => ({
        text: "- **Manutenzione ordinaria** (piccole riparazioni, caldaia, serramenti): a carico del Conduttore [p. 3]\n"
          + "- **Manutenzione straordinaria**: a carico del Locatore, salvo che derivi da incuria del Conduttore [p. 3]\n"
          + "- I guasti che richiedono interventi straordinari vanno segnalati per iscritto [p. 3]", sources: [] })],
    ],
    manuale: [
      [/riassum|sintesi|punti principali|di cosa|principali/i, () => ({
        text: "Il manuale raccoglie le regole di sicurezza dello stabilimento di Bologna. I temi principali sono:\n\n"
          + "- **Responsabilità** di datore di lavoro, RSPP, preposti e lavoratori [p. 1]\n- **DPI** obbligatori per ogni area [p. 2]\n"
          + "- **Formazione:** 4 ore generali, 8 specifiche, aggiornamento ogni 5 anni [p. 3]\n- **Emergenze** e primo soccorso [p. 4]\n"
          + "- **Segnalazione** di infortuni e quasi incidenti [p. 5]",
        sources: [{ page: 1, text: "Il presente manuale raccoglie le regole di sicurezza che tutto il personale è tenuto a rispettare nelle aree produttive e di magazzino." }, SRC.m2, SRC.m3, SRC.m4, SRC.m5],
        notes: ["Documento lungo: il riassunto si basa su una selezione di parti distribuite lungo il documento."] })],
      [/dpi|protezion|presse|elmett|guanti|verniciatura/i, () => ({
        text: "Nel **reparto presse** servono elmetto, occhiali, guanti antitaglio e scarpe S3 [p. 2]. Negli altri reparti:\n\n"
          + "| Area | DPI obbligatori |\n|---|---|\n| Magazzino | Scarpe antinfortunistiche, giubbotto alta visibilità |\n| Verniciatura | Mascherina FFP3, tuta, guanti in nitrile |\n| Uffici tecnici | Nessuno (elmetto per accedere alle aree produttive) |\n\n"
          + "I DPI vengono controllati ogni trimestre dal preposto [p. 2].", sources: [SRC.m2, SRC.m2b] })],
      [/formazion|ore|aggiorn|neoassunt/i, () => ({
        text: "Prima di accedere al reparto ogni lavoratore riceve **4 ore** di formazione generale e **8 ore** di formazione specifica. L'aggiornamento è di **6 ore ogni cinque anni** [p. 3].", sources: [SRC.m3] })],
      [/emergen|evacua|incend|soccors|allarme/i, () => ({
        text: "Al segnale di allarme si interrompono le attività, si mettono in sicurezza le macchine e si raggiunge il punto di raccolta senza usare gli ascensori [p. 4]:\n\n"
          + "- **Punto A:** piazzale nord (presse e verniciatura)\n- **Punto B:** parcheggio visitatori (uffici e magazzino)\n\n"
          + "In caso di infortunio chiamare il **112** e avvisare l'addetto al primo soccorso [p. 4].", sources: [SRC.m4, SRC.m4b] })],
      [/infortun|incident|segnal|modulo/i, () => ({
        text: "Ogni infortunio, anche lieve, e ogni quasi incidente vanno segnalati al preposto **entro la fine del turno** con il modulo **MS-12** [p. 5].", sources: [SRC.m5] })],
    ],
    relazione: [
      [/ricavi|fatturato|margine|risultat|andamento/i, () => ({
        text: "Il terzo trimestre chiude con ricavi di **4,82 milioni di euro**, in crescita del 6,4% [p. 1].\n\n"
          + "| Indicatore | Q3 | Q3 anno prec. | Variazione |\n|---|---|---|---|\n| Ricavi | 4,82 M€ | 4,53 M€ | +6,4% |\n| Margine operativo lordo | 0,88 M€ | 0,76 M€ | +15,8% |\n| Costi del personale | 1,64 M€ | 1,58 M€ | +3,8% |", sources: [] })],
      [/previsi|obiettiv|quarto|futuro|investiment/i, () => ({
        text: "Per il quarto trimestre si prevedono ricavi tra **5,0 e 5,3 milioni di euro** e l'obiettivo annuo di **18,9 milioni** è confermato [p. 3]. Gli investimenti previsti sono 1,2 milioni, per il 60% in nuove linee di collaudo [p. 3].", sources: [] })],
    ],
  };

  function compose(d, question) {
    for (const [re, make] of ANSWERS[d.kind] || []) {
      if (re.test(question)) return { notes: [], ...make() };
    }
    const known = ANSWERS[d.kind];
    return {
      text: known
        ? `Negli estratti del documento non trovo un'indicazione precisa su questo punto. Prova a chiedermi di ${d.kind === "contratto" ? "durata, canone, deposito, manutenzione o disdetta" : d.kind === "manuale" ? "DPI, formazione, emergenze o segnalazione degli infortuni" : "ricavi, margini o previsioni"}.`
        : `**Risposta di esempio.** Sul Jetson qui compare la risposta scritta dal modello linguistico scelto, leggendo il tuo documento e citando le pagine da cui prende le informazioni [p. 1].\n\nLa tua domanda era: «${question}»`,
      sources: [], notes: [],
    };
  }

  function seedConversation(docId, user, title, secondsAgo, questions) {
    const d = docOf(docId);
    const id = state.nextConv++;
    state.convs.push({ id, document_id: docId, user, title, created_at: ago(secondsAgo), updated_at: ago(secondsAgo) });
    let t = ago(secondsAgo);
    for (const q of questions) {
      const ans = compose(d, q);
      addMessage(id, "user", q, [], null, "ok", t);
      addMessage(id, "assistant", ans.text, ans.sources, state.llm.llm_model, "ok", (t += 9));
      t += 40;
    }
    return id;
  }
  seedConversation(1, "giacomo", "Scadenza e canone del contratto", 5400, ["Quando scade il contratto?", "E il canone mensile?"]);
  seedConversation(1, "giacomo", "Come si fa la disdetta", 86400 * 2, ["Come funziona la disdetta?"]);
  seedConversation(2, "giacomo", "Quali DPI nel reparto presse", 86400, ["Quali DPI servono nel reparto presse?"]);
  seedConversation(5, "mario", "Riassunto del capitolato", 3600, ["Riassumi il documento"]);

  // ---- elaborazione simulata dei documenti
  function settleDoc(d) {
    if (!d.sim) return d;
    const el = Date.now() / 1000 - d.sim.t0;
    if (el >= d.sim.run) {
      Object.assign(d, { status: "ready", error: null, pages: d.sim.pages ?? d.pages, ocr_pages: d.sim.ocr ?? d.ocr_pages, updated_at: Date.now() / 1000 });
      delete d.progress; delete d.message; d.sim = null;
    } else {
      const frac = el / d.sim.run;
      d.status = "processing";
      d.progress = Math.min(0.97, frac);
      d.message = frac > 0.85 ? "Indicizzazione del documento…"
        : `${d.sim.ocr ? "Lettura con OCR" : "Lettura"} della pagina ${Math.max(1, Math.min(d.pages, Math.ceil((frac / 0.85) * d.pages)))} di ${d.pages}…`;
    }
    return d;
  }

  function convsOf(docId, user) {
    return state.convs.filter((c) => (state.messages[c.id] || []).length
      && (docId == null || c.document_id === docId) && (user === null || c.user === user));
  }

  function docOut(d) {
    settleDoc(d);
    const convs = convsOf(d.id, d.user);
    const out = {
      id: d.id, title: d.title, filename: d.filename, size_bytes: d.size_bytes, pages: d.pages, ocr_pages: d.ocr_pages,
      status: d.status, error: d.error, notes: d.notes, embed_model: d.embed_model, created_at: d.created_at, updated_at: d.updated_at,
      username: d.user, conversation_count: convs.length,
      last_activity: convs.length ? Math.max(...convs.map((c) => c.updated_at)) : null,
      semantic: false, semantic_outdated: false,
    };
    if (d.status === "processing") { out.progress = d.progress || 0; out.message = d.message || "In coda"; }
    return out;
  }

  function convOut(c) {
    const d = docOf(c.document_id);
    const msgs = state.messages[c.id] || [];
    const lastUser = [...msgs].reverse().find((m) => m.role === "user");
    return { id: c.id, document_id: c.document_id, document_title: d ? d.title : "", title: c.title, created_at: c.created_at,
      updated_at: c.updated_at, message_count: msgs.length, last_question: lastUser ? lastUser.content : null, username: c.user };
  }

  const visibleDoc = (id) => {
    const d = docOf(id);
    return d && (d.user === state.me.username || state.me.is_admin) ? d : null;
  };
  const visibleConv = (id) => {
    const c = state.convs.find((x) => x.id === id);
    return c && (c.user === state.me.username || state.me.is_admin) ? c : null;
  };

  function titleFrom(q) {
    const t = q.split(/\s+/).join(" ");
    return t.length <= 60 ? t : (t.slice(0, 60).replace(/\s+\S*$/, "") || t.slice(0, 60)) + "…";
  }

  async function createDocument(file) {
    if (!file || !file.name || !/\.pdf$/i.test(file.name)) return fail(400, "Carica un file PDF");
    let pages = 0;
    try { pages = ((await file.slice(0, 6e6).text()).match(/\/Type\s*\/Page[^s]/g) || []).length; } catch { /* resta il valore stimato */ }
    pages = pages || 6;
    const d = doc(state.nextDoc++, state.me.username, file.name.replace(/\.pdf$/i, "").slice(0, 120), file.name, "generico", pages, {
      created_at: Date.now() / 1000, size_bytes: file.size || pages * 52000, status: "processing",
      sim: { t0: Date.now() / 1000, run: 6 + Math.min(10, pages * 0.4), pages, ocr: 0 },
    });
    state.documents.unshift(d);
    return [200, docOut(d)];
  }

  function anyJobRunning() { return state.jobs.some((j) => settle(j).status === "running"); }

  /** Risposta in streaming: stessi eventi del server vero (user_message, status, sources, token, done, error). */
  function streamMessage(convId, text, signal) {
    const conv = visibleConv(convId);
    if (!conv) return new Response(JSON.stringify({ detail: "Conversazione non trovata" }), { status: 404 });
    const d = docOf(conv.document_id);
    text = String(text || "").trim();
    if (!text) return new Response(JSON.stringify({ detail: "Scrivi una domanda" }), { status: 400 });
    if (!d || settleDoc(d).status !== "ready") {
      return new Response(JSON.stringify({ detail: "Il documento non è ancora pronto: attendi la fine dell'elaborazione" }), { status: 409 });
    }
    const enc = new TextEncoder();
    const stream = new ReadableStream({
      async start(controller) {
        let closed = false;
        const send = (ev) => { if (!closed) { try { controller.enqueue(enc.encode(`data: ${JSON.stringify(ev)}\n\n`)); } catch { closed = true; } } };
        const aborted = () => !!(signal && signal.aborted);
        if (signal) signal.addEventListener("abort", () => { closed = true; try { controller.error(new DOMException("Aborted", "AbortError")); } catch { /* già chiuso */ } });
        let partial = "", sources = [];
        try {
          if (!state.llm.llm_model) {
            send({ type: "error", detail: "Nessun modello linguistico scelto: un amministratore deve sceglierlo nella pagina Modelli linguistici." });
            return;
          }
          const past = state.messages[conv.id] || [];
          const userMsg = addMessage(conv.id, "user", text);
          if (conv.title === DEFAULT_TITLE && !past.length) conv.title = titleFrom(text);
          send({ type: "user_message", message: userMsg, title: conv.title });
          if (anyJobRunning()) {
            send({ type: "status", state: "waiting", text: "Il server sta eseguendo un'analisi video: la risposta parte appena finisce." });
            await delay(2200);
          }
          if (aborted()) return;
          send({ type: "status", state: "searching", text: "Ricerca nel documento…" });
          await delay(550);
          const ans = compose(d, text);
          sources = ans.sources;
          send({ type: "sources", sources, notes: ans.notes });
          send({ type: "status", state: "writing", text: "Scrittura della risposta…" });
          await delay(450);
          for (const piece of ans.text.match(/\S+\s*/g) || []) {
            if (aborted()) return;
            await delay(24);
            partial += piece;
            send({ type: "token", text: piece });
          }
          send({ type: "done", message: addMessage(conv.id, "assistant", ans.text, sources, state.llm.llm_model) });
        } catch (e) {
          send({ type: "error", detail: String((e && e.message) || e) });
        } finally {
          if (aborted() && partial.trim()) addMessage(conv.id, "assistant", partial.trim(), sources, state.llm.llm_model, "stopped");
          try { controller.close(); } catch { /* già chiuso */ }
        }
      },
    });
    return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }

  // ------------------------------------------------------------------ router

  function body(init) {
    const b = init && init.body;
    if (typeof b === "string") { try { return JSON.parse(b); } catch { return {}; } }
    return {};
  }

  function route(method, url, init) {
    const u = new URL(url, "http://demo.local");
    const path = u.pathname;
    let m;

    if (method === "POST" && path === "/api/login") {
      const b = body(init);
      const name = String(b.username || "").trim() || "giacomo";
      const known = state.users.find((x) => x.username.toLowerCase() === name.toLowerCase());
      state.me = known ? { ...known } : { id: 99, username: name, is_admin: true };
      state.loggedIn = true;
      return [200, { token: "demo", user: { ...state.me } }];
    }
    if (method === "POST" && path === "/api/logout") { state.loggedIn = false; return [200, { ok: true }]; }
    if (!state.loggedIn) return fail(401, "Accesso richiesto");

    const admin = () => (state.me.is_admin ? null : fail(403, "Solo gli amministratori possono farlo"));

    if (method === "GET" && path === "/api/me") return [200, { ...state.me }];
    if (method === "POST" && path === "/api/me/password") {
      const b = body(init);
      if (!b.current_password) return fail(400, "La password attuale non è corretta");
      if ((b.new_password || "").length < 8) return fail(400, "La password deve avere almeno 8 caratteri");
      state.loggedIn = false;
      return [200, { ok: true }];
    }

    // utenti
    if (path === "/api/users" && method === "GET") return admin() || [200, state.users.map((x) => ({ ...x }))];
    if (path === "/api/users" && method === "POST") {
      const denied = admin(); if (denied) return denied;
      const b = body(init), name = String(b.username || "").trim();
      if (!/^[A-Za-z0-9_.-]{3,32}$/.test(name)) return fail(400, "Nome utente: 3-32 caratteri tra lettere, numeri, . _ -");
      if ((b.password || "").length < 8) return fail(400, "La password deve avere almeno 8 caratteri");
      if (state.users.some((x) => x.username.toLowerCase() === name.toLowerCase())) return fail(400, "Nome utente già esistente");
      const user = { id: state.nextUser++, username: name, is_admin: !!b.is_admin };
      state.users.push(user);
      return [200, { ...user }];
    }
    if ((m = path.match(/^\/api\/users\/(\d+)\/password$/)) && method === "POST") {
      const denied = admin(); if (denied) return denied;
      if ((body(init).new_password || "").length < 8) return fail(400, "La password deve avere almeno 8 caratteri");
      return [200, { ok: true }];
    }
    if ((m = path.match(/^\/api\/users\/(\d+)$/)) && method === "DELETE") {
      const denied = admin(); if (denied) return denied;
      const id = Number(m[1]);
      if (id === state.me.id) return fail(400, "Non puoi eliminare il tuo stesso account");
      state.users = state.users.filter((x) => x.id !== id);
      return [200, { ok: true }];
    }

    // compiti e lavori
    if (path === "/api/tasks" && method === "GET") return [200, tasksOut()];
    if (path === "/api/jobs" && method === "GET") {
      const all = u.searchParams.get("all") === "true" && state.me.is_admin;
      const section = u.searchParams.get("section");
      const list = state.jobs.filter((j) => (all || j.username === state.me.username)
        && (section === null || (SECTION[j.task] === section && j.task !== "optimize_model"))).map(jobOut);
      return [200, list.sort((a, b) => b.created_at - a.created_at)];
    }
    if ((m = path.match(/^\/api\/jobs\/([^/]+)$/))) {
      const job = visibleJob(m[1]);
      if (!job) return fail(404, "Lavoro non trovato");
      if (method === "GET") return [200, jobOut(job)];
      if (method === "DELETE") {
        settle(job);
        if (job.status === "running") return fail(400, "Annulla il lavoro prima di eliminarlo");
        state.jobs = state.jobs.filter((j) => j !== job);
        return [200, { ok: true }];
      }
    }
    if ((m = path.match(/^\/api\/jobs\/([^/]+)\/cancel$/)) && method === "POST") {
      const job = visibleJob(m[1]);
      if (!job) return fail(404, "Lavoro non trovato");
      settle(job);
      if (!["queued", "running"].includes(job.status)) return fail(400, "Il lavoro è già terminato");
      Object.assign(job, { status: "cancelled", message: "Annullato", sim: null, finished_at: Date.now() / 1000 });
      return [200, { ok: true }];
    }
    if ((m = path.match(/^\/api\/jobs\/([^/]+)\/rerun$/)) && method === "POST") {
      const job = visibleJob(m[1]);
      if (!job) return fail(404, "Lavoro non trovato");
      if (!RERUN[job.task]) return fail(400, "Questo lavoro non si può rieseguire");
      return createJob(job.task, { ...job.params, ...(body(init).params || {}) }, job.input_name);
    }

    // modelli YOLO
    if (path === "/api/models" && method === "GET") return [200, state.models.map(modelOut)];
    if (path === "/api/models" && method === "POST") {
      const denied = admin(); if (denied) return denied;
      const fd = init.body, file = fd && fd.get && fd.get("file"), replace = fd && fd.get && fd.get("replace");
      const fname = String((file && file.name) || "").replace(/ /g, "_");
      let name;
      if (replace) {
        if (!state.models.some((x) => x.name === replace)) return fail(404, "Modello da sostituire non trovato");
        if (!/\.pt$/i.test(fname)) return fail(400, "Serve un file .pt");
        name = replace;
      } else {
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.pt$/.test(fname)) return fail(400, "Serve un file .pt con un nome semplice (lettere, numeri, _ . -)");
        name = fname;
      }
      const old = state.models.find((x) => x.name === name);
      const fresh = old || { name, task: "detect", task_label: "Detection", classes: ["oggetto_a", "oggetto_b", "oggetto_c"], imgsz: 640, size_mb: Math.round(((file && file.size) || 6e6) / 1e5) / 10 };
      fresh.uploaded_at = Date.now() / 1000;
      startOptimization(fresh);
      if (!old) state.models.push(fresh);
      return [200, { ...modelOut(fresh), warnings: [] }];
    }
    if ((m = path.match(/^\/api\/models\/([^/]+)\/optimize$/)) && method === "POST") {
      const denied = admin(); if (denied) return denied;
      const mod = state.models.find((x) => x.name === decodeURIComponent(m[1]));
      if (!mod) return fail(404, "Modello non trovato");
      startOptimization(mod);
      return [200, { job_id: "demo-opt" }];
    }
    if ((m = path.match(/^\/api\/models\/([^/]+)$/)) && method === "DELETE") {
      const denied = admin(); if (denied) return denied;
      const name = decodeURIComponent(m[1]);
      if (!state.models.some((x) => x.name === name)) return fail(404, "Modello non trovato");
      const used = pipelinesUsing(name);
      if (used.length) return fail(400, `Il modello è usato dalle pipeline: ${used.join(", ")}. Toglilo prima dai nodi.`);
      state.models = state.models.filter((x) => x.name !== name);
      return [200, { ok: true }];
    }

    // pipeline
    if (path === "/api/pipelines" && method === "GET") return [200, state.pipelines.map(pipelineOut)];
    if (path === "/api/pipelines" && method === "POST") {
      const denied = admin(); if (denied) return denied;
      const b = body(init), name = String(b.name || "").trim().slice(0, 80);
      if (!name) return fail(400, "Dai un nome alla pipeline");
      if (state.pipelines.some((p) => p.name.toLowerCase() === name.toLowerCase())) return fail(400, "Esiste già una pipeline con questo nome");
      let tree = [];
      try { tree = b.tree && b.tree.length ? normalizeTree(b.tree) : []; } catch (e) { return fail(400, e.message); }
      const p = { id: state.nextPipeline++, name, description: String(b.description || "").slice(0, 500), tree, version: 1, created_by: state.me.id, created_by_name: state.me.username, updated_at: Date.now() / 1000 };
      state.pipelines.push(p);
      return [200, pipelineOut(p)];
    }
    if ((m = path.match(/^\/api\/pipelines\/(\d+)$/))) {
      const p = state.pipelines.find((x) => x.id === Number(m[1]));
      if (method === "GET") return p ? [200, pipelineOut(p)] : fail(404, "Pipeline non trovata");
      const denied = admin(); if (denied) return denied;
      if (!p) return fail(404, "Pipeline non trovata");
      if (method === "DELETE") { state.pipelines = state.pipelines.filter((x) => x !== p); return [200, { ok: true }]; }
      if (method === "PUT") {
        const b = body(init), name = String(b.name || "").trim().slice(0, 80);
        if (b.version == null) return fail(400, "Versione mancante");
        if (!name) return fail(400, "Dai un nome alla pipeline");
        let tree;
        try { tree = normalizeTree(b.tree); } catch (e) { return fail(400, e.message); }
        if (state.pipelines.some((x) => x !== p && x.name.toLowerCase() === name.toLowerCase())) return fail(400, "Esiste già una pipeline con questo nome");
        if (b.version !== p.version) return fail(409, "Qualcun altro ha modificato questa pipeline nel frattempo: ricarica la pagina");
        Object.assign(p, { name, description: String(b.description || "").slice(0, 500), tree, version: p.version + 1, updated_at: Date.now() / 1000 });
        return [200, pipelineOut(p)];
      }
    }

    // panoramica
    if (path === "/api/overview" && method === "GET") {
      const mine = state.jobs.filter((j) => j.username === state.me.username && SECTION[j.task] === "video" && j.task !== "optimize_model").map(settle);
      const docs = state.documents.filter((d) => d.user === state.me.username).map(settleDoc);
      return [200, {
        documents: { count: docs.length, processing: docs.filter((d) => d.status === "processing").length,
          conversations: convsOf(null, state.me.username).length, llm: state.llm.llm_model },
        video: { analyses: mine.length, active: mine.filter((j) => ["queued", "running"].includes(j.status)).length,
          pipelines: state.pipelines.length, models: state.models.length },
        other_tools: [],
      }];
    }

    // documenti e conversazioni
    if (path === "/api/documents" && method === "GET") {
      const all = u.searchParams.get("all") === "true" && state.me.is_admin;
      return [200, state.documents.filter((d) => all || d.user === state.me.username).map(docOut)
        .sort((x, y) => y.created_at - x.created_at)];
    }
    if ((m = path.match(/^\/api\/documents\/(\d+)$/))) {
      const d = visibleDoc(Number(m[1]));
      if (!d) return fail(404, "Documento non trovato");
      if (method === "GET") return [200, docOut(d)];
      if (method === "PATCH") {
        const title = String(body(init).title || "").split(/\s+/).join(" ").slice(0, 120);
        if (!title) return fail(400, "Il nome non può essere vuoto");
        d.title = title;
        return [200, docOut(d)];
      }
      if (method === "DELETE") {
        const ids = state.convs.filter((c) => c.document_id === d.id).map((c) => c.id);
        state.convs = state.convs.filter((c) => c.document_id !== d.id);
        ids.forEach((id) => delete state.messages[id]);
        state.documents = state.documents.filter((x) => x !== d);
        return [200, { ok: true }];
      }
    }
    if ((m = path.match(/^\/api\/documents\/(\d+)\/reindex$/)) && method === "POST") {
      const d = visibleDoc(Number(m[1]));
      if (!d) return fail(404, "Documento non trovato");
      if (settleDoc(d).status === "processing") return fail(400, "Il documento è già in elaborazione");
      Object.assign(d, { status: "processing", error: null, sim: { t0: Date.now() / 1000, run: 6, pages: d.pages, ocr: d.ocr_pages } });
      return [200, docOut(d)];
    }
    if ((m = path.match(/^\/api\/documents\/(\d+)\/conversations$/))) {
      const d = visibleDoc(Number(m[1]));
      if (!d) return fail(404, "Documento non trovato");
      if (method === "GET") {
        return [200, convsOf(d.id, state.me.is_admin ? null : state.me.username).map(convOut).sort((a, b) => b.updated_at - a.updated_at)];
      }
      if (method === "POST") {
        const now = Date.now() / 1000;
        const c = { id: state.nextConv++, document_id: d.id, user: state.me.username, title: DEFAULT_TITLE, created_at: now, updated_at: now };
        state.convs.push(c);
        return [200, convOut(c)];
      }
    }
    if (path === "/api/conversations" && method === "GET") {
      const all = u.searchParams.get("all") === "true" && state.me.is_admin;
      return [200, convsOf(null, all ? null : state.me.username).map(convOut).sort((a, b) => b.updated_at - a.updated_at)];
    }
    if ((m = path.match(/^\/api\/conversations\/(\d+)$/))) {
      const c = visibleConv(Number(m[1]));
      if (!c) return fail(404, "Conversazione non trovata");
      if (method === "GET") return [200, { ...convOut(c), messages: clone(state.messages[c.id] || []) }];
      if (method === "PATCH") {
        const title = String(body(init).title || "").split(/\s+/).join(" ").slice(0, 120);
        if (!title) return fail(400, "Il titolo non può essere vuoto");
        c.title = title;
        return [200, convOut(c)];
      }
      if (method === "DELETE") {
        state.convs = state.convs.filter((x) => x !== c);
        delete state.messages[c.id];
        return [200, { ok: true }];
      }
    }

    // LLM (Ollama)
    if (path === "/api/llm" && method === "GET") return [200, llmStatus()];
    if (path === "/api/llm/pull" && method === "POST") {
      const denied = admin(); if (denied) return denied;
      const name = String(body(init).name || "").trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}(:[A-Za-z0-9._-]{1,60})?$/.test(name)) return fail(400, "Nome del modello non valido (es. qwen2.5:3b)");
      const total = (/embed|bge|minilm|e5/i.test(name) ? 0.6 : 1.9 + (name.length % 5) * 0.2) * GB;
      state.pulls[name] = { t0: Date.now() / 1000, duration: 7, total, done: false, finished_at: null };
      return [200, { ok: true }];
    }
    if (path === "/api/llm/select" && method === "POST") {
      const denied = admin(); if (denied) return denied;
      const b = body(init), names = state.llm.installed.map((x) => x.name);
      if (b.llm_model && !names.includes(b.llm_model)) return fail(400, `Il modello LLM '${b.llm_model}' non è scaricato`);
      if (b.embed_model && !names.includes(b.embed_model)) return fail(400, `Il modello di embedding '${b.embed_model}' non è scaricato`);
      if (![2048, 4096, 8192, 16384].includes(b.context)) return fail(400, "Dimensione del contesto non valida");
      Object.assign(state.llm, { llm_model: b.llm_model || "", embed_model: b.embed_model || "", context: b.context });
      return [200, { ok: true }];
    }
    if ((m = path.match(/^\/api\/llm\/models\/(.+)$/)) && method === "DELETE") {
      const denied = admin(); if (denied) return denied;
      const name = decodeURIComponent(m[1]);
      if (name === state.llm.llm_model || name === state.llm.embed_model) return fail(400, "Il modello è quello scelto in uso: scegline un altro prima di eliminarlo");
      state.llm.installed = state.llm.installed.filter((x) => x.name !== name);
      delete state.pulls[name];
      return [200, { ok: true }];
    }

    if (path === "/api/system" && method === "GET") return [200, systemStatus()];
    if (path === "/api/health") return [200, { ok: true }];
    return fail(404, "Non disponibile nella demo");
  }

  // ------------------------------------------------------------------ fetch e XMLHttpRequest finti

  const realFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith("/api/")) return realFetch ? realFetch(input, init) : Promise.reject(new Error("rete non disponibile"));
    await delay(50 + Math.random() * 90);
    const stream = url.match(/^\/api\/conversations\/(\d+)\/messages$/);
    if (stream && (init.method || "GET").toUpperCase() === "POST") {
      if (!state.loggedIn) return new Response(JSON.stringify({ detail: "Accesso richiesto" }), { status: 401 });
      return streamMessage(Number(stream[1]), body(init).content, init.signal);
    }
    const [status, data] = route((init.method || "GET").toUpperCase(), url, init);
    return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
  };

  class FakeXHR {
    constructor() { this.upload = {}; this.status = 0; this.responseText = ""; }
    open(method, url) { this.method = method; this.url = url; }
    send(form) {
      let loaded = 0;
      const finish = async () => {
        let status, data;
        const file = form.get("file");
        if (this.url === "/api/documents") {
          [status, data] = await createDocument(file);
        } else {
          let params = {};
          try { params = JSON.parse(form.get("params") || "{}"); } catch { /* vuoto */ }
          const task = tasksOut().find((t) => t.id === form.get("task"));
          if (!task) [status, data] = fail(404, "Compito sconosciuto");
          else if (!file || !file.name) [status, data] = fail(400, "Seleziona un file");
          else if (!task.accept.includes("." + file.name.split(".").pop().toLowerCase())) [status, data] = fail(400, `Formato non supportato. Accettati: ${task.accept.join(", ")}`);
          else [status, data] = createJob(task.id, params, file.name);
        }
        this.status = status; this.responseText = JSON.stringify(data);
        if (this.onload) this.onload();
      };
      const step = () => {
        loaded += 12;
        if (this.upload.onprogress) this.upload.onprogress({ lengthComputable: true, loaded: Math.min(loaded, 100), total: 100 });
        if (loaded < 100) return setTimeout(step, 90);
        setTimeout(finish, 150);
      };
      step();
    }
  }
  window.XMLHttpRequest = FakeXHR;

  // ------------------------------------------------------------------ comportamenti del browser da adattare

  // Video e immagini dei risultati: file incorporati al posto dei file del server.
  // Alcuni browser (es. Chromium senza codec proprietari) non leggono H.264: in quel caso si usa WebM.
  const canH264 = (() => {
    try { return document.createElement("video").canPlayType('video/mp4; codecs="avc1.42E01E"') !== ""; } catch { return true; }
  })();
  const origSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (name === "src" && typeof value === "string" && this.tagName === "IMG") {
      const page = value.match(/^\/api\/documents\/(\d+)\/pages\/(\d+)/);
      const d = page && docOf(Number(page[1]));
      if (d) value = pageImage(d, Number(page[2]));
    }
    if (name === "src" && typeof value === "string" && value.startsWith("/api/jobs/") && (this.tagName === "VIDEO" || this.tagName === "IMG")) {
      if (/\.(mp4|webm)(\?|$)/i.test(value)) {
        origSetAttribute.call(this, "poster", ASSETS.image);
        value = canH264 || !ASSETS.video_webm ? ASSETS.video : ASSETS.video_webm;
      } else {
        value = ASSETS.image;
      }
    }
    return origSetAttribute.call(this, name, value);
  };

  // I download non esistono nella demo.
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest('a[href^="/api/"]');
    if (a) { e.preventDefault(); toastMsg("Nella demo i download sono disattivati"); }
  }, true);

  // Finestre di conferma e di inserimento: nel visualizzatore non esistono, quindi si risponde da soli.
  let promptCount = 0;
  window.confirm = () => true;
  window.alert = (msg) => toastMsg(msg);
  window.prompt = (msg) => {
    if (/pipeline/i.test(msg)) return `Nuova pipeline ${++promptCount}`;
    if (/password/i.test(msg)) return "password-di-prova";
    return null;
  };

  // Schermata di accesso: credenziali già compilate e un suggerimento.
  new MutationObserver(() => {
    const user = document.querySelector('input[autocomplete="username"]');
    if (!user || user.dataset.prefilled) return;
    user.dataset.prefilled = "1";
    user.value = "giacomo";
    const pass = document.querySelector('input[type="password"]');
    if (pass) pass.value = "demo-demo";
    const card = user.closest(".card");
    if (card) {
      const hint = document.createElement("p");
      hint.className = "small muted";
      hint.style.marginTop = "14px";
      hint.textContent = "Demo: va bene qualsiasi password. Entra come «mario» per vedere l'interfaccia di un utente senza permessi di amministratore.";
      card.append(hint);
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
