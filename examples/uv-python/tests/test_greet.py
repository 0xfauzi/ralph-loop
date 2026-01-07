from __future__ import annotations

from ralph_uv_example import greet


def test_greet() -> None:
    assert greet("Alice") == "Hello, Alice!"


def test_greet_defaults_to_world() -> None:
    assert greet("   ") == "Hello, world!"
