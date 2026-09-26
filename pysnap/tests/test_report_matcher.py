"""Unit tests for matching check files against reports and grading."""

from __future__ import annotations

import unittest

from pysnap.report.checkfile import parse_check
from pysnap.report.matcher import run_check
from pysnap.report.models import ReportArchive, ReportName
from pysnap.report.recording import parse_recording
from pysnap.report.render import render_transcript
from pysnap.tests.report_factory import ReportBuilder

SCALE = {"total": 10, "scale": [[90, "5"], [75, "4"], [50, "3"], [0, "2"]]}


def _transcript(*runs: tuple[str, str], host: str = "first"):
    """Render a session with the given ``(command, output)`` runs."""
    builder = ReportBuilder(host=host).show_prompt()
    for command, output in runs:
        builder.run(command, output)
    archive = ReportArchive(members=builder.finish(), mtimes={})
    return render_transcript(parse_recording(archive, f"report.01.{host}"))


def _check(transcript, commands=(), outputs=(), report=None, **kwargs):
    """Run a check built from item dictionaries."""
    data = {"command": list(commands), "output": list(outputs), "grading": dict(SCALE)}
    if report:
        data["report"] = report
    return run_check(parse_check(data, "lab.check.toml"), transcript, **kwargs)


def _passed(result) -> list[bool]:
    """Return the pass flags of all items."""
    return [item.passed for item in result.items]


class CommandMatchingTests(unittest.TestCase):
    """Verify command items, labels and ``after``."""

    def test_labels_synchronize_between_commands(self) -> None:
        """Require a labeled value to repeat and accept any value unlabeled."""
        transcript = _transcript(("ping -c3 10.0.0.2", ""), ("traceroute 10.0.0.3", ""))
        result = _check(
            transcript,
            commands=[
                {"cmd": "ping -c3 <IP-A>"},
                {"cmd": "traceroute <IP-A>"},
                {"cmd": "traceroute <IP>"},
            ],
        )

        self.assertEqual(_passed(result), [True, False, True])
        self.assertEqual(result.bindings, {"IP-A": "10.0.0.2"})
        self.assertIn("IP-A=10.0.0.2", result.items[1].reason)
        self.assertEqual(result.items[1].closest, "traceroute 10.0.0.3")

    def test_backtracks_to_the_occurrence_that_satisfies_more_items(self) -> None:
        """Choose the second ping so that the traceroute also passes."""
        transcript = _transcript(
            ("ping -c3 10.0.0.3", ""), ("ping -c3 10.0.0.2", ""), ("traceroute 10.0.0.2", "")
        )
        result = _check(
            transcript, commands=[{"cmd": "ping -c3 <IP-A>"}, {"cmd": "traceroute <IP-A>"}]
        )

        self.assertEqual(_passed(result), [True, True])
        self.assertEqual(result.bindings, {"IP-A": "10.0.0.2"})
        self.assertEqual(result.items[0].command, 1)

    def test_prefers_the_earliest_occurrence_on_ties(self) -> None:
        """Keep the result deterministic when several matches score the same."""
        transcript = _transcript(("ping -c3 10.0.0.3", ""), ("ping -c3 10.0.0.2", ""))
        result = _check(transcript, commands=[{"cmd": "ping -c3 <IP-A>"}])

        self.assertEqual(result.items[0].command, 0)
        self.assertEqual(result.bindings, {"IP-A": "10.0.0.3"})

    def test_after_requires_a_later_command(self) -> None:
        """Pass a child only after its parent and explain failures."""
        transcript = _transcript(
            ("ping -fc3 10.0.12.2", ""),
            ("ip route del dev eth1 10.0.12.0/24", ""),
        )
        result = _check(
            transcript,
            commands=[
                {"id": "del", "cmd": "ip route del dev <ETH> <X>"},
                {"cmd": "ping -fc3 <IP>", "after": "del"},
                {"id": "add", "cmd": "ip route add dev <ETH> <X>"},
                {"cmd": "ip route", "after": "add"},
            ],
        )

        self.assertEqual(_passed(result), [True, False, False, False])
        self.assertIn('only before command "del"', result.items[1].reason)
        self.assertIn('"after" command "add" did not pass', result.items[3].reason)

    def test_normalizes_spacing_in_commands(self) -> None:
        """Ignore repeated spaces typed by the student."""
        transcript = _transcript(("ip  a   show eth1", ""))
        result = _check(transcript, commands=[{"cmd": "ip a show <ETH>"}])

        self.assertEqual(_passed(result), [True])


class OutputMatchingTests(unittest.TestCase):
    """Verify output blocks, their scopes and options."""

    def test_output_uses_labels_from_commands_and_of_scope(self) -> None:
        """Find a block only in the output of its command with bound labels."""
        transcript = _transcript(
            ("ip a show eth2", "4: eth2: <UP>\n    inet 10.0.0.9/24 scope global eth2\n"),
            ("ip a show eth1", "3: eth1: <UP>\n    link/ether x\n    inet 10.0.0.1/24 scope global eth1\n"),
        )
        result = _check(
            transcript,
            commands=[{"id": "addr", "cmd": "ip a show eth1"}],
            outputs=[
                {"of": "addr", "text": "<*>: <ETH-A>: <*>\n...\ninet <IP-A>/<MASK> scope global <ETH-A>"},
                {"of": "addr", "text": "inet 10.0.0.9/24 scope global eth2"},
                {"text": "inet 10.0.0.9/24 scope global eth2"},
            ],
        )

        self.assertEqual(_passed(result), [True, True, False, True])
        self.assertEqual(result.bindings, {"ETH-A": "eth1", "IP-A": "10.0.0.1"})
        self.assertEqual(result.items[1].lines, (4, 6))  # after two lines of eth2
        self.assertIn('not found in the output of "addr"', result.items[2].reason)

    def test_sequence_blocks_need_adjacent_lines_without_gaps(self) -> None:
        """Require consecutive lines unless ``...`` allows others between."""
        transcript = _transcript(("cat f", "a\nx\nb\n"))
        result = _check(
            transcript,
            outputs=[{"text": "a\nb"}, {"text": "a\n...\nb"}, {"text": "b\n...\na"}],
        )

        self.assertEqual(_passed(result), [False, True, False])

    def test_any_order_blocks_match_table_rows(self) -> None:
        """Find table rows in any order with ``order = "any"``."""
        transcript = _transcript(
            ("ip route", "10.0.12.0/24 dev eth1 proto kernel\ndefault via 10.0.2.2 dev eth0\n")
        )
        block = "default via <IP> dev eth0\n<IP>/<MASK> dev eth1 proto kernel"
        result = _check(transcript, outputs=[{"text": block, "order": "any"}, {"text": block}])

        self.assertEqual(_passed(result), [True, False])

    def test_min_count_counts_stream_lines(self) -> None:
        """Count repeated lines such as ping replies."""
        replies = "".join(f"64 bytes from 10.0.0.1: icmp_seq={n} time=0.{n} ms\n" for n in range(1, 4))
        transcript = _transcript(("ping -c3 10.0.0.1", replies))
        line = "64 bytes from 10.0.0.1: icmp_seq=<*> time=<*>"
        result = _check(
            transcript,
            outputs=[
                {"text": line, "min_count": 3},
                {"text": line, "min_count": 4},
                {"text": "64 bytes from 10.0.0.1: icmp_seq=<N> time=<*>", "min_count": 2},
            ],
        )

        self.assertEqual(_passed(result), [True, False, False])
        self.assertEqual(result.items[0].count, 3)
        self.assertIn("found 3 time(s) in the report, 4 required", result.items[1].reason)
        self.assertIn("found 1 time(s)", result.items[2].reason)

    def test_normalizes_column_alignment(self) -> None:
        """Match aligned columns written with single spaces (D35)."""
        transcript = _transcript(
            ("bridge vlan show", "port              vlan-id\neth1              1 PVID Egress Untagged\n")
        )
        result = _check(
            transcript,
            outputs=[{"text": "  port vlan-id\n<ETH> 1 PVID\tEgress Untagged  "}],
        )

        self.assertEqual(_passed(result), [True])


class GradingTests(unittest.TestCase):
    """Verify points, percentages, marks and expectations."""

    def test_scales_weights_and_selects_the_mark(self) -> None:
        """Weight items, scale to the total and pick the first reached step."""
        transcript = _transcript(("hostname", "first\n"))
        result = _check(
            transcript,
            commands=[{"cmd": "hostname", "points": 3}, {"cmd": "whoami"}],
            outputs=[{"text": "first"}],
        )

        self.assertEqual(_passed(result), [True, False, True])
        self.assertEqual((result.earned_weight, result.total_weight), (4.0, 5.0))
        self.assertAlmostEqual(result.percent, 80.0)
        self.assertAlmostEqual(result.points, 8.0)
        self.assertEqual(result.mark, "4")

    def test_reports_mismatched_expectations_as_warnings_only(self) -> None:
        """Warn about another host without changing the grade (D36)."""
        transcript = _transcript(("hostname", "second\n"), host="second")
        result = _check(
            transcript,
            commands=[{"cmd": "hostname"}],
            report={"task": 1, "host": "first"},
            report_name=ReportName(1, "second"),
        )

        self.assertEqual(result.percent, 100.0)
        self.assertEqual(
            result.warnings,
            (
                'The check file expects host "first", but the report file name shows host "second".',
                'The check file expects host "first", but the report prompt shows host "second".',
            ),
        )

    def test_uses_the_best_result_found_when_the_search_budget_ends(self) -> None:
        """Flag an exhausted search instead of running forever."""
        runs = [(f"ping 10.0.0.{n}", "") for n in range(1, 8)]
        transcript = _transcript(*runs)
        commands = [{"cmd": f"ping <IP-{label}>"} for label in "ABCDEFG"]
        result = _check(transcript, commands=commands, budget=5)

        self.assertTrue(result.search_exhausted)
        self.assertGreater(result.earned_weight, 0)
        self.assertEqual(len(result.items), 7)


if __name__ == "__main__":
    unittest.main()
