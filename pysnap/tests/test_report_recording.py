"""Unit tests for parsing the ``script`` recording inside a report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

from pysnap.errors import ReportFormatError
from pysnap.report.models import ReportArchive, ReportName
from pysnap.report.recording import (
    DIAG_BOTH_MISMATCH,
    DIAG_HEADER_VALUE,
    DIAG_MISSING_MEMBER,
    DIAG_STREAM_HEADER,
    DIAG_STREAM_TRAILER,
    DIAG_TIMING_LINE,
    DIAG_TIMING_OVERRUN,
    DIAG_TIMING_UNDERRUN,
    load_report,
    parse_recording,
)
from pysnap.tests.report_factory import ReportBuilder, write_report


def _session() -> ReportBuilder:
    """Return a two-command session as recorded in the task 02 example."""
    builder = ReportBuilder(task=1, host="first").show_prompt()
    builder.run("ip a show eth1", "3: eth1: <UP> mtu 1500\n    inet 10.9.0.1/24\n")
    builder.run("ping -c1 10.9.0.2", "1 packets transmitted, 1 received\n")
    return builder


def _parse(members: dict[str, bytes], name: str = "report.01.first"):
    """Parse report members without writing an archive."""
    return parse_recording(ReportArchive(members=members, mtimes={}), name)


class ReportNameTests(unittest.TestCase):
    """Verify parsing of ``report.<NN>.<host>`` file names."""

    def test_parses_task_and_host(self) -> None:
        """Split the task number and the host, keeping dots in the host."""
        self.assertEqual(ReportName.parse("report.01.first"), ReportName(1, "first"))
        self.assertEqual(ReportName.parse("report.12.R1.lab"), ReportName(12, "R1.lab"))

    def test_rejects_other_names(self) -> None:
        """Return ``None`` for names that do not follow the convention."""
        for name in ("first.report", "report.first", "report.01.", "report..x"):
            with self.subTest(name=name):
                self.assertIsNone(ReportName.parse(name))


class ParseRecordingTests(unittest.TestCase):
    """Verify headers, event slicing and integrity diagnostics."""

    def test_parses_a_consistent_report_without_diagnostics(self) -> None:
        """Expose headers, timed chunks and stream bodies of a clean report."""
        builder = _session()
        members = builder.finish(exit_code=0)
        recording = _parse(members)

        self.assertEqual(recording.diagnostics, ())
        self.assertEqual(recording.name, ReportName(1, "first"))
        self.assertEqual(
            recording.start_time,
            datetime(2026, 9, 25, 21, 29, 33, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(recording.duration, builder.elapsed, places=5)
        self.assertEqual(recording.exit_code, 0)
        self.assertEqual((recording.columns, recording.lines), (133, 24))
        self.assertEqual(recording.headers["TTY"], "/dev/ttyS0")
        self.assertEqual(recording.input_bytes, bytes(builder.input))
        self.assertEqual(recording.output_bytes, bytes(builder.output_log))
        self.assertIn("Test CPU", recording.cpu_info)

        inputs = b"".join(e.data for e in recording.events if e.kind == "I")
        outputs = b"".join(e.data for e in recording.events if e.kind == "O")
        self.assertEqual(inputs, bytes(builder.input))
        self.assertEqual(outputs, bytes(builder.output_log))
        self.assertTrue(inputs.startswith(b"ip a show eth1\r"))
        self.assertTrue(inputs.endswith(b"\x04"))

    def test_accumulates_event_times_from_delays(self) -> None:
        """Compute absolute event times as the running sum of delays."""
        builder = ReportBuilder().output("a", delay=0.5).key("b", delay=1.25, echo="")
        recording = _parse(builder.finish())

        self.assertEqual([e.kind for e in recording.events[:2]], ["O", "I"])
        self.assertAlmostEqual(recording.events[0].time, 0.5)
        self.assertAlmostEqual(recording.events[1].time, 1.75)
        self.assertAlmostEqual(recording.events[1].delay, 1.25)

    def test_keeps_signal_events(self) -> None:
        """Expose ``S`` entries such as terminal resizes as events."""
        builder = ReportBuilder().output("a").signal("SIGWINCH ROWS=40 COLS=120", 0.3)
        recording = _parse(builder.finish())

        signal = recording.events[1]
        self.assertEqual((signal.kind, signal.data), ("S", b"SIGWINCH ROWS=40 COLS=120"))
        self.assertAlmostEqual(signal.time, 0.301)
        self.assertEqual(recording.diagnostics, ())

    def test_reports_timing_that_exceeds_the_recorded_streams(self) -> None:
        """Flag a truncated output log and keep the available bytes."""
        members = _session().finish()
        members["OUT.txt"] = members["OUT.txt"][:40]
        recording = _parse(members)

        codes = [item.code for item in recording.diagnostics]
        self.assertIn(DIAG_STREAM_TRAILER, codes)
        self.assertIn(DIAG_TIMING_OVERRUN, codes)
        self.assertEqual(codes.count(DIAG_TIMING_OVERRUN), 1)

    def test_reports_recorded_bytes_missing_from_the_timing_log(self) -> None:
        """Flag stream bytes that no ``I`` or ``O`` entry covers."""
        members = _session().finish()
        members["TIME.txt"] = members["TIME.txt"].replace(b"\nO ", b"\nX ", 1)
        recording = _parse(members)

        codes = [item.code for item in recording.diagnostics]
        self.assertIn(DIAG_TIMING_LINE, codes)
        self.assertIn(DIAG_TIMING_UNDERRUN, codes)

    def test_reports_a_both_log_that_does_not_match(self) -> None:
        """Compare the ``BOTH.txt`` length with ``IN.txt`` plus ``OUT.txt``."""
        members = _session().finish()
        members["BOTH.txt"] = members["BOTH.txt"].replace(b"eth1", b"eth", 1)

        codes = [item.code for item in _parse(members).diagnostics]
        self.assertEqual(codes, [DIAG_BOTH_MISMATCH])

    def test_reports_stream_logs_without_start_line(self) -> None:
        """Use the whole stream when the ``Script started`` line is missing."""
        members = _session().finish()
        start_line_end = members["IN.txt"].index(b"\n") + 1
        members["IN.txt"] = members["IN.txt"][start_line_end:]
        recording = _parse(members)

        self.assertIn(DIAG_STREAM_HEADER, [item.code for item in recording.diagnostics])
        self.assertTrue(recording.input_bytes.startswith(b"ip a show"))

    def test_tolerates_a_missing_input_log(self) -> None:
        """Keep output events and report the missing keyboard input once."""
        members = _session().finish()
        del members["IN.txt"]
        del members["BOTH.txt"]
        recording = _parse(members)

        self.assertEqual(
            [item.code for item in recording.diagnostics], [DIAG_MISSING_MEMBER]
        )
        self.assertTrue(all(e.data == b"" for e in recording.events if e.kind == "I"))
        self.assertTrue(recording.output_bytes)

    def test_reports_invalid_header_values(self) -> None:
        """Ignore unparsable headers with a diagnostic."""
        members = _session().finish()
        members["TIME.txt"] = members["TIME.txt"].replace(
            b"COLUMNS 133", b"COLUMNS wide"
        ).replace(b"START_TIME 2026", b"START_TIME yesterday 2026")
        recording = _parse(members)

        self.assertIsNone(recording.columns)
        self.assertIsNone(recording.start_time)
        self.assertEqual(
            [item.code for item in recording.diagnostics], [DIAG_HEADER_VALUE] * 2
        )

    def test_treats_negative_terminal_size_as_unknown(self) -> None:
        """Accept the ``-1`` size ``script`` writes without a terminal size."""
        members = _session().finish()
        members["TIME.txt"] = members["TIME.txt"].replace(
            b"COLUMNS 133", b"COLUMNS -1"
        ).replace(b"LINES 24", b"LINES -1")
        recording = _parse(members)

        self.assertEqual((recording.columns, recording.lines), (None, None))
        self.assertEqual(recording.diagnostics, ())

    def test_rejects_reports_without_timing_or_output(self) -> None:
        """Refuse reports that cannot be replayed at all."""
        for missing in ("TIME.txt", "OUT.txt"):
            members = _session().finish()
            del members[missing]
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ReportFormatError, missing):
                    _parse(members)

    def test_rejects_the_single_stream_timing_format(self) -> None:
        """Refuse the classic ``<delay> <size>`` timing format."""
        members = _session().finish()
        members["TIME.txt"] = b"0.012351 27\n0.500000 3\n"

        with self.assertRaisesRegex(ReportFormatError, "single-stream"):
            _parse(members)


class LoadReportTests(unittest.TestCase):
    """Verify loading report files from disk."""

    def test_loads_a_packed_report(self) -> None:
        """Read and parse a report packed like ``tar -czf NAME .``."""
        members = _session().finish()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_report(Path(temp_dir) / "report.03.pc1", members)
            recording = load_report(path)

        self.assertEqual(recording.source_name, "report.03.pc1")
        self.assertEqual(recording.name, ReportName(3, "pc1"))
        self.assertEqual(recording.diagnostics, ())
        self.assertEqual(len(recording.mtimes), 5)


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("script"),
    "util-linux script is required for the cross-check",
)
class RealScriptCrossCheckTests(unittest.TestCase):
    """Cross-check the parser against a session recorded by real ``script``."""

    def test_parses_a_real_multi_stream_recording(self) -> None:
        """Record a short shell session through a PTY and parse it cleanly."""
        import pty  # Linux-only; imported here to keep the module portable.

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            pid, fd = pty.fork()
            if pid == 0:  # pragma: no cover - child process
                os.environ["TERM"] = "xterm"
                os.execvp(
                    "script",
                    [
                        "script", "-q",
                        "-I", str(base / "IN.txt"),
                        "-O", str(base / "OUT.txt"),
                        "-B", str(base / "BOTH.txt"),
                        "-T", str(base / "TIME.txt"),
                        "-c", "sh",
                    ],
                )
            _drain(fd, 0.5)
            for chunk in (b"echo pysnap-check\r", b"exit\r"):
                os.write(fd, chunk)
                _drain(fd, 0.5)
            os.waitpid(pid, 0)
            os.close(fd)
            members = {
                name: (base / name).read_bytes()
                for name in ("IN.txt", "OUT.txt", "BOTH.txt", "TIME.txt")
            }

        recording = _parse(members, "report.01.real")

        self.assertEqual(recording.diagnostics, ())
        self.assertEqual(recording.exit_code, 0)
        self.assertIsNotNone(recording.start_time)
        self.assertLess(
            abs(recording.start_time - datetime.now(timezone.utc)), timedelta(minutes=5)
        )
        self.assertIn(b"echo pysnap-check\r", recording.input_bytes)
        self.assertIn(b"pysnap-check", recording.output_bytes)


def _drain(fd: int, seconds: float) -> None:
    """Read and discard PTY output for a while so the child never blocks."""
    import select

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                if not os.read(fd, 4096):
                    return
            except OSError:
                return


if __name__ == "__main__":
    unittest.main()
