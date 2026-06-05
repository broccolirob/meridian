import json
import logging
import os
import pickle
import re
import threading
import time as _time
import weakref
from pathlib import Path
from typing import Annotated, Any

import yaml
from langchain_core.tools import InjectedToolArg
from trailmark.query.api import QueryEngine

from src.graph.persist import CACHE_ROOT, load_graph
from src.render.mermaid import (
    render_call_graph,
    render_inheritance,
    render_sequence,
)
from src.tools import (
    annotations_of,
    callees_of,
    get_node,
    list_nodes,
    nodes_with_annotation,
)

_log = logging.getLogger(__name__)

VAULT_SUBDIRS: tuple[str, ...] = (
    "_meta",
    "contracts",
    "interfaces",
    "libraries",
    "flows",
    "diagrams",
    "attack-surface",
    "risks",
    "diffs",
)

# Threshold for sweeping atomic-write tmp file orphans. 1
# hour is conservative: any in-flight write older than this
# is either a real orphan from a SIGKILL'd prior run or a
# legitimately stuck process the operator should investigate.
# Balances false-positive risk (sweeping a real in-flight
# write) against accumulation bounds.
_TMP_SWEEP_AGE_SECONDS = 3600.0


def _sweep_stale_tmp_files(vault: Path) -> int:
    """Remove `.<name>.tmp.<pid>.<tid>` files older than
    `_TMP_SWEEP_AGE_SECONDS` from each vault subdir.

    Atomic writes via tmp+rename leave behind tmp files when
    the process is killed between `write_text` and
    `os.replace`. Without a startup sweep, tmps would
    accumulate indefinitely over crash-rerun cycles.

    Returns the count of files removed. Errors during sweep
    (permission, race-with-concurrent-write) are silently
    swallowed — the sweep is best-effort hygiene, not a
    correctness primitive.
    """
    now = _time.time()
    removed = 0
    for subdir_name in VAULT_SUBDIRS:
        subdir = vault / subdir_name
        if not subdir.exists():
            continue
        try:
            entries = list(subdir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith("."):
                continue
            if ".tmp." not in entry.name:
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age > _TMP_SWEEP_AGE_SECONDS:
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def ensure_vault(vault_path: str | Path) -> Path:
    """Create the canonical washable vault skeleton at `vault_path`.
    Idempotent. Returns the resolved vault Path.

    Also sweeps stale atomic-write tmp files (older than 1
    hour) from every VAULT_SUBDIR — these orphans accumulate
    when a process is killed mid-write (between write_text
    and os.replace).
    """
    vault = Path(vault_path)
    vault.mkdir(parents=True, exist_ok=True)
    for sub in VAULT_SUBDIRS:
        (vault / sub).mkdir(exist_ok=True)
    # Best-effort sweep of atomic-write tmp orphans.
    _sweep_stale_tmp_files(vault)
    return vault


def write_obsidian_note(
    vault_path: str | Path,
    rel_path: str,
    frontmatter: dict[str, Any],
    body: str,
) -> Path:
    """Write an Obsidian note at `<vault>/<rel_path>`.

    Frontmatter is serialized as YAML between `---` markers. If
    `frontmatter` is empty, no marker block is emitted. The body is
    written verbatim. Parent directories under the vault are created
    on demand. Returns the absolute path written.

    Atomic-write semantics: the file is written to
    `.<name>.tmp.<pid>.<tid>` in the same directory, then
    `os.replace`d into the final path. Vault scanners
    (`write_root_moc`, `validate_vault.py`) and Obsidian's file
    watcher always observe EITHER the previous contents OR the
    complete new contents — never a partial file. Mirrors the
    pattern in `src/graph/persist.py::save_graph`.
    """
    vault = Path(vault_path)

    # Codex round-16 fix: reject any `..` traversal segment
    # under POSIX or Windows separators BEFORE the resolve-
    # based containment check.
    #
    # Why the resolve check alone is insufficient: a
    # `rel_path="contracts/../risks/pwn.md"` resolves to
    # `vault/risks/pwn.md`, which IS inside the vault, so
    # `relative_to(vault)` succeeds. The route was meant to
    # be `vault/contracts/<bare>.md` — the traversal jumped
    # the kind-routed folder and gave the caller an in-vault
    # arbitrary-write primitive (it could clobber a curated
    # risk note, a flow note, or a MOC). The repro: an
    # LLM-forged `node["name"]="../risks/node-pwn"` flows
    # through `_disambiguated_path` → rel_path
    # `contracts/../risks/node-pwn.md`. The refetch defense
    # in `render_and_write_node_note` / `render_and_write_flow_note`
    # is the primary fix; this is the catch-all so any future
    # caller that bypasses refetch still can't traverse.
    #
    # Split on BOTH POSIX `/` and Windows `\` because Python
    # on macOS/Linux normally normalizes `\` as a literal
    # character, but Obsidian vaults can live on Windows
    # filesystems via SMB/mount, and `Path.parts` does NOT
    # split on `\` on POSIX. So we check both explicitly.
    rel_parts = rel_path.replace("\\", "/").split("/")
    if ".." in rel_parts:
        raise ValueError(
            f"rel_path contains traversal segment: {rel_path!r}"
        )
    # Absolute paths also break the vault-rooted contract.
    if rel_path.startswith(("/", "\\")) or (
        len(rel_path) >= 2 and rel_path[1] == ":"
    ):
        raise ValueError(
            f"rel_path must be relative to vault: {rel_path!r}"
        )

    target = vault / rel_path
    try:
        target.resolve().relative_to(vault.resolve())
    except ValueError as e:
        raise ValueError(
            f"rel_path escapes vault: {rel_path!r}"
        ) from e
    target.parent.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    if frontmatter:
        yaml_block = yaml.safe_dump(
            frontmatter, sort_keys=False, default_flow_style=False
        )
        parts.append("---\n")
        parts.append(yaml_block)
        parts.append("---\n\n")
    parts.append(body)
    if not body.endswith("\n"):
        parts.append("\n")
    content = "".join(parts)

    # PID + thread ID in the tmp name so concurrent ThreadPool
    # workers in the same process can't clobber each other's tmp.
    # (save_graph uses PID only because _ANNOTATE_LOCK serializes
    # its writes; write_obsidian_note has no such lock.)
    tmp_path = target.parent / (
        f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    except Exception:
        # Intentionally broad: the catch exists to clean up
        # the tmp file on ANY failure path, then re-raises.
        # Coding bugs (TypeError, AttributeError) still surface
        # to the caller because of the `raise` below — the
        # broad catch is a cleanup hook, not a swallower.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return target


KIND_TO_FOLDER: dict[str, str] = {
    "contract": "contracts",
    "library": "libraries",
    "interface": "interfaces",
    "trait": "interfaces",
    "class": "contracts",
    "struct": "contracts",
    "enum": "contracts",
    "namespace": "contracts",
    "function": "contracts",
    "module": "_meta",
}


# Per-engine collision map cache. Keyed by engine instance via
# WeakKeyDictionary — when load_graph's lru_cache evicts the
# engine and it's GC'd, the entry auto-removes. No manual
# invalidation needed.
#
# Module-level rather than attached as `engine._washable_collision_map`
# because instance attributes survive pickle round-trips —
# save_graph pickles the engine AS-IS, and the next load_graph
# would reconstitute an engine carrying a STALE map. Today's
# annotate/clear_annotations don't add/remove nodes so the
# pickled map would still be valid, but Phase 4's augment_sarif
# WILL add finding nodes via save_graph. Storing the map outside
# the engine guarantees pickle stays clean and the post-save
# load gets a fresh build with the new nodes included.
_COLLISION_MAPS: weakref.WeakKeyDictionary[
    QueryEngine, dict[tuple[str, str], set[str]]
] = weakref.WeakKeyDictionary()


def _build_collision_map(
    engine: QueryEngine,
) -> dict[tuple[str, str], set[str]]:
    """Map (folder, bare_name) → set of node IDs routing there.

    Computed once per engine instance and cached in module-level
    `_COLLISION_MAPS` by `_disambiguated_path`. Nodes whose kind
    has no `KIND_TO_FOLDER` entry (e.g. `method`, documented
    inside its parent's note) are skipped — they don't get their
    own files and can't collide on filename.
    """
    data = json.loads(engine.to_json())
    collision_map: dict[tuple[str, str], set[str]] = {}
    for nid, n in data["nodes"].items():
        folder = KIND_TO_FOLDER.get(n["kind"])
        if folder is None:
            continue
        collision_map.setdefault(
            (folder, n["name"]), set()
        ).add(nid)
    return collision_map


def _disambiguated_path(
    node: dict[str, Any],
    graph_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Vault-relative path stem (no `.md`) for a node note.

    Returns `<folder>/<bare>` when the bare name is unique among
    nodes routing to the same folder, or
    `<folder>/<module>.<bare>` when a collision exists. Per-folder
    (not per-kind) because `KIND_TO_FOLDER` maps multiple kinds to
    the same folder (contract/struct/enum → contracts).

    Used by both `render_and_write_node_note` (the file's path)
    and `resolve_wikilink` (the link target) so they stay in
    sync — wikilinks always point at whatever filename the writer
    picks.

    Performance: caches the collision map in
    `_COLLISION_MAPS[engine]` (module-level WeakKeyDictionary).
    Built once per engine instance, reused for every subsequent
    resolve on the same engine. When `save_graph` evicts the
    engine via `cache_clear()` and GC frees it, the WeakKeyDict
    entry auto-removes; the next `load_graph` returns a fresh
    pickled engine that builds a fresh map. No state survives
    the save/load round-trip — safe for any future mutator that
    adds or removes nodes.
    """
    kind = node["kind"]
    folder = KIND_TO_FOLDER.get(kind, "contracts")
    bare = node["name"]

    engine = load_graph(graph_id, cache_root=cache_root)
    collision_map = _COLLISION_MAPS.get(engine)
    if collision_map is None:
        collision_map = _build_collision_map(engine)
        _COLLISION_MAPS[engine] = collision_map

    others = collision_map.get((folder, bare), set()) - {node["id"]}
    if not others:
        return f"{folder}/{bare}"
    module = node["id"].rsplit(":", 1)[0]
    return f"{folder}/{module}.{bare}"


def resolve_wikilink(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Return an Obsidian wikilink string for `node_id`.

    Top-level kinds (contract, library, interface, etc.) point at
    their own note: ``[[contracts/Pair|Pair]]``.

    Methods point at their parent's note with a qualified display
    label: ``[[contracts/Pair|Pair.swap]]``.

    When two nodes route to the same folder with the same bare
    name, the link target gets qualified with the module prefix
    (``[[contracts/contracts.A.Vault|Vault]]``). The display
    label stays bare.

    Raises `KeyError` if `node_id` (or, for methods, its parent) is
    not in the cached graph.
    """
    node = get_node(graph_id, node_id, cache_root=cache_root)
    kind = node["kind"]
    name = node["name"]

    if kind == "method":
        parent_id = node_id.rsplit(".", 1)[0]
        parent = get_node(graph_id, parent_id, cache_root=cache_root)
        method_name = name.rsplit(".", 1)[-1]
        parent_path = _disambiguated_path(
            parent, graph_id, cache_root=cache_root
        )
        return f"[[{parent_path}|{parent['name']}.{method_name}]]"

    path = _disambiguated_path(node, graph_id, cache_root=cache_root)
    return f"[[{path}|{name}]]"


_VISIBILITY_ORDER: tuple[str, ...] = ("external", "public", "internal", "private")


def _bare_name_from_wikilink(wikilink: str) -> str:
    """Extract the display label from `[[path|display]]` (or the path
    portion if no `|` is present)."""
    inner = wikilink.strip("[]")
    if "|" in inner:
        return inner.split("|", 1)[1]
    return inner.rsplit("/", 1)[-1]


def _render_overview(body: str) -> str:
    """Render the Overview section.

    `body` is LLM-authored prose from attacker-influenced
    Solidity context. Apply `_defang_block_text` here so
    every caller (node notes, flow notes, risk notes) gets
    the same markdown-injection defense without remembering
    to pre-defang. Block-aware: preserves paragraph
    structure (3-5 sentence overviews stay readable) but
    neutralizes line-start headings/lists/blockquotes/
    reference defs AND inline HTML/wikilinks/URI schemes/
    code fences (Codex round-13 fix)."""
    if body.strip():
        safe = _defang_block_text(body)
        return f"## Overview\n\n{safe.rstrip()}\n"
    return "## Overview\n\n_Overview not yet written._\n"


def _render_link_list(items: list[str], empty_msg: str) -> str:
    if not items:
        return f"{empty_msg}\n"
    return "".join(
        f"- {_defang_link_list_item(item)}\n" for item in items
    )


def _render_graph_context(graph_ctx: dict[str, Any]) -> str:
    inheritance_diagram = graph_ctx.get("inheritance_mermaid", "")
    call_graph_diagram = graph_ctx.get("call_graph_mermaid", "")
    inherits = graph_ctx.get("inherits") or []
    implements = graph_ctx.get("implements") or []
    uses = graph_ctx.get("uses") or []
    callers = graph_ctx.get("callers") or []
    callees = graph_ctx.get("callees") or []

    parts = ["## Graph context\n"]
    # Visual structure first (auditors scan diagrams before lists).
    if inheritance_diagram:
        parts.append(
            f"\n### Inheritance diagram\n\n{inheritance_diagram}"
        )
    if call_graph_diagram:
        parts.append(f"\n### Call graph\n\n{call_graph_diagram}")
    parts.append(
        "\n### Inheritance\n\n"
        f"{_render_link_list(inherits, '_No inheritance edges._')}\n"
        "### Implements\n\n"
        f"{_render_link_list(implements, '_Implements nothing._')}\n"
        "### Uses\n\n"
        f"{_render_link_list(uses, '_No library uses._')}\n"
        "### Callers\n\n"
        f"{_render_link_list(callers, '_No callers in this graph._')}\n"
        "### Callees\n\n"
        f"{_render_link_list(callees, '_No callees in this graph._')}"
    )
    return "".join(parts)


def _render_state(node: dict[str, Any]) -> str:
    loc = node["location"]
    return (
        "## State\n\n"
        "_Trailmark does not extract state variables yet — read the "
        f"source at `{loc['file_path']}` lines "
        f"`{loc['start_line']}`-`{loc['end_line']}`._\n"
    )


def _render_functions(graph_ctx: dict[str, Any]) -> str:
    functions = graph_ctx.get("functions") or []
    if not functions:
        body = "_No functions._\n"
        return "## Functions\n\n" + body

    by_visibility: dict[str, list[dict[str, Any]]] = {
        v: [] for v in _VISIBILITY_ORDER
    }
    for fn in functions:
        vis = fn.get("visibility") or "internal"
        by_visibility.setdefault(vis, []).append(fn)

    out = ["## Functions\n"]
    for vis in _VISIBILITY_ORDER:
        out.append(f"\n### {vis.capitalize()}\n\n")
        bucket = by_visibility.get(vis, [])
        if not bucket:
            out.append("_None._\n")
            continue
        for fn in bucket:
            # LLM-authored fields (wikilink/signature/docstring)
            # are defanged at the renderer boundary — same
            # markdown-injection defenses applied to overview /
            # observations / annotations (round-13 fix) extend
            # here. `name` fallback is Trailmark-derived
            # (Solidity identifier), still flattened defensively
            # for one-line bullet shape.
            raw_link = fn.get("wikilink") or fn.get("name", "?")
            wikilink = _defang_link_list_item(raw_link)
            signature = fn.get("signature", "")
            cc = fn.get("cyclomatic_complexity")
            callers_n = fn.get("callers_count", 0)
            callees_n = fn.get("callees_count", 0)
            doc = fn.get("docstring")
            sig_part = (
                f" `{_defang_inline_code_text(signature)}`"
                if signature else ""
            )
            cc_part = f" — complexity {cc}" if cc is not None else ""
            out.append(
                f"- {wikilink}{sig_part}{cc_part} "
                f"(callers: {callers_n}, callees: {callees_n})\n"
            )
            if doc:
                # First-line italic prose — flatten so multi-
                # line docstrings can't escape the italic span
                # and inject a heading/list on a fresh line.
                first_line = _flatten_to_one_line(
                    doc.strip().splitlines()[0]
                )
                out.append(f"  - _{first_line}_\n")
    return "".join(out)


def _render_events_etc(node: dict[str, Any]) -> str:
    loc = node["location"]
    return (
        "## Events / Errors / Modifiers\n\n"
        "_Trailmark does not extract events, errors, or modifiers yet "
        f"— read the source at `{loc['file_path']}` lines "
        f"`{loc['start_line']}`-`{loc['end_line']}`._\n"
    )


def _render_annotations(graph_ctx: dict[str, Any]) -> str:
    annotations = graph_ctx.get("annotations") or []
    # graph_ctx is LLM-supplied — gpt-5-mini has been observed
    # passing bare strings instead of `{kind, description, source}`
    # dicts. Drop non-dict entries rather than crashing; the
    # renderer's contract is "never raise on adversarial input"
    # (same posture as the link-list defang in rounds 14-17).
    annotations = [a for a in annotations if isinstance(a, dict)]
    # Findings (kind="finding") are owned by the Risks section
    # — `_render_risks` pulls them from the graph via
    # `annotations_of(kind="finding")`. Filter them out here
    # so an LLM that accidentally also includes finding-kind
    # entries in graph_ctx["annotations"] doesn't produce
    # double bullets across the two sections.
    annotations = [
        a for a in annotations if a.get("kind") != "finding"
    ]
    if not annotations:
        return "## Annotations\n\n_No annotations yet._\n"
    out = ["## Annotations\n\n"]
    for a in annotations:
        # All three fields are LLM-authored — defang inline
        # (single-line bullet body) to neutralize embedded
        # newlines, headings, wikilinks, raw HTML, and URI
        # schemes (Codex round-13 fix).
        kind = _flatten_to_one_line(a.get("kind", "note"))
        source = _flatten_to_one_line(a.get("source", ""))
        desc = _flatten_to_one_line(a.get("description", ""))
        src_part = f" _(via {source})_" if source else ""
        out.append(f"- **{kind}**: {desc}{src_part}\n")
    return "".join(out)


# Source field identifying findings synthesized by
# RiskSynthesizer (chunk 4.5). The subagent prompt
# (`src/subagents.py:_RISK_SYNTHESIZER_PROMPT`) requires this
# value when calling `annotate()`. Used by `_render_risks` to
# route curated findings (linked wikilinks to the risk note)
# vs raw SARIF findings (plain bullets) — switching from the
# old description-prefix regex to a source check eliminates
# a near-miss collision with Trailmark's SARIF description
# format `[WARNING] rule-id: msg (Tool)` which is one
# `.upper()` call away from looking like a curated prefix.
_RISK_SYNTHESIZER_SOURCE = "risk-synthesizer"

# Mirrors the kebab-case allowlist in `_RISK_NAME_RE` (the
# pattern `render_and_write_risk_note` enforces on risk_name).
# Anchored to fail closed: malformed brackets fall through to
# plain-text rendering rather than producing broken wikilinks
# from LLM-hallucinated prefixes like `[Hotspots]`,
# `[has space]`, or `[trailing-]`. Applied ONLY to
# risk-synthesizer-sourced descriptions, not all
# `kind="finding"` descriptions.
#
# Intentionally NOT re.DOTALL: RiskSynthesizer's prompt
# contracts a one-line reason. DOTALL would let `(.+)`
# swallow newlines from a malicious description like
# `"[hotspots] benign\n- [[../../evil|x]] — injected"`,
# turning one annotation into two rendered bullets — a
# prompt-injection vector since descriptions ultimately come
# from LLM output over attacker-controlled Solidity comments
# and slither projections.
_RISK_PREFIX_RE = re.compile(r"^\[([a-z0-9]+(?:-[a-z0-9]+)*)\]\s+(.+)$")


# Schemes that don't use `://` and need explicit defang
# (URI auto-linking + click-handling in Obsidian/browsers).
# `javascript:`, `data:`, `file:`, `obsidian:` are the
# obvious dangerous ones; `vbscript:`/`about:`/`chrome:`
# round out the historical attack surface.
_DANGEROUS_BARE_SCHEMES_RE = re.compile(
    r"(?i)\b(javascript|data|file|obsidian|vbscript|about|chrome)\s*:"
)

# Strict shape produced by `resolve_wikilink`:
#   [[<folder>/<name-or-module.name>|<display>]]
# Folder names + node names + the display label are derived
# from Trailmark-parsed identifiers (Solidity names) +
# constants from `KIND_TO_FOLDER`. The path component can
# contain `/` (folder separator) and `.` (module qualifier
# for collision-disambiguated paths). The display label can
# contain `.` (method qualifier, e.g. `Pair.swap`).
#
# Used by `_defang_link_list_item` + `_defang_function_wikilink`
# to whitelist legitimate wikilinks while defanging anything
# LLM-authored that doesn't match — e.g. `[[../../etc/passwd]]`,
# `[[name]] ## INJECTED`, or `[[a|b]]<iframe>x</iframe>`. The
# anchors `^`/`$` are critical: a string like `[[ok|ok]] junk`
# must NOT match.
_SAFE_WIKILINK_RE = re.compile(
    r"^\[\[[A-Za-z0-9_./\-]{1,200}"
    r"(?:\|[A-Za-z0-9_.\-]{1,200})?\]\]$"
)


def _defang_text(s: str) -> str:
    """Defang attacker-controlled finding/risk-note text
    against markdown + HTML + Obsidian-specific injection.

    Defenses applied (order matters: HTML-escape FIRST so
    later substitutions don't get re-escaped):

    1. HTML escape `&`, `<`, `>`. Obsidian renders inline
       HTML by default — raw `<iframe src="https://evil">`
       or `<a href="file:///etc/passwd">` would be a
       clickable/embeddable threat. Escaping makes the tags
       render as literal text.

    2. Defang Obsidian transclusion `![[..]]` (which would
       embed the linked file inline — useful for an attacker
       referencing `![[../../secrets]]`).

    3. Defang Obsidian wikilink `[[..]]` (vault-traversal,
       e.g. `[[../../etc/passwd]]`).

    4. Defang markdown link `](` (breaks `[trusted text]
       (file://evil)` shape).

    5. Defang the `://` URL-scheme marker so bare URLs don't
       auto-link to attacker-chosen targets.

    6. Defang dangerous schemes that don't use `://`
       (`javascript:`, `data:`, `file:`, `obsidian:` etc).

    7. Defang ALL backticks (single + fenced) to HTML-entity
       backticks. Beyond the 3+ fenced-code attack, single
       backticks invoke Obsidian Dataview inline queries if
       that plugin is enabled (Codex round-19 finding):
         `= some_dql_expression`
         `$= some_dataviewjs_expression`
       Dataview EXECUTES the contents. An attacker-controlled
       risk overview / observation / finding description with
       a single-backtick payload would run arbitrary Dataview
       JavaScript in the auditor's vault. Escaping every
       backtick neutralizes both fenced code AND inline
       queries. Trade-off: legitimate LLM prose that includes
       ``inline code`` (e.g., function names in backticks)
       renders as literal backtick characters instead of
       styled inline code. The trade is worth it — auditor
       prose readability is preserved enough, and the
       executable-Dataview surface is non-negotiable.

    Preserves newlines and other formatting — for inline use
    (bullet bodies), compose with `_flatten_to_one_line`."""
    # 1. HTML entities. Must precede every other substitution
    # because we introduce `&` characters below (e.g.,
    # `&#x60;` for backtick defang).
    s = s.replace("&", "&amp;")
    s = s.replace("<", "&lt;")
    s = s.replace(">", "&gt;")
    # 2-4. Obsidian/markdown link syntax. Order: transclusion
    # before wikilink (else `![` then `[[` would partially
    # match `![[`).
    s = s.replace("![", "! [")
    s = s.replace("[[", "[ [")
    s = s.replace("](", "] (")
    # 5. Bare URLs. `://` defang breaks http(s), ftp, etc.
    s = s.replace("://", ":[//]")
    # 6. Dangerous bare-colon schemes.
    s = _DANGEROUS_BARE_SCHEMES_RE.sub(r"\1[:]", s)
    # 7. ALL backticks (single + fenced) → HTML-entity
    # backticks. Covers Obsidian Dataview inline queries
    # (`= ...`, `$= ...`) as well as 3+ code fences. The
    # plain `.replace` is correct — entity replacement
    # preserves visible length, no overlap concerns.
    s = s.replace("`", "&#x60;")
    return s


def _flatten_to_one_line(s: str) -> str:
    """Defang attacker-controlled text AND collapse to a
    single line. For inline use (bullet bodies). Calls
    `_defang_text` after whitespace flatten. Newlines in the
    input become single spaces — preserves the no-extra-
    bullets / no-header-injection contract from chunk 4.7."""
    return _defang_text(" ".join(s.split()))


def _defang_inline_code_text(s: str) -> str:
    """Defang text destined for an INLINE CODE SPAN
    (between backticks like `` `text` ``). Extends
    `_flatten_to_one_line` by also escaping single
    backticks so embedded backticks in the input don't
    close the wrapping span early.

    Without this, an LLM-controlled label like
    `foo` `` ` `` `## Injected\\n[[evil]]` rendered as
    `` ` ``foo`` ` `` (closed code span) + `## Injected`
    (mid-bullet text, possibly heading at line start) +
    `[[evil]]` (active wikilink). The escape `&#x60;`
    renders as a visible backtick character but is NOT
    interpreted by the parser as a code-span delimiter.

    Used by `render_and_write_flow_note` (hop fallback)
    and `render_and_write_risk_note` (involved-node
    fallback)."""
    return _flatten_to_one_line(s).replace("`", "&#x60;")


def _defang_link_list_item(item: str) -> str:
    """Whitelist legitimate `resolve_wikilink`-shape strings;
    defang everything else.

    `graph_ctx["inherits" | "implements" | "uses" | "callers"
    | "callees"]` is LLM-authored — NodeDocumenter calls
    `resolve_wikilink` to produce these strings, but a
    prompt-injected NodeDocumenter could emit raw markdown
    (`## INJECTED`), vault-escape wikilinks
    (`[[../../etc/passwd]]`), raw HTML (`<iframe>`), or
    trailing garbage after a legitimate-looking prefix
    (`[[ok|ok]]\\n## Pwned`).

    Strategy: match the exact shape `_SAFE_WIKILINK_RE`
    produces AND reject any `..` sequence (vault-traversal —
    `.` and `/` are in the char class for legitimate dotted
    module paths like `contracts.A.Vault`, but legitimate
    paths never contain consecutive dots). Pass through
    unchanged on match, otherwise `_flatten_to_one_line`
    (HTML escape + scheme/wikilink/code-fence defang +
    collapse to one line so multi-line injection can't
    reach the next bullet)."""
    if _SAFE_WIKILINK_RE.match(item) and ".." not in item:
        return item
    return _flatten_to_one_line(item)


# Zero-width space — invisible to the auditor reading the
# rendered note, but breaks markdown block-level parsing
# when prepended to a line. Used by `_defang_block_text` to
# neutralize line-start injection (headings, lists,
# blockquotes, reference definitions, setext underlines, HRs)
# without disrupting the visible paragraph structure.
_ZWSP = "​"

# Line-start markdown block constructs. Anchored at line
# start with `re.MULTILINE` so each line is checked
# independently. Matches the leading whitespace separately
# to preserve indent on re-insertion.
_BLOCK_START_RE = re.compile(
    r"(?m)^(\s*)("
    r"#{1,6}\s"                # ATX headings (`# `..`###### `)
    r"|[-*+]\s"                # unordered list (`- `, `* `, `+ `)
    r"|\d+\.\s"                # ordered list (`1. `)
    r"|>\s?"                   # blockquote (`> `; space optional)
    r"|---+\s*$"               # HR / setext H2 underline
    r"|\*\*\*+\s*$"            # HR (`***`)
    r"|___+\s*$"               # HR (`___`)
    r"|===+\s*$"               # setext H1 underline
    r"|\[[^\]\n]+\]:\s"        # link reference definition
    r"|\[\^[^\]\n]+\]:\s"      # footnote definition
    r")",
)


def _defang_block_text(s: str) -> str:
    """Block-aware defang for risk-note overview /
    observations prose. Composes `_defang_text` (inline
    defenses: HTML, wikilinks, URL schemes, code fences)
    with line-start markdown defang.

    Why this exists beyond `_defang_text`: the risk-note
    body PRESERVES newlines so a 3-5 sentence overview can
    render with paragraph structure. But preserving newlines
    lets attacker-controlled prose inject:
      - `\\n## Fake Finding Accepted` (false heading)
      - `\\n- Action item: ...` (false list bullet)
      - `\\n> Quote from auditor` (false blockquote)
      - `\\n[ref]: file:///etc/passwd` (link reference def)
      - `\\nText\\n=====` (setext H1 underline turns text into heading)

    Fix: prepend a zero-width space to any line whose
    start would parse as a markdown block construct. ZWSP
    is invisible to the auditor reading the rendered note
    (browsers/Obsidian don't render it), but breaks the
    parser's "first character of line" recognition for
    block-level constructs. Inline markdown elsewhere on
    the line still works.
    """
    s = _defang_text(s)
    return _BLOCK_START_RE.sub(
        lambda m: f"{m.group(1)}{_ZWSP}{m.group(2)}", s,
    )


def _render_risks(graph_ctx: dict[str, Any]) -> str:
    """Render the Risks section from finding annotations.

    `graph_ctx["finding_annotations"]` is the list returned by
    `annotations_of(graph_id, node_id, kind="finding")`,
    populated by `render_and_write_node_note`. Each entry is a
    dict with `kind`, `description`, `source`.

    Two paths, distinguished by `source`:
    - `source="risk-synthesizer"` → curated. Parse the
      `[<risk_name>] <reason>` prefix and render as a
      wikilink to `vault/risks/<risk_name>.md`. Fall back to
      a plain bullet if the prefix is malformed.
    - Any other source (typically `"sarif:Slither"`,
      `"sarif:semgrep"`) → raw. Render as a plain bullet.

    Filtering on `source` instead of the description shape
    avoids a near-miss collision with Trailmark's SARIF
    description format `[WARNING] rule-id: msg (Tool)`, which
    is structurally one `.upper()` call away from looking
    like a curated `[hotspots] ...` prefix.

    RiskSynthesizer items render first so the auditor reads
    the LLM's curated prioritization before raw analyzer
    output.

    Two safety properties this function enforces (not the
    caller's job):

    1. Markdown-injection defense: every rendered description
       is flattened to a single line + link-syntax defanged
       via `_flatten_to_one_line` before interpolation.
       Auditor-supplied Solidity comments can poison LLM
       finding descriptions; a literal `\\n` or embedded
       `[[../evil]]` would otherwise inject extra bullets,
       fake headings, or vault-traversal wikilinks.

    2. Idempotency: duplicate annotations (same flattened
       description) collapse to one bullet via
       `dict.fromkeys` ordered dedup. Re-running
       RiskSynthesizer attaches duplicate annotations to the
       graph (Trailmark's annotate is append-only) — without
       dedup, a re-render doubles every bullet. The dedup
       preserves first-seen order so the curated/raw split
       stays stable.
    """
    findings = graph_ctx.get("finding_annotations") or []
    curated: list[str] = []
    raw: list[str] = []
    for f in findings:
        desc = _flatten_to_one_line(f.get("description") or "")
        if not desc:
            continue
        if f.get("source") == _RISK_SYNTHESIZER_SOURCE:
            # Curated path. Parse the bracketed kebab-case
            # prefix; if the LLM produced a malformed prefix
            # (hallucinated despite the prompt allowlist),
            # render as a plain bullet rather than building
            # a broken wikilink. `desc` was already defanged
            # by `_flatten_to_one_line` above; just trim
            # surrounding whitespace from the captured reason
            # — re-running `_flatten_to_one_line` would
            # double-escape HTML entities (`&amp;` →
            # `&amp;amp;`).
            m = _RISK_PREFIX_RE.match(desc)
            if m:
                risk_name = m.group(1)
                reason = m.group(2).strip()
                curated.append(
                    f"- [[risks/{risk_name}|{risk_name}]] — {reason}"
                )
            else:
                raw.append(f"- {desc}")
        else:
            # Raw path: SARIF findings (source="sarif:Slither"
            # etc.) and any other non-curated finding.
            raw.append(f"- {desc}")

    # Order-preserving dedup. dict.fromkeys keeps the
    # first-occurrence order across Python 3.7+, so a
    # repeated annotation collapses to its first bullet.
    curated = list(dict.fromkeys(curated))
    raw = list(dict.fromkeys(raw))

    if not curated and not raw:
        return "## Risks\n\n_No risks recorded._\n"

    return "## Risks\n\n" + "\n".join(curated + raw) + "\n"


def _build_frontmatter(
    node: dict[str, Any], graph_ctx: dict[str, Any]
) -> dict[str, Any]:
    loc = node["location"]
    start = loc["start_line"]
    end = loc["end_line"]
    fm: dict[str, Any] = {
        "name": node["name"],
        "kind": node["kind"],
        "node_id": node["id"],
        "file": loc["file_path"],
        "lines": f"{start}-{end}",
        # Trailmark ranges are inclusive on both ends. Lines 10-183 is
        # 174 lines (183 - 10 + 1), not 173.
        "loc": end - start + 1,
        "cyclomatic_complexity": node.get("cyclomatic_complexity"),
        "callers_count": len(graph_ctx.get("callers") or []),
        "callees_count": len(graph_ctx.get("callees") or []),
    }
    for key in ("inherits", "implements", "uses"):
        wikilinks = graph_ctx.get(key) or []
        # graph_ctx is LLM-supplied — gpt-5-mini has been observed
        # nesting lists (`[["[[a]]", "[[b]]"]]`) instead of a flat
        # `list[str]`. Drop non-string entries rather than crashing;
        # same posture as `_render_annotations` and the link-list
        # defang.
        wikilinks = [w for w in wikilinks if isinstance(w, str)]
        if wikilinks:
            fm[key] = [_bare_name_from_wikilink(w) for w in wikilinks]
    return fm


def render_node_note(
    node: dict[str, Any],
    graph_ctx: dict[str, Any] | None = None,
    body: str = "",
) -> tuple[dict[str, Any], str]:
    """Render a complete Obsidian note for one graph node.

    Returns `(frontmatter, body)` ready to hand to
    `write_obsidian_note(vault, rel_path, frontmatter, body)`.

    `node` is a Trailmark node dict (from `get_node`).
    `graph_ctx` is structured graph data the caller pre-computed;
    missing keys produce placeholder sections.
    `body` is the LLM-written Overview narrative; empty produces a
    placeholder.
    """
    ctx = graph_ctx or {}
    frontmatter = _build_frontmatter(node, ctx)
    sections = [
        _render_overview(body),
        _render_graph_context(ctx),
        _render_state(node),
        _render_functions(ctx),
        _render_events_etc(node),
        _render_annotations(ctx),
        _render_risks(ctx),
    ]
    return frontmatter, "\n".join(sections)


def _pick_primary_method(
    graph_id: str,
    container_node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str | None:
    """Highest-CC method belonging to `container_node_id`, tiebreak
    by name ascending. Returns None if the container has no methods.

    "Belonging to" = method ID starts with `container.` (Trailmark's
    `module:Contract.method` format). Could be re-implemented via
    `contains` edges if the ID format ever changes — for now
    ID-prefix is the simplest version-tolerant approach.
    """
    methods = [
        n
        for n in list_nodes(graph_id, kind="method", cache_root=cache_root)
        if n["id"].startswith(container_node_id + ".")
    ]
    if not methods:
        return None
    methods.sort(
        key=lambda m: (-(m.get("cyclomatic_complexity") or 0), m["name"])
    )
    return methods[0]["id"]


def render_and_write_node_note(
    vault_path: str | Path,
    graph_id: str,
    node: dict[str, Any],
    graph_ctx: dict[str, Any] | None = None,
    body: str = "",
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Render canonical note + write to vault in one atomic call.

    Picks rel_path automatically from `node["kind"]` via
    `KIND_TO_FOLDER`. Computes Mermaid diagrams deterministically
    (inheritance always; call graph if a primary method exists)
    and embeds them in the "Graph context" section. Returns the
    absolute file path as a string (LLM-friendly — tool returns are
    easier to handle when they're primitives, not Path objects).

    Use this from agents instead of calling `render_node_note` +
    `write_obsidian_note` separately. It guarantees every note uses
    the canonical 7-section template AND ships with diagrams.

    `graph_id` is the 12-hex repo identifier (the same one the
    agent already receives in its task message). Diagrams compute
    from it without agent involvement — the LLM never sees or
    forwards them.

    Diagram computation failures are logged and skipped rather than
    raised: the note is the primary artifact, diagrams are
    enrichment. A note still ships if (e.g.) the graph cache is
    missing or the node was constructed for a routing test.

    Raises `ValueError` if `node["kind"] == "method"` — methods are
    documented inside their parent's note, not as standalone files.
    """
    # Codex round-16 fix: treat the LLM-supplied `node` as
    # an ID carrier only. Refetch the canonical dict from
    # the graph using ONLY `node["id"]`, then use the
    # refetched node for every downstream decision (kind,
    # name, location, frontmatter, rel_path, annotations).
    #
    # Why: a prompt-injected NodeDocumenter could forge
    # arbitrary fields. The reproduced exploit set
    # `node["name"]="../risks/node-pwn"`, which
    # `_disambiguated_path` interpolated into
    # `f"{folder}/{bare}"` producing
    # `contracts/../risks/node-pwn.md`. `write_obsidian_note`'s
    # `resolve().relative_to(vault)` check passes (the
    # resolved path is still inside the vault) — so the
    # attacker got an in-vault arbitrary-write primitive
    # that could clobber a curated risk note.
    #
    # The refetch contract: callers MUST pass a node_id that
    # resolves in the graph. Any failure (graph not loaded,
    # node not in graph, cache I/O error) raises here rather
    # than degrading to "use LLM-supplied dict" — that
    # degradation IS the bypass an attacker exploits by
    # forging a graph_id pointing at a non-existent cache.
    node_id = node["id"]
    node = get_node(graph_id, node_id, cache_root=cache_root)

    kind = node["kind"]
    if kind == "method":
        raise ValueError(
            f"method nodes are documented inside their parent's "
            f"note, not as standalone files "
            f"(got node_id={node['id']!r})"
        )

    ctx = dict(graph_ctx) if graph_ctx else {}
    # Codex round-15 fix: drop LLM-supplied diagram fields
    # BEFORE trusted regen. Only the renderer-produced
    # `render_inheritance` / `render_call_graph` strings may
    # populate these keys. If diagram regen fails inside the
    # try-block below, the keys stay absent and
    # `_render_graph_context` emits no diagram section —
    # raw LLM Mermaid (`## INJECTED HEADING`,
    # `[[../../etc/passwd]]`, `<iframe>`) can never survive.
    #
    # Pop both unconditionally (even when the LLM didn't
    # supply them) so the narrow blast radius is obvious to
    # future readers — there is no caller-controlled
    # fall-through path.
    ctx.pop("inheritance_mermaid", None)
    ctx.pop("call_graph_mermaid", None)

    # Diagrams are enrichment — never block note writing on
    # EXPECTED failures (bad graph_id, test-synthesized node,
    # malformed Trailmark output, transient cache I/O). The
    # narrow tuple catches graph-lookup variants (KeyError,
    # FileNotFoundError, ValueError) plus filesystem/pickle
    # exceptions from the cache layer (OSError, EOFError,
    # pickle.UnpicklingError). Coding bugs (TypeError,
    # AttributeError) propagate to the dispatcher's failure
    # recorder so they surface in the run summary instead of
    # silently producing diagram-less notes.
    try:
        ctx["inheritance_mermaid"] = render_inheritance(
            graph_id, node["id"], cache_root=cache_root
        )
        primary = _pick_primary_method(
            graph_id, node["id"], cache_root=cache_root
        )
        if primary is not None:
            ctx["call_graph_mermaid"] = render_call_graph(
                graph_id, primary, cache_root=cache_root
            )
    except (
        KeyError,
        FileNotFoundError,
        ValueError,
        OSError,
        EOFError,
        pickle.UnpicklingError,
    ) as e:
        _log.warning(
            "diagram computation failed for %s: %s — proceeding "
            "without diagrams",
            node["id"],
            e,
        )
        # Codex round-15 fix: clear any partial diagram
        # output from the trusted regen path too. If
        # `render_inheritance` succeeded but `render_call_graph`
        # raised, the inheritance diagram is still trusted —
        # but a partial state is harder to reason about than
        # "all-or-nothing diagrams". Drop both for symmetry
        # with the pre-try strip; the rendered note shows no
        # diagrams instead of half a diagram block.
        ctx.pop("inheritance_mermaid", None)
        ctx.pop("call_graph_mermaid", None)

    # Filename disambiguation needs the graph to detect bare-
    # name collisions. Same graceful fallback as the diagram
    # block above — if the graph isn't loadable (bad gid, test
    # fixture, transient cache I/O), use the bare path. The
    # narrow catch tuple lets coding bugs (TypeError,
    # AttributeError) propagate so they're noticed.
    try:
        rel_path = (
            f"{_disambiguated_path(node, graph_id, cache_root=cache_root)}"
            f".md"
        )
    except (
        KeyError,
        FileNotFoundError,
        ValueError,
        OSError,
        EOFError,
        pickle.UnpicklingError,
    ) as e:
        _log.warning(
            "filename disambiguation failed for %s: %s — using "
            "bare path",
            node["id"],
            e,
        )
        folder = KIND_TO_FOLDER.get(kind, "contracts")
        rel_path = f"{folder}/{node['name']}.md"

    # Pull finding annotations from the graph so the Risks
    # section can render them. Includes findings attached to
    # the container node itself PLUS findings attached to
    # child methods (Trailmark IDs `<container>.<method>`).
    #
    # Why bubble methods up: slither/semgrep attach findings
    # at method-level granularity (e.g.,
    # `contracts.UniswapV2Pair:UniswapV2Pair._update`), but
    # methods are documented INSIDE their parent's note
    # (line 715 above rejects method-kind for standalone
    # rendering). Without bubbling, method findings have no
    # standalone note to land in — the auditor would see
    # them only via vault/risks/ summaries (curated) and
    # never directly on the parent contract's note.
    #
    # Same graceful-degradation pattern as the diagram block
    # above. Coding bugs (TypeError, AttributeError) propagate.
    try:
        node_id = node["id"]
        method_prefix = node_id + "."
        # `nodes_with_annotation` returns BARE node dicts (no
        # embedded findings — Trailmark 0.3.x). One enumeration
        # + per-match annotations_of call is necessary to get
        # the descriptions. Filter to container itself + method
        # children by ID prefix.
        annotated_nodes = nodes_with_annotation(
            graph_id, "finding", cache_root=cache_root,
        )
        bubbled: list[dict[str, Any]] = []
        for n in annotated_nodes:
            nid = n.get("id", "")
            if nid == node_id or nid.startswith(method_prefix):
                bubbled.extend(
                    annotations_of(
                        graph_id, nid, kind="finding",
                        cache_root=cache_root,
                    )
                )
        ctx["finding_annotations"] = bubbled
    except (
        KeyError,
        FileNotFoundError,
        ValueError,
        OSError,
        EOFError,
        pickle.UnpicklingError,
    ) as e:
        _log.warning(
            "finding annotation lookup failed for %s: %s — "
            "rendering note without Risks content",
            node["id"],
            e,
        )
        ctx["finding_annotations"] = []

    frontmatter, body_text = render_node_note(node, ctx, body)
    written = write_obsidian_note(
        vault_path, rel_path, frontmatter, body_text
    )
    return str(written)


def _validate_flow_path(
    graph_id: str,
    path: list[str],
    expected_entrypoint_id: str,
    *,
    cache_root: Path,
) -> str | None:
    """Validate a flow path against the trusted graph. Return
    None when valid, or a short reason string when invalid.

    Codex round-17 fix: an LLM-supplied path is a list of node
    ids. `render_sequence` is explicitly path-agnostic — it
    will happily draw `A --> B --> C` even when `(A, B)` is
    NOT a real call edge in the graph. A prompt-injected
    FlowTracer could synthesize misleading paths
    (`UniswapV2Pair.swap --> UniswapV2ERC20.constructor`)
    that look authoritative in the rendered note.

    Three checks, in order:
      1. `path[0] == expected_entrypoint_id` — every flow path
         must start at the entrypoint the dispatcher bound.
      2. Every hop id must resolve via `get_node` — defends
         against fabricated node ids that look real.
      3. Every consecutive `(src, dst)` pair must appear in
         `callees_of(graph_id, src)` — defends against
         fabricated call edges.

    Why return a reason string instead of raising: the caller
    emits an inline placeholder for invalid paths so a partial
    flow note still ships. Raising would lose the per-path
    granularity that the existing render_sequence except
    handler already provides for legitimate-but-uncomputable
    paths."""
    if not path:
        return "empty path"
    if path[0] != expected_entrypoint_id:
        return (
            f"path[0]={path[0]!r} != expected entrypoint "
            f"{expected_entrypoint_id!r}"
        )
    # Check every hop exists. get_node raises KeyError on
    # unknown ids; we want a reason string, not the raise.
    for hop in path:
        try:
            get_node(graph_id, hop, cache_root=cache_root)
        except KeyError:
            return f"hop {hop!r} not in graph"
    # Check every (src, dst) is a real call edge.
    for src, dst in zip(path, path[1:]):
        callee_ids = {
            c["id"] for c in callees_of(
                graph_id, src, cache_root=cache_root,
            )
        }
        if dst not in callee_ids:
            return (
                f"hop {src!r} -> {dst!r} is not a real call "
                f"edge in the graph"
            )
    return None


def render_and_write_flow_note(
    vault_path: str | Path,
    graph_id: str,
    entrypoint_node: dict[str, Any],
    paths: list[list[str]],
    overview: str = "",
    observations: list[str] | None = None,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Render a flow note + write to `vault/flows/<name>.md` in
    one atomic call.

    `entrypoint_node` is a Trailmark node dict (from `get_node`).
    `paths` is a list of caller-chosen call chains; each chain is
    `[entrypoint_id, ..., sink_id]`. One Mermaid sequence diagram
    is rendered per path.

    Body layout:
        ## Overview         ← overview parameter (LLM prose)
        ## Paths
          ### Path N — entry → sink
          ```mermaid sequenceDiagram ... ```
        ## Observations    ← bullet list (skipped when empty)

    Empty `paths` produces a placeholder Paths section — the note
    still ships. Per-path diagram failures (bad node IDs, etc.)
    log and emit an inline placeholder for that path.

    Returns the absolute file path as a string (LLM-friendly).
    """
    # Codex round-16 fix: refetch the canonical entrypoint
    # from the graph. Mirrors `render_and_write_node_note`'s
    # defense — the LLM-supplied dict is an ID carrier only.
    # Reproduced exploit: forged
    # `entrypoint_node["name"]="../risks/flow-pwn"` produces
    # `rel_path="flows/../risks/flow-pwn.md"` which resolves
    # to `vault/risks/flow-pwn.md` — INSIDE the vault, so the
    # resolve-based containment check passed. Because flow
    # dispatch runs before risk synthesis + MOC generation,
    # the attacker could plant a fake "risk" note that gets
    # indexed alongside real ones. Strict refetch closes
    # that; `write_obsidian_note`'s `..`-segment reject is
    # the belt-and-suspenders catch-all.
    entrypoint_node = get_node(
        graph_id, entrypoint_node["id"], cache_root=cache_root,
    )

    bare = entrypoint_node["name"]
    # Always qualify with the containing class so two entrypoints
    # with the same method name (e.g. UniswapV2Pair.swap vs
    # IUniswapV2Pair.swap, which both appear on Tier 1's attack
    # surface) land in distinct files. The bare method name alone
    # isn't auditor-meaningful for a flow note anyway.
    tail = entrypoint_node["id"].rsplit(":", 1)[-1]
    parent = tail.rsplit(".", 1)[0] if "." in tail else None
    if parent and parent != bare:
        rel_path = f"flows/{parent}.{bare}.md"
    else:
        rel_path = f"flows/{bare}.md"

    frontmatter: dict[str, Any] = {
        "type": "flow",
        "name": bare,
        "entrypoint": entrypoint_node["id"],
        "path_count": len(paths),
    }

    parts: list[str] = [_render_overview(overview), "\n"]
    if paths:
        parts.append("## Paths\n\n")
        safe_bare = _flatten_to_one_line(bare)
        for i, path in enumerate(paths, 1):
            # Codex round-18 fix: validate FIRST, before any
            # `path[-1]` / `path[0]` indexing. The validator
            # has an explicit empty-path branch but it was
            # previously unreachable — `path[-1]` on
            # `paths=[[]]` raised IndexError before validation
            # ran, failing the whole flow note instead of
            # degrading to a per-path placeholder.
            invalid_reason = _validate_flow_path(
                graph_id, path, entrypoint_node["id"],
                cache_root=cache_root,
            )
            if invalid_reason is not None:
                _log.warning(
                    "rejecting invalid flow path %d (%s): %s",
                    i, path, invalid_reason,
                )
                # Generic heading — no `path[-1]` indexing,
                # safe for the empty-path case.
                parts.append(
                    f"### Path {i} — invalid path\n\n"
                )
                parts.append(
                    f"_Path rejected: "
                    f"{_flatten_to_one_line(invalid_reason)}_\n\n"
                )
                # Skip the diagram render AND the hop list —
                # rendering hops of a fabricated path would
                # still be misleading.
                continue

            # Sink bare is LLM-influenced (path is the LLM's
            # argument); bare is the canonical entrypoint name
            # from the round-16 refetch — trusted. Flatten so
            # a newline in `sink_bare` can't escape the heading
            # line and inject a second heading or list. The
            # validator above guarantees `path` is non-empty
            # before we index `path[-1]`.
            sink_bare = _flatten_to_one_line(
                path[-1].rsplit(":", 1)[-1].rsplit(".", 1)[-1]
            )
            parts.append(
                f"### Path {i} — {safe_bare} → {sink_bare}\n\n"
            )

            try:
                parts.append(
                    render_sequence(graph_id, path, cache_root=cache_root)
                )
            except (
                KeyError,
                FileNotFoundError,
                ValueError,
                OSError,
                EOFError,
                pickle.UnpicklingError,
            ) as e:
                # KeyError (bad node ID in path), FileNotFoundError
                # (missing graph cache), ValueError (render_sequence
                # input validation), OSError / EOFError /
                # UnpicklingError (cache I/O) are the expected
                # per-path failures — keep emitting inline
                # placeholders so a partial flow note still ships.
                # Coding bugs (TypeError, AttributeError) propagate.
                _log.warning(
                    "sequence render failed for path %d (%s): %s",
                    i,
                    path,
                    e,
                )
                # Defang the exception message — it may
                # echo back LLM-controlled hop_id content
                # (KeyError on a malicious node ID puts the
                # whole bad string in the exception). Without
                # defang, embedded wikilinks/HTML in the LLM
                # input survive into the rendered placeholder.
                parts.append(
                    f"_Sequence diagram unavailable: "
                    f"{_flatten_to_one_line(str(e))}_\n\n"
                )
            # Hop wikilinks: per-method navigation below the
            # sequence diagram. Unresolvable hops fall back to
            # a backticked bare name. Must catch the same family
            # of expected failures as the outer render_sequence
            # block — resolve_wikilink → get_node → load_graph
            # can raise FileNotFoundError (missing cache),
            # OSError / EOFError / UnpicklingError (cache I/O).
            # Without these, a partial-failure flow note aborts
            # entirely instead of shipping with placeholder hops.
            # Coding bugs (TypeError, AttributeError) propagate.
            parts.append("\n**Hops:**\n\n")
            for j, hop_id in enumerate(path, 1):
                try:
                    link = resolve_wikilink(
                        graph_id, hop_id, cache_root=cache_root
                    )
                    parts.append(f"{j}. {link}\n")
                except (
                    KeyError,
                    FileNotFoundError,
                    ValueError,
                    OSError,
                    EOFError,
                    pickle.UnpicklingError,
                ):
                    # Same defang as risk-note involved-node
                    # fallback — hop_id is LLM-controlled
                    # via the `paths` arg.
                    hop_bare = _defang_inline_code_text(
                        hop_id.rsplit(":", 1)[-1]
                    )
                    parts.append(
                        f"{j}. `{hop_bare}` (no contract note)\n"
                    )
            parts.append("\n")
    else:
        parts.append(
            "## Paths\n\n"
            "_No multi-hop paths from this entrypoint in the graph._\n\n"
        )

    if observations:
        parts.append("## Observations\n\n")
        for obs in observations:
            # Defang LLM-authored observation text (Codex
            # round-13 fix). Block-aware so a multi-line
            # observation can't inject ## headings, but the
            # bullet itself stays single-bullet because we
            # control the `- ` prefix.
            parts.append(f"- {_defang_block_text(obs)}\n")

    body = "".join(parts)
    written = write_obsidian_note(
        vault_path, rel_path, frontmatter, body
    )
    return str(written)


# Risk-note name allowlist: lowercase-alnum kebab-case.
# Pattern allows e.g. `hotspots`, `delegatecall-sites`,
# `reentrancy-candidates` but rejects: path-traversal
# (`../foo`), hyphen-only or hyphen-bookended (`-`, `--`,
# `-foo`, `foo-`), and double-internal hyphens (`a--b`) —
# anything that would produce ambiguous or ugly vault file
# names. Mirrors _VAULT_PATH_BAD_CHARS defense pattern from
# src/agent.py.
_RISK_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def render_and_write_risk_note(
    vault_path: str | Path,
    graph_id: str,
    risk_name: str,
    overview: str,
    involved_nodes: list[str],
    observations: list[str] | None = None,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Render a risk note + write to `vault/risks/<risk_name>.md`
    in one atomic call.

    `risk_name` is a kebab-case slug (e.g., 'hotspots',
    'delegatecall-sites', 'reentrancy-candidates'). Must match
    `[a-z0-9-]+` — rejects path-traversal attempts from
    attacker-controlled risk_name strings.

    `involved_nodes` is a list of Trailmark node IDs that this
    risk touches. Each is rendered as a wikilink in the
    "Involved Nodes" section. Unresolvable hops fall back to a
    backticked bare name — same except-tuple as flow note hops
    (KeyError + cache I/O failures + ValueError).

    Body layout:
        ## Overview          ← overview parameter (LLM prose)
        ## Involved Nodes
        - [[contracts/Pair|swap]]
        - ...
        ## Observations      ← bullet list (skipped when empty)

    Empty `involved_nodes` produces a placeholder section so
    the note still ships. Returns the absolute file path as a
    string (LLM-friendly).

    Raises:
        ValueError: `risk_name` contains characters outside
            `[a-z0-9-]` (path-traversal defense).
    """
    if not _RISK_NAME_RE.fullmatch(risk_name):
        raise ValueError(
            f"risk_name {risk_name!r} must be lowercase "
            f"kebab-case (e.g. 'hotspots', "
            f"'delegatecall-sites') — path-traversal defense"
        )
    # LLM may pass None for an empty list (deepagents tool
    # serialization sometimes converts [] → null). Defend
    # against TypeError on len() in the frontmatter. Same
    # one-liner pattern as `observations` default.
    if involved_nodes is None:
        involved_nodes = []

    # Codex review fixes (F5 + follow-up F1):
    # RiskSynthesizer's `overview` and `observations` come
    # from the LLM processing attacker-influenced SARIF
    # context. The overview defang now lives inside
    # `_render_overview` so EVERY caller (node + flow + risk)
    # gets it. We still pre-defang observations here because
    # observations are interpolated inline into the body
    # below (not via _render_overview).
    safe_observations = (
        [_defang_block_text(o) for o in observations]
        if observations
        else observations
    )

    rel_path = f"risks/{risk_name}.md"
    frontmatter: dict[str, Any] = {
        "type": "risk",
        "risk_name": risk_name,
        "nodes_count": len(involved_nodes),
    }

    parts: list[str] = [_render_overview(overview), "\n"]

    parts.append("## Involved Nodes\n\n")
    if involved_nodes:
        for nid in involved_nodes:
            try:
                link = resolve_wikilink(
                    graph_id, nid, cache_root=cache_root
                )
                parts.append(f"- {link}\n")
            except (
                KeyError,
                FileNotFoundError,
                ValueError,
                OSError,
                EOFError,
                pickle.UnpicklingError,
            ):
                # Same fallback as flow note hops: backticked
                # bare name when wikilink resolution fails.
                # LLM-controlled nid: defang for inline-code-
                # span context so embedded backticks /
                # newlines / wikilinks can't break out.
                bare = _defang_inline_code_text(
                    nid.rsplit(":", 1)[-1]
                )
                parts.append(f"- `{bare}` (no contract note)\n")
        parts.append("\n")
    else:
        parts.append(
            "_No involved nodes recorded for this risk._\n\n"
        )

    if safe_observations:
        parts.append("## Observations\n\n")
        for obs in safe_observations:
            parts.append(f"- {obs}\n")

    body = "".join(parts)
    written = write_obsidian_note(
        vault_path, rel_path, frontmatter, body
    )
    return str(written)
