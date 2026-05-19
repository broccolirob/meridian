from pathlib import Path

import pytest
from trailmark.query.api import QueryEngine

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

    The default cache key is deterministic (sha256(abs_path)[:12]),
    so this fixture is idempotent across runs and across machines —
    no leakage problem despite writing outside tmp_path.
    """
    return trailmark_parse(str(tier0_dir), language="solidity")
