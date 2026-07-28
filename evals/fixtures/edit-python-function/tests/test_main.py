from src.main import greet


def test_greet_default():
    assert greet() == "Hello"
