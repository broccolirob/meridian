"""Reusable Mermaid styling primitives.

Ported from Trail of Bits' diagramming-code skill at
`~/.claude/plugins/cache/trailofbits/trailmark/0.8.1/skills/
diagramming-code/`. The bucket thresholds and color values match
the skill's defaults verbatim so meridian diagrams stay visually
consistent with ToB's existing tooling.
"""

# Cyclomatic-complexity bucket boundaries.
# CC <= LOW_MAX     → low
# CC <= MEDIUM_MAX  → medium
# CC >  MEDIUM_MAX  → high
COMPLEXITY_LOW_MAX = 4
COMPLEXITY_MEDIUM_MAX = 10

# Mermaid classDef strings. Drop these into a flowchart's body
# and tag nodes with `:::low`/`:::medium`/`:::high` to color
# them. Colors taken verbatim from ToB's diagramming-code skill.
CLASSDEF_LOW = (
    "classDef low "
    "fill:rgba(40,167,69,0.2),stroke:#28a745,color:#28a745"
)
CLASSDEF_MEDIUM = (
    "classDef medium "
    "fill:rgba(255,193,7,0.2),stroke:#e6a817,color:#e6a817"
)
CLASSDEF_HIGH = (
    "classDef high "
    "fill:rgba(220,53,69,0.2),stroke:#dc3545,color:#dc3545"
)
COMPLEXITY_CLASSDEFS: tuple[str, ...] = (
    CLASSDEF_LOW,
    CLASSDEF_MEDIUM,
    CLASSDEF_HIGH,
)

# Focus highlight for the call-graph root marker. Consumed by
# `render_call_graph` — the only renderer in the Phase 3 surface
# that highlights a "you are here" node. Lives in
# mermaid_styles.py (not inline in mermaid.py) so a future
# renderer can adopt the same visual cue by importing the
# constant rather than duplicating the classDef string.
FOCUS_CLASSDEF = "classDef focus stroke:#f66,stroke-width:3px"


def bucket_for_complexity(cc: int | None) -> str:
    """Return `low` / `medium` / `high` for a cyclomatic
    complexity value. `None` defaults to `low` (Trailmark uses
    None for methods where CC wasn't computed)."""
    if cc is None or cc <= COMPLEXITY_LOW_MAX:
        return "low"
    if cc <= COMPLEXITY_MEDIUM_MAX:
        return "medium"
    return "high"
