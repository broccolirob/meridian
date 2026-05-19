import hashlib
import pickle
from pathlib import Path

from trailmark.query.api import QueryEngine

CACHE_ROOT = Path(".washable/graph")


def repo_hash(repo_path: str | Path) -> str:
    abs_path = str(Path(repo_path).expanduser().resolve())
    return hashlib.sha256(abs_path.encode()).hexdigest()[:12]


def save_graph(
    engine: QueryEngine,
    repo_hash: str,
    cache_root: Path = CACHE_ROOT,
) -> Path:
    out_dir = Path(cache_root) / repo_hash
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "engine.pkl"
    with open(path, "wb") as f:
        pickle.dump(engine, f)
    return path


def load_graph(
    repo_hash: str,
    cache_root: Path = CACHE_ROOT,
) -> QueryEngine:
    path = Path(cache_root) / repo_hash / "engine.pkl"
    with open(path, "rb") as f:
        return pickle.load(f)
