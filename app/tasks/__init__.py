"""Registro dei compiti: trova automaticamente i plugin all'avvio."""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

from .base import JobCancelled, JobContext, Param, Task, TaskError

__all__ = ["JobCancelled", "JobContext", "Param", "Task", "TaskError", "registry"]

log = logging.getLogger(__name__)


class TaskRegistry:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def register(self, task: Task) -> None:
        if not task.id:
            raise ValueError(f"{type(task).__name__} non ha un id")
        if task.id in self.tasks:
            log.warning("Compito '%s' definito due volte: uso l'ultimo", task.id)
        self.tasks[task.id] = task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def all(self) -> list[Task]:
        return list(self.tasks.values())

    def _register_module(self, module: ModuleType) -> None:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Task) and obj is not Task and obj.__module__ == module.__name__ and obj.id:
                self.register(obj())

    def discover(self, extra_dirs: list[Path] | None = None) -> None:
        """Carica i moduli in app/tasks/ e i file .py nelle cartelle extra.

        Un plugin che fallisce al caricamento viene saltato (con un messaggio nel
        log) senza bloccare il server.
        """
        package_dir = Path(__file__).parent
        for info in pkgutil.iter_modules([str(package_dir)]):
            if info.name in ("base",) or info.name.startswith("_"):
                continue
            try:
                self._register_module(importlib.import_module(f"{__name__}.{info.name}"))
            except Exception:
                log.exception("Impossibile caricare il compito %s", info.name)

        for directory in extra_dirs or []:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                name = f"jas_plugin_{path.stem}"
                try:
                    spec = importlib.util.spec_from_file_location(name, path)
                    assert spec and spec.loader
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[name] = module
                    spec.loader.exec_module(module)
                    self._register_module(module)
                except Exception:
                    log.exception("Impossibile caricare il plugin %s", path)
        log.info("Compiti disponibili: %s", ", ".join(self.tasks) or "nessuno")


registry = TaskRegistry()
