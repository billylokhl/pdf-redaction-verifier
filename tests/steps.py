"""Deterministic step counts for linearity tests (#94).

Wall-clock ratios flake on a loaded runner, and the budget's own work
counter cannot see work that is never charged -- the bug class the
linearity tests exist for (ADR 0010: "work counting alone cannot see
uncharged work"). Counting the lines Python executes in the inventory's
modules sees every Python loop, charged or not, and is the same on every
machine and under coverage. It cannot see work done inside C (a linear
`in` on a list, a copy, a sort) or Python outside those modules (a
dataclass's generated __eq__): the graph and page-walk tests keep a
wall-clock backstop for that, sized not to flake; the trees tests do not."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import FrameType
from typing import Any, TypeVar

from redaction_verifier.budget import Budget

T = TypeVar("T")


def _inventory_files() -> set[str]:
    """Every module of redaction_verifier.inventory, and the budget."""
    import redaction_verifier.budget
    import redaction_verifier.inventory
    files = {m.__file__ for name, m in list(sys.modules.items())
             if name.startswith("redaction_verifier.inventory") and m is not None}
    files.add(redaction_verifier.budget.__file__)
    files.add(redaction_verifier.inventory.__file__)
    return {f for f in files if f}


def lines_executed(files: set[str], call: Callable[[], T]) -> tuple[T, int]:
    """*call()*'s result and the number of line events in *files* while
    it ran (other code is not traced). The tracer in place before
    (coverage's, say) is restored afterwards."""
    count = 0

    def local(frame: FrameType, event: str, arg: Any) -> Any:
        nonlocal count
        if event == "line":
            count += 1
        return local

    def tracer(frame: FrameType, event: str, arg: Any) -> Any:
        return local if frame.f_code.co_filename in files else None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        result = call()
    finally:
        sys.settrace(previous)
    return result, count


def charged_steps(data: bytes, entry: str,
                  budget: Budget | None = None) -> tuple[int, int, Any]:
    """Build *data*'s inventory; return (the work charged inside
    build.<entry> -- one part of the inventory, e.g. "build_graph" --, the
    lines executed in every inventory module and the budget while that
    part ran -- the callbacks it makes into build.py included --, the
    inventory). *budget*, if given, is the one the whole build charges
    (its total work is the caller's to read)."""
    import redaction_verifier.inventory.build as build_module
    from redaction_verifier.inventory import build_inventory
    if budget is None:
        budget = Budget(file_size=len(data))
    files = _inventory_files()
    measured: list[tuple[int, int]] = []
    real = getattr(build_module, entry)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        before = budget.work
        result, lines = lines_executed(files, lambda: real(*args, **kwargs))
        measured.append((budget.work - before, lines))
        return result
    setattr(build_module, entry, wrapped)
    try:
        inv = build_inventory(data, budget=budget)
    finally:
        setattr(build_module, entry, real)
    work, lines = measured[0]
    return work, lines, inv


def step_cost(data: bytes, entry: str) -> tuple[float, int]:
    """(process time, work charged) of build.<entry> while *data*'s
    inventory is built, untraced: time per charged unit, which grows with
    the input only through work no charge pays for -- inside C, which no
    line count sees (#94)."""
    import time

    import redaction_verifier.inventory.build as build_module
    from redaction_verifier.budget import Budget
    from redaction_verifier.inventory import build_inventory
    budget = Budget(file_size=len(data))
    taken: list[tuple[float, int]] = []
    real = getattr(build_module, entry)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        before, started = budget.work, time.process_time()
        try:
            return real(*args, **kwargs)
        finally:
            taken.append((time.process_time() - started, budget.work - before))
    setattr(build_module, entry, wrapped)
    try:
        build_inventory(data, budget=budget)
    finally:
        setattr(build_module, entry, real)
    return taken[0]
