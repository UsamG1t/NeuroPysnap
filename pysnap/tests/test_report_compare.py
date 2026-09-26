"""Tests for the pairwise comparison of reports."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest

from pysnap.cli.app import run_cli
from pysnap.report.compare import (
    collect_report_paths,
    compare_pair,
    compare_reports,
    fingerprint_report,
)
from pysnap.report.recording import load_report
from pysnap.report.render import render_transcript
from pysnap.tests.report_factory import ReportBuilder, write_report


def _cpu(model: str = "Test CPU", bogomips: str = "4992.07") -> str:
    return (
        "Architecture:                            x86_64\n"
        f"Model name:                              {model}\n"
        f"BogoMIPS:                                {bogomips}\n"
    )


def _session(
    *,
    mac: str = "08:00:27:a9:84:3a",
    pings: tuple[str, ...] = ("0.672", "0.884", "1.04"),
    start: str = "2026-09-25 21:29:33+00:00",
    key_delay: float = 0.1,
    think_delay: float = 1.0,
    extra: tuple[tuple[str, str], ...] = (),
) -> ReportBuilder:
    """Record a lab session whose random values are given by the scenario."""
    builder = ReportBuilder(start_time=start).show_prompt()
    builder.run(
        "ip a show eth1",
        f"2: eth1: <BROADCAST,MULTICAST,UP> mtu 1500\n    link/ether {mac} brd ff:ff:ff:ff:ff:ff\n",
        key_delay=key_delay,
        think_delay=think_delay,
    )
    lines = "".join(
        f"64 bytes from 10.0.0.2: icmp_seq={number} ttl=64 time={value} ms\n"
        for number, value in enumerate(pings, start=1)
    )
    builder.run(f"ping -c{len(pings)} 10.0.0.2", lines, key_delay=key_delay * 1.3, think_delay=think_delay * 2.1)
    for command, output in extra:
        builder.run(command, output, key_delay=key_delay, think_delay=think_delay)
    return builder


class CompareTests(unittest.TestCase):
    """Verify signals between two reports."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _write(self, relative: str, builder: ReportBuilder, cpu_info: str | None = None, **changes) -> Path:
        members = builder.finish(cpu_info=cpu_info or _cpu())
        members.update(changes)
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return write_report(path, members, builder.archive_mtimes())

    def _fingerprint(self, path: Path):
        recording = load_report(path)
        return fingerprint_report(path, recording, render_transcript(recording))

    def _codes(self, first: Path, second: Path) -> list[str]:
        pair = compare_pair(self._fingerprint(first), self._fingerprint(second))
        return [signal.code for signal in pair.signals]

    def _independent(self) -> tuple[Path, Path]:
        first = self._write("a/report.01.first", _session(), _cpu("CPU A"))
        second = self._write(
            "b/report.01.first",
            _session(
                mac="08:00:27:11:22:33", pings=("0.401", "0.912", "1.31"),
                start="2026-09-25 21:40:02+00:00", key_delay=0.13, think_delay=1.7,
            ),
            _cpu("CPU B", "5011.20"),
        )
        return first, second

    def test_independent_reports_have_no_signals(self) -> None:
        """Report nothing for reports that share only the task."""
        self.assertEqual(self._codes(*self._independent()), [])

    def test_identical_files_report_only_the_strongest_signal(self) -> None:
        """Do not list what an identical file implies."""
        first = self._write("a/report.01.first", _session())
        second = self.root / "b/report.01.first"
        second.parent.mkdir()
        second.write_bytes(first.read_bytes())

        pair = compare_pair(self._fingerprint(first), self._fingerprint(second))
        self.assertEqual([signal.code for signal in pair.signals], ["S1"])
        self.assertEqual(pair.signals[0].level, "strong")

    def test_edited_copy_keeps_its_traces(self) -> None:
        """Find a copied session whose input log was edited."""
        builder = _session()
        members = builder.finish(cpu_info=_cpu())
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        first = write_report(self.root / "a/report.01.first", members, builder.archive_mtimes())
        edited = dict(members, **{"IN.txt": members["IN.txt"] + b"\n"})
        second = write_report(self.root / "b/report.01.first", edited, builder.archive_mtimes())

        pair = compare_pair(self._fingerprint(first), self._fingerprint(second))
        self.assertEqual([signal.code for signal in pair.signals], ["S2", "S3", "S4", "M1", "M2", "M3"])
        descriptions = {signal.code: signal.description for signal in pair.signals}
        self.assertIn("08:00:27:a9:84:3a", descriptions["S3"])
        self.assertIn("2026-09-25T21:29:33+00:00", descriptions["S4"])
        self.assertIn("time=0.672 ms", descriptions["M2"])

    def test_same_mac_address(self) -> None:
        """Report a unicast MAC seen by two reports."""
        first, _ = self._independent()
        second = self._write(
            "b/report.01.first",
            _session(pings=("0.3", "0.5", "0.7"), start="2026-09-25 22:00:00+00:00",
                     key_delay=0.17, think_delay=2.3),
            _cpu("CPU B"),
        )
        self.assertEqual(self._codes(first, second), ["S3"])

    def test_ignores_service_and_group_mac_addresses(self) -> None:
        """Skip zero, broadcast and multicast addresses."""
        extra = (("ip maddr", "link  33:33:00:00:00:01\nlink  01:00:5e:00:00:01\nlink/ether 00:00:00:00:00:00\n"),)
        first = self._write("a/report.01.first", _session(extra=extra), _cpu("CPU A"))
        second = self._write(
            "b/report.01.first",
            _session(mac="08:00:27:11:22:33", pings=("0.401", "0.912", "1.31"),
                     start="2026-09-25 21:40:02+00:00", key_delay=0.13, think_delay=1.7, extra=extra),
            _cpu("CPU B"),
        )
        self.assertEqual(self._codes(first, second), [])

    def _other(self, relative: str, pings: tuple[str, ...], **changes) -> Path:
        """Write a report of another student with the given ping times."""
        options = dict(mac="08:00:27:11:22:33", start="2026-09-25 21:40:02+00:00",
                       key_delay=0.13, think_delay=1.7)
        options.update(changes)
        return self._write(relative, _session(pings=pings, **options), _cpu(relative))

    def test_ping_times_need_a_run_in_the_same_order(self) -> None:
        """Count three consecutive ping times in the same order, not loose matches."""
        first = self._write("a/report.01.first", _session(pings=("0.672", "0.884", "1.04", "0.5")), _cpu("A"))
        shuffled = self._other("b/report.01.first", ("1.04", "0.672", "0.884", "0.9"))
        two = self._other("c/report.01.first", ("0.3", "0.672", "0.884", "0.9"))
        shifted = self._other("d/report.01.first", ("0.3", "0.672", "0.884", "1.04"))
        self.assertEqual(self._codes(first, shuffled), [])
        self.assertEqual(self._codes(first, two), [])

        pair = compare_pair(self._fingerprint(first), self._fingerprint(shifted))
        self.assertEqual([signal.code for signal in pair.signals], ["M2"])
        self.assertEqual(
            pair.signals[0].description,
            "same random output values: ping times in the same order: "
            "time=0.672 ms, time=0.884 ms, time=1.04 ms",
        )

    def test_tcpdump_timestamps_need_three_matches(self) -> None:
        """Count shared tcpdump timestamps whatever their order."""
        stamps = ("12:01:02.123456", "12:01:03.654321", "12:01:04.000017")

        def dump(values: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
            return (("tcpdump -c3 -i eth1", "".join(f"{value} ARP, Request\n" for value in values)),)

        first = self._write("a/report.01.first", _session(extra=dump(stamps)), _cpu("A"))
        three = self._other("b/report.01.first", ("2.1", "2.2", "2.3"), extra=dump(stamps[::-1]))
        two = self._other("c/report.01.first", ("3.1", "3.2", "3.3"), extra=dump(stamps[:2]),
                          mac="08:00:27:44:55:66")
        self.assertEqual(self._codes(first, three), ["M2"])
        self.assertEqual(self._codes(first, two), [])

    def test_same_start_and_duration(self) -> None:
        """Report reports that started at the same second and lasted as long."""
        first = self._write("a/report.01.first", _session(), _cpu("CPU A"))
        second = self._write(
            "b/report.01.first",
            _session(mac="08:00:27:11:22:33", pings=("0.401", "0.912", "1.31")),
            _cpu("CPU B"),
        )
        # The same scripted delays also give the same keyboard rhythm.
        self.assertEqual(self._codes(first, second), ["S4", "M1"])

    def test_identical_cpu_file_and_same_model(self) -> None:
        """Tell an identical CPU.txt from the same CPU model."""
        first, second = self._independent()
        same_model = self._write(
            "c/report.01.first",
            _session(mac="08:00:27:77:88:99", pings=("2.1", "2.2", "2.3"), start="2026-09-26 08:00:00+00:00",
                     key_delay=0.19, think_delay=3.1),
            _cpu("CPU A", "4999.99"),
        )
        same_file = self._write(
            "d/report.01.first",
            _session(mac="08:00:27:77:88:98", pings=("3.1", "3.2", "3.3"), start="2026-09-26 09:00:00+00:00",
                     key_delay=0.23, think_delay=2.9),
            _cpu("CPU A"),
        )
        self.assertEqual(self._codes(first, same_model), ["W1"])
        self.assertEqual(self._codes(first, same_file), ["M3"])

    def test_ignores_runs_of_one_repeated_delay(self) -> None:
        """Do not count a run of one delay value as a copied rhythm."""
        long_command = (("echo " + "x" * 30, "x" * 30 + "\n"), ("ls", ""))
        first = self._write("a/report.01.first", _session(extra=long_command), _cpu("CPU A"))
        second = self._write(
            "b/report.01.first",
            _session(mac="08:00:27:11:22:33", pings=("0.401", "0.912", "1.31"),
                     start="2026-09-25 21:40:02+00:00", think_delay=1.7, extra=long_command),
            _cpu("CPU B"),
        )
        self.assertEqual(self._codes(first, second), [])

    def test_same_erroneous_commands(self) -> None:
        """Report two shared failing commands, not one."""
        errors = (
            ("ipp a", "bash: ipp: command not found\n"),
            ("cat /etc/netplan/01.yml", "cat: /etc/netplan/01.yml: No such file or directory\n"),
        )
        first = self._write("a/report.01.first", _session(extra=errors), _cpu("CPU A"))
        second = self._write(
            "b/report.01.first",
            _session(mac="08:00:27:11:22:33", pings=("0.401", "0.912", "1.31"),
                     start="2026-09-25 21:40:02+00:00", key_delay=0.13, think_delay=1.7, extra=errors),
            _cpu("CPU B"),
        )
        one = self._write(
            "c/report.01.first",
            _session(mac="08:00:27:44:55:66", pings=("2.1", "2.2", "2.3"),
                     start="2026-09-25 23:00:00+00:00", key_delay=0.21, think_delay=0.7, extra=errors[:1]),
            _cpu("CPU C"),
        )
        self.assertEqual(self._codes(first, second), ["M4"])
        self.assertEqual(self._codes(first, one), [])


class CollectAndCompareTests(unittest.TestCase):
    """Verify input collection, ordering and the command line."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        builder = _session()
        self.members = builder.finish(cpu_info=_cpu("CPU A"))
        self.mtimes = builder.archive_mtimes()
        other = _session(mac="08:00:27:11:22:33", pings=("0.401", "0.912", "1.31"),
                         start="2026-09-25 21:40:02+00:00", key_delay=0.13, think_delay=1.7)
        self.other = other.finish(cpu_info=_cpu("CPU B"))

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _report(self, relative: str, members: dict[str, bytes] | None = None) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return write_report(path, members or self.members, self.mtimes)

    def test_collects_reports_from_files_and_directories(self) -> None:
        """Search directories recursively and take explicit files as given."""
        nested = self._report("group/ivanov/report.01.first")
        second = self._report("group/petrov/deep/report.02.second")
        (self.root / "group/petrov/notes.txt").write_text("not a report")
        explicit = self._report("loose/copy.tar.gz")
        (self.root / "empty").mkdir()

        paths, warnings = collect_report_paths(
            [self.root / "group", explicit, nested, self.root / "empty", self.root / "missing"]
        )
        self.assertEqual(paths, [nested, second, explicit])
        self.assertEqual(len(warnings), 2)
        self.assertIn("no report files", warnings[0])
        self.assertIn("no such file or directory", warnings[1])

    def test_orders_pairs_with_signals_first(self) -> None:
        """List flagged pairs by their strongest signal, then the others."""
        first = self._report("a/report.01.first")
        other = self._report("b/report.01.first", self.other)
        copy = self._report("c/report.01.first")
        broken = self.root / "d/report.01.first"
        broken.parent.mkdir()
        broken.write_bytes(b"not an archive")

        result = compare_reports([self.root])
        self.assertEqual(result.reports, (str(first), str(other), str(copy)))
        self.assertEqual(len(result.pairs), 3)
        self.assertEqual([pair.signals[0].code for pair in result.pairs[:1]], ["S1"])
        self.assertEqual((result.pairs[0].first, result.pairs[0].second), (str(first), str(copy)))
        self.assertEqual([pair.signals for pair in result.pairs[1:]], [(), ()])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("skipped", result.warnings[0])

    def _run(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        code = run_cli(list(arguments), service=object(), stdout=output, stderr=errors)
        return code, output.getvalue(), errors.getvalue()

    def test_command_line_output(self) -> None:
        """Print flagged pairs with their signals and one line per clean pair."""
        first = self._report("a/report.01.first")
        other = self._report("b/report.01.first", self.other)
        copy = self._report("c/report.01.first")

        code, output, errors = self._run("report", "compare", str(self.root), str(self.root / "missing"))
        self.assertEqual(code, 0)
        self.assertEqual(errors, f"Warning: {self.root / 'missing'}: no such file or directory\n")
        self.assertEqual(
            output.splitlines(),
            [
                f"SIGNALS  {first}  <->  {copy}",
                "  strong  S1  identical report files; other signals are not listed",
                f"OK       {first}  <->  {other}",
                f"OK       {other}  <->  {copy}",
                "",
                "Compared 3 reports, 3 pairs: 1 with signals (strongest: 1 strong, 0 medium, 0 weak), 2 OK.",
            ],
        )

    def test_command_line_without_readable_reports(self) -> None:
        """Fail when nothing could be read, after the warnings."""
        (self.root / "bad").write_bytes(b"x")
        code, output, errors = self._run("report", "compare", str(self.root / "bad"))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Warning:", errors)
        self.assertIn("No readable reports to compare.", errors)

    def test_single_report_has_no_pairs(self) -> None:
        """Accept one report and say that there is nothing to pair."""
        self._report("a/report.01.first")
        code, output, _ = self._run("report", "compare", str(self.root))
        self.assertEqual(code, 0)
        self.assertEqual(
            output.splitlines(),
            ["Compared 1 report, 0 pairs: 0 with signals (strongest: 0 strong, 0 medium, 0 weak), 0 OK."],
        )


if __name__ == "__main__":
    unittest.main()
