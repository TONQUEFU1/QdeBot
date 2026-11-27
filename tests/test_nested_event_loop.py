"""
QDE V1.1 - tests/test_nested_event_loop.py

Regression tests for the CRITICAL "nested asyncio.run()" bug, found by
inspecting a real, user-generated AI Audit package where all 660 Top15
history rows had an empty current_price/pct_change.

Root cause: audit_engine.generate_package() and
learning_engine.analyze_and_optimize() are plain synchronous functions
that main.py calls directly (not awaited) from within main_async(),
which is itself already running under asyncio.run(). Their internal
price-fetch helpers used to call asyncio.run(pt.fetch_current_prices())
directly, which raises RuntimeError when a loop is already running in
the current thread - silently swallowed by a broad except clause,
producing an all-None price dict with no visible error.

Both modules now use a local _run_async_safely() helper that detects
this situation and runs the coroutine on a separate thread with its
own fresh event loop instead.
"""

from __future__ import annotations

import asyncio

import audit_engine
import learning_engine


async def _slow_coro():
    await asyncio.sleep(0.01)
    return {"ok": True}


def test_audit_engine_run_async_safely_works_standalone():
    """No parent event loop running (matches `python audit_engine.py`) -
    should behave exactly like a plain asyncio.run().
    """
    result = audit_engine._run_async_safely(_slow_coro())
    assert result == {"ok": True}


async def test_audit_engine_run_async_safely_works_nested():
    """A parent event loop IS already running (matches
    `python main.py --ai-audit`, where main.py calls
    audit_engine.generate_package() synchronously from inside
    main_async()) - this is the exact scenario that silently failed
    in the real production bug this test guards against.
    """
    result = audit_engine._run_async_safely(_slow_coro())
    assert result == {"ok": True}


async def test_audit_engine_run_async_safely_propagates_real_errors():
    """A genuine failure inside the coroutine must still surface as an
    exception, not be silently swallowed into a None/empty result.
    """
    async def failing_coro():
        raise ValueError("simulated real failure")

    try:
        audit_engine._run_async_safely(failing_coro())
        assert False, "expected ValueError to propagate"
    except ValueError as exc:
        assert "simulated real failure" in str(exc)


def test_learning_engine_run_async_safely_works_standalone():
    result = learning_engine._run_async_safely(_slow_coro())
    assert result == {"ok": True}


async def test_learning_engine_run_async_safely_works_nested():
    """Matches `python main.py --learn` - main.py calls
    learning_engine.analyze_and_optimize() synchronously from inside
    main_async(). Before this fix, there wasn't even a try/except
    around the old bare asyncio.run() call here, so this scenario
    would raise and abort the entire --learn run every time.
    """
    result = learning_engine._run_async_safely(_slow_coro())
    assert result == {"ok": True}
