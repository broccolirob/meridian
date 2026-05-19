# Tier 1 — Uniswap V2 core full closure

End-to-end fixture for the parsing → analysis → render pipeline.

- **Upstream:** https://github.com/Uniswap/v2-core
- **Commit:** `6a9e7c978606`
- **License:** GPL-3.0 (repo `LICENSE` copied as `LICENSE-uniswap`)
- **Files:** 11 Solidity sources — 3 contracts + 5 interfaces + 3
  libraries (the full import closure of `UniswapV2Pair`)

## Layout

```
contracts/
├── UniswapV2ERC20.sol           # base ERC20 used by Pair
├── UniswapV2Factory.sol         # deploys Pair instances
├── UniswapV2Pair.sol            # AMM core, the contract under test
├── interfaces/                  # IERC20 + IUniswapV2{Callee,ERC20,Factory,Pair}
└── libraries/                   # Math, SafeMath, UQ112x112
```

The closure means Trailmark's cross-file resolution can land every
call edge with `certain` confidence — no missing symbols to apologize
for in later phases.
