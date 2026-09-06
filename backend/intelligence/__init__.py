"""intelligence — one name for the layer that was already here.

This is a FAÇADE, not a move. The nine modules below stay where they are and
keep their own tests; this package re-exports them so new code (and a reader
arriving at the repository) can see that they are one layer rather than nine
files that happen to sit next to `crm.py`.

Moving them would touch roughly forty imports and every test that patches one,
for zero behavioural change — and a rename that large is indistinguishable
from a rewrite in a diff. Only `missions/` and `learning/` are born inside a
package, because they are new.

The layer, in the order a fact travels through it:

    interaction_logs        what a person did                (perception)
    belief_store            what is held to be true, and disputes
    intent_states           declared vs observed vs latent intent
    opportunity_engine      what is worth doing about it
    expected_value          what it is worth, with the interval
    command_center          what to show first
    agent_twin              how this agent decides
    autonomy                what may be done without asking
    decision_traces         what was decided, for later scoring
    outcome_memory          whether it worked
"""

from __future__ import annotations

import agent_twin
import autonomy
import belief_store
import command_center
import decision_traces
import expected_value
import intent_states
import opportunity_engine
import outcome_memory

__all__ = [
    "agent_twin",
    "autonomy",
    "belief_store",
    "command_center",
    "decision_traces",
    "expected_value",
    "intent_states",
    "opportunity_engine",
    "outcome_memory",
]
