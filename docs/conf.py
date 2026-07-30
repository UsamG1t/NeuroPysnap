"""Sphinx configuration for the PySnap project."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pysnap import __version__

project = "PySnap"
author = "PySnap Authors"
copyright = "2026, PySnap Authors"
release = __version__

# Download links for the ``Downloads`` section on the documentation index.
# The wheel file name is computed from the distribution name in
# ``pyproject.toml`` and the current release, following the default
# ``python -m build --wheel`` naming rules for a pure-Python package:
# the escaped distribution name plus the ``py3-none-any`` tags.
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

with (ROOT / "pyproject.toml").open("rb") as _pyproject_file:
    _distribution_name = tomllib.load(_pyproject_file)["project"]["name"]

REPOSITORY_URL = "https://github.com/UsamG1t/NeuroPysnap"
REPOSITORY_BRANCH = "main"
_wheel_file_name = (
    f"{re.sub(r'[-_.]+', '_', _distribution_name).lower()}"
    f"-{release}-py3-none-any.whl"
)
_latest_wheel_url = (
    f"{REPOSITORY_URL}/raw/{REPOSITORY_BRANCH}/dist/{_wheel_file_name}"
)
_dist_directory_url = f"{REPOSITORY_URL}/tree/{REPOSITORY_BRANCH}/dist"

# ``rst_epilog`` is appended to every source file, so the named hyperlink
# targets below are available where the index page references them.
rst_epilog = f"""
.. _latest version: {_latest_wheel_url}
.. _other versions: {_dist_directory_url}
"""

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.graphviz",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
language = "en"

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_mock_imports = [
    "prompt_toolkit",
    "pyte",
]
autodoc_type_aliases = {
    "SessionRegistry": "pysnap.runtime.sessions.SessionRegistry",
}

# ``viewcode`` follows imported members by default and therefore also renders
# source pages for standard-library modules such as ``pathlib`` and ``re``.
# Restricting it to locally defined objects keeps ``_modules`` limited to the
# PySnap sources.
viewcode_follow_imported_members = False

graphviz_dot = shutil.which("dot") or "dot"
graphviz_output_format = "svg"
graphviz_enabled = shutil.which("dot") is not None
if graphviz_enabled:
    tags.add("graphviz")

html_theme = "alabaster"
html_static_path = ["_static"]
