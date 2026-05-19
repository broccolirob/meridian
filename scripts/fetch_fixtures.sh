#!/usr/bin/env bash
# Fetch pinned upstream Solidity fixtures into tests/fixtures/.
# Idempotent: re-running overwrites. To refresh, bump the commit shas
# below and re-run. Source: chunk 0.2 in CHUNKS.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX="$ROOT/tests/fixtures"

SOLMATE_REPO="transmissions11/solmate"
SOLMATE_SHA="89365b880c4f"

UNISWAP_REPO="Uniswap/v2-core"
UNISWAP_SHA="6a9e7c978606"

raw() {
  # $1=repo  $2=sha  $3=path-in-repo
  echo "https://raw.githubusercontent.com/$1/$2/$3"
}

fetch() {
  # $1=url  $2=destination-absolute-path
  mkdir -p "$(dirname "$2")"
  curl -fsSL "$1" -o "$2"
  printf "  -> %s\n" "${2#${ROOT}/}"
}

echo "[solmate @ ${SOLMATE_SHA}] -> tests/fixtures/tier0_erc4626/"
fetch "$(raw "$SOLMATE_REPO" "$SOLMATE_SHA" LICENSE)" \
      "$FIX/tier0_erc4626/LICENSE-solmate"
for path in \
  src/tokens/ERC4626.sol \
  src/tokens/ERC20.sol \
  src/utils/SafeTransferLib.sol \
  src/utils/FixedPointMathLib.sol; do
  fetch "$(raw "$SOLMATE_REPO" "$SOLMATE_SHA" "$path")" \
        "$FIX/tier0_erc4626/$path"
done

echo "[uniswap-v2-core @ ${UNISWAP_SHA}] -> tests/fixtures/tier1_uniswap_v2/"
fetch "$(raw "$UNISWAP_REPO" "$UNISWAP_SHA" LICENSE)" \
      "$FIX/tier1_uniswap_v2/LICENSE-uniswap"
for path in \
  contracts/UniswapV2ERC20.sol \
  contracts/UniswapV2Factory.sol \
  contracts/UniswapV2Pair.sol \
  contracts/interfaces/IERC20.sol \
  contracts/interfaces/IUniswapV2Callee.sol \
  contracts/interfaces/IUniswapV2ERC20.sol \
  contracts/interfaces/IUniswapV2Factory.sol \
  contracts/interfaces/IUniswapV2Pair.sol \
  contracts/libraries/Math.sol \
  contracts/libraries/SafeMath.sol \
  contracts/libraries/UQ112x112.sol; do
  fetch "$(raw "$UNISWAP_REPO" "$UNISWAP_SHA" "$path")" \
        "$FIX/tier1_uniswap_v2/$path"
done

echo "Done. 15 source files + 2 LICENSE files under tests/fixtures/."
