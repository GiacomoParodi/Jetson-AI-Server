"""Classi base per i compiti (plugin).

Per aggiungere un nuovo compito basta creare un file .py in `app/tasks/` (o nella
cartella `plugins/`) con una sottoclasse di `Task`: il server la trova da solo
all'avvio. Vedi il README per un esempio completo.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


class TaskError(Exception):
    """Errore 'previsto' da mostrare all'utente così com'è (senza traceback)."""


class JobCancelled(Exception):
    pass


@dataclass
class Param:
    """Un parametro del compito. L'interfaccia web genera il modulo da questi campi.

    type: text | textarea | number | bool | select
    choices: elenco di valori, oppure di {"value": ..., "label": ...}
    """

    name: str
    label: str
    type: str = "text"
    default: Any = None
    required: bool = False
    help: str = ""
    choices: list[Any] | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def coerce(self, value: Any) -> Any:
        """Converte e valida il valore ricevuto dal client."""
        if value is None or value == "":
            if self.required:
                raise TaskError(f"Il campo '{self.label}' è obbligatorio")
            return self.default
        if self.type == "number":
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise TaskError(f"'{self.label}' deve essere un numero") from None
            if self.min is not None and num < self.min:
                raise TaskError(f"'{self.label}' deve essere almeno {self.min:g}")
            if self.max is not None and num > self.max:
                raise TaskError(f"'{self.label}' deve essere al massimo {self.max:g}")
            return int(num) if num.is_integer() and isinstance(self.default, int) else num
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("1", "true", "on", "yes", "si", "sì")
        if self.type == "select":
            value = str(value)
            allowed = [str(c["value"]) if isinstance(c, dict) else str(c) for c in self.choices or []]
            if self.choices is not None and value not in allowed:
                raise TaskError(f"Valore non valido per '{self.label}': {value}")
            return value
        return str(value)


@dataclass
class JobContext:
    """Quello che un compito riceve quando viene eseguito."""

    job_id: str
    input_path: Path | None
    params: dict[str, Any]
    output_dir: Path
    cache_dir: Path
    _report: Callable[[float, str], None] = field(repr=False, default=lambda p, m: None)
    _is_cancelled: Callable[[], bool] = field(repr=False, default=lambda: False)
    _last_report: float = field(default=0.0, repr=False)

    def progress(self, fraction: float, message: str = "", force: bool = False) -> None:
        """Aggiorna l'avanzamento (0..1). Le scritture sono limitate a ~2 al secondo."""
        now = time.monotonic()
        if force or now - self._last_report >= 0.5:
            self._last_report = now
            self._report(max(0.0, min(1.0, fraction)), message)

    def check_cancelled(self) -> None:
        """Da chiamare spesso nei cicli lunghi: interrompe il lavoro se l'utente l'ha annullato."""
        if self._is_cancelled():
            raise JobCancelled()


class Task:
    """Base di ogni compito. Le sottoclassi definiscono gli attributi e `run`."""

    id: str = ""
    title: str = ""
    description: str = ""
    # Estensioni accettate (es. [".pdf"]). Lista vuota con needs_file=False: nessun file.
    accept: list[str] = []
    needs_file: bool = True
    params: list[Param] = []
    # Testo del pulsante per rieseguire sullo stesso file con altri parametri.
    rerun_label: str = "Riesegui con altri parametri"
    # Compito interno: non compare tra gli strumenti e non si avvia a mano.
    hidden: bool = False
    # Area dell'interfaccia a cui appartiene: "video", "docs" oppure "" (altri strumenti, es. plugin).
    section: str = ""
    # Come lo si descrive a chi aspetta il proprio turno ("Il server sta eseguendo …").
    activity: str = "un'elaborazione"

    def available(self) -> tuple[bool, str]:
        """(True, "") se il compito può girare; altrimenti (False, motivo)."""
        return True, ""

    def param_choices(self, param: Param) -> list[Any] | None:
        """Scelte dinamiche per un parametro (es. elenco delle pipeline)."""
        return param.choices

    def describe(self) -> dict[str, Any]:
        ok, reason = self.available()
        params = []
        for p in self.params:
            d = p.to_dict()
            d["choices"] = self.param_choices(p)
            params.append(d)
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "accept": self.accept,
            "needs_file": self.needs_file,
            "params": params,
            "rerun_label": self.rerun_label,
            "hidden": self.hidden,
            "section": self.section,
            "available": ok,
            "unavailable_reason": reason,
        }

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Valida i parametri. Le sottoclassi possono aggiungere chiavi che iniziano
        con '_' (dati interni, es. una copia della pipeline usata)."""
        out = {}
        for p in self.params:
            choices = self.param_choices(p)
            checker = Param(**{**p.to_dict(), "choices": choices})
            out[p.name] = checker.coerce(raw.get(p.name))
        return out

    def run(self, ctx: JobContext) -> dict[str, Any]:
        """Esegue il compito e restituisce il risultato da mostrare.

        Formato del risultato (tutte le chiavi sono facoltative):
          summary: str                testo principale (es. la risposta dell'LLM)
          table:   {columns, rows}    una tabella
          outputs: [{file, kind, label}]  file in output_dir; kind: video|image|text|file
          sources: [{label, text}]    estratti/citazioni
          notes:   [str]              avvisi
        """
        raise NotImplementedError
