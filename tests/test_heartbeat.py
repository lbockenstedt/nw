"""Tests for the idle-progress watchdog (heartbeat.guard / heartbeat.beat)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
import heartbeat  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_guard_returns_result_for_fast_op():
    async def op():
        return 42
    assert _run(heartbeat.guard(op(), idle_timeout=1.0)) == 42


def test_guard_extends_while_op_beats():
    # An op that runs far longer than idle_timeout but beats regularly must
    # complete — the idle deadline resets on every beat.
    async def op():
        for _ in range(10):
            await asyncio.sleep(0.05)
            heartbeat.beat()
        return "done"
    assert _run(heartbeat.guard(op(), idle_timeout=0.2)) == "done"


def test_guard_cancels_stalled_op():
    cancelled = {"hit": False}

    async def op():
        try:
            await asyncio.sleep(10)  # never beats
        except asyncio.CancelledError:
            cancelled["hit"] = True
            raise
        return "should not happen"

    with pytest.raises(asyncio.TimeoutError):
        _run(heartbeat.guard(op(), idle_timeout=0.2))
    assert cancelled["hit"] is True


def test_guard_hard_cap_fires_even_while_beating():
    # A pathological op that beats forever must still be bounded by hard_cap.
    async def op():
        while True:
            await asyncio.sleep(0.02)
            heartbeat.beat()

    with pytest.raises(asyncio.TimeoutError):
        _run(heartbeat.guard(op(), idle_timeout=1.0, hard_cap=0.3))


def test_beat_outside_guard_is_noop():
    # Must not raise when there is no enclosing guard/watch.
    heartbeat.beat()


def test_concurrent_guards_have_independent_watches():
    # A busy (beating) op and a stalled op run concurrently: the stalled one
    # times out while the busy one completes — proving no cross-talk between
    # each guard's ambient watch under gather.
    async def busy():
        for _ in range(8):
            await asyncio.sleep(0.05)
            heartbeat.beat()
        return "busy-ok"

    async def stalled():
        await asyncio.sleep(10)

    async def scenario():
        results = await asyncio.gather(
            heartbeat.guard(busy(), idle_timeout=0.2),
            heartbeat.guard(stalled(), idle_timeout=0.2),
            return_exceptions=True,
        )
        return results

    busy_res, stalled_res = _run(scenario())
    assert busy_res == "busy-ok"
    assert isinstance(stalled_res, asyncio.TimeoutError)


def test_guard_propagates_op_exception():
    class Boom(Exception):
        pass

    async def op():
        raise Boom("kaboom")

    with pytest.raises(Boom):
        _run(heartbeat.guard(op(), idle_timeout=1.0))
