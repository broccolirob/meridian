#!/usr/bin/env bash
# Generate the chunk 1.5 golden notes by running the single-node
# harness twice and copying the produced notes into tests/golden/.
# Re-run this after intentional prompt or template changes; commit
# the updated goldens.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p tests/golden

# Start from a clean vault so each note is freshly written
rm -rf .washable/vaults/tier0

echo "[1/2] ERC4626 contract..."
uv run python scripts/document_one_node.py src.tokens.ERC4626:ERC4626
cp .washable/vaults/tier0/contracts/ERC4626.md tests/golden/ERC4626.md

echo "[2/2] FixedPointMathLib library..."
uv run python scripts/document_one_node.py src.utils.FixedPointMathLib:FixedPointMathLib
cp .washable/vaults/tier0/libraries/FixedPointMathLib.md tests/golden/FixedPointMathLib.md

echo ""
echo "Goldens written:"
ls -la tests/golden/
