import hashlib
import pickle
import re
from pathlib import Path

from trailmark.query.api import QueryEngine

CACHE_ROOT = Path(".washable/graph")

# Graph IDs are the first 12 hex chars of a sha256 — see repo_hash().
# Validating shape on save/load defends pickle.load against an attacker-
# (or LLM-)controlled graph_id that escapes the cache via "../" or
# resolves to an attacker-written file elsewhere on disk.
_GRAPH_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _validate_graph_id(graph_id: str) -> None:
    if not _GRAPH_ID_RE.fullmatch(graph_id):
        raise ValueError(
            f"invalid graph_id {graph_id!r}: expected 12 lowercase hex chars"
        )


def repo_hash(repo_path: str | Path) -> str:
    abs_path = str(Path(repo_path).expanduser().resolve())
    return hashlib.sha256(abs_path.encode()).hexdigest()[:12]


def save_graph(
    engine: QueryEngine,
    graph_id: str,
    cache_root: Path = CACHE_ROOT,
) -> Path:
    _validate_graph_id(graph_id)
    out_dir = Path(cache_root) / graph_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "engine.pkl"
    with open(path, "wb") as f:
        pickle.dump(engine, f)
    return path


def load_graph(
    graph_id: str,
    cache_root: Path = CACHE_ROOT,
) -> QueryEngine:
    _validate_graph_id(graph_id)
    path = Path(cache_root) / graph_id / "engine.pkl"
    with open(path, "rb") as f:
        return pickle.load(f)
