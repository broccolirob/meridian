import pytest

from src.graph.persist import load_graph, repo_hash, save_graph


def test_repo_hash_stable_and_unique():
    h1 = repo_hash("/tmp/foo")
    h2 = repo_hash("/tmp/foo")
    h3 = repo_hash("/tmp/bar")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 12


def test_save_load_round_trip(tier0_engine, tier0_dir, tmp_path):
    rh = repo_hash(str(tier0_dir))
    path = save_graph(tier0_engine, rh, cache_root=tmp_path)
    assert path.exists()
    assert path.name == "engine.pkl"

    loaded = load_graph(rh, cache_root=tmp_path)
    assert loaded.summary() == tier0_engine.summary()


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_graph("deadbeef", cache_root=tmp_path)
