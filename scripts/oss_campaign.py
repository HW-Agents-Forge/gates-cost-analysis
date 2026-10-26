#!/usr/bin/env python3
"""Reproducible open-source validation campaign for gatecost (maintainers only, not part of the CI).

Pinned projects (URL + exact commit) live in ``scripts/oss/projects.json``; the before/after edits replayed by
``deltas`` live in ``scripts/oss/deltas.json`` (declarative: literal substitutions, no code execution). Clones are
kept outside the repository, under ``$GATECOST_OSS_DIR`` (default ``~/gatecost-refs/oss``) in ``<dir>/repo``.

Sub-commands::

    python3 scripts/oss_campaign.py fetch  [--only NAME ...]       # git clone + checkout of the pinned commit
    python3 scripts/oss_campaign.py smoke  [--only NAME ...] [--timeout S] [--out DIR]
    python3 scripts/oss_campaign.py deltas [--only SUBSTR ...] [--timeout S] [--out DIR] [--no-truth]
    python3 scripts/oss_campaign.py summary [--out DIR]            # regenerate the .md/.csv from the .json files

* ``fetch`` only runs ``git`` (no hook, no submodule, no code of the project is ever executed).
* ``smoke`` runs ``gatecost cost`` on the listed tops with their include dirs, filelists and parameters and reports
  crashes, ``INTERNAL`` errors/diagnostics, timeouts, time, peak memory and diagnostic counts.
* ``deltas`` builds before/after trees from the pinned sources, applies the declarative edits, runs ``gatecost diff``
  and, when Yosys (and GHDL for VHDL) are installed with the Nangate45 liberty file, the same edit through
  ``yosys_area.py`` as ground truth; ``in_range`` tells whether the truth delta lies in the reported [min, max].

Outputs (``--out``, default ``$GATECOST_OSS_DIR/_out``): ``smoke.json|md|csv`` and ``deltas.json|md|csv``. Rows are
sorted by name; ``--no-timing`` drops the time and memory columns so the summaries are byte-reproducible.
Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import yosys_area as ya  # noqa: E402

PROJECTS_FILE = HERE / "oss" / "projects.json"
DELTAS_FILE = HERE / "oss" / "deltas.json"
GATECOST = REPO_ROOT / "scripts" / "gatecost"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NOISE_DIAGS = {"IGNORED_REGION", "UNOBSERVABLE_LOGIC", "CONSTANT_REGISTER"}


class CampaignError(RuntimeError):
    pass


# --------------------------------------------------------------------------- data files

def oss_dir() -> Path:
    return Path(os.environ.get("GATECOST_OSS_DIR") or (Path.home() / "gatecost-refs" / "oss"))


def load_projects(path: Path = PROJECTS_FILE) -> list[dict]:
    """Validated project list (name, https URL, 40-hex commit, smoke targets)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out, names = [], set()
    for p in data["projects"]:
        for key in ("name", "url", "commit", "lang", "smoke"):
            if key not in p:
                raise CampaignError(f"project {p.get('name', '?')}: missing key {key!r}")
        if not str(p["url"]).startswith("https://"):
            raise CampaignError(f"project {p['name']}: URL must be https")
        if not _SHA_RE.match(p["commit"]):
            raise CampaignError(f"project {p['name']}: commit must be a full 40-hex SHA")
        if p["name"] in names:
            raise CampaignError(f"duplicate project {p['name']}")
        names.add(p["name"])
        p.setdefault("dir", p["name"])
        out.append(p)
    return out


def load_deltas(path: Path = DELTAS_FILE) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = set()
    for d in data["deltas"]:
        for key in ("name", "project", "srcsub", "files", "top", "lang", "edit"):
            if key not in d:
                raise CampaignError(f"delta {d.get('name', '?')}: missing key {key!r}")
        if d["name"] in names:
            raise CampaignError(f"duplicate delta {d['name']}")
        names.add(d["name"])
    return data["deltas"]


def repo_dir(project: dict, base: Path | None = None) -> Path:
    return (base or oss_dir()) / project["dir"] / "repo"


def select(items: list[dict], only: list[str] | None, exact: bool = True) -> list[dict]:
    if not only:
        return items
    return [i for i in items if any((o == i["name"]) if exact else (o in i["name"]) for o in only)]


# --------------------------------------------------------------------------- fetch

def fetch_commands(project: dict, repo: Path) -> list[list[str]]:
    """git commands bringing ``repo`` to the pinned commit (clone shallow, fetch that commit, detached checkout)."""
    g = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.version=2"]
    return [
        g + ["clone", "--depth", "1", "--no-checkout", "--no-tags", project["url"], str(repo)],
        g + ["-C", str(repo), "fetch", "--depth", "1", "origin", project["commit"]],
        g + ["-C", str(repo), "-c", "advice.detachedHead=false", "checkout", "--detach", project["commit"]],
    ]


def _git_head(repo: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_fetch(args) -> int:
    rc = 0
    for p in select(load_projects(), args.only):
        repo = repo_dir(p)
        if (repo / ".git").exists():
            if _git_head(repo) == p["commit"]:
                print(f"{p['name']}: already at {p['commit'][:8]}")
                continue
            cmds = fetch_commands(p, repo)[1:]  # existing clone: only move to the pinned commit
        else:
            repo.parent.mkdir(parents=True, exist_ok=True)
            cmds = fetch_commands(p, repo)
        for c in cmds:
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode:
                print(f"{p['name']}: FAILED {' '.join(c[-3:])}: {r.stderr.strip()[-200:]}", file=sys.stderr)
                rc = 1
                break
        else:
            print(f"{p['name']}: {p['commit'][:8]} ok")
    return rc


# --------------------------------------------------------------------------- smoke

def resolve_target(project: dict, target: dict, repo: Path) -> tuple[list[str], list[str]]:
    """(source arguments, incdirs) of a smoke target, as absolute paths. A ``filelist`` (a file of this repository,
    one repo-relative path per line, ``+incdir+DIR`` for include dirs) is expanded."""
    sources = [str(repo / s) for s in target.get("sources", [])]
    incdirs = [str(repo / i) for i in target.get("incdirs", [])]
    if "filelist" in target:
        base = repo / target.get("filelist_base", "")
        for raw in (REPO_ROOT / target["filelist"]).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("+incdir+"):
                incdirs += [str(base / x) for x in line[8:].split("+") if x]
            else:
                sources.append(str(base / line))
    return sources, incdirs


def cost_command(project: dict, target: dict, repo: Path, gatecost: Path = GATECOST) -> list[str]:
    sources, incdirs = resolve_target(project, target, repo)
    cmd = [sys.executable, str(gatecost), "cost", "--format", "json", "--no-cache", "--top", target["top"]]
    if "root" in target:
        cmd += ["--root", str(repo / target["root"])]
    for i in incdirs:
        cmd += ["--incdir", i]
    for p in target.get("params", []):
        cmd += ["--param", p]
    for k, v in sorted(target.get("defines", {}).items()):
        cmd += ["--define", f"{k}={v}" if v is not None else k]
    return cmd + sources


def run_measured(cmd: list[str], timeout: float, env: dict | None = None) -> dict:
    """Run ``cmd``; returns rc, stdout, stderr, seconds, peak RSS (MB, this child only) or ``timeout``: True."""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        t0 = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, env=env)
        rss, status, timed_out = None, 0, False
        while True:
            pid, st, ru = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                status, rss = st, ru.ru_maxrss
                break
            if time.monotonic() - t0 > timeout:
                proc.kill()
                _, status, ru = os.wait4(proc.pid, 0)
                timed_out = True
                break
            time.sleep(0.02)
        secs = time.monotonic() - t0
        fo.seek(0)
        fe.seek(0)
        out, err = fo.read().decode("utf-8", "replace"), fe.read().decode("utf-8", "replace")
    proc.returncode = 0  # reaped by wait4
    rc = os.waitstatus_to_exitcode(status)
    if rss is not None:
        rss = rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    return {"rc": rc, "stdout": out, "stderr": err, "seconds": round(secs, 2), "rss_mb": round(rss, 1) if rss is not None else None,
            "timeout": timed_out}


def classify_cost(run: dict) -> dict:
    """Smoke record fields from one ``gatecost cost --format json`` run."""
    rec: dict = {"rc": run["rc"], "seconds": run["seconds"], "rss_mb": run["rss_mb"]}
    if run["timeout"]:
        rec["status"] = "timeout"
        return rec
    if "Traceback" in run["stderr"]:
        rec["status"] = "crash"
        rec["detail"] = run["stderr"].strip().splitlines()[-1][:200]
        return rec
    try:
        d = json.loads(run["stdout"])
    except ValueError:
        rec["status"] = "crash"
        rec["detail"] = ("unparseable output: " + (run["stderr"] or run["stdout"]).strip()[-160:])
        return rec
    diags = collections.Counter(x.get("code", "?") for x in d.get("diagnostics", []))
    rec["diagnostics"] = dict(sorted(diags.items()))
    if d.get("status") == "error":
        err = d.get("error") or {}
        rec["status"] = "internal" if err.get("code") == "INTERNAL" else "error"
        rec["detail"] = f"{err.get('code')}: {str(err.get('message', ''))[:160]}"
        return rec
    rec["status"] = "internal" if diags.get("INTERNAL") else "ok"
    res = d.get("result", {})
    ge = res.get("total", {}).get("ge", {})
    rec.update(min_ge=round(ge.get("min", 0), 1), typ_ge=round(ge.get("typ", 0), 1), max_ge=round(ge.get("max", 0), 1),
               confidence=res.get("confidence", {}).get("level"))
    return rec


def cmd_smoke(args) -> int:
    rows = []
    for p in select(load_projects(), args.only):
        repo = repo_dir(p)
        for t in p["smoke"]:
            row = {"project": p["name"], "top": t["top"], "commit": p["commit"][:8]}
            if not repo.is_dir():
                row.update(status="missing", detail="run 'fetch' first")
            else:
                row.update(classify_cost(run_measured(cost_command(p, t, repo, Path(args.gatecost)), args.timeout)))
            print(f"{row['project']}/{row['top']}: {row['status']}" + (f" {row.get('typ_ge')} GE {row.get('seconds')}s" if row["status"] == "ok" else ""))
            sys.stdout.flush()
            rows.append(row)
    out = Path(args.out or oss_dir() / "_out")
    out.mkdir(parents=True, exist_ok=True)
    write_outputs(out, "smoke", rows, args.no_timing)
    bad = [r for r in rows if r["status"] in ("crash", "internal", "timeout")]
    return 1 if bad else 0


# --------------------------------------------------------------------------- deltas

def apply_ops(text: str, ops: list[dict]) -> str:
    """Apply declarative edits. ``replace``: [old, new], ``count`` 1 (first) or 0 (all); ``insert_before_last``:
    marker, ``code``. A pattern that is not found raises (a stale edit must be loud)."""
    for op in ops:
        if "replace" in op:
            old, new = op["replace"]
            if old not in text:
                raise CampaignError("pattern not found: " + old[:60])
            text = text.replace(old, new) if op.get("count", 1) == 0 else text.replace(old, new, 1)
        elif "insert_before_last" in op:
            i = text.rfind(op["insert_before_last"])
            if i < 0:
                raise CampaignError("marker not found: " + op["insert_before_last"])
            text = text[:i] + op["code"] + "\n" + text[i:]
        else:
            raise CampaignError(f"unknown edit operation {sorted(op)}")
    return text


def delta_sources(delta: dict, project_repo: Path) -> list[Path]:
    src = project_repo / delta["srcsub"]
    out: list[Path] = []
    for f in delta["files"]:
        out += sorted(src.glob(f)) if any(c in f for c in "*?[") else [src / f]
    return out


def build_trees(delta: dict, repo: Path, work: Path) -> tuple[Path, Path]:
    """Copy the sources twice (``before``, ``after``), apply ``pre`` to both and ``edit`` to ``after`` only."""
    base = work / delta["name"]
    shutil.rmtree(base, ignore_errors=True)
    srcs = delta_sources(delta, repo)
    for side in ("before", "after"):
        (base / side).mkdir(parents=True)
        for s in srcs:
            shutil.copy(s, base / side / s.name)
    for fname, ops in delta.get("pre", {}).items():
        for side in ("before", "after"):
            p = base / side / fname
            p.write_text(apply_ops(p.read_text(encoding="utf-8"), ops), encoding="utf-8")
    for fname, ops in delta["edit"].items():
        p = base / "after" / fname
        old = p.read_text(encoding="utf-8")
        new = apply_ops(old, ops)
        if new == old:
            raise CampaignError(f"edit of {fname} changes nothing")
        p.write_text(new, encoding="utf-8")
    return base / "before", base / "after"


def diff_command(delta: dict, before: Path, after: Path, gatecost: Path = GATECOST) -> list[str]:
    cmd = [sys.executable, str(gatecost), "diff", "--no-cache", "--format", "json", "--top", delta["top"],
           "--before", str(before), "--after", str(after)]
    for p in delta.get("params", []):
        cmd += ["--param", p]
    return cmd


def parse_diff(run: dict) -> dict:
    """Delta record fields from one ``gatecost diff --format json`` run."""
    if run["timeout"]:
        return {"status": "timeout"}
    if "Traceback" in run["stderr"]:
        return {"status": "crash", "detail": run["stderr"].strip().splitlines()[-1][:200]}
    try:
        d = json.loads(run["stdout"])
    except ValueError:
        return {"status": "crash", "detail": "unparseable output"}
    if d.get("status") == "error":
        err = d.get("error") or {}
        return {"status": "internal" if err.get("code") == "INTERNAL" else "error", "detail": f"{err.get('code')}: {str(err.get('message', ''))[:160]}"}
    r = d["result"]
    dl = r["delta"]["ge"]
    return {"status": "ok", "verdict": r["verdict"], "min": round(dl["min"], 1), "typ": round(dl["typ"], 1), "max": round(dl["max"], 1),
            "before_typ": round(r["before"]["ge"]["typ"]), "after_typ": round(r["after"]["ge"]["typ"]),
            "confidence": r["confidence"]["level"]}


def add_truth(row: dict, ys_before, ys_after) -> None:
    """Add the Yosys truth columns (GE floats or error strings) and the comparison to ``row``."""
    row["ys_before"], row["ys_after"] = ys_before, ys_after
    if isinstance(ys_before, (int, float)) and isinstance(ys_after, (int, float)) and "typ" in row:
        yd = round(ys_after - ys_before, 1)
        row["ys_delta"] = yd
        row["in_range"] = row["min"] <= yd <= row["max"]
        row["ratio"] = round(row["typ"] / yd, 2) if abs(yd) > 0.5 else None


def vhdl_ge(files: list[Path], top: str, generics: list[str], work: str = "work", timeout: float = 900.0) -> float:
    """Ground truth for VHDL: ``ghdl --synth --out=verilog`` then the usual Yosys flow (needs ghdl)."""
    ghdl = shutil.which("ghdl")
    if not ghdl:
        raise ya.YosysError("ghdl not installed")
    with tempfile.TemporaryDirectory(prefix="gc_ghdl_") as t:
        std = ["--std=08", "-frelaxed", f"--work={work}"]
        pending, failed, last = list(files), [], None
        for _ in range(8):  # files may be given in any order: retry the ones that fail until a fixpoint
            failed = []
            for f in pending:
                r = subprocess.run([ghdl, "-a", *std, str(f)], cwd=t, capture_output=True, text=True)
                if r.returncode:
                    failed.append(f)
                    last = r
            if not failed or len(failed) == len(pending):
                break
            pending = failed
        if failed:
            raise ya.YosysError("ghdl -a: " + ((last.stderr or last.stdout)[:200] if last else "?"))
        r = subprocess.run([ghdl, "--synth", *std, "--out=verilog", *[f"-g{g}" for g in generics], top], cwd=t, capture_output=True, text=True)
        if r.returncode:
            raise ya.YosysError("ghdl --synth: " + (r.stderr or r.stdout)[:200])
        out = Path(t) / "o.v"
        out.write_text(r.stdout, encoding="utf-8")
        return ya.synthesize([out], top, timeout=timeout).ge


def truth_ge(delta: dict, tree: Path, repo: Path) -> float | str:
    try:
        if delta["lang"] == "vhdl":
            lf = REPO_ROOT / "scripts" / "oss" / "filelists" / f"{delta['project']}.f"
            names = [ln.strip() for ln in lf.read_text(encoding="utf-8").splitlines() if ln.strip()] if lf.is_file() else None
            files = [tree / n for n in names] if names else sorted(tree.glob("*.vhd"))
            return round(vhdl_ge(files, delta["top"], delta.get("params", []), delta.get("vhdl_work", "work")), 1)
        files = [tree / n for n in delta["files"]]
        return round(ya.synthesize(files, delta["top"], params=delta.get("params", [])).ge, 1)
    except ya.YosysError as exc:
        return "FAIL " + str(exc).splitlines()[0][:150]


def truth_available(lang: str) -> bool:
    return bool(shutil.which("yosys") and ya.liberty_path().is_file() and (lang != "vhdl" or shutil.which("ghdl")))


def cmd_deltas(args) -> int:
    projects = {p["name"]: p for p in load_projects()}
    out = Path(args.out or oss_dir() / "_out")
    work = out / "trees"
    rows = []
    for d in select(load_deltas(), args.only, exact=False):
        row = {"name": d["name"], "project": d["project"], "top": d["top"]}
        repo = repo_dir(projects[d["project"]])
        try:
            if not repo.is_dir():
                raise CampaignError("project not fetched (run 'fetch')")
            before, after = build_trees(d, repo, work)
            row.update(parse_diff(run_measured(diff_command(d, before, after, Path(args.gatecost)), args.timeout)))
            if not args.no_truth and truth_available(d["lang"]):
                add_truth(row, truth_ge(d, before, repo), truth_ge(d, after, repo))
        except CampaignError as exc:
            row.update(status="edit-failed", detail=str(exc))
        print(json.dumps(row, sort_keys=True))
        sys.stdout.flush()
        rows.append(row)
    out.mkdir(parents=True, exist_ok=True)
    write_outputs(out, "deltas", rows, args.no_timing)
    return 1 if any(r["status"] in ("crash", "internal", "timeout", "edit-failed") for r in rows) else 0


# --------------------------------------------------------------------------- summaries

SMOKE_COLS = ["project", "top", "status", "confidence", "min_ge", "typ_ge", "max_ge", "seconds", "rss_mb", "diagnostics", "detail"]
DELTA_COLS = ["name", "status", "verdict", "min", "typ", "max", "confidence", "ys_delta", "in_range", "ratio", "detail"]
TIMING = {"seconds", "rss_mb"}


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return " ".join(f"{k}={n}" for k, n in sorted(v.items()) if k not in NOISE_DIAGS)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def render(kind: str, rows: list[dict], timing: bool = True) -> tuple[str, str]:
    """(Markdown, CSV) of the rows, sorted by name, deterministic for given data."""
    cols = SMOKE_COLS if kind == "smoke" else DELTA_COLS
    if not timing:
        cols = [c for c in cols if c not in TIMING]
    key = (lambda r: (r["project"], r["top"])) if kind == "smoke" else (lambda r: r["name"])
    rows = sorted(rows, key=key)
    md = io.StringIO()
    md.write(f"# gatecost open-source campaign: {kind}\n\n")
    counts = collections.Counter(r.get("status", "?") for r in rows)
    md.write("Status: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + "\n")
    if kind == "deltas":
        cmp_rows = [r for r in rows if "in_range" in r]
        if cmp_rows:
            md.write(f"Truth comparison: {sum(1 for r in cmp_rows if r['in_range'])}/{len(cmp_rows)} Yosys deltas inside the reported range\n")
    md.write("\n| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n")
    for r in rows:
        md.write("| " + " | ".join(_cell(r.get(c)).replace("|", "/") for c in cols) + " |\n")
    cs = io.StringIO()
    w = csv.writer(cs, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return md.getvalue(), cs.getvalue()


def write_outputs(out: Path, kind: str, rows: list[dict], no_timing: bool = False) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{kind}.json").write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    md, cs = render(kind, rows, not no_timing)
    (out / f"{kind}.md").write_text(md, encoding="utf-8")
    (out / f"{kind}.csv").write_text(cs, encoding="utf-8")
    print(f"wrote {out}/{kind}.json|md|csv")


def cmd_summary(args) -> int:
    out = Path(args.out or oss_dir() / "_out")
    found = False
    for kind in ("smoke", "deltas"):
        f = out / f"{kind}.json"
        if f.is_file():
            write_outputs(out, kind, json.loads(f.read_text(encoding="utf-8")), args.no_timing)
            found = True
    if not found:
        print(f"no smoke.json/deltas.json in {out}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("fetch", cmd_fetch, "clone/checkout the pinned commits"),
                               ("smoke", cmd_smoke, "gatecost cost on the listed tops"),
                               ("deltas", cmd_deltas, "before/after edits: gatecost diff vs Yosys"),
                               ("summary", cmd_summary, "regenerate .md/.csv from the .json files")):
        sp = sub.add_parser(name, help=helptext)
        sp.set_defaults(fn=fn)
        if name in ("fetch", "smoke", "deltas"):
            sp.add_argument("--only", nargs="+", metavar="NAME", help="project names (deltas: name substrings)")
        if name != "fetch":
            sp.add_argument("--out", help="output directory (default $GATECOST_OSS_DIR/_out)")
            sp.add_argument("--no-timing", action="store_true", help="omit time/memory columns (reproducible summaries)")
        if name in ("smoke", "deltas"):
            sp.add_argument("--timeout", type=float, default=600.0)
            sp.add_argument("--gatecost", default=str(GATECOST))
        if name == "deltas":
            sp.add_argument("--no-truth", action="store_true", help="skip Yosys/GHDL ground truth")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except CampaignError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
