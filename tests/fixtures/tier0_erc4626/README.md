# Tier 0 — solmate ERC4626 smoke fixture

Single-contract smoke test for meridian's parsing layer.

- **Upstream:** https://github.com/transmissions11/solmate
- **Commit:** `89365b880c4f`
- **License:** AGPL-3.0-only (per-file SPDX header preserved)
- **Files:** 4 Solidity sources, mirroring upstream `src/` layout

## Scope

`ERC4626` plus its direct imports:

- `src/tokens/ERC4626.sol` — the contract under test
- `src/tokens/ERC20.sol` — direct import of ERC4626
- `src/utils/SafeTransferLib.sol` — direct import of ERC4626
- `src/utils/FixedPointMathLib.sol` — direct import of ERC4626

Tier 0 deliberately omits anything those direct imports transitively
depend on. The fixture is sized for the "does Trailmark parse this at
all" smoke check; expect some `uncertain` edges where Trailmark can't
resolve symbols that live outside the vendored tree.

For a fully closed-over fixture, see `../tier1_uniswap_v2/`.
