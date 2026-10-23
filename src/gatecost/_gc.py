"""Pause the cyclic garbage collector around allocation-heavy phases.

The IR, AST and cost tree are built from millions of small container objects; generational
collections triggered by those allocations cost 30-40 % of the run time and free nothing (the
data is live until the phase ends).  ``paused()`` disables collection for its duration and
restores the previous state, so nesting and library use are safe.
"""
from __future__ import annotations

import gc
from contextlib import contextmanager


@contextmanager
def paused():
    was = gc.isenabled()
    if was:
        gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


def nogc(fn):
    """Decorator: run ``fn`` with the cyclic GC paused."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        with paused():
            return fn(*a, **k)

    return wrapper
