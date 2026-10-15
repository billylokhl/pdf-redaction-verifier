"""Deterministic step counts for linearity tests (#94).

Wall-clock ratios flake on a loaded runner, and the budget's own work
counter cannot see work that is never charged -- the bug class the
linearity tests exist for (ADR 0010: "work counting alone cannot see
uncharged work"). Counting the lines Python executes inside the module
under test sees every loop, charged or not, and is the same on every
machine and under coverage."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import FrameType, ModuleType
from typing import Any, TypeVar

T = TypeVar("T")


def lines_executed(modules: tuple[ModuleType, ...], call: Callable[[], T]) -> tuple[T, int]:
    """*call()*'s result and the number of line events in *modules*' code
    while it ran (other code is not traced). The tracer in place before
    (coverage's, say) is restored afterwards."""
    files = {module.__file__ for module in modules}
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


def charged_steps(data: bytes, module: ModuleType, entry: str) -> tuple[int, int, Any]:
    """Build *data*'s inventory and return (the work charged inside
    build.<entry> -- one part of the inventory, e.g. "build_graph" --,
    the lines executed in *module* meanwhile, the inventory)."""
    import redaction_verifier.inventory.build as build_module
    from redaction_verifier.budget import Budget
    from redaction_verifier.inventory import build_inventory
    budget = Budget(file_size=len(data))
    charged: list[int] = []
    real = getattr(build_module, entry)

    def measured(*args: Any, **kwargs: Any) -> Any:
        before = budget.work
        result = real(*args, **kwargs)
        charged.append(budget.work - before)
        return result
    setattr(build_module, entry, measured)
    try:
        inv, lines = lines_executed((module,), lambda: build_inventory(data, budget=budget))
    finally:
        setattr(build_module, entry, real)
    return charged[0], lines, inv
