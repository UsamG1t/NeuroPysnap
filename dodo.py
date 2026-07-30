"""Project automation tasks powered by doit."""

from __future__ import annotations

from pathlib import Path
import shutil

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python < 3.11
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent
PROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]
VENV_BIN = ROOT / ".pysnap" / "bin"
PYTHON = str(VENV_BIN / "python")
SPHINX_APIDOC = str(VENV_BIN / "sphinx-apidoc")
SPHINX_BUILD = str(VENV_BIN / "sphinx-build")

DOCS_DIR = ROOT / "docs"
DOCS_API_DIR = DOCS_DIR / "api"
DOCS_BUILD_DIR = DOCS_DIR / "_build"
PACKAGED_DOCS_DIR = ROOT / "pysnap" / "docs"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
EGG_INFO_DIR = ROOT / "pysnap.egg-info"
DOIT_DB = ROOT / ".doit.db"
SPHINX_GRAPHVIZ_TAG = ["-t", "graphviz"] if shutil.which("dot") else []

DOIT_CONFIG = {
    "default_tasks": ["test", "docs", "wheel"],
}


def _package_source_files() -> list[str]:
    """Collect the package sources that invalidate generated artifacts.

    Globbing keeps newly added modules covered without editing this file.

    :returns: Repository-relative paths of the package sources.
    """

    return sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "pysnap").rglob("*.py")
        if "tests" not in path.relative_to(ROOT).parts
    )


def _documentation_source_files() -> list[str]:
    """Collect the hand-written and generated documentation sources.

    The generated ``docs/api`` stubs are included so that a rebuilt API tree
    also triggers a documentation rebuild.

    :returns: Repository-relative paths of the documentation sources.
    """

    sources = [ROOT / "README.md", DOCS_DIR / "conf.py"]
    sources.extend(sorted(DOCS_DIR.glob("*.rst")))
    sources.extend(sorted(DOCS_API_DIR.glob("*.rst")))
    return [str(path.relative_to(ROOT)) for path in sources if path.is_file()]


def _remove_generated_artifacts() -> None:
    """Delete generated artifacts while preserving tracked documentation sources."""

    for path in (
        DOCS_BUILD_DIR,
        DIST_DIR,
        BUILD_DIR,
        EGG_INFO_DIR,
        PACKAGED_DOCS_DIR,
    ):
        if path.exists():
            shutil.rmtree(path)

    for pycache_dir in ROOT.rglob("__pycache__"):
        if pycache_dir.is_dir():
            shutil.rmtree(pycache_dir)

    for compiled_file in ROOT.rglob("*.py[cod]"):
        if compiled_file.is_file():
            compiled_file.unlink()

    if DOIT_DB.exists():
        DOIT_DB.unlink()

    PACKAGED_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (PACKAGED_DOCS_DIR / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")


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
        "file_dep": _package_source_files(),
        "targets": [str(DOCS_API_DIR / "modules.rst")],
        "verbosity": 2,
    }


def task_docs() -> dict:
    """Build the HTML documentation."""

    return {
        "actions": [
            [
                SPHINX_BUILD,
                "-W",
                *SPHINX_GRAPHVIZ_TAG,
                "-b",
                "html",
                str(DOCS_DIR),
                str(DOCS_BUILD_DIR / "html"),
            ]
        ],
        "file_dep": _documentation_source_files(),
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
        "file_dep": ["pyproject.toml", "README.md", *_package_source_files()],
        "task_dep": ["package_docs"],
        "targets": [str(DIST_DIR / f"pysnap-{PROJECT_VERSION}-py3-none-any.whl")],
        "verbosity": 2,
    }


def task_package_docs() -> dict:
    """Copy compiled HTML documentation into the package tree."""

    def sync_packaged_docs() -> None:
        """Replace packaged documentation with the latest compiled HTML tree."""
        PACKAGED_DOCS_DIR.mkdir(parents=True, exist_ok=True)
        for path in PACKAGED_DOCS_DIR.iterdir():
            if path.name == ".gitignore":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        shutil.copytree(DOCS_BUILD_DIR / "html", PACKAGED_DOCS_DIR, dirs_exist_ok=True)
        (PACKAGED_DOCS_DIR / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")

    return {
        "actions": [sync_packaged_docs],
        "task_dep": ["docs"],
        "file_dep": [str(DOCS_BUILD_DIR / "html" / "index.html")],
        "targets": [str(PACKAGED_DOCS_DIR / "index.html")],
        "verbosity": 2,
    }


def task_erase() -> dict:
    """Remove generated build, documentation, and packaging artifacts."""

    return {
        "actions": [_remove_generated_artifacts],
        "verbosity": 2,
    }


def task_cleanup() -> dict:
    """Backward-compatible alias for ``doit erase``."""

    return {
        "actions": [_remove_generated_artifacts],
        "verbosity": 2,
    }
