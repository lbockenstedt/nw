"""Idle-progress watchdog ("heartbeat") for long device operations.

A fixed wall-clock timeout (``asyncio.wait_for(op, 3.0)``) kills an operation
that is *still working* — e.g. an AOS-S switch that streams a slow, multi-line
login banner + terminal repaint before its CLI prompt appears takes well over
the 3 s fleet-reachability budget, so it is wrongly reported unreachable even
though bytes were flowing the whole time.

This module replaces that with an *idle* timeout. The guarded operation calls
:func:`beat` whenever it makes observable progress (a chunk of bytes read, a
banner/pager gate answered, a command sent, a connection established); the
watchdog cancels it only if **no** progress happens for ``idle_timeout``
seconds (optionally also bounded by a total ``hard_cap`` safety net). A
progressing operation runs to completion; a genuinely hung or dead one still
fails fast.

The beat channel is ambient (a :class:`contextvars.ContextVar`) so deep IO code
(``transports/cli_io.py``) can signal progress without threading a callback
through every layer. :func:`guard` sets the current context's watch *before*
scheduling the operation as a child task — which copies the context — so each
concurrent probe under ``asyncio.gather`` gets its own independent watch with
no cross-talk. Calling :func:`beat` outside a :func:`guard` is a safe no-op.
"""
import asyncio
import contextvars
from typing import Awaitable, Optional, TypeVar

T = TypeVar("T")

_watch: "contextvars.ContextVar" = contextvars.ContextVar(
    "nw_heartbeat", default=None)


class _Watch:
    """Records the monotonic time of the most recent progress beat."""

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last = asyncio.get_event_loop().time()

    def beat(self) -> None:
        self._last = asyncio.get_event_loop().time()

    @property
    def idle(self) -> float:
        return asyncio.get_event_loop().time() - self._last


def beat() -> None:
    """Signal progress to an enclosing :func:`guard`. No-op when called outside
    one, so IO code may call it unconditionally."""
    w = _watch.get()
    if w is not None:
        w.beat()


async def guard(op: "Awaitable[T]", idle_timeout: float,
                hard_cap: "Optional[float]" = None) -> "T":
    """Run ``op``, cancelling it only if it makes no progress (no :func:`beat`)
    for ``idle_timeout`` seconds — not on a fixed wall clock. Optionally bound
    total runtime by ``hard_cap`` seconds as a safety net against an operation
    that dribbles progress forever. Raises :class:`asyncio.TimeoutError` on
    either bound so callers can treat it like ``asyncio.wait_for``.

    An operation that emits no beats at all degrades to a plain ``idle_timeout``
    total timeout — so SNMP/REST probes (no heartbeat instrumentation) keep
    their original fixed-budget behaviour."""
    if idle_timeout <= 0:
        raise ValueError("idle_timeout must be > 0")
    loop = asyncio.get_event_loop()
    watch = _Watch()
    token = _watch.set(watch)
    task = asyncio.ensure_future(op)
    started = loop.time()
    try:
        while True:
            # Sleep until the earliest of: op finishing, the idle deadline, or
            # (if set) the hard cap — then re-evaluate. Beats landing during
            # the sleep push watch.idle back down, extending the idle deadline.
            waits = [idle_timeout - watch.idle]
            if hard_cap is not None:
                waits.append(hard_cap - (loop.time() - started))
            delay = max(0.05, min(waits))
            done, _ = await asyncio.wait({task}, timeout=delay)
            if task in done:
                return task.result()
            if watch.idle >= idle_timeout:
                raise asyncio.TimeoutError(
                    f"no progress for {watch.idle:.1f}s "
                    f"(idle timeout {idle_timeout:.1f}s)")
            if hard_cap is not None and (loop.time() - started) >= hard_cap:
                raise asyncio.TimeoutError(
                    f"exceeded hard cap {hard_cap:.1f}s")
    finally:
        _watch.reset(token)
        if not task.done():
            # Cancel the still-running op and let its own cleanup (e.g.
            # cli_io.connect's session teardown) run before we return, so an
            # authenticated switch session/vty slot is never leaked.
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 — cancellation/cleanup errors
                pass
