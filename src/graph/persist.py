import hashlib
import os
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
    final_path = out_dir / "engine.pkl"
    # Atomic write: pickle.dump into a same-directory tmp file, then
    # os.replace into place. Eliminates the partial-read race where a
    # reader (e.g. another tool call) opens engine.pkl while a writer
    # is mid-pickle.dump and hits EOFError. PID in the tmp name keeps
    # concurrent writers from clobbering each other's tmp.
    # (Lost-update — two writers racing on the same graph — is still
    # parked for chunk 2.3 where multi-agent dispatch lands.)
    tmp_path = out_dir / f".engine.pkl.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "wb") as f:
            pickle.dump(engine, f)
        os.replace(tmp_path, final_path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return final_path


def load_graph(
    graph_id: str,
    cache_root: Path = CACHE_ROOT,
) -> QueryEngine:
    _validate_graph_id(graph_id)
    path = Path(cache_root) / graph_id / "engine.pkl"
    with open(path, "rb") as f:
        return pickle.load(f)
