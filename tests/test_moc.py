import yaml

from src.render.moc import _list_notes, write_root_moc
from src.render.obsidian import ensure_vault


def _make_fake_vault(tmp_path):
    """Build a fake vault with some pre-existing notes — simulates
    what chunk 2.3's dispatch_topo would have written."""
    vault = ensure_vault(tmp_path / "vault")
    (vault / "contracts" / "ERC20.md").write_text("# ERC20\n")
    (vault / "contracts" / "ERC4626.md").write_text("# ERC4626\n")
    (vault / "libraries" / "SafeMath.md").write_text("# SafeMath\n")
    (vault / "_meta" / "src.tokens.ERC20.md").write_text("# mod\n")
    # flows / risks left empty — should be skipped by MOC
    return vault


def test_write_root_moc_creates_root_readme(tmp_path, tier0_graph_id):
    gid, cache_root = tier0_graph_id
    vault = _make_fake_vault(tmp_path)
    written = write_root_moc(vault, gid, cache_root=cache_root)

    root = vault / "README.md"
    assert root.exists()
    assert root in written
    assert written[0] == root  # root comes first


def test_write_root_moc_creates_folder_moc_per_populated(
    tmp_path, tier0_graph_id
):
    gid, cache_root = tier0_graph_id
    vault = _make_fake_vault(tmp_path)
    write_root_moc(vault, gid, cache_root=cache_root)

    assert (vault / "contracts" / "README.md").exists()
    assert (vault / "libraries" / "README.md").exists()
    assert (vault / "_meta" / "README.md").exists()
    # Empty folders skipped
    assert not (vault / "flows" / "README.md").exists()
    assert not (vault / "risks" / "README.md").exists()


def test_root_moc_links_to_populated_folders_only(
    tmp_path, tier0_graph_id
):
    gid, cache_root = tier0_graph_id
    vault = _make_fake_vault(tmp_path)
    write_root_moc(vault, gid, cache_root=cache_root)

    text = (vault / "README.md").read_text()
    assert "[[contracts/README|Contracts]]" in text
    assert "[[libraries/README|Libraries]]" in text
    assert "[[_meta/README|Modules]]" in text
    assert "flows/README" not in text
    assert "risks/README" not in text


def test_folder_moc_lists_every_note_excluding_readme(
    tmp_path, tier0_graph_id
):
    gid, cache_root = tier0_graph_id
    vault = _make_fake_vault(tmp_path)
    write_root_moc(vault, gid, cache_root=cache_root)

    text = (vault / "contracts" / "README.md").read_text()
    assert "[[contracts/ERC20|ERC20]]" in text
    assert "[[contracts/ERC4626|ERC4626]]" in text
    assert "[[contracts/README|README]]" not in text


def test_root_moc_has_canonical_frontmatter(tmp_path, tier0_graph_id):
    gid, cache_root = tier0_graph_id
    vault = _make_fake_vault(tmp_path)
    write_root_moc(vault, gid, cache_root=cache_root)

    text = (vault / "README.md").read_text()
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    fm = yaml.safe_load(text[4:end])
    assert fm["type"] == "moc"
    assert fm["graph_id"] == gid


def test_root_moc_includes_graph_summary_stats(tmp_path, tier0_graph_id):
    gid, cache_root = tier0_graph_id
    vault = _make_fake_vault(tmp_path)
    write_root_moc(vault, gid, cache_root=cache_root)

    text = (vault / "README.md").read_text()
    # Tier 0: 50 total nodes, 42 functions/methods, 19 entrypoints
    assert "Total graph nodes: 50" in text
    assert "Functions/methods: 42" in text
    assert "Entrypoints:       19" in text


def test_write_root_moc_is_idempotent(tmp_path, tier0_graph_id):
    gid, cache_root = tier0_graph_id
    vault = _make_fake_vault(tmp_path)

    first = write_root_moc(vault, gid, cache_root=cache_root)
    first_root = (vault / "README.md").read_text()
    second = write_root_moc(vault, gid, cache_root=cache_root)
    second_root = (vault / "README.md").read_text()

    assert first == second
    assert first_root == second_root


def test_list_notes_skips_readme_case_insensitive(tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    for n in ("README.md", "readme.md", "ERC20.md", "ERC4626.md"):
        (folder / n).write_text("x")
    notes = _list_notes(folder)
    assert notes == ["ERC20", "ERC4626"]
