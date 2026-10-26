"""The compact ASCII `agent` format (OUTPUT.md section 2)."""
from __future__ import annotations

from ..result import (
    CompareResult,
    CostResult,
    DiffResult,
    EstimateResult,
    MessageResult,
    Output,
    SnapshotResult,
)
from .fmt import ge, loc, note_lines, pct, rng, sge, warn_lines


def _why(conf) -> list[str]:
    if conf.level != "high" and conf.reasons:
        return [f"WHY {conf.level}: " + "; ".join(conf.reasons)]
    return []


def _contributor_line(c) -> str:
    parts = [sge(c.delta.typ), loc(c.file, c.line), c.reason, c.description]
    if c.instances > 1 or c.children:
        parts.append(f"x{c.instances}")
    if c.cause:
        parts.append(f"cause={c.cause.get('kind')}:{c.cause.get('name')}")
    return " ".join(parts)


def _diff(r: DiffResult, diags, max_lines: int, verbose: bool, quiet: bool) -> list[str]:
    if r.delta is None:
        head = f"DELTA n/a verdict={r.verdict} confidence={r.confidence.level}"
    else:
        head = (
            f"DELTA {sge(r.delta.typ)} GE {rng(r.delta)} {pct(r.percent)} "
            f"verdict={r.verdict} confidence={r.confidence.level}"
        )
    lines = [head]
    if quiet:
        return lines
    if r.before is not None and r.after is not None:
        lines.append(f"TOP {r.top} before={ge(r.before.typ)} after={ge(r.after.typ)}")
    shown = r.contributors[:max_lines]
    lines += [_contributor_line(c) for c in shown]
    hidden = r.contributors[max_lines:]
    others_count = r.others_count + len(hidden)
    if others_count:
        net = r.others_net.typ + sum(c.delta.typ for c in hidden)
        lines.append(f"OTHERS {others_count} net={sge(net)}")
    if verbose:
        lines += list(r.modules_verbose)
        if r.by_category:
            lines.append("CATEGORIES " + " ".join(f"{k}={sge(v)}" for k, v in r.by_category.items()))
        for s in r.structural:
            lines.append("STRUCT " + str(s.get("description", s)))
    lines += _why(r.confidence)
    lines += warn_lines(tuple(diags) + r.diagnostics_new)
    lines += [f"ASSUMED {a}" for a in r.assumed]
    lines += note_lines(tuple(diags) + r.diagnostics_new)
    return lines


def _cost(r: CostResult, diags, max_lines: int, quiet: bool) -> list[str]:
    lines = [f"COST {ge(r.total.typ)} GE {rng(r.total, False)} confidence={r.confidence.level}"]
    if quiet:
        return lines
    lines.append(f"TOP {r.top}")
    total = r.total.typ or 1
    for m in r.modules[:max_lines]:
        x = f" x{m.instances}" if m.instances > 1 else ""
        share = 100.0 * m.total.typ / total
        lines.append(f"{ge(m.total.typ)} {m.module}{x} {share:.0f}%")
    if r.by_category:
        lines.append("CATEGORIES " + " ".join(f"{k}={ge(v)}" for k, v in r.by_category.items()))
    lines += _why(r.confidence)
    lines += warn_lines(tuple(diags))
    lines += [f"ASSUMED {a}" for a in r.assumed]
    lines += note_lines(tuple(diags))
    return lines


def _estimate(r: EstimateResult, diags, quiet: bool) -> list[str]:
    lines = [f"ESTIMATE {ge(r.ge.typ)} GE {rng(r.ge, False)} confidence={r.confidence.level}"]
    if quiet:
        return lines
    lines.append(f"OP {r.normalized}")
    lines.append("ARCH " + " ".join(f"{k}={r.architecture[k]}" for k in ("min", "typ", "max") if k in r.architecture))
    lines.append("BREAKDOWN " + " ".join(f"{k}={v}" for k, v in r.breakdown.items()))
    lines += _why(r.confidence)
    lines += warn_lines(tuple(diags))
    lines += [f"ASSUMED {a}" for a in r.assumed]
    return lines


def _compare(r: CompareResult, diags) -> list[str]:
    lines = [f"COMPARE {len(r.alternatives)} alternatives basis={r.basis}"]
    for i, a in enumerate(r.alternatives):
        lines.append(f"{chr(65 + i)} {ge(a.ge.typ)} GE {rng(a.ge, False)} {a.text}")
    best, other = chr(65 + r.best_index), chr(65 + r.other_index)
    lines.append(
        f"BEST {best} saves {ge(r.saves.typ)} GE vs {other} ranges={'overlap' if r.overlap else 'disjoint'}"
    )
    lines += [f"ASSUMED {a}" for a in r.assumed]
    return lines


def render_agent(out: Output, *, max_lines: int = 10, verbose: bool = False, quiet: bool = False) -> str:
    if out.status == "error" and out.error:
        return f"ERROR {out.error[0]} {out.error[1]}\n"
    r = out.result
    if isinstance(r, DiffResult):
        lines = _diff(r, out.diagnostics, max_lines, verbose, quiet)
    elif isinstance(r, CostResult):
        lines = _cost(r, out.diagnostics, max_lines, quiet)
    elif isinstance(r, EstimateResult):
        lines = _estimate(r, out.diagnostics, quiet)
    elif isinstance(r, CompareResult):
        lines = _compare(r, out.diagnostics)
    elif isinstance(r, SnapshotResult):
        lines = [f"SNAPSHOT saved id={r.id} files={r.files} modules={r.modules}"]
    elif isinstance(r, MessageResult):
        lines = list(r.lines)
    else:
        lines = []
    return "\n".join(lines) + "\n"
