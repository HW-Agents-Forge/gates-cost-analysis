"""Reporters: render an Output in agent, json, text or markdown form."""
from __future__ import annotations

from ..result import Output

FORMATS = ("agent", "json", "text", "markdown")


def render(out: Output, fmt: str, *, max_lines: int = 10, verbose: bool = False, quiet: bool = False) -> str:
    if fmt == "json":
        from .json_fmt import render_json

        return render_json(out)
    if fmt == "agent":
        from .agent import render_agent

        return render_agent(out, max_lines=max_lines, verbose=verbose, quiet=quiet)
    if fmt == "text":
        from .text import render_text

        return render_text(out, max_lines=max_lines, verbose=verbose, quiet=quiet)
    if fmt == "markdown":
        from .markdown import render_markdown

        return render_markdown(out, max_lines=max_lines)
    raise ValueError(f"unknown format {fmt!r}")
