"""Unit tests for loading check files and compiling patterns."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pysnap.report.checkfile import (
    CheckFileError,
    GradeStep,
    Parameter,
    compile_pattern,
    load_check,
    normalize_spacing,
    parse_check,
)

VALID = """
[report]
task = 1
host = "first"

[[command]]
id = "addr"
cmd = "ip a show <ETH-A>"

[[command]]
cmd = "ping -c5 <IP-B>"
after = "addr"
points = 2

[[output]]
of = "addr"
text = '''

    inet <IP-A>/<MASK> scope global <ETH-A>
...
'''

[[output]]
order = "any"
min_count = 2
text = "default via <IP>"

[grading]
total = 10
scale = [[90, "5"], [75, "4"], [50, "3"], [0, "2"]]
"""


def _parse(text: str):
    """Parse check file text through a temporary file."""
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "lab.check.toml"
        path.write_text(text, encoding="utf-8")
        return load_check(path)


def _base(**overrides) -> dict:
    """Return minimal valid check data with overrides."""
    data = {
        "command": [{"cmd": "hostname"}],
        "grading": {"total": 10, "scale": [[50, "pass"], [0, "fail"]]},
    }
    data.update(overrides)
    return data


class PatternTests(unittest.TestCase):
    """Verify placeholder parsing and matching."""

    def test_normalizes_whitespace(self) -> None:
        """Trim and collapse whitespace like decision D35."""
        self.assertEqual(normalize_spacing("  eth1 \t  1 PVID  "), "eth1 1 PVID")

    def test_splits_literals_and_parameters(self) -> None:
        """Recognize typed, labeled, free and wildcard placeholders."""
        pattern = compile_pattern("ip  a add <IP>/<MASK-N> dev <ETH-A> <*> <X> \\<raw>")

        self.assertEqual(
            pattern.parts,
            (
                "ip a add ",
                Parameter("IP", None),
                "/",
                Parameter("MASK", "MASK-N"),
                " dev ",
                Parameter("ETH", "ETH-A"),
                " ",
                None,
                " ",
                Parameter("WORD", "X"),
                " <raw>",
            ),
        )
        self.assertEqual(pattern.names, ("MASK-N", "ETH-A", "X"))

    def test_keeps_shell_redirections_literal(self) -> None:
        """Treat ``< /dev/ttyS1`` as text, not as a placeholder."""
        pattern = compile_pattern("stty raw -echo < /dev/ttyS1")

        self.assertEqual(pattern.parts, ("stty raw -echo < /dev/ttyS1",))
        self.assertEqual(pattern.match("stty raw -echo < /dev/ttyS1", {}), {})

    def test_typed_parameters_validate_structure(self) -> None:
        """Accept only valid IPv4 addresses, ethN names and prefixes 0-32."""
        pattern = compile_pattern("ip a add <IP>/<MASK> dev <ETH>")

        self.assertEqual(pattern.match("ip a add 10.0.0.1/24 dev eth1", {}), {})
        for text in (
            "ip a add 10.0.0.256/24 dev eth1",
            "ip a add 10.0.0.1/33 dev eth1",
            "ip a add 10.0.0.1/24 dev enp0s8",
            "ip a add 10.0.0/24 dev eth1",
        ):
            with self.subTest(text=text):
                self.assertIsNone(pattern.match(text, {}))

    def test_labels_bind_and_require_the_same_value(self) -> None:
        """Bind a labeled value once and require it afterwards."""
        ping = compile_pattern("ping -c3 <IP-A>")
        trace = compile_pattern("traceroute <IP-A>")

        bindings = ping.match("ping -c3 10.0.0.2", {})
        self.assertEqual(bindings, {"IP-A": "10.0.0.2"})
        self.assertEqual(trace.match("traceroute 10.0.0.2", bindings), bindings)
        self.assertIsNone(trace.match("traceroute 10.0.0.3", bindings))
        self.assertEqual(compile_pattern("traceroute <IP>").match("traceroute 10.0.0.3", bindings), bindings)

    def test_free_parameters_bind_words(self) -> None:
        """Bind one word for names that are not parameter types."""
        pattern = compile_pattern("ip link add <X> type bridge")

        self.assertEqual(pattern.match("ip link add br0 type bridge", {}), {"X": "br0"})
        self.assertIsNone(pattern.match("ip link add my br type bridge", {}))
        self.assertIsNone(pattern.match("ip link add br1 type bridge", {"X": "br0"}))

    def test_wildcards_match_any_text_in_a_line(self) -> None:
        """Let ``<*>`` absorb varying text such as times."""
        pattern = compile_pattern("64 bytes from <IP-A>: icmp_seq=<*> time=<*>")

        self.assertIsNotNone(pattern.match("64 bytes from 10.0.0.1: icmp_seq=3 time=0.4 ms", {}))

    def test_rejects_labels_on_free_parameters(self) -> None:
        """Allow labels only on the typed parameters."""
        with self.assertRaisesRegex(CheckFileError, "labels are allowed only for IP, ETH, MASK"):
            compile_pattern("echo <X-1>")


class LoadCheckTests(unittest.TestCase):
    """Verify loading and validating check files."""

    def test_loads_a_complete_check_file(self) -> None:
        """Read expectations, items, defaults and the grading scale."""
        spec = _parse(VALID)

        self.assertEqual((spec.expected_task, spec.expected_host), (1, "first"))
        first, second = spec.commands
        self.assertEqual((first.id, first.points, first.after), ("addr", 1.0, None))
        self.assertEqual((second.id, second.after, second.points), (None, "addr", 2.0))
        block, table = spec.outputs
        self.assertEqual(block.of, "addr")
        self.assertEqual(block.order, "sequence")
        self.assertEqual(len(block.lines), 2)
        self.assertEqual(block.lines[0].source, "inet <IP-A>/<MASK> scope global <ETH-A>")
        self.assertIsNone(block.lines[1])
        self.assertEqual((table.order, table.min_count), ("any", 2))
        self.assertEqual(spec.total, 10.0)
        self.assertEqual(spec.scale[0], GradeStep(90.0, "5"))

    def test_reports_missing_files_and_toml_errors(self) -> None:
        """Fail with clear messages before any check runs."""
        with self.assertRaisesRegex(CheckFileError, "was not found"):
            load_check("/nonexistent/lab.check.toml")
        with self.assertRaisesRegex(CheckFileError, "is not valid TOML"):
            _parse("[[command]\ncmd = 1")

    def test_rejects_invalid_structures(self) -> None:
        """Explain each format violation with its location."""
        cases = [
            (_base(extra=1), "top level: unknown key"),
            (_base(command=[{"cmd": "a", "cmdd": "b"}]), "command #1: unknown key"),
            (_base(command=[{"id": "x"}]), "command #1 needs a non-empty cmd"),
            (_base(command=[{"cmd": "a", "after": "later"}, {"id": "later", "cmd": "b"}]), "must name an id of an earlier command"),
            (_base(command=[{"id": "x", "cmd": "a"}, {"id": "x", "cmd": "b"}]), 'duplicate id "x"'),
            (_base(output=[{"text": "a", "after": "x"}]), "after is allowed only for commands"),
            (_base(output=[{"text": "a", "of": "nothing"}]), "must name an id of a command"),
            (_base(output=[{"text": "a", "order": "random"}]), 'order must be "sequence" or "any"'),
            (_base(output=[{"text": "a", "min_count": 0}]), "min_count must be a positive integer"),
            (_base(output=[{"text": "a\n...\nb", "order": "any"}]), "not allowed with order"),
            (_base(output=[{"text": "\n...\n"}]), "text has no lines to match"),
            (_base(command=[{"cmd": "a", "points": 0}]), "points must be a positive number"),
            ({"grading": {"total": 1, "scale": [[0, "x"]]}}, "no [[command]] or [[output]] items"),
            (_base(grading=None), "[grading] table with total and scale is required"),
            (_base(grading={"total": 0, "scale": [[0, "x"]]}), "total must be a positive number"),
            (_base(grading={"total": 5, "scale": [[0, "2"], [50, "3"]]}), "from highest to lowest"),
            (_base(grading={"total": 5, "scale": [[120, "5"]]}), "between 0 and 100"),
            (_base(grading={"total": 5, "scale": [["a", "5"]]}), "threshold, \"mark\"] pairs"),
            (_base(report={"task": "one"}), "task must be an integer"),
        ]
        for data, message in cases:
            if data.get("grading", True) is None:
                del data["grading"]
            with self.subTest(message=message):
                with self.assertRaisesRegex(CheckFileError, _escape(message)):
                    parse_check(data, "lab.check.toml")


def _escape(text: str) -> str:
    """Escape a message for ``assertRaisesRegex``."""
    import re

    return re.escape(text)


if __name__ == "__main__":
    unittest.main()
