"""Unit tests for report information and statistics."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from pysnap.report.models import ReportArchive
from pysnap.report.recording import parse_recording
from pysnap.report.render import render_transcript
from pysnap.report.stats import compute_stats, split_keys
from pysnap.tests.report_factory import ReportBuilder

START = datetime(2026, 9, 25, 21, 29, 33, tzinfo=timezone.utc)


def _stats(builder: ReportBuilder, name: str = "report.01.first", mtimes=None, **kwargs):
    """Finish a scripted session and compute its statistics."""
    archive = ReportArchive(members=builder.finish(), mtimes=mtimes or {})
    recording = parse_recording(archive, name)
    return compute_stats(recording, render_transcript(recording), **kwargs)


class SplitKeysTests(unittest.TestCase):
    """Verify splitting raw input into keystrokes."""

    def test_counts_escape_sequences_and_utf8_as_single_keys(self) -> None:
        """Keep cursor keys, Alt+key and UTF-8 characters whole."""
        self.assertEqual(
            split_keys("th\x1b[A\x1bOB\x1b[1;5C\x1bbя\x7f\r".encode()),
            [b"t", b"h", b"\x1b[A", b"\x1bOB", b"\x1b[1;5C", b"\x1bb", "я".encode(), b"\x7f", b"\r"],
        )


class ComputeStatsTests(unittest.TestCase):
    """Verify the metrics defined by D29 and D33."""

    def test_reports_identity_timing_and_environment(self) -> None:
        """Take identity from the file name and prompts, timing from headers."""
        builder = ReportBuilder(task=3, host="pc1").show_prompt()
        builder.run("hostname", "pc1\n")
        stats = _stats(builder, name="report.03.pc1")

        self.assertEqual((stats.file_task, stats.file_host), (3, "pc1"))
        self.assertEqual(stats.prompt_identities, ((3, "pc1"),))
        self.assertEqual(stats.start_time, START)
        self.assertAlmostEqual(stats.duration, builder.elapsed, places=5)
        self.assertEqual(
            stats.end_time.replace(microsecond=0),
            datetime(2026, 9, 25, 21, 29, 33 + int(builder.elapsed), tzinfo=timezone.utc),
        )
        self.assertEqual(stats.exit_code, 0)
        self.assertEqual((stats.tty, stats.term, stats.columns, stats.lines), ("/dev/ttyS0", "vt220", 133, 24))
        self.assertEqual((stats.cpu_model, stats.hypervisor), ("Test CPU", "Oracle"))
        self.assertEqual(stats.warnings, ())

    def test_warns_when_file_name_and_prompts_disagree(self) -> None:
        """Flag renamed reports and sessions with several hosts."""
        builder = ReportBuilder(task=1, host="first").show_prompt()
        builder.run("hostname", "first\n")
        renamed = _stats(builder, name="report.01.second")

        self.assertIn("but the prompts differ", " ".join(renamed.warnings))

        mixed = ReportBuilder(task=1, host="first").show_prompt()
        mixed.type_text("ssh second")
        mixed.enter()
        mixed.output("Last login: today\r\n")
        mixed.host = "second"  # The remote shell also runs report's prompt.
        mixed.show_prompt()
        mixed.run("hostname", "second\n")
        stats = _stats(mixed, name="report.01.first")
        self.assertEqual(stats.prompt_identities, ((1, "first"), (1, "second")))
        self.assertIn("several task/host pairs: 01-first, 01-second", " ".join(stats.warnings))

    def test_measures_pauses_as_thinking_time(self) -> None:
        """Measure from the last output before a command to its first key."""
        builder = ReportBuilder().show_prompt(delay=0.0)
        builder.run("sleep 5", "", key_delay=0.1, think_delay=4.0)
        builder.output("done\r\n", delay=5.0)  # Program output, not thinking.
        builder.show_prompt(delay=0.0)
        builder.run("ls", "a\n", key_delay=0.1, think_delay=2.0)
        stats = _stats(builder)

        first, second = stats.commands
        self.assertAlmostEqual(first.pause, 4.0, places=3)
        self.assertAlmostEqual(second.pause, 2.0, places=3)
        self.assertAlmostEqual(stats.pause_max, 4.0, places=3)
        self.assertAlmostEqual(stats.pause_median, 3.0, places=3)
        self.assertEqual(stats.longest_pause_command, "sleep 5")

    def test_counts_typing_backspaces_history_and_interrupts(self) -> None:
        """Count keystrokes up to Enter and the per-command marks."""
        builder = ReportBuilder().show_prompt()
        builder.type_text("pinx", delay=0.5)
        builder.key("\x7f", delay=0.5, echo="\b \b")
        builder.type_text("g 10.0.0.1", delay=0.5)
        builder.enter(delay=0.5)
        builder.output("64 bytes\r\n")
        builder.key("\x03", echo="^C")
        builder.output("\r\n")
        builder.show_prompt()
        builder.key("\x1b[A", delay=0.5, echo="ping 10.0.0.1")
        builder.enter(delay=0.5)
        builder.show_prompt()
        stats = _stats(builder)

        first, second = stats.commands
        self.assertEqual((first.keys, first.backspaces), (16, 1))
        self.assertTrue(first.interrupted)
        self.assertFalse(first.from_history)
        self.assertEqual(second.keys, 2)
        self.assertTrue(second.from_history)
        self.assertFalse(second.interrupted)
        # 18 keys over 8 s of typing; echo delays add a few microseconds.
        self.assertAlmostEqual(stats.typing_speed, 18 / 8.0, places=2)
        self.assertAlmostEqual(stats.backspace_share, 1 / 18, places=5)
        self.assertEqual(stats.unique_commands, 1)

    def test_detects_pasted_chunks_from_five_characters(self) -> None:
        """Treat short chunks as fast typing and long chunks as pastes."""
        builder = ReportBuilder().show_prompt()
        builder.key("ip", echo="ip")  # Two fast keys grouped by the serial line.
        builder.key(" a show", echo=" a show")
        builder.enter()
        builder.show_prompt()
        builder.key("\x1b[200~ls\x1b[201~", echo="ls")
        builder.enter()
        builder.show_prompt()
        stats = _stats(builder)

        self.assertEqual([(paste.text, paste.command) for paste in stats.pastes], [(" a show", 0), ("ls", 1)])
        self.assertIn("2 input chunk(s) look pasted", " ".join(stats.warnings))
        self.assertEqual(_stats(ReportBuilder().show_prompt().run("ls", ""), paste_min_characters=2).pastes, ())

    def test_collects_addresses_from_output_only(self) -> None:
        """Collect valid IPv4 and MAC addresses shown in command output."""
        builder = ReportBuilder().show_prompt()
        builder.run(
            "ping -c1 10.9.9.9",
            "link/ether 08:00:27:A9:84:3A brd ff:ff:ff:ff:ff:ff\n"
            "link/loopback 00:00:00:00:00:00\n"
            "inet 10.9.0.1/24 and 10.9.0.20 not 999.1.1.1 or 1.2.3.4.5\n",
        )
        stats = _stats(builder)

        self.assertEqual(stats.ip_addresses, ("10.9.0.1", "10.9.0.20"))
        self.assertEqual(stats.mac_addresses, ("08:00:27:a9:84:3a",))

    def test_checks_archive_times_against_the_recording(self) -> None:
        """Compare member times with start and end within the tolerance."""
        builder = ReportBuilder().show_prompt()
        builder.run("hostname", "first\n")
        members = builder.finish()
        start = int(START.timestamp())
        end = start + int(builder.elapsed)  # tar keeps whole seconds
        mtimes = {"CPU.txt": start, "OUT.txt": end, "TIME.txt": end + 30}
        recording = parse_recording(ReportArchive(members=members, mtimes=mtimes), "report.01.first")
        stats = compute_stats(recording, render_transcript(recording))

        checks = {check.member: check for check in stats.member_times}
        self.assertTrue(checks["CPU.txt"].within_tolerance)
        self.assertTrue(checks["OUT.txt"].within_tolerance)
        self.assertFalse(checks["TIME.txt"].within_tolerance)
        self.assertAlmostEqual(checks["TIME.txt"].difference, 30 - (builder.elapsed % 1), places=3)
        self.assertIn("TIME.txt archive time differs from the recording end", " ".join(stats.warnings))

    def test_handles_reports_without_commands_or_headers(self) -> None:
        """Report unknown values instead of failing on sparse reports."""
        members = ReportBuilder().output("hello").finish()
        members["TIME.txt"] = b"O 0.1 5\n"
        members["OUT.txt"] = b"hello"
        del members["IN.txt"], members["BOTH.txt"], members["CPU.txt"]
        recording = parse_recording(ReportArchive(members=members, mtimes={}), "notes")
        stats = compute_stats(recording, render_transcript(recording))

        self.assertIsNone(stats.start_time)
        self.assertIsNone(stats.file_host)
        self.assertEqual(stats.commands, ())
        self.assertIsNone(stats.pause_max)
        self.assertIsNone(stats.typing_speed)
        self.assertEqual(stats.member_times, ())
        self.assertIn("report problem(s)", " ".join(stats.warnings))


if __name__ == "__main__":
    unittest.main()
