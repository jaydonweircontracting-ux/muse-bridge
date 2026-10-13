"""Virtual clock for the relay simulation lab.

The production CompletionWatcher takes injectable sleep/now functions;
driving it with this clock makes 1000s of simulated streaming scenarios
run in milliseconds with full determinism.
"""
from __future__ import annotations


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0
        self._on_advance: list = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        for cb in self._on_advance:
            cb()

    def on_advance(self, cb) -> None:
        self._on_advance.append(cb)
