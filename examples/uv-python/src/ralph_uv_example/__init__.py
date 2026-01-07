from __future__ import annotations

__all__ = ["greet"]


def greet(name: str) -> str:
    name = name.strip()
    if not name:
        name = "world"
    return f"Hello, {name}!"
