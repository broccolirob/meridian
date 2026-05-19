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
