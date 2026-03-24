"""Project automation tasks powered by doit."""

from __future__ import annotations

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent
VENV_BIN = ROOT / ".pysnap" / "bin"
PYTHON = str(VENV_BIN / "python")
SPHINX_APIDOC = str(VENV_BIN / "sphinx-apidoc")
SPHINX_BUILD = str(VENV_BIN / "sphinx-build")

DOCS_DIR = ROOT / "docs"
DOCS_API_DIR = DOCS_DIR / "api"
DOCS_BUILD_DIR = DOCS_DIR / "_build"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
EGG_INFO_DIR = ROOT / "pysnap.egg-info"
DOIT_DB = ROOT / ".doit.db"
ROOT_PYCACHE_DIR = ROOT / "__pycache__"

DOIT_CONFIG = {
    "default_tasks": ["test", "docs", "wheel"],
}


def task_test() -> dict:
    """Run the unit test suite."""

    return {
        "actions": [f"{PYTHON} -m unittest discover -s pysnap/tests -t ."],
        "verbosity": 2,
    }


def task_apidoc() -> dict:
    """Generate Sphinx API stubs from the source package."""

    return {
        "actions": [
            f"{SPHINX_APIDOC} --force --module-first -o {DOCS_API_DIR} pysnap pysnap/tests"
        ],
        "file_dep": [
            "pysnap/__init__.py",
            "pysnap/cli/app.py",
            "pysnap/core/service.py",
            "pysnap/vbox/client.py",
        ],
        "targets": [str(DOCS_API_DIR / "modules.rst")],
        "verbosity": 2,
    }


def task_docs() -> dict:
    """Build the HTML documentation."""

    return {
        "actions": [
            f"{SPHINX_BUILD} -W -b html {DOCS_DIR} {DOCS_BUILD_DIR / 'html'}"
        ],
        "file_dep": [
            "docs/conf.py",
            "docs/index.rst",
            "docs/usage.rst",
            "docs/architecture.rst",
            "README.md",
        ],
        "task_dep": ["apidoc"],
        "targets": [str(DOCS_BUILD_DIR / "html" / "index.html")],
        "verbosity": 2,
    }


def task_wheel() -> dict:
    """Build a wheel distribution for the project."""

    return {
        "actions": [
            [PYTHON, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(DIST_DIR)],
        ],
        "file_dep": [
            "pyproject.toml",
            "README.md",
            "pysnap/__init__.py",
            "pysnap/cli/app.py",
            "pysnap/core/service.py",
            "pysnap/vbox/client.py",
        ],
        "targets": [str(DIST_DIR / "pysnap-0.1.0-py3-none-any.whl")],
        "verbosity": 2,
    }


def task_cleanup() -> dict:
    """Remove generated build, documentation, and packaging artifacts."""

    def clean() -> None:
        """Delete generated artifacts from previous runs."""

        for path in (DOCS_BUILD_DIR, DOCS_API_DIR, DIST_DIR, BUILD_DIR, EGG_INFO_DIR):
            if path.exists():
                shutil.rmtree(path)
        for path in (ROOT_PYCACHE_DIR,):
            if path.exists():
                shutil.rmtree(path)
        if DOIT_DB.exists():
            DOIT_DB.unlink()
        DOCS_API_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_API_DIR / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")

    return {
        "actions": [clean],
        "verbosity": 2,
    }
