"""Reference parser of the `agent` output grammar (OUTPUT.md section 2.2 and 2.4).

``parse(text)`` returns a :class:`Parsed` (records in order) or raises :class:`GrammarError` naming the
offending line.  It is deliberately strict and independent of the reporters: it re-implements the grammar
from the specification, so the golden and end-to-end outputs are checked against the document rather
than against the code that produced them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

REASONS = {"added", "removed", "width", "constant", "parameter", "structure", "architecture", "sharing",
           "fusion", "register", "multiplicity", "blackbox", "renamed", "modified"}
VERDICTS = {"INCREASE", "DECREASE", "NEGLIGIBLE", "UNCERTAIN", "NO_CHANGE", "NOT_COMPARABLE"}
LEVELS = ("high", "medium", "low")

_S = r"(?:[+-][1-9]\d*|0)"      # signed integer: zero has no sign
_N = r"-?\d+"
_RE = {
    "DELTA": re.compile(rf"^DELTA ({_S}) GE \(({_S})\.\.({_S})\) (n/a|[+-]\d+\.\d%|0\.0%) verdict=([A-Z_]+) confidence=(high|medium|low)$"),
    "DELTA_NA": re.compile(r"^DELTA n/a verdict=([A-Z_]+) confidence=(high|medium|low)$"),
    "TOP_DIFF": re.compile(rf"^TOP (\S+) before=({_N}) after=({_N})$"),
    "TOP": re.compile(r"^TOP (\S+)$"),
    "OTHERS": re.compile(rf"^OTHERS (\d+) net=({_S})$"),
    "WHY": re.compile(r"^WHY (high|medium|low): (\S.*)$"),
    "WARN": re.compile(r"^WARN ([A-Z_]+) (\S+) (\S.*)$"),
    "ASSUMED": re.compile(r"^ASSUMED (\S.*)$"),
    "ERROR": re.compile(r"^ERROR ([A-Z_]+) (\S.*)$"),
    "CONTRIB": re.compile(rf"^({_S}) (\S+?):(\d+) ([a-z]+) (\S.*?)(?: x(\d+))?(?: cause=([a-z]+):(\S+))?$"),
    "MODULE": re.compile(r"^MODULE (\S+) x(\d+)->x(\d+) intrinsic=([+-]?\d+) multiplicity=([+-]?\d+)$"),
    "CATEGORIES": re.compile(r"^CATEGORIES( [a-z]+=[+-]?\d+)+$"),
    "STRUCT": re.compile(r"^STRUCT (\S.*)$"),
    "COST": re.compile(rf"^COST (\d+) GE \((\d+)\.\.(\d+)\) confidence=(high|medium|low)$"),
    "COST_MODULE": re.compile(r"^(\d+) (\S+)(?: x(\d+))? (\d+)%$"),
    "ESTIMATE": re.compile(rf"^ESTIMATE (\d+) GE \((\d+)\.\.(\d+)\) confidence=(high|medium|low)$"),
    "OP": re.compile(r"^OP (\S.*)$"),
    "ARCH": re.compile(r"^ARCH( (?:min|typ|max)=\S+)+$"),
    "BREAKDOWN": re.compile(r"^BREAKDOWN( \S+=\d+)*$"),
    "COMPARE": re.compile(r"^COMPARE (\d+) alternatives basis=(typ|max)$"),
    "ALT": re.compile(rf"^([A-Z]) (\d+) GE \((\d+)\.\.(\d+)\) (\S.*)$"),
    "BEST": re.compile(r"^BEST ([A-Z]) saves (\d+) GE vs ([A-Z]) ranges=(overlap|disjoint)$"),
    "SNAPSHOT": re.compile(r"^SNAPSHOT (?:saved id=[0-9a-f]{4,64} files=\d+ modules=\d+|none|deleted=\d+.*|[0-9a-f]{4,64} files=\d+ modules=\d+ created=\S+)$"),
}


class GrammarError(ValueError):
    pass


@dataclass
class Record:
    kind: str
    line: str
    groups: tuple = ()


@dataclass
class Parsed:
    records: list = field(default_factory=list)

    def of(self, kind: str) -> list:
        return [r for r in self.records if r.kind == kind]

    @property
    def head(self) -> Record:
        return self.records[0]

    def contributors(self) -> list:
        return self.of("CONTRIB")


def _classify(line: str, first: bool, mode: str) -> Record:
    order = ["DELTA", "DELTA_NA", "ERROR", "COST", "ESTIMATE", "COMPARE", "SNAPSHOT"] if first else [
        "TOP_DIFF", "TOP", "OTHERS", "WHY", "WARN", "ASSUMED", "MODULE", "CATEGORIES", "STRUCT", "OP", "ARCH",
        "BREAKDOWN", "BEST", "CONTRIB", "COST_MODULE", "ALT"]
    for k in order:
        m = _RE[k].match(line)
        if m:
            return Record(k, line, m.groups())
    raise GrammarError(f"line does not match the agent grammar: {line!r}")


def parse(text: str, *, max_lines: int | None = None) -> Parsed:
    if not text.endswith("\n"):
        raise GrammarError("output must end with a newline")
    if any(ord(c) > 126 or (ord(c) < 32 and c != "\n") for c in text):
        raise GrammarError("agent output must be printable ASCII")
    lines = text.split("\n")[:-1]
    if not lines:
        raise GrammarError("empty output")
    out = Parsed()
    for i, ln in enumerate(lines):
        if ln != ln.rstrip() or not ln:
            raise GrammarError(f"blank or trailing-space line {i + 1}: {ln!r}")
        out.records.append(_classify(ln, i == 0, ""))
    head = out.head
    kinds = [r.kind for r in out.records]
    if head.kind == "ERROR":
        if len(lines) != 1:
            raise GrammarError("an error is exactly one line")
        return out
    if head.kind in ("DELTA", "DELTA_NA"):
        _check_diff(out, kinds, max_lines)
    elif head.kind == "COST":
        if "TOP" not in kinds[1:2] and len(lines) > 1:
            raise GrammarError("COST must be followed by TOP")
    return out


_ORDER = {"TOP_DIFF": 1, "TOP": 1, "CONTRIB": 2, "OTHERS": 3, "MODULE": 4, "CATEGORIES": 5, "STRUCT": 6, "WHY": 7,
          "WARN": 8, "ASSUMED": 9}


def _check_diff(out: Parsed, kinds: list, max_lines) -> None:
    h = out.head
    if h.kind == "DELTA":
        d, lo, hi, _pct, verdict, _lvl = h.groups
        if verdict not in VERDICTS:
            raise GrammarError(f"unknown verdict {verdict}")
        if not int(lo) <= int(d) <= int(hi):
            raise GrammarError("range must contain the typical delta")
        if verdict == "NO_CHANGE" and (int(d), int(lo), int(hi)) != (0, 0, 0):
            raise GrammarError("NO_CHANGE needs a zero delta and range")
        if len(kinds) > 1 and kinds[1] != "TOP_DIFF":
            raise GrammarError("DELTA must be followed by TOP")
    elif h.groups[0] not in VERDICTS:
        raise GrammarError("unknown verdict")
    last = 0
    prev_abs = None
    for r in out.records[1:]:
        rank = _ORDER.get(r.kind)
        if rank is None:
            raise GrammarError(f"unexpected record in a diff: {r.line!r}")
        if rank < last:
            raise GrammarError(f"line out of order: {r.line!r}")
        last = rank
        if r.kind == "CONTRIB":
            if r.groups[3] not in REASONS:
                raise GrammarError(f"unknown reason {r.groups[3]}")
            a = abs(int(r.groups[0]))
            if prev_abs is not None and a > prev_abs + 1:   # rounding of sorted values may swap neighbours by one
                raise GrammarError("contributors must be sorted by absolute delta")
            prev_abs = a
    if max_lines is not None and kinds.count("CONTRIB") > max_lines:
        raise GrammarError("more contributor lines than --max-lines")
    warns = kinds.count("WARN")
    if warns > 6:
        raise GrammarError("at most 5 WARN lines plus one truncation line")
