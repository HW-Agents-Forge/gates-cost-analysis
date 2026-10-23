"""Helpers for the end-to-end tests: throw-away git projects, in-process and launcher runs."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gatecost.cli import main

from tests.agent_grammar import parse

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "gatecost"
HAVE_GIT = shutil.which("git") is not None
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


class Project:
    """A temporary project directory, optionally a git repository."""

    def __init__(self, files: dict[str, str], git: bool = True):
        self._tmp = tempfile.TemporaryDirectory(prefix="gc_e2e_")
        self.dir = Path(self._tmp.name).resolve()
        self.cache = self.dir.parent / (self.dir.name + "_ucache")
        self.git = git
        for rel, text in files.items():
            self.write(rel, text)
        if git:
            self._git("init", "-q", "-b", "main")
            self.commit("initial")

    def close(self) -> None:
        self._tmp.cleanup()
        shutil.rmtree(self.cache, ignore_errors=True)

    # -- files
    def write(self, rel: str, text: str) -> None:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.dir / rel).read_text(encoding="utf-8")

    def edit(self, rel: str, old: str, new: str) -> None:
        s = self.read(rel)
        assert old in s, f"{old!r} not in {rel}"
        self.write(rel, s.replace(old, new))

    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.dir, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        assert r.returncode == 0, r.stderr
        return r.stdout

    def commit(self, msg: str = "c") -> None:
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg, "--allow-empty")

    def checkout_clean(self) -> None:
        self._git("checkout", "-q", ".")

    # -- running
    def env(self) -> dict:
        return {"GATECOST_CACHE_DIR": str(self.cache)}

    def run(self, *argv: str, env: dict | None = None) -> tuple[int, str]:
        out = io.StringIO()
        old = os.getcwd()
        try:
            os.chdir(self.dir)
            with mock.patch.dict(os.environ, {**self.env(), **(env or {})}), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                os.environ.pop("GATECOST_NO_CACHE", None)
                code = main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue()

    def diff(self, *argv: str) -> tuple[int, "object", str]:
        """Run `diff`, check the agent grammar, return (exit code, parsed output, raw text)."""
        code, text = self.run("diff", *argv)
        ml = None
        if "--max-lines" in argv:
            ml = int(argv[argv.index("--max-lines") + 1])
        return code, parse(text, max_lines=ml), text

    def diff_json(self, *argv: str) -> tuple[int, dict]:
        code, text = self.run("diff", "--format", "json", "--no-meta", *argv)
        return code, json.loads(text)

    def launcher(self, *argv: str, launcher: Path = LAUNCHER, env: dict | None = None, cwd: Path | None = None):
        e = {**os.environ, **GIT_ENV, **self.env(), **(env or {})}
        return subprocess.run([sys.executable, str(launcher), *argv], cwd=cwd or self.dir, capture_output=True,
                              text=True, env=e)


@unittest.skipUnless(HAVE_GIT, "git is required")
class GitCase(unittest.TestCase):
    FILES: dict[str, str] = {}

    def setUp(self) -> None:
        self.p = Project(self.FILES)
        self.addCleanup(self.p.close)


def head(parsed) -> tuple:
    return parsed.head.groups


def verdict(parsed) -> str:
    g = parsed.head.groups
    return g[4] if parsed.head.kind == "DELTA" else g[0]


def delta(parsed) -> int:
    return int(parsed.head.groups[0])
