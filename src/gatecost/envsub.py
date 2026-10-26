"""Environment-variable expansion for filelist paths (``$VAR``, ``${VAR}``, ``$(VAR)``).

Pure text substitution of identifiers only: nothing is executed (``$(cmd arg)`` is not a variable and stays
literal), and an undefined variable is reported to the caller instead of raising or silently becoming empty.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|\(([A-Za-z_][A-Za-z0-9_]*)\)|([A-Za-z_][A-Za-z0-9_]*))")


def expand_env(text: str, env: Mapping[str, str] | None = None) -> tuple[str, tuple[str, ...]]:
    """``(expanded text, names of undefined variables in order of appearance)``.

    An undefined variable is left untouched in the text so that the path stays recognisable in messages."""
    if "$" not in text:
        return text, ()
    src = os.environ if env is None else env
    missing: list[str] = []

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        val = src.get(name)
        if val is None:
            if name not in missing:
                missing.append(name)
            return m.group(0)
        return val

    return _VAR.sub(sub, text), tuple(missing)
