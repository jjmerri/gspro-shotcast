"""For tests that swap out something the engine's code looks up by name (a wait, the clock, a helper).
The engine is a package of parts (commentator/engine/), each with its own copy of the names it uses,
so the swap is made in every part that has the name."""
import importlib
import pkgutil

import commentator.engine


def patch_engine(monkeypatch, name: str, value) -> None:
    """monkeypatch.setattr(<the engine's code>, name, value)."""
    found = False
    for info in pkgutil.iter_modules(commentator.engine.__path__):
        mod = importlib.import_module(f"commentator.engine.{info.name}")
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, value)
            found = True
    assert found, f"nothing in the engine uses {name}"
