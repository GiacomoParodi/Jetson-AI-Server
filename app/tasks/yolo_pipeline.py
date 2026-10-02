"""Esecuzione di una pipeline YOLO ad albero su video e immagini.

Ogni nodo ha un modello (detection, segmentazione o classificazione). I nodi
radice guardano il fotogramma intero; i figli di un nodo detection/segmentazione
guardano il ritaglio di ogni oggetto trovato dal padre; i figli di un nodo di
classificazione guardano la stessa immagine del padre se la classe predetta
corrisponde. Vedi app/services/pipelines.py per la struttura dei nodi.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..services import ollama, pipelines, yolo_models
from .base import JobContext, Param, Task, TaskError

VIDEO_EXT = [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"]
IMAGE_EXT = [".jpg", ".jpeg", ".png", ".bmp", ".webp"]

# Colori (BGR) dei nodi, nell'ordine in cui compaiono nell'albero.
# Devono coincidere con NODE_COLORS in app/static/app.js (stessi colori in RGB).
PALETTE = [
    (26, 159, 255), (255, 134, 46), (75, 180, 60), (251, 64, 224), (200, 200, 0),
    (77, 77, 255), (255, 107, 156), (0, 212, 200), (191, 127, 255), (99, 110, 141),
]
MIN_CROP_PX = 8

CSV_FIELDS = ["id", "parent_id", "frame", "time_s", "node", "path", "type", "class",
              "confidence", "track_id", "x1", "y1", "x2", "y2"]


@dataclass
class RunNode:
    cfg: dict[str, Any]
    model: Any
    task: str
    names: dict[int, str]
    imgsz: int
    color: tuple[int, int, int]
    path: str
    root: bool
    class_ids: list[int] | None
    parent_filter: set[str]
    children: list["RunNode"] = field(default_factory=list)


@dataclass
class FrameOutput:
    rows: list[dict[str, Any]] = field(default_factory=list)
    boxes: list[tuple] = field(default_factory=list)      # (x1, y1, x2, y2, colore, etichetta)
    polygons: list[tuple] = field(default_factory=list)   # (punti, colore)
    labels: list[tuple] = field(default_factory=list)     # (x1, y1, x2, y2, colore, testo) classificazioni


class YoloPipeline(Task):
    id = "yolo_pipeline"
    title = "Analisi video"
    section = "video"
    activity = "un'analisi video"
    description = (
        "Esegue una pipeline di modelli di visione (rilevamento, segmentazione, classificazione) su un video "
        "o un'immagine. Restituisce il file annotato, i conteggi per fase e un CSV con tutti i risultati."
    )
    accept = VIDEO_EXT + IMAGE_EXT
    params = [
        Param("pipeline", "Pipeline", "select", required=True,
              help="Gli amministratori creano e modificano le pipeline nella sezione Pipeline"),
        Param("track", "Conta gli oggetti unici (tracking)", "bool", default=True,
              help="Solo video, per i nodi radice di detection/segmentazione"),
        Param("frame_step", "Analizza un frame ogni", "number", default=1, min=1, max=30, step=1,
              help="Valori più alti = più veloce ma meno fluido (es. 3 = un frame su tre)"),
    ]

    def available(self) -> tuple[bool, str]:
        try:
            import cv2  # noqa: F401
            import ultralytics  # noqa: F401
        except ImportError as e:
            return False, f"Libreria mancante: {e.name}"
        if not self.param_choices(self.params[0]):
            return False, "Nessuna pipeline pronta: un amministratore deve crearne una nella sezione Pipeline"
        return True, ""

    def param_choices(self, param: Param) -> list[Any] | None:
        if param.name == "pipeline":
            return [{"value": str(p["id"]), "label": p["name"]} for p in pipelines.list_all() if p["node_count"]]
        return param.choices

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        out = super().validate_params(raw)
        p = pipelines.get(int(out["pipeline"]))
        if not p:
            raise TaskError("Pipeline non trovata")
        try:
            tree = pipelines.normalize(p["tree"])
        except pipelines.PipelineError as e:
            raise TaskError(f"La pipeline '{p['name']}' non è più valida: {e}") from None
        # Copia della pipeline al momento dell'invio: se poi viene modificata, il
        # lavoro usa comunque quella scelta.
        out["_pipeline"] = {"id": p["id"], "name": p["name"], "version": p["version"], "tree": tree}
        return out

    # ------------------------------------------------------------------ preparazione

    def _build(self, tree: list[dict], track: bool) -> tuple[list[RunNode], list[str]]:
        notes: list[str] = []
        shared: dict[str, Any] = {}
        counter = [0]

        def make(cfg: dict, parent: RunNode | None) -> RunNode:
            meta = yolo_models.get_model(cfg["model"])
            if not meta:
                raise TaskError(f"Il modello {cfg['model']} del nodo '{cfg['name']}' non esiste più")
            if meta["status"] != yolo_models.READY or meta["backend"] != "tensorrt":
                if yolo_models.tensorrt_enabled():
                    msg = f"Il modello {cfg['model']} non è ancora ottimizzato: questa analisi è più lenta del normale."
                    if msg not in notes:
                        notes.append(msg)
            root = parent is None
            # Un nodo radice con tracking ha bisogno di un'istanza tutta sua (il tracker
            # vive dentro il modello); gli altri nodi condividono lo stesso modello.
            if root and track and meta["task"] != "classify":
                model = yolo_models.load(cfg["model"])
            else:
                model = shared.get(cfg["model"]) or shared.setdefault(cfg["model"], yolo_models.load(cfg["model"]))
            names = model.names if isinstance(model.names, dict) else dict(enumerate(model.names))
            by_name = {v: k for k, v in names.items()}
            class_ids = [by_name[c] for c in cfg["classes"] if c in by_name] or None
            node = RunNode(
                cfg=cfg, model=model, task=meta["task"], names=names, imgsz=meta.get("imgsz") or 640,
                color=PALETTE[counter[0] % len(PALETTE)],
                path=f"{parent.path} › {cfg['name']}" if parent else cfg["name"],
                root=root, class_ids=class_ids if meta["task"] != "classify" else None,
                parent_filter=set(cfg.get("parent_classes") or []),
            )
            counter[0] += 1
            node.children = [make(c, node) for c in cfg.get("children") or []]
            return node

        return [make(c, None) for c in tree], notes

    # ------------------------------------------------------------------ inferenza

    def _process(self, frame, roots: list[RunNode], frame_idx: int, time_s: float,
                 track: bool, next_id: list[int]) -> FrameOutput:
        out = FrameOutput()
        h, w = frame.shape[:2]
        for node in roots:
            self._run_node(node, frame, frame, (0, 0, w, h), None, out, frame_idx, time_s, track, next_id)
        return out

    def _run_node(self, node: RunNode, frame, image, region: tuple[int, int, int, int], parent_id: int | None,
                  out: FrameOutput, frame_idx: int, time_s: float, track: bool, next_id: list[int]) -> None:
        ox, oy = region[0], region[1]
        conf = float(node.cfg["conf"])

        def add_row(kind: str, cls: str, score: float, box: tuple, track_id=None) -> int:
            next_id[0] += 1
            out.rows.append({
                "id": next_id[0], "parent_id": parent_id, "frame": frame_idx, "time_s": round(time_s, 3),
                "node": node.cfg["name"], "path": node.path, "type": kind, "class": cls,
                "confidence": round(score, 3), "track_id": track_id,
                "x1": round(box[0], 1), "y1": round(box[1], 1), "x2": round(box[2], 1), "y2": round(box[3], 1),
            })
            return next_id[0]

        if node.task == "classify":
            result = node.model.predict(image, imgsz=node.imgsz, verbose=False)[0]
            if result.probs is None:
                return
            cls = node.names.get(int(result.probs.top1), str(result.probs.top1))
            score = float(result.probs.top1conf)
            if score < conf or (node.cfg["classes"] and cls not in node.cfg["classes"]):
                return
            row_id = add_row("classificazione", cls, score, region)
            out.labels.append((*region, node.color, f"{node.cfg['name']}: {cls} {score:.2f}"))
            for child in node.children:
                if not child.parent_filter or cls in child.parent_filter:
                    self._run_node(child, frame, image, region, row_id, out, frame_idx, time_s, track, next_id)
            return

        kwargs = dict(conf=conf, classes=node.class_ids, imgsz=node.imgsz, verbose=False)
        if node.root and track:
            result = node.model.track(image, persist=True, tracker="bytetrack.yaml", **kwargs)[0]
        else:
            result = node.model.predict(image, **kwargs)[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return
        kind = "segmentazione" if node.task == "segment" else "detection"
        polys = result.masks.xy if (node.task == "segment" and result.masks is not None) else None
        ids = boxes.id.int().tolist() if boxes.id is not None else [None] * len(boxes)
        fh, fw = frame.shape[:2]
        for i, (cls_i, score, xyxy, tid) in enumerate(zip(boxes.cls.int().tolist(), boxes.conf.tolist(),
                                                            boxes.xyxy.tolist(), ids)):
            cls = node.names.get(cls_i, str(cls_i))
            box = (xyxy[0] + ox, xyxy[1] + oy, xyxy[2] + ox, xyxy[3] + oy)
            row_id = add_row(kind, cls, score, box, tid if node.root else None)
            label = f"{cls} {score:.2f}" + (f" #{tid}" if tid is not None and node.root else "")
            out.boxes.append((*box, node.color, label))
            if polys is not None and i < len(polys) and len(polys[i]):
                pts = polys[i].copy()
                pts[:, 0] += ox
                pts[:, 1] += oy
                out.polygons.append((pts, node.color))
            for child in node.children:
                if child.parent_filter and cls not in child.parent_filter:
                    continue
                bw, bh = box[2] - box[0], box[3] - box[1]
                pad_x, pad_y = bw * child.cfg["padding"], bh * child.cfg["padding"]
                x1, y1 = max(0, int(box[0] - pad_x)), max(0, int(box[1] - pad_y))
                x2, y2 = min(fw, int(box[2] + pad_x)), min(fh, int(box[3] + pad_y))
                if x2 - x1 < MIN_CROP_PX or y2 - y1 < MIN_CROP_PX:
                    continue
                crop = frame[y1:y2, x1:x2]
                self._run_node(child, frame, crop, (x1, y1, x2, y2), row_id, out, frame_idx, time_s, track, next_id)

    # ------------------------------------------------------------------ disegno

    @staticmethod
    def _draw(frame, fo: FrameOutput):
        import cv2
        import numpy as np

        img = frame.copy()
        h, w = img.shape[:2]
        scale = max(0.4, min(1.2, w / 1600))
        thick = max(1, int(round(w / 640)))
        if fo.polygons:
            overlay = img.copy()
            for pts, color in fo.polygons:
                cv2.fillPoly(overlay, [pts.astype(np.int32)], color)
            img = cv2.addWeighted(overlay, 0.35, img, 0.65, 0)
            for pts, color in fo.polygons:
                cv2.polylines(img, [pts.astype(np.int32)], True, color, thick)

        def text(x, y, s, color):
            (tw, th), base = cv2.getTextSize(s, cv2.FONT_HERSHEY_SIMPLEX, scale, max(1, thick - 1))
            x = int(max(0, min(x, w - tw - 4)))
            y = int(max(th + 4, min(y, h - 2)))
            cv2.rectangle(img, (x, y - th - 4), (x + tw + 4, y + base - 2), color, -1)
            cv2.putText(img, s, (x + 2, y - 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), max(1, thick - 1), cv2.LINE_AA)

        for x1, y1, x2, y2, color, label in fo.boxes:
            cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), color, thick)
            text(x1, y1, label, color)
        # Le classificazioni si scrivono in basso dentro la regione (in alto a sinistra se è tutto il frame).
        stacked: dict[tuple, int] = defaultdict(int)
        line_h = int(28 * scale) + 6
        for x1, y1, x2, y2, color, label in fo.labels:
            key = (int(x1), int(y1), int(x2), int(y2))
            n = stacked[key]
            stacked[key] += 1
            if x1 == 0 and y1 == 0 and x2 == w and y2 == h:
                text(6, line_h * (n + 1), label, color)
            else:
                text(x1 + 2, y2 - 4 - n * line_h, label, color)
        return img

    # ------------------------------------------------------------------ esecuzione

    def run(self, ctx: JobContext) -> dict[str, Any]:
        assert ctx.input_path is not None
        snapshot = ctx.params.get("_pipeline")
        if not snapshot:
            raise TaskError("Pipeline mancante")
        is_image = ctx.input_path.suffix.lower() in IMAGE_EXT
        track = bool(ctx.params.get("track")) and not is_image

        ollama.unload_all()  # sul Jetson CPU e GPU condividono la RAM
        ctx.progress(0, "Caricamento dei modelli…", force=True)
        roots, notes = self._build(snapshot["tree"], track)
        try:
            if is_image:
                return self._run_image(ctx, roots, snapshot, notes)
            return self._run_video(ctx, roots, snapshot, notes, track)
        finally:
            del roots
            yolo_models.release_gpu_memory()

    def _run_image(self, ctx, roots, snapshot, notes) -> dict[str, Any]:
        import cv2

        frame = cv2.imread(str(ctx.input_path))
        if frame is None:
            raise TaskError("Impossibile leggere l'immagine")
        ctx.progress(0.3, "Analisi dell'immagine…", force=True)
        fo = self._process(frame, roots, 0, 0.0, False, [0])
        cv2.imwrite(str(ctx.output_dir / "annotata.jpg"), self._draw(frame, fo))
        self._write_tables(ctx.output_dir, fo.rows)
        stats = self._stats([fo.rows], track=False)
        return self._result(snapshot, stats, fo.rows, notes, "annotata.jpg", "image", "Immagine annotata",
                            f"Pipeline **{snapshot['name']}**: {len(fo.rows)} risultati.", track=False)

    def _run_video(self, ctx, roots, snapshot, notes, track) -> dict[str, Any]:
        import cv2

        cap = cv2.VideoCapture(str(ctx.input_path))
        if not cap.isOpened():
            raise TaskError("Impossibile aprire il video (formato non supportato?)")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        step = max(1, int(ctx.params.get("frame_step") or 1))

        raw_path = ctx.output_dir / "_grezzo.mp4"
        writer = cv2.VideoWriter(str(raw_path), cv2.VideoWriter_fourcc(*"mp4v"), fps / step, (width, height))
        all_rows: list[dict[str, Any]] = []
        per_frame_rows: list[list[dict[str, Any]]] = []
        next_id = [0]
        frame_idx = processed = 0
        try:
            while True:
                ctx.check_cancelled()
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_idx % step == 0:
                    fo = self._process(frame, roots, frame_idx, frame_idx / fps, track, next_id)
                    all_rows.extend(fo.rows)
                    per_frame_rows.append(fo.rows)
                    writer.write(self._draw(frame, fo))
                    processed += 1
                frame_idx += 1
                if total:
                    ctx.progress(0.95 * frame_idx / total, f"Frame {frame_idx} di {total}")
                else:
                    ctx.progress(0.5, f"Frame {frame_idx}")
        finally:
            cap.release()
            writer.release()
        if processed == 0:
            raise TaskError("Il video non contiene frame leggibili")

        ctx.progress(0.96, "Conversione del video per il browser…", force=True)
        video_name = self._to_browser_mp4(raw_path, ctx.output_dir / "annotato.mp4")
        if video_name == raw_path.name:
            notes.append("ffmpeg non è installato: il video potrebbe non essere riproducibile nel browser, ma è scaricabile.")
        self._write_tables(ctx.output_dir, all_rows)
        stats = self._stats(per_frame_rows, track)
        duration = frame_idx / fps if fps else 0
        summary = (f"Pipeline **{snapshot['name']}**: analizzati {processed} frame su {frame_idx} "
                   f"({duration:.1f} s di video), {len(all_rows)} risultati.")
        return self._result(snapshot, stats, all_rows, notes, video_name, "video", "Video annotato", summary, track)

    @staticmethod
    def _stats(per_frame_rows: list[list[dict]], track: bool) -> dict[tuple[str, str], dict[str, Any]]:
        stats: dict[tuple[str, str], dict[str, Any]] = {}
        for rows in per_frame_rows:
            in_frame: dict[tuple[str, str], int] = defaultdict(int)
            for r in rows:
                key = (r["path"], r["class"])
                s = stats.setdefault(key, {"type": r["type"], "total": 0, "max": 0, "tracks": set()})
                s["total"] += 1
                in_frame[key] += 1
                if track and r["track_id"] is not None:
                    s["tracks"].add(r["track_id"])
            for key, n in in_frame.items():
                stats[key]["max"] = max(stats[key]["max"], n)
        return stats

    def _result(self, snapshot, stats, rows, notes, media, kind, media_label, summary, track) -> dict[str, Any]:
        order = [n["name"] for n, _, _ in pipelines.iter_nodes(snapshot["tree"])]

        def sort_key(item):
            (path, cls), s = item
            leaf = path.split(" › ")[-1]
            return (order.index(leaf) if leaf in order else 99, path, -s["total"])

        columns = ["Nodo", "Tipo", "Classe", "Max nello stesso frame"]
        if track:
            columns.append("Oggetti unici (stima)")
        columns.append("Totale")
        table_rows = []
        for (path, cls), s in sorted(stats.items(), key=sort_key):
            row: list[Any] = [path, s["type"], cls, s["max"]]
            if track:
                row.append(len(s["tracks"]) if s["tracks"] else "—")
            row.append(s["total"])
            table_rows.append(row)
        if not rows:
            summary += " Nessun oggetto trovato con queste impostazioni."
        return {
            "summary": summary,
            "table": {"columns": columns, "rows": table_rows},
            "outputs": [
                {"file": media, "kind": kind, "label": media_label},
                {"file": "risultati.csv", "kind": "file", "label": "Risultati (CSV)"},
                {"file": "risultati.json", "kind": "file", "label": "Risultati (JSON)"},
            ],
            "notes": notes,
            "pipeline": {"name": snapshot["name"], "version": snapshot["version"], "tree": snapshot["tree"]},
        }

    @staticmethod
    def _to_browser_mp4(raw: Path, out: Path) -> str:
        """OpenCV scrive mp4v, che i browser non riproducono: convertiamo in H.264."""
        if not shutil.which("ffmpeg"):
            return raw.name
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
        if subprocess.run(cmd, capture_output=True).returncode != 0 or not out.is_file():
            return raw.name
        raw.unlink(missing_ok=True)
        return out.name

    @staticmethod
    def _write_tables(output_dir: Path, rows: list[dict[str, Any]]) -> None:
        (output_dir / "risultati.json").write_text(json.dumps(rows))
        with open(output_dir / "risultati.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
