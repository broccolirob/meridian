#!/usr/bin/env bash
# Tier 1 end-to-end runbook — chunk 2.5.
#
# Runs the full washable pipeline against Uniswap V2 core
# (~22 nodes), then verifies:
#   1. expected_files.txt is satisfied (every listed path exists)
#   2. every produced .md has a parseable YAML frontmatter block
#
# Cost: ~$0.30 in OpenAI tokens (22 nodes x gpt-5-mini).
# Don't run this on every CI build. It's the "I changed the
# dispatch pipeline / prompts / templates" verification.
#
# Usage:
#   bash tests/integration/uniswap_v2/run.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

FIXTURE="tests/fixtures/tier1_uniswap_v2"
VAULT=".washable/vaults/tier1"
MANIFEST="tests/integration/uniswap_v2/expected_files.txt"

rm -rf "$VAULT"

echo "=== [1/3] Dispatch + MOC over Tier 1 ==="
uv run python scripts/document_repo.py --repo "$FIXTURE" --vault "$VAULT"

echo ""
echo "=== [2/3] Verifying expected files ==="
missing=0
total=0
while IFS= read -r expected; do
    [ -z "$expected" ] && continue
    case "$expected" in '#'*) continue ;; esac
    total=$((total + 1))
    if [ ! -f "$VAULT/$expected" ]; then
        echo "  MISSING: $expected"
        missing=$((missing + 1))
    fi
done < "$MANIFEST"

if [ "$missing" -gt 0 ]; then
    echo "FAIL: $missing of $total expected files missing"
    exit 1
fi
echo "  $total expected files present"

echo ""
echo "=== [3/3] Validating frontmatter on every .md ==="
uv run python <<'PY'
import sys
from pathlib import Path

import yaml

vault = Path(".washable/vaults/tier1")
errors = []
ok = 0
for md in sorted(vault.rglob("*.md")):
    text = md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        ok += 1
        continue
    try:
        end = text.index("\n---\n", 4)
        yaml.safe_load(text[4:end])
        ok += 1
    except (ValueError, yaml.YAMLError) as e:
        errors.append((md, str(e)))

if errors:
    for path, err in errors:
        print(f"  YAML ERROR in {path.relative_to(vault)}: {err}",
              file=sys.stderr)
    sys.exit(1)
print(f"  {ok} notes - all frontmatter parses cleanly")
PY

echo ""
echo "PASS: all expected files present, all frontmatter parses"
