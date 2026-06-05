# Screenshots

This project ships one canonical screenshot:
**`docs/screenshot-note.png`** — a rendered Obsidian view of the
`UniswapV2Pair` contract note from the Tier 1 quickstart vault.

The [README](../README.md) links to this path; until the binary
is committed, the README shows "Screenshot coming soon."

## Capture procedure

1. Run the quickstart against Tier 1:

   ```bash
   uv run python scripts/document_repo.py \
       --repo tests/fixtures/tier1_uniswap_v2 \
       --vault .meridian/vaults/screenshot-capture
   ```

2. Open the vault in Obsidian:

   ```bash
   open -a Obsidian .meridian/vaults/screenshot-capture
   ```

3. Navigate to `contracts/UniswapV2Pair.md`. This contract is the
   most visually rich:
   - Long Functions section with several public/external methods.
   - Inheritance diagram (extends `UniswapV2ERC20`).
   - Call-graph diagram (uses `_safeTransfer`, `_update`, etc.).
   - Several annotations + at least one Risks entry.

4. Recommended Obsidian setup for a consistent look:
   - **Theme:** default (light).
   - **Font size:** 16pt (Settings → Appearance → Font size).
   - **Viewport:** at least 1400×900 so the call-graph diagram
     renders without horizontal scroll.
   - **Sidebars:** off — keep the screenshot focused on the note body.
   - **Reading mode:** on (Ctrl/Cmd+E to toggle).

5. Capture the screenshot:
   - macOS: Cmd+Shift+4, then drag over the Obsidian window.
   - Save as `docs/screenshot-note.png` (PNG, no compression).
   - Trim to the Obsidian window only (no desktop background).

6. Commit:

   ```bash
   git add docs/screenshot-note.png
   git commit -m "docs: capture canonical screenshot of UniswapV2Pair note"
   ```

7. Edit the README — delete the "Screenshot coming soon" line and
   replace it with the image embed:

   ```markdown
   ![UniswapV2Pair note in Obsidian](docs/screenshot-note.png)
   ```

   The existing path reference becomes the rendered image.

## Why no auto-capture

The screenshot requires Obsidian's rendering engine. No headless
mode exists for Obsidian; a playwright-driven Markdown renderer
would not produce the same layout (Obsidian's wikilink resolution,
the Mermaid plugin, the theme CSS, the embed transclusion all
matter). Manual capture, taken once per significant template
change, is the right cost trade-off.

## When to re-capture

Re-take the screenshot when any of these change:

- The 7-section node-note template in `src/render/obsidian.py`.
- The frontmatter shape (`_build_frontmatter` in obsidian.py).
- The Mermaid renderer output for inheritance / call graphs.
- The Risks section format (when chunk 4.x findings change shape).

Old screenshots aren't deleted; the canonical path stays
`docs/screenshot-note.png`. If you want to keep history, archive
the previous capture to `docs/screenshots/<date>.png` before
overwriting.
