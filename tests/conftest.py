import shutil
from pathlib import Path

import pytest
from trailmark.query.api import QueryEngine

from src.graph.persist import CACHE_ROOT, repo_hash
from src.tools import trailmark_parse

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def tier0_dir() -> Path:
    return FIXTURES / "tier0_erc4626"


@pytest.fixture(scope="session")
def tier0_engine(tier0_dir: Path) -> QueryEngine:
    return QueryEngine.from_directory(str(tier0_dir), language="solidity")


@pytest.fixture(scope="session")
def tier1_dir() -> Path:
    return FIXTURES / "tier1_uniswap_v2"


@pytest.fixture(scope="session")
def tier0_graph_id(tier0_dir, tmp_path_factory):
    cache_root = tmp_path_factory.mktemp("cache-tier0")
    gid = trailmark_parse(
        str(tier0_dir), language="solidity", cache_root=cache_root
    )
    return gid, cache_root


@pytest.fixture(scope="session")
def tier1_graph_id(tier1_dir, tmp_path_factory):
    cache_root = tmp_path_factory.mktemp("cache-tier1")
    gid = trailmark_parse(
        str(tier1_dir), language="solidity", cache_root=cache_root
    )
    return gid, cache_root


@pytest.fixture(scope="session")
def tier0_graph_id_default_cache(tier0_dir):
    """Parse Tier 0 into the DEFAULT cache (`.washable/graph/`).

    dispatch_topo and the subagent's tool surface all read from the
    default cache — there is no cache_root threading through the
    agent layer because subagent tools bind their cache_root default
    at module-import time. Tests that exercise dispatch_topo
    end-to-end need the graph in default cache, not a tmp one.

    CONSTRAINT — tests using this fixture must NOT call
    `annotate` or `clear_annotations` against the default cache
    root. Mutations would persist in the cache file across the
    session; chunk 3.12's mtime-aware lru_cache invalidates on
    save, so subsequent reads SEE the mutation. That's the
    leakage path. Use `test_annotations.py::fresh_tier0`
    (function-scoped, tmp_path) for any test that mutates the
    graph.

    Lifecycle (chunk 3.16, /review I9):
      1. Pre-wipe `.washable/graph/<gid>/` so prior-session
         crashes can't leak stale state into this run.
      2. Parse fresh via `trailmark_parse` — writes engine.pkl.
      3. Yield gid for the session.
      4. Teardown wipes the cache dir so this session's state
         can't leak into the next.

    Cache key is deterministic (`sha256(abs_path)[:12]`), so the
    wipe targets ONLY this fixture's graph — other cached graphs
    (e.g. Tier 1 from a `document_repo.py` run) stay intact.
    """
    gid = repo_hash(str(tier0_dir))
    cache_dir = CACHE_ROOT / gid

    # Pre-wipe: handles prior-session crashes that left stale
    # state (engine.pkl with annotations, leaked .tmp.* files
    # from a torn write — chunk 3.13's atomic-write idiom should
    # clean tmps, but defense in depth).
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)

    # Parse fresh into the default cache.
    parsed_gid = trailmark_parse(str(tier0_dir), language="solidity")
    assert parsed_gid == gid, (
        f"repo_hash drift between conftest ({gid}) and "
        f"trailmark_parse ({parsed_gid})"
    )

    yield gid

    # Teardown: scoped wipe by gid — only this fixture's graph.
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
