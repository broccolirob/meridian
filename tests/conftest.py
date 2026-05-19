from pathlib import Path

import pytest
from trailmark.query.api import QueryEngine

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def tier0_dir() -> Path:
    return FIXTURES / "tier0_erc4626"


@pytest.fixture(scope="session")
def tier0_engine(tier0_dir: Path) -> QueryEngine:
    return QueryEngine.from_directory(str(tier0_dir), language="solidity")
