"""Turno per l'uso della GPU e della memoria.

Il Jetson Orin Nano ha 8 GB condivisi tra CPU e GPU: un LLM e dei modelli di
visione insieme rischiano di non starci. Chat con i documenti, analisi video e
indicizzazione dei documenti si mettono quindi in fila, nell'ordine di arrivo
(FIFO). Con JAS_CONCURRENT_AI=on la fila è disattivata e tutto può girare insieme.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from ..config import settings


@dataclass(eq=False)
class Ticket:
    kind: str                      # "job" | "chat"
    label: str = ""                # descrizione mostrata a chi aspetta
    created: float = field(default_factory=time.time)


class Gate:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._cond = threading.Condition()
        self._queue: deque[Ticket] = deque()
        self._holder: Ticket | None = None

    def enter(self, kind: str, label: str = "") -> Ticket:
        """Si mette in fila. Poi si chiama wait() e, a fine lavoro, leave()."""
        ticket = Ticket(kind, label)
        with self._cond:
            self._queue.append(ticket)
        return ticket

    def wait(self, ticket: Ticket, timeout: float | None = None) -> bool:
        """True quando è il proprio turno (da quel momento il ticket tiene la risorsa)."""
        with self._cond:
            if not self.enabled:
                self._discard(ticket)
                return True
            ok = self._cond.wait_for(
                lambda: self._holder is None and bool(self._queue) and self._queue[0] is ticket, timeout)
            if ok:
                self._queue.popleft()
                self._holder = ticket
            return ok

    def leave(self, ticket: Ticket) -> None:
        with self._cond:
            if self._holder is ticket:
                self._holder = None
            else:
                self._discard(ticket)
            self._cond.notify_all()

    def _discard(self, ticket: Ticket) -> None:
        try:
            self._queue.remove(ticket)
        except ValueError:
            pass

    @property
    def holder(self) -> Ticket | None:
        return self._holder

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    def ahead_of(self, ticket: Ticket) -> int:
        """Quanti sono in fila prima di questo ticket (senza contare chi sta lavorando)."""
        with self._cond:
            for i, t in enumerate(self._queue):
                if t is ticket:
                    return i
        return 0

    @contextmanager
    def hold(self, kind: str, label: str = "") -> Iterator[Ticket]:
        ticket = self.enter(kind, label)
        try:
            self.wait(ticket)
            yield ticket
        finally:
            self.leave(ticket)


gate = Gate(enabled=not settings.concurrent_ai)
