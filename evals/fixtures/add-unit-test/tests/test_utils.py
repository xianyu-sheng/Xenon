from src.utils import normalize_items


def test_normalize_items():
    assert normalize_items([" a "]) == ["a"]
