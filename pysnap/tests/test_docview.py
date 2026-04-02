"""Unit tests for bundled documentation helpers."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pysnap.docview import DOCUMENTATION_BROWSER_ERROR, open_bundled_documentation
from pysnap.errors import PySnapError


class DocumentationViewTests(unittest.TestCase):
    """Verify bundled documentation lookup and browser launching."""

    def test_open_bundled_documentation_uses_default_firefox(self) -> None:
        """Launch the packaged docs in the default browser when available."""
        with tempfile.TemporaryDirectory() as temp_dir:
            docs_dir = Path(temp_dir)
            index_path = docs_dir / "index.html"
            index_path.write_text("<html></html>", encoding="utf-8")

            with (
                patch("pysnap.docview.PACKAGE_DOCS_DIR", docs_dir),
                patch("pysnap.docview.REPO_DOCS_BUILD_DIR", docs_dir / "missing"),
                patch("pysnap.docview.shutil.which", return_value="/usr/bin/firefox"),
                patch("pysnap.docview.subprocess.Popen") as popen,
            ):
                opened_path = open_bundled_documentation()

        self.assertEqual(opened_path, index_path)
        popen.assert_called_once()
        arguments = popen.call_args.args[0]
        self.assertEqual(arguments[0], "/usr/bin/firefox")
        self.assertTrue(arguments[1].startswith("file:"))
        self.assertTrue(arguments[1].endswith("/index.html"))

    def test_open_bundled_documentation_requires_explicit_browser_when_default_missing(
        self,
    ) -> None:
        """Fail with a clear message when Firefox is not available by default."""
        with tempfile.TemporaryDirectory() as temp_dir:
            docs_dir = Path(temp_dir)
            (docs_dir / "index.html").write_text("<html></html>", encoding="utf-8")

            with (
                patch("pysnap.docview.PACKAGE_DOCS_DIR", docs_dir),
                patch("pysnap.docview.REPO_DOCS_BUILD_DIR", docs_dir / "missing"),
                patch("pysnap.docview.shutil.which", return_value=None),
            ):
                with self.assertRaises(PySnapError) as context:
                    open_bundled_documentation()

        self.assertEqual(str(context.exception), DOCUMENTATION_BROWSER_ERROR)

    def test_open_bundled_documentation_reports_invalid_browser(self) -> None:
        """Reuse the same guidance when an explicit browser executable is invalid."""
        with tempfile.TemporaryDirectory() as temp_dir:
            docs_dir = Path(temp_dir)
            (docs_dir / "index.html").write_text("<html></html>", encoding="utf-8")

            with (
                patch("pysnap.docview.PACKAGE_DOCS_DIR", docs_dir),
                patch("pysnap.docview.REPO_DOCS_BUILD_DIR", docs_dir / "missing"),
                patch(
                    "pysnap.docview.subprocess.Popen",
                    side_effect=FileNotFoundError("browser not found"),
                ),
            ):
                with self.assertRaises(PySnapError) as context:
                    open_bundled_documentation(browser="missing-browser")

        self.assertEqual(str(context.exception), DOCUMENTATION_BROWSER_ERROR)


if __name__ == "__main__":
    unittest.main()
