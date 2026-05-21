# python_smol

Tiny Python fixture used only by `tests/test_semgrep.py`.

Contains:
- `bad.py` — `exec(input())` + a hardcoded API key string.
- `rules.yaml` — a tiny LOCAL semgrep rule pack with two
  rules (`smol-dangerous-exec`, `smol-hardcoded-secret`)
  that catch the patterns in `bad.py`.

The tests use `rules.yaml` instead of `p/python` so they're
hermetic: no registry network fetch, no version drift
between semgrep releases changing what rules fire. The
registry packs (`p/python`, `p/default`) catch different
patterns at different versions; a local rule fixes the
contract.

Not a representative codebase. Do NOT add real-looking code
here — its only job is to exercise the `run_semgrep`
wrapper and the augment_sarif ingestion path.
