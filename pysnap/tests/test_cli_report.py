"""CLI tests for ``pysnap report``."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pysnap.cli.app import run_cli
from pysnap.tests.report_factory import ReportBuilder, write_report


class _TtyStringIO(io.StringIO):
    """Pretend to be a terminal."""

    def isatty(self) -> bool:
        """Report a terminal stream."""
        return True


class ReportTextCommandTests(unittest.TestCase):
    """Verify ``pysnap report text``."""

    def setUp(self) -> None:
        """Write a two-command report into a scratch directory."""
        self._temp_dir = tempfile.TemporaryDirectory()
        builder = ReportBuilder().show_prompt()
        builder.run("ip a show eth1", "3: eth1: <UP>\n    inet \x1b[32m10.9.0.1\x1b[0m/24\n")
        builder.run("ping -c1 10.9.0.2", "1 packets transmitted\n")
        self.report = write_report(Path(self._temp_dir.name) / "report.01.first", builder.finish())

    def tearDown(self) -> None:
        """Remove the scratch directory."""
        self._temp_dir.cleanup()

    def _run(self, *arguments: str, stdout: io.StringIO | None = None) -> tuple[int, str, str]:
        """Run the CLI and capture its output."""
        output = stdout or io.StringIO()
        errors = io.StringIO()
        code = run_cli(list(arguments), service=object(), stdout=output, stderr=errors)
        return code, output.getvalue(), errors.getvalue()

    def test_prints_plain_text_when_output_is_not_a_terminal(self) -> None:
        """Print the transcript without escape sequences for pipes."""
        code, output, errors = self._run("report", "text", str(self.report))

        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertNotIn("\x1b", output)
        self.assertEqual(
            output.splitlines(),
            [
                "[root@01-first ~]# ip a show eth1",
                "3: eth1: <UP>",
                "    inet 10.9.0.1/24",
                "[root@01-first ~]# ping -c1 10.9.0.2",
                "1 packets transmitted",
                "[root@01-first ~]#",
                "exit",
            ],
        )

    def test_highlights_prompt_command_and_guest_colors_on_terminals(self) -> None:
        """Color prompt parts, bold commands and keep guest colors."""
        code, output, _ = self._run("report", "text", str(self.report), stdout=_TtyStringIO())

        self.assertEqual(code, 0)
        first_line = output.splitlines()[0]
        self.assertEqual(
            first_line,
            "[\x1b[1;32mroot\x1b[0m@\x1b[1;36m01-first\x1b[0m "
            "\x1b[1;34m~\x1b[0m]\x1b[1m#\x1b[0m \x1b[1mip a show eth1\x1b[0m",
        )
        self.assertIn("\x1b[32m10.9.0.1\x1b[0m", output)

    def test_color_option_overrides_terminal_detection(self) -> None:
        """Honor ``--color always``, ``--color never`` and ``NO_COLOR``."""
        _, forced, _ = self._run("report", "text", str(self.report), "--color", "always")
        _, disabled, _ = self._run(
            "report", "text", str(self.report), "--color", "never", stdout=_TtyStringIO()
        )
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            _, no_color, _ = self._run("report", "text", str(self.report), stdout=_TtyStringIO())

        self.assertIn("\x1b[1m", forced)
        self.assertNotIn("\x1b", disabled)
        self.assertNotIn("\x1b", no_color)

    def test_lists_commands_with_times(self) -> None:
        """Print a numbered command list for writing check files."""
        code, output, _ = self._run("report", "text", str(self.report), "--commands")

        self.assertEqual(code, 0)
        lines = output.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^1  00:\d\d\.\d\d  ip a show eth1$")
        self.assertRegex(lines[1], r"^2  00:\d\d\.\d\d  ping -c1 10\.9\.0\.2$")

    def test_marks_commands_entered_at_foreign_prompts(self) -> None:
        """Show the prompt of commands typed inside other programs."""
        builder = ReportBuilder().show_prompt()
        builder.type_text("vtysh")
        builder.enter()
        builder.output("\r\nR1# ")
        builder.type_text("show ip route")
        builder.enter()
        builder.output("\r\nR1# ")
        path = write_report(Path(self._temp_dir.name) / "report.08.R1", builder.finish())

        _, output, _ = self._run("report", "text", str(path), "--commands")

        self.assertTrue(output.splitlines()[1].endswith("show ip route  [prompt: R1#]"))

    def test_warns_about_report_diagnostics(self) -> None:
        """Print non-fatal report problems to stderr and still show the text."""
        members = ReportBuilder().show_prompt().run("hostname", "first\n").finish()
        start_line_end = members["OUT.txt"].index(b"\n") + 1
        members["OUT.txt"] = members["OUT.txt"][: start_line_end + 40]
        path = write_report(Path(self._temp_dir.name) / "report.01.cut", members)

        code, output, errors = self._run("report", "text", str(path))

        self.assertEqual(code, 0)
        self.assertIn("Warning:", errors)
        self.assertIn("truncated", errors)
        self.assertTrue(output)

    def test_reports_unreadable_files_as_errors(self) -> None:
        """Exit with an error for files that are not reports."""
        not_a_report = Path(self._temp_dir.name) / "notes.txt"
        not_a_report.write_text("hello\n", encoding="utf-8")

        code, output, errors = self._run("report", "text", str(not_a_report))

        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(errors.startswith("Error: File"))

    def test_requires_a_subcommand(self) -> None:
        """Refuse ``pysnap report`` without a subcommand."""
        code, _, errors = self._run("report")

        self.assertEqual(code, 2)
        self.assertIn("SUBCOMMAND", errors)


if __name__ == "__main__":
    unittest.main()
