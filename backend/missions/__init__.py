"""missions — tell Neoh the outcome; Neoh figures out the work.

A mission states an objective, a deadline, a budget and the channels it may
use. The engine finds candidates, plans a sequence, simulates it, and — only
when it is live, has credentials, is inside budget and holds a consented grant
for that channel — executes it through the one send path that already exists.

Read `costs` and `simulator` before `executor`: what a mission would do has to
be legible before anything does it.
"""

from __future__ import annotations

__all__ = ["costs", "simulator"]
