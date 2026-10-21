"""Command-line entry point shared by the launcher and the console script."""
from __future__ import annotations

import sys
from pathlib import Path

from .. import REPO_ROOT, version
from ..errors import EXIT_FAILED, EXIT_OK, GatecostError
from ..result import Output

def _emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _error_output(command: str, err: GatecostError, fmt: str) -> str:
    from ..report import render

    return render(Output(command=command, status="error", error=(err.code, err.message)), fmt)


def _guess_format(argv: list[str]) -> str:
    """Best-effort output format for failures that happen before settings are resolved."""
    import os

    fmt = os.environ.get("GATECOST_FORMAT", "agent")
    for i, a in enumerate(argv):
        if a == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
        elif a.startswith("--format="):
            fmt = a.split("=", 1)[1]
    return fmt if fmt in ("agent", "json", "text", "markdown") else "agent"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = next((a for a in argv if not a.startswith("-")), "gatecost")
    try:
        return _run(argv)
    except GatecostError as err:
        _emit(_error_output(command, err, _guess_format(argv)))
        return err.exit_code
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:  # unexpected: one ERROR line, details on stderr
        import traceback

        sys.stderr.write(traceback.format_exc())
        err = GatecostError("INTERNAL", f"unexpected failure ({type(exc).__name__})")
        _emit(_error_output(command, err, _guess_format(argv)))
        return err.exit_code


def _run(argv: list[str]) -> int:
    from ..config import resolve_settings
    from ..report import render
    from .parser import build_parser, help_text, validate_args

    args = build_parser().parse_args(argv)
    if args.version:
        _emit(f"gatecost {version()}\n")
        return EXIT_OK
    if args.command is None:
        _emit(help_text(None))
        return EXIT_OK
    validate_args(args)
    cmd = args.command
    skill_dir = REPO_ROOT

    if cmd == "help":
        _emit(help_text(args.topic))
        return EXIT_OK

    from . import commands

    if cmd == "validate-output":
        try:
            res = commands.cmd_validate(args.file)
        except commands._Invalid as inv:
            _emit("\n".join(inv.errors) + "\n")
            return EXIT_FAILED
        _emit(render(Output(command=cmd, result=res), "agent"))
        return EXIT_OK

    if cmd in ("init", "clean"):
        from ..config import find_project_root

        root = Path(args.root).resolve() if args.root else find_project_root(Path.cwd(), skill_dir)
        res = (
            commands.cmd_init(root, args.force, skill_dir)
            if cmd == "init"
            else commands.cmd_clean(root, args.global_, args.snapshots)
        )
        _emit(render(Output(command=cmd, result=res), "agent"))
        return EXIT_OK

    settings, _ = resolve_settings(args, Path.cwd(), skill_dir)
    # Stages 2-5 replace this with the real analysis commands.
    raise commands.not_implemented(cmd)
