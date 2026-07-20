"""Sphinx configuration for the PySnap project."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pysnap import __version__

project = "PySnap"
author = "PySnap Authors"
copyright = "2026, PySnap Authors"
release = __version__

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
