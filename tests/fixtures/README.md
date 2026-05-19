# Test fixtures

Vendored Solidity codebases washable parses, analyzes, and renders as
Obsidian notes during testing.

All fixtures come from pinned upstream commits, fetched by
`scripts/fetch_fixtures.sh`. To refresh, bump the commit shas in that
script and re-run it — never edit fixture source files by hand.

## Tiers

| Tier | Path | Upstream | Commit | License |
| --- | --- | --- | --- | --- |
| 0 | `tier0_erc4626/` | [transmissions11/solmate](https://github.com/transmissions11/solmate) | `89365b880c4f` | AGPL-3.0-only (per-file SPDX) |
| 1 | `tier1_uniswap_v2/` | [Uniswap/v2-core](https://github.com/Uniswap/v2-core) | `6a9e7c978606` | GPL-3.0 |

## License posture

Fixtures are vendored under their original copyleft licenses
(AGPL-3.0-only for solmate, GPL-3.0 for Uniswap V2 core). Per-file SPDX
headers are preserved verbatim where upstream sets them; upstream
`LICENSE` files are copied alongside each tier.

These fixtures live exclusively under `tests/` and are never packaged
into any sdist or wheel built from this project.

## Refresh

```bash
bash scripts/fetch_fixtures.sh
```
