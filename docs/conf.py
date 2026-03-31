"""Sphinx configuration for the PySnap project."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

project = "PySnap"
author = "PySnap Authors"
copyright = "2026, PySnap Authors"
release = "0.1.0"

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
autodoc_type_aliases = {
    "SessionRegistry": "pysnap.runtime.sessions.SessionRegistry",
}

graphviz_dot = shutil.which("dot") or "dot"
graphviz_output_format = "svg"
graphviz_enabled = shutil.which("dot") is not None
if graphviz_enabled:
    tags.add("graphviz")

html_theme = "alabaster"
html_static_path = ["_static"]
