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
        members = builder.finish()
        self.report = write_report(
            Path(self._temp_dir.name) / "report.01.first", members, builder.archive_mtimes()
        )

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

    def test_opens_the_pager_only_for_long_text_on_terminals(self) -> None:
        """Page long text on terminals and print it otherwise."""
        size = __import__("os").terminal_size((80, 5))
        with patch("pysnap.cli.report.shutil.get_terminal_size", return_value=size), patch(
            "pysnap.report.viewer.run_pager"
        ) as run_pager:
            code, output, _ = self._run("report", "text", str(self.report), stdout=_TtyStringIO())
            _, piped, _ = self._run("report", "text", str(self.report))
            _, no_pager, _ = self._run(
                "report", "text", str(self.report), "--no-pager", stdout=_TtyStringIO()
            )

        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        run_pager.assert_called_once()
        title, lines = run_pager.call_args.args
        self.assertEqual(title, "report.01.first")
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[0][-1].text, "ip a show eth1")
        self.assertIn("exit", piped)
        self.assertIn("exit", no_pager)

    def test_prints_short_text_without_the_pager(self) -> None:
        """Print text that fits on the terminal directly."""
        size = __import__("os").terminal_size((80, 50))
        with patch("pysnap.cli.report.shutil.get_terminal_size", return_value=size), patch(
            "pysnap.report.viewer.run_pager"
        ) as run_pager:
            _, output, _ = self._run("report", "text", str(self.report), stdout=_TtyStringIO())

        run_pager.assert_not_called()
        self.assertIn("exit", output)

    def test_show_starts_the_player_with_options(self) -> None:
        """Start the player with the requested speed and pause limit."""
        with patch("pysnap.report.viewer.run_player") as run_player:
            code, _, _ = self._run(
                "report", "show", str(self.report), "--speed", "4", "--max-delay", "0.5",
                stdout=_TtyStringIO(),
            )

        self.assertEqual(code, 0)
        controller, title = run_player.call_args.args
        self.assertEqual(title, "report.01.first")
        self.assertEqual(controller.speed, 4.0)
        self.assertTrue(controller.playing)
        self.assertEqual(len(controller.player.command_times), 2)
        self.assertTrue(all(event_delay <= 0.5 + 1e-9 for event_delay in _delays(controller.player)))

    def test_show_requires_a_terminal(self) -> None:
        """Refuse to replay into redirected output."""
        with patch("pysnap.report.viewer.run_player") as run_player:
            code, _, errors = self._run("report", "show", str(self.report))

        self.assertEqual(code, 1)
        self.assertIn("interactive terminal", errors)
        run_player.assert_not_called()

    def test_check_prints_report_information(self) -> None:
        """Print the information block without a check file."""
        code, output, errors = self._run("report", "check", str(self.report))

        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        lines = output.splitlines()
        self.assertEqual(lines[0], "Report information: report.01.first")
        self.assertIn("  File name:  task 01, host first", lines)
        self.assertIn("  Prompt:     task 01, host first", lines)
        self.assertIn("  Entered:    2 (2 unique)", lines)
        self.assertIn("  IPv4:       10.9.0.1", lines)
        self.assertIn("  Pasted input: 0", lines)
        self.assertNotIn("WARNING", output)

    def test_check_shows_warnings(self) -> None:
        """End the block with warnings when something looks wrong."""
        renamed = Path(self._temp_dir.name) / "report.02.second"
        renamed.write_bytes(self.report.read_bytes())

        _, output, _ = self._run("report", "check", str(renamed))

        self.assertIn("WARNING: File name says task 02, host second, but the prompts differ.", output)

    def test_check_grades_a_report_with_a_check_file(self) -> None:
        """Print the information block, item results and the grade."""
        check_file = Path(self._temp_dir.name) / "lab.check.toml"
        check_file.write_text(
            '[report]\nhost = "first"\n'
            '[[command]]\nid = "addr"\ncmd = "ip a show <ETH-A>"\n'
            '[[command]]\ncmd = "ping -c1 <IP-B>"\n'
            '[[command]]\ncmd = "traceroute <IP-B>"\n'
            '[[output]]\nof = "addr"\ntext = "inet <IP-A>/<MASK>"\n'
            '[grading]\ntotal = 10\nscale = [[75, "good"], [0, "poor"]]\n',
            encoding="utf-8",
        )

        code, output, _ = self._run("report", "check", str(self.report), str(check_file))

        self.assertEqual(code, 0)
        checks = output[output.index("Checks: lab.check.toml"):].splitlines()
        self.assertEqual(checks[1], "  PASS  command 1 [addr]: ip a show <ETH-A>")
        self.assertEqual(checks[2], "        matched command 1: ip a show eth1")
        self.assertIn("  FAIL  command 3: traceroute <IP-B>", checks)
        self.assertIn("        reason: no entered command matches", checks)
        self.assertIn("  Values: ETH-A=eth1, IP-B=10.9.0.2, IP-A=10.9.0.1", checks)
        self.assertIn("Result: 3 of 4 checks passed, 7.5 of 10 points (75.0%), mark good", checks)
        self.assertNotIn("WARNING: The check file", output)

    def test_check_reports_invalid_check_files(self) -> None:
        """Stop with an error before reading the report."""
        check_file = Path(self._temp_dir.name) / "bad.check.toml"
        check_file.write_text("[[command]]\ncmd = 'x'\n", encoding="utf-8")

        code, output, errors = self._run("report", "check", str(self.report), str(check_file))

        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("[grading] table with total and scale is required", errors)

    def test_extract_copies_and_validates_the_file(self) -> None:
        """Print the transfer summary and warn when the file is no report."""
        from pysnap.report.extract import ExtractResult

        target = Path(self._temp_dir.name) / "copied"

        def fake_extract(service, vm, name, *, destination, force):
            target.write_bytes(self.report.read_bytes() if name == "report.01.first" else b"text")
            return ExtractResult(vm, f'"$HOME"/{name}', target, 3, "abc")

        with patch("pysnap.cli.report.extract_file", side_effect=fake_extract) as extract:
            code, output, errors = self._run(
                "report", "extract", "first", "report.01.first", "--output", str(target), "--force"
            )
            _, _, warning = self._run("report", "extract", "first", "notes.txt", "--output", str(target))

        self.assertEqual(code, 0)
        self.assertEqual(
            output.strip(),
            f'Extracted "$HOME"/report.01.first from first to {target} (3 bytes, sha256 abc).',
        )
        self.assertEqual(errors, "")
        self.assertEqual(extract.call_args_list[0].kwargs, {"destination": target, "force": True})
        self.assertIn("Warning: the file is not a readable report", warning)

    def test_extract_reports_errors(self) -> None:
        """Show transfer problems as errors."""
        from pysnap.report.extract import ExtractError

        with patch("pysnap.cli.report.extract_file", side_effect=ExtractError("boom")):
            code, _, errors = self._run("report", "extract", "first", "report.01.first")

        self.assertEqual(code, 1)
        self.assertEqual(errors.strip(), "Error: boom")

    def test_requires_a_subcommand(self) -> None:
        """Refuse ``pysnap report`` without a subcommand."""
        code, _, errors = self._run("report")

        self.assertEqual(code, 2)
        self.assertIn("SUBCOMMAND", errors)


def _delays(player) -> list[float]:
    """Return the gaps between consecutive events on the player timeline."""
    times = [0.0, *player.event_times]
    return [later - earlier for earlier, later in zip(times, times[1:])]


if __name__ == "__main__":
    unittest.main()
