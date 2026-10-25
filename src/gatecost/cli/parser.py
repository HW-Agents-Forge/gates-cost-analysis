"""Argument parser for every command and option of CLI.md."""
from __future__ import annotations

import argparse

from ..errors import GatecostError

COMMANDS = ("diff", "cost", "estimate", "snapshot", "init", "profiles", "clean", "validate-output", "help")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # noqa: D102 - argparse hook
        raise GatecostError("BAD_ARGUMENT", message.splitlines()[0])

    def parse_known_args(self, args=None, namespace=None):  # noqa: D102
        # Options may appear anywhere after the command name (CLI.md section 1), so leaf
        # parsers accept positionals interleaved with options. Python 3.11 implements
        # parse_known_intermixed_args by calling self.parse_known_args again, hence the guard.
        if (
            not getattr(self, "_in_intermixed", False)
            and self._positionals._group_actions
            and not any(isinstance(a, argparse._SubParsersAction) for a in self._actions)
        ):
            self._in_intermixed = True
            try:
                return self.parse_known_intermixed_args(args, namespace)
            finally:
                self._in_intermixed = False
        return super().parse_known_args(args, namespace)

    def exit(self, status: int = 0, message: str | None = None):  # noqa: D102
        raise GatecostError("BAD_ARGUMENT", (message or "invalid usage").strip().splitlines()[0])


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--top", metavar="MODULE", help="top module or entity")
    p.add_argument("--define", action="append", metavar="NAME[=VALUE]", help="preprocessor define; repeatable")
    p.add_argument("--param", action="append", metavar="NAME=VALUE",
                   help="top-level parameter or generic value; repeatable")
    p.add_argument("--incdir", action="append", metavar="DIR", help="include directory; repeatable")
    p.add_argument("--filelist", action="append", metavar="FILE", help="read a .f filelist; repeatable")
    p.add_argument("--profile", metavar="NAME|PATH", help="cost profile (default generic-asic)")
    p.add_argument("--config", metavar="FILE", help="project file instead of gatecost.toml")
    p.add_argument("--root", metavar="DIR", help="project root (default: detected)")
    _output(p)
    p.add_argument("--no-cache", action="store_true", help="neither read nor write any cache")
    p.add_argument("--budget", type=float, metavar="GE", help="exit 1 if the total exceeds GE")


def _output(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=("agent", "json", "text", "markdown"), help="output format (default agent)")
    p.add_argument("--max-lines", type=int, metavar="N", help="maximum contributor or module lines (default 10)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--verbose", action="store_true", help="more detail")
    g.add_argument("--quiet", action="store_true", help="only the headline")
    p.add_argument("--no-meta", action="store_true", help="omit meta in json")


def build_parser() -> argparse.ArgumentParser:
    top = _Parser(prog="gatecost", add_help=False)
    top.add_argument("--version", action="store_true", help="print the version")
    sub = top.add_subparsers(dest="command", parser_class=_Parser)

    def add(name: str, **kw) -> argparse.ArgumentParser:
        return sub.add_parser(name, add_help=False, **kw)

    d = add("diff")
    _common(d)
    d.add_argument("paths", nargs="*", metavar="PATH", help="restrict the source set")
    base = d.add_argument_group("baseline (mutually exclusive)")
    base.add_argument("--base-ref", metavar="REF", help="git reference; merge-base means the default-branch merge base")
    base.add_argument("--since-snapshot", nargs="?", const="latest", metavar="ID", help="compare with a snapshot (latest)")
    base.add_argument("--before", metavar="DIR", help="source tree before (needs --after)")
    base.add_argument("--after", metavar="DIR", help="source tree after (needs --before)")
    d.add_argument("--threshold", type=float, metavar="GE", help="fold smaller contributors into OTHERS (default 5)")
    d.add_argument("--fail-on-increase", type=float, metavar="GE", help="exit 1 if the delta exceeds GE")
    d.add_argument("--basis", choices=("typ", "max"), help="scenario tested by budgets (default typ)")
    d.add_argument("--limit", type=int, metavar="N", help="json only: maximum contributors")
    d.add_argument("--detail", choices=("ops",), help="json only: include per-operation nodes")

    c = add("cost")
    _common(c)
    c.add_argument("paths", nargs="*", metavar="PATH", help="restrict the source set")
    c.add_argument("--detail", choices=("ops",), help="json only: all operations")
    c.add_argument("--by", choices=("module", "category", "file"), default="module", help="breakdown (default module)")

    s = add("snapshot")
    _common(s)
    s.add_argument("paths", nargs="*", metavar="PATH", help="restrict the source set")
    sg = s.add_mutually_exclusive_group()
    sg.add_argument("--list", action="store_true", help="list snapshots")
    sg.add_argument("--delete", metavar="ID", help="delete a snapshot")
    sg.add_argument("--all", action="store_true", help="delete all snapshots (with --delete)")
    s.add_argument("--keep", type=int, metavar="N", help="snapshots to keep (default 10)")

    e = add("estimate")
    e.add_argument("expression", nargs="?", help="compact expression, e.g. 'mul 32x32'")
    e.add_argument("--rtl", metavar="FRAGMENT", help="RTL fragment to cost")
    e.add_argument("--rtl-file", metavar="FILE", help="read the RTL fragment from FILE")
    e.add_argument("--compare", nargs="+", metavar="ALT", help="compare two or more alternatives")
    e.add_argument("--widths", metavar="a=16,b=8", help="widths of undeclared identifiers")
    e.add_argument("--lang", choices=("verilog", "vhdl"), default=None, help="language of --rtl")
    e.add_argument("--count", type=int, metavar="N", help="multiply the result by N instances")
    e.add_argument("--profile", metavar="NAME|PATH", help="cost profile")
    _output(e)

    i = add("init")
    i.add_argument("--force", action="store_true", help="overwrite an existing gatecost.toml")
    i.add_argument("--root", metavar="DIR", help="project root")

    pr = add("profiles")
    pr.add_argument("action", nargs="?", choices=("show",), help="show NAME prints a resolved profile")
    pr.add_argument("name", nargs="?", help="profile name")
    _output(pr)

    cl = add("clean")
    cl.add_argument("--global", dest="global_", action="store_true", help="also remove the user cache")
    cl.add_argument("--snapshots", action="store_true", help="also remove snapshots")
    cl.add_argument("--root", metavar="DIR", help="project root")

    v = add("validate-output")
    v.add_argument("file", metavar="FILE|-", help="JSON result to validate")

    h = add("help")
    h.add_argument("topic", nargs="?", help="command to describe")
    return top


def validate_args(args: argparse.Namespace) -> None:
    """Cross-option rules that argparse cannot express."""
    cmd = args.command
    if cmd == "diff":
        modes = [bool(args.base_ref), args.since_snapshot is not None, bool(args.before or args.after)]
        if sum(modes) > 1:
            raise GatecostError("BAD_ARGUMENT", "--base-ref, --since-snapshot and --before/--after are mutually exclusive")
        if bool(args.before) != bool(args.after):
            raise GatecostError("BAD_ARGUMENT", "--before and --after must be given together")
    elif cmd == "estimate":
        given = [x for x in (args.expression, args.rtl, args.rtl_file, args.compare) if x]
        if len(given) != 1:
            raise GatecostError("BAD_ARGUMENT", "estimate takes exactly one of: expression, --rtl, --rtl-file, --compare")
        if args.compare and len(args.compare) < 2:
            raise GatecostError("BAD_ARGUMENT", "--compare needs at least two alternatives")
    elif cmd == "snapshot":
        if args.all and not args.delete and not args.list:
            pass
    elif cmd == "profiles":
        if args.action == "show" and not args.name:
            raise GatecostError("BAD_ARGUMENT", "profiles show needs a profile name")
    if getattr(args, "max_lines", None) is not None and args.max_lines < 0:
        raise GatecostError("BAD_ARGUMENT", "--max-lines must be >= 0")


def help_text(topic: str | None) -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    if topic is None:
        lines = ["usage: gatecost <command> [options]", "commands:"]
        desc = {
            "diff": "cost delta of a modification, with attribution",
            "cost": "absolute cost of a design",
            "estimate": "cost of a hypothetical construct, or a comparison",
            "snapshot": "record the current state as a baseline",
            "init": "write a gatecost.toml from what is detected",
            "profiles": "list cost profiles, or show one",
            "clean": "remove caches and snapshots",
            "validate-output": "validate a JSON result against the schema",
            "help": "this text; help COMMAND for a command",
        }
        lines += [f"  {n:<16}{desc[n]}" for n in COMMANDS]
        return "\n".join(lines) + "\n"
    p = sub.choices.get(topic)
    if p is None:
        raise GatecostError("BAD_ARGUMENT", f"unknown command {topic!r}")
    lines = [f"usage: gatecost {topic} [options]"]
    shared = {"--top", "--define", "--param", "--incdir", "--filelist", "--profile", "--config", "--root", "--format",
              "--max-lines", "--verbose", "--quiet", "--no-meta", "--no-cache", "--budget"}
    compact = topic in ("diff", "cost", "snapshot")
    common: list[str] = []
    for a in p._actions:
        if a.option_strings and compact and a.option_strings[0] in shared:
            common.append(a.option_strings[0])
            continue
        if a.option_strings:
            flag = ", ".join(a.option_strings)
            if a.metavar:
                flag += f" {a.metavar}"
            elif a.choices:
                flag += " " + "|".join(a.choices)
        elif a.dest != "help":
            flag = a.metavar or a.dest
        else:
            continue
        lines.append(f"  {flag:<34}{a.help or ''}")
    if common:
        lines.append("  common options (see docs/CLI.md section 2.1):")
        lines.append("    " + " ".join(common[:7]))
        lines.append("    " + " ".join(common[7:]))
    return "\n".join(lines) + "\n"
