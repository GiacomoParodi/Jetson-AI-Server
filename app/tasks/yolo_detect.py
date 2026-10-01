"""Rilevamento oggetti con YOLO su video e immagini."""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..services import ollama, yolo_models
from .base import JobContext, Param, Task, TaskError

VIDEO_EXT = [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"]
IMAGE_EXT = [".jpg", ".jpeg", ".png", ".bmp", ".webp"]


class YoloDetect(Task):
    id = "yolo_detect"
    title = "Rilevamento oggetti (YOLO)"
    description = (
        "Rileva oggetti in un video o in un'immagine con un modello YOLO. "
        "Restituisce il file con i riquadri disegnati, il conteggio per classe e un CSV con tutte le detection."
    )
    accept = VIDEO_EXT + IMAGE_EXT
    params = [
        Param("model", "Modello", "yolo_model", required=True,
              help="Carica i tuoi modelli .pt dalla pagina Modelli"),
        Param("confidence", "Confidenza minima", "number", default=0.35, min=0.05, max=0.95, step=0.05,
              help="Le detection sotto questa soglia vengono scartate"),
        Param("classes", "Classi da cercare", "text", default="",
              help="Nomi separati da virgola (es. desk, chair). Vuoto = tutte le classi del modello"),
        Param("track", "Conta gli oggetti unici (tracking)", "bool", default=True,
              help="Solo video: segue ogni oggetto tra i frame per non contarlo più volte"),
        Param("frame_step", "Analizza un frame ogni", "number", default=1, min=1, max=30, step=1,
              help="Valori più alti = più veloce ma meno fluido (es. 3 = un frame su tre)"),
    ]

    def available(self) -> tuple[bool, str]:
        try:
            import cv2  # noqa: F401
            import ultralytics  # noqa: F401
        except ImportError as e:
            return False, f"Libreria mancante: {e.name}"
        return True, ""

    def param_choices(self, param: Param) -> list[str] | None:
        if param.type == "yolo_model":
            return yolo_models.model_names()
        return param.choices

    # ------------------------------------------------------------------ esecuzione

    def run(self, ctx: JobContext) -> dict[str, Any]:
        assert ctx.input_path is not None
        ext = ctx.input_path.suffix.lower()

        # Sul Jetson CPU e GPU condividono la RAM: liberiamo l'LLM se era caricato.
        ollama.unload_all()

        ctx.progress(0, "Caricamento del modello…", force=True)
        model = yolo_models.load(ctx.params["model"], status=lambda m: ctx.progress(0, m, force=True))
        try:
            class_ids = self._class_ids(model, ctx.params.get("classes") or "")
            if ext in IMAGE_EXT:
                return self._run_image(ctx, model, class_ids)
            return self._run_video(ctx, model, class_ids)
        finally:
            del model
            yolo_models.release_gpu_memory()

    @staticmethod
    def _names(model) -> dict[int, str]:
        names = model.names
        return dict(names) if isinstance(names, dict) else dict(enumerate(names))

    def _class_ids(self, model, text: str) -> list[int] | None:
        wanted = [c.strip().lower() for c in text.split(",") if c.strip()]
        if not wanted:
            return None
        by_name = {name.lower(): i for i, name in self._names(model).items()}
        missing = [c for c in wanted if c not in by_name]
        if missing:
            available = ", ".join(sorted(self._names(model).values()))
            raise TaskError(f"Classi non presenti nel modello: {', '.join(missing)}. Disponibili: {available}")
        return [by_name[c] for c in wanted]

    def _predict(self, ctx: JobContext, model, frame, class_ids, track: bool):
        kwargs = dict(conf=float(ctx.params["confidence"]), classes=class_ids, verbose=False)
        if track:
            return model.track(frame, persist=True, tracker="bytetrack.yaml", **kwargs)[0]
        return model.predict(frame, **kwargs)[0]

    def _rows(self, result, names: dict[int, str], frame_idx: int, time_s: float) -> list[dict[str, Any]]:
        rows = []
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return rows
        ids = boxes.id.int().tolist() if boxes.id is not None else [None] * len(boxes)
        for cls, conf, xyxy, tid in zip(boxes.cls.int().tolist(), boxes.conf.tolist(), boxes.xyxy.tolist(), ids):
            rows.append({
                "frame": frame_idx,
                "time_s": round(time_s, 3),
                "class": names.get(cls, str(cls)),
                "confidence": round(conf, 3),
                "track_id": tid,
                "x1": round(xyxy[0], 1), "y1": round(xyxy[1], 1),
                "x2": round(xyxy[2], 1), "y2": round(xyxy[3], 1),
            })
        return rows

    def _run_image(self, ctx: JobContext, model, class_ids) -> dict[str, Any]:
        import cv2

        image = cv2.imread(str(ctx.input_path))
        if image is None:
            raise TaskError("Impossibile leggere l'immagine")
        ctx.progress(0.3, "Analisi dell'immagine…", force=True)
        result = self._predict(ctx, model, image, class_ids, track=False)
        rows = self._rows(result, self._names(model), 0, 0)
        cv2.imwrite(str(ctx.output_dir / "annotata.jpg"), result.plot())
        self._write_tables(ctx.output_dir, rows)

        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            counts[r["class"]] += 1
        total = sum(counts.values())
        return {
            "summary": f"Trovati {total} oggetti." if total else "Nessun oggetto trovato con questi parametri.",
            "table": {
                "columns": ["Classe", "Quantità"],
                "rows": [[k, v] for k, v in sorted(counts.items(), key=lambda kv: -kv[1])],
            },
            "outputs": [
                {"file": "annotata.jpg", "kind": "image", "label": "Immagine annotata"},
                {"file": "detections.csv", "kind": "file", "label": "Detection (CSV)"},
                {"file": "detections.json", "kind": "file", "label": "Detection (JSON)"},
            ],
        }

    def _run_video(self, ctx: JobContext, model, class_ids) -> dict[str, Any]:
        import cv2

        cap = cv2.VideoCapture(str(ctx.input_path))
        if not cap.isOpened():
            raise TaskError("Impossibile aprire il video (formato non supportato?)")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        step = max(1, int(ctx.params["frame_step"]))
        track = bool(ctx.params["track"])
        names = self._names(model)

        raw_path = ctx.output_dir / "_grezzo.mp4"
        writer = cv2.VideoWriter(str(raw_path), cv2.VideoWriter_fourcc(*"mp4v"), fps / step, (width, height))

        rows: list[dict[str, Any]] = []
        max_per_frame: dict[str, int] = defaultdict(int)
        unique_tracks: dict[str, set[int]] = defaultdict(set)
        frame_idx = 0
        processed = 0
        try:
            while True:
                ctx.check_cancelled()
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_idx % step == 0:
                    result = self._predict(ctx, model, frame, class_ids, track)
                    frame_rows = self._rows(result, names, frame_idx, frame_idx / fps)
                    rows.extend(frame_rows)
                    per_frame: dict[str, int] = defaultdict(int)
                    for r in frame_rows:
                        per_frame[r["class"]] += 1
                        if r["track_id"] is not None:
                            unique_tracks[r["class"]].add(r["track_id"])
                    for cls, n in per_frame.items():
                        max_per_frame[cls] = max(max_per_frame[cls], n)
                    writer.write(result.plot())
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
        self._write_tables(ctx.output_dir, rows)

        classes = sorted(set(max_per_frame) | set(unique_tracks), key=lambda c: -max_per_frame.get(c, 0))
        detections_per_class: dict[str, int] = defaultdict(int)
        for r in rows:
            detections_per_class[r["class"]] += 1
        columns = ["Classe", "Max nello stesso frame"]
        if track:
            columns.append("Oggetti unici (stima)")
        columns.append("Detection totali")
        table_rows = []
        for c in classes:
            row: list[Any] = [c, max_per_frame.get(c, 0)]
            if track:
                row.append(len(unique_tracks.get(c, ())))
            row.append(detections_per_class[c])
            table_rows.append(row)

        duration = frame_idx / fps if fps else 0
        summary = f"Analizzati {processed} frame su {frame_idx} ({duration:.1f} s di video). "
        if classes:
            top = classes[0]
            summary += f"Classe più presente: {top} (fino a {max_per_frame[top]} nello stesso frame)."
        else:
            summary += "Nessun oggetto trovato con questi parametri."

        notes = []
        if video_name == "_grezzo.mp4":
            notes.append("ffmpeg non è installato: il video potrebbe non essere riproducibile nel browser, ma è scaricabile.")
        return {
            "summary": summary,
            "table": {"columns": columns, "rows": table_rows},
            "outputs": [
                {"file": video_name, "kind": "video", "label": "Video annotato"},
                {"file": "detections.csv", "kind": "file", "label": "Detection (CSV)"},
                {"file": "detections.json", "kind": "file", "label": "Detection (JSON)"},
            ],
            "notes": notes,
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
        (output_dir / "detections.json").write_text(json.dumps(rows))
        fields = ["frame", "time_s", "class", "confidence", "track_id", "x1", "y1", "x2", "y2"]
        with open(output_dir / "detections.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
