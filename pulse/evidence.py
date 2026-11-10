"""Evidence ledger: every number a tool returns is recorded under a short id (E1, E2, ...).

The Writer cites these ids; the Verifier checks each cited number against them.
One ledger per request, shared by all agents via a ContextVar (asyncio tasks inherit it).
"""
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Evidence:
    id: str
    tool: str
    args: dict
    table: str
    date_range: tuple[str, str] | None
    n: int | None
    values: dict[str, float]          # named numbers that may be cited
    note: str = ""
    source_url: str | None = None

    def footnote(self) -> str:
        rng = f"{self.date_range[0]} to {self.date_range[1]}" if self.date_range else "all dates"
        n = f", n={self.n} days" if self.n is not None else ""
        src = f" ({self.source_url})" if self.source_url else ""
        return f"[{self.id}] `{self.tool}` on `{self.table}`, {rng}{n}{src}"


@dataclass
class EvidenceLedger:
    items: dict[str, Evidence] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **kw) -> str:
        with self._lock:
            eid = f"E{len(self.items) + 1}"
            self.items[eid] = Evidence(id=eid, **kw)
            return eid

    def get(self, eid: str) -> Evidence | None:
        return self.items.get(eid)


_current: ContextVar[EvidenceLedger | None] = ContextVar("pulse_ledger", default=None)


def new_ledger() -> EvidenceLedger:
    ledger = EvidenceLedger()
    _current.set(ledger)
    return ledger


def ledger() -> EvidenceLedger:
    current = _current.get()
    if current is None:  # tools called outside a request (tests, MCP): use a throwaway ledger
        current = new_ledger()
    return current
