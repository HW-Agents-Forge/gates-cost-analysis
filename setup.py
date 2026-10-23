"""Build hook: copy the repository-root data files (VERSION, schema/) into the package.

The repository is itself an agent skill and keeps these files at its root; ``pip install`` needs them
inside the package. Everything else is declared in pyproject.toml.
"""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


class BuildPy(build_py):
    def run(self):
        super().run()
        dest = Path(self.build_lib) / "gatecost" / "_data"
        (dest / "schema").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "VERSION", dest / "VERSION")
        for f in (ROOT / "schema").glob("*.json"):
            shutil.copy2(f, dest / "schema" / f.name)


setup(cmdclass={"build_py": BuildPy})
