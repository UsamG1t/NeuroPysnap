"""Helpers for opening bundled HTML documentation."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from pysnap.errors import PySnapError


DEFAULT_BROWSER = "firefox"
DOCUMENTATION_BROWSER_ERROR = (
    "Unable to open the bundled documentation. "
    "Please specify a browser executable with --browser=BROWSER."
)

PACKAGE_DOCS_DIR = Path(__file__).resolve().parent / "docs"
REPO_DOCS_BUILD_DIR = Path(__file__).resolve().parent.parent / "docs" / "_build" / "html"


def documentation_index_path() -> Path:
    """Return the bundled documentation index page path.

    The installed wheel stores compiled HTML files inside ``pysnap/docs``.
    During development, PySnap also accepts the Sphinx build directory under
    ``docs/_build/html`` as a fallback.

    :returns: Path to the ``index.html`` entry point.
    :raises PySnapError: If no compiled HTML documentation is available.
    """
    for candidate in (
        PACKAGE_DOCS_DIR / "index.html",
        REPO_DOCS_BUILD_DIR / "index.html",
    ):
        if candidate.is_file():
            return candidate
    raise PySnapError(
        "Bundled documentation was not found. Build the documentation before packaging."
    )


def open_bundled_documentation(browser: str | None = None) -> Path:
    """Launch a browser that opens the bundled HTML documentation.

    :param browser: Optional browser executable. When omitted, ``firefox`` is used.
    :returns: Path to the opened documentation index page.
    :raises PySnapError: If the documentation or browser executable is unavailable.
    """
    index_path = documentation_index_path()
    executable = _resolve_browser(browser)
    try:
        subprocess.Popen([executable, index_path.as_uri()])
    except OSError as error:
        raise PySnapError(DOCUMENTATION_BROWSER_ERROR) from error
    return index_path


def _resolve_browser(browser: str | None) -> str:
    """Resolve the browser executable used for documentation viewing.

    :param browser: Optional browser override.
    :returns: Browser executable or path.
    :raises PySnapError: If the default browser is unavailable.
    """
    if browser:
        return browser
    resolved = shutil.which(DEFAULT_BROWSER)
    if resolved is None:
        raise PySnapError(DOCUMENTATION_BROWSER_ERROR)
    return resolved
