"""Front-end boundary: the protocol a UI speaks to the Python kernel.

See ``docs/design/frontend-protocol.md``.  Nothing here renders; it exists so a
TypeScript front-end can drive real turns without reimplementing the kernel.

The names are re-exported lazily: importing them eagerly makes
``python -m xenon.frontend.host`` load the module twice, which Python warns
about (and which would matter once the module keeps per-process state).
"""

from importlib import import_module
from typing import Any

__all__ = ["PROTOCOL_VERSION", "ProtocolHost"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        module = import_module("xenon.frontend.host")
        return getattr(module, name)
    raise AttributeError(name)
