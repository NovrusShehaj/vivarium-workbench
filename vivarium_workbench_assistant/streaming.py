"""SSE framing, the run registry and the stream consumer.

A run has two halves:

* the **producer** — an ``asyncio`` task running the chat service; it pushes
  events onto the run's queue and owns the upstream provider stream;
* the **consumer** — the async generator a ``StreamingResponse`` iterates; it
  frames queued events as SSE and emits a ``: ka`` keep-alive comment when the
  producer is quiet for ``HEARTBEAT_S`` seconds.

When the browser disconnects (or aborts), Starlette cancels the consumer; its
``finally`` cancels the producer, and the provider's ``httpx`` stream is closed
by the producer's own ``async with`` — so an abandoned run never keeps a
provider (or a local GPU) busy. The explicit cancel endpoint does the same.

Limits (plan §6.3): one active run per conversation, a per-session cap and a
global cap. Client-facing event schema (versioned ``v: 1``): ``run.start``,
``text.delta``, ``tool.call``, ``tool.result``, ``proposal``, ``usage``,
``error``, ``run.end``.
"""
from __future__ import annotations

import asyncio
import json
import secrets as _secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

HEARTBEAT_S = 15.0
QUEUE_MAX = 1024
PER_SESSION_RUNS = 2
GLOBAL_RUNS = 8
RID_RE_PATTERN = r"^r_[0-9a-f]{16}$"


def sse(event: str, data: Any) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


HEARTBEAT = b": ka\n\n"


class RunLimitError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Approval:
    id: str
    run_id: str
    tool: str
    category: str
    args_hash: str
    created_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str | None = None
    scope: str = "once"


@dataclass
class Run:
    id: str
    conversation_id: str
    scope: str
    created_at: float
    queue: "asyncio.Queue[bytes | None]" = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAX))
    task: "asyncio.Task[None] | None" = None
    cancel_reason: str | None = None
    status: str = "starting"
    approvals: dict[str, Approval] = field(default_factory=dict)

    def emit_nowait(self, event: str, data: Any) -> None:
        try:
            self.queue.put_nowait(sse(event, data))
        except asyncio.QueueFull:
            pass

    async def emit(self, event: str, data: Any) -> None:
        await self.queue.put(sse(event, data))

    def cancel(self, reason: str) -> bool:
        """Request cancellation; returns False if the run already finished."""
        if self.cancel_reason is None:
            self.cancel_reason = reason
        for a in self.approvals.values():
            if a.decision is None:
                a.decision = "deny"
                a.event.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            return True
        return False


class RunRegistry:
    def __init__(self, *, per_session: int = PER_SESSION_RUNS, global_limit: int = GLOBAL_RUNS) -> None:
        self.per_session = per_session
        self.global_limit = global_limit
        self._runs: dict[str, Run] = {}

    def active(self) -> list[Run]:
        return [r for r in self._runs.values() if r.task is None or not r.task.done()]

    def start(self, *, scope: str, conversation_id: str) -> Run:
        active = self.active()
        if any(r.conversation_id == conversation_id and r.scope == scope for r in active):
            raise RunLimitError("A response is already streaming in this conversation.", 409)
        if sum(1 for r in active if r.scope == scope) >= self.per_session:
            raise RunLimitError("Too many responses are streaming at once; wait for one to finish.", 429)
        if len(active) >= self.global_limit:
            raise RunLimitError("The server is busy with other responses; try again shortly.", 429)
        run = Run(id="r_" + _secrets.token_hex(8), conversation_id=conversation_id, scope=scope,
                  created_at=time.time())
        self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def discard(self, run: Run) -> None:
        """Forget a run that never started (its preparation was refused)."""
        self._runs.pop(run.id, None)

    def finish(self, run: Run) -> None:
        # Keep finished runs briefly so a late cancel/approval gets a clean answer.
        cutoff = time.time() - 600
        for rid in [rid for rid, r in self._runs.items()
                    if r.task is not None and r.task.done() and r.created_at < cutoff]:
            self._runs.pop(rid, None)

    def cancel_scope(self, scope: str) -> None:
        for r in self.active():
            if r.scope == scope:
                r.cancel("session ended")


async def consume(run: Run, *, heartbeat_s: float | None = None) -> AsyncIterator[bytes]:
    """Frame a run's events for ``StreamingResponse``; cancel the run on disconnect."""
    interval = heartbeat_s if heartbeat_s is not None else HEARTBEAT_S
    finished_normally = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(run.queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                if run.task is not None and run.task.done() and run.queue.empty():
                    break
                yield HEARTBEAT
                continue
            if item is None:
                break
            yield item
        finished_normally = True
    finally:
        if not finished_normally and run.task is not None and not run.task.done():
            # The client went away mid-stream (or the response was torn down).
            run.cancel("disconnect")
