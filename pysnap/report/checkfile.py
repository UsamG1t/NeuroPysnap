"""Check files: expected commands, output blocks and grading.

A check file is TOML (decision D17)::

    [report]              # optional expectations, reported as warnings
    task = 1
    host = "first"

    [[command]]
    id = "addr"           # optional, referenced by ``of`` and ``after``
    cmd = "ip a show <ETH-A>"
    after = "other-id"    # optional, commands only (D18, D34)
    points = 1            # optional weight, default 1

    [[output]]
    of = "addr"           # optional: search only the output of that command
    order = "any"         # optional: lines in any order (default: sequence)
    min_count = 3         # optional: the block must occur this many times
    text = '''
    inet <IP-A>/<MASK> scope global <ETH-A>
    '''

    [grading]
    total = 10
    scale = [[90, "5"], [75, "4"], [50, "3"], [0, "2"]]

Parameters (D11): ``<IP>``, ``<ETH>`` and ``<MASK>`` match any value of their
type; ``<IP-A>``-style labels bind the first matched value and require it
later; any other name such as ``<X>`` binds one word. ``<*>`` matches any
text inside a line and a line holding only ``...`` matches any number of
lines. Whitespace is normalized on both sides (D35).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import tomllib

from pysnap.errors import PySnapError

PARAMETER_TYPES = {
    "IP": r"(?:\d{1,3}\.){3}\d{1,3}",
    "ETH": r"eth\d+",
    "MASK": r"\d{1,2}",
}
WORD_PATTERN = r"\S+"
GAP_LINE = "..."
ORDER_SEQUENCE = "sequence"
ORDER_ANY = "any"

_TOKEN_PATTERN = re.compile(r"\\<|<(\*|[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)?)>")
_WHITESPACE = re.compile(r"\s+")

_REPORT_KEYS = {"task", "host"}
_COMMAND_KEYS = {"id", "cmd", "after", "points"}
_OUTPUT_KEYS = {"of", "text", "order", "min_count", "points"}
_GRADING_KEYS = {"total", "scale"}
_TOP_LEVEL_KEYS = {"report", "command", "output", "grading"}


class CheckFileError(PySnapError):
    """Report a check file that cannot be used."""


@dataclass(frozen=True)
class Parameter:
    """Describe one ``<...>`` placeholder of a pattern.

    :param kind: ``"IP"``, ``"ETH"``, ``"MASK"`` or ``"WORD"`` for free names.
    :param name: Binding name, ``None`` for unlabeled typed parameters.
    """

    kind: str
    name: str | None


@dataclass(frozen=True)
class Pattern:
    """Represent one line pattern split into literal text and parameters.

    :param source: Pattern text as written, whitespace-normalized.
    :param parts: Literal strings, :class:`Parameter` objects and ``None``
        for ``<*>``, in order.
    """

    source: str
    parts: tuple[str | Parameter | None, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """Return the binding names used by the pattern."""
        return tuple(
            part.name for part in self.parts if isinstance(part, Parameter) and part.name
        )

    def match(self, text: str, bindings: dict[str, str]) -> dict[str, str] | None:
        """Match a whitespace-normalized line.

        :param text: Normalized line.
        :param bindings: Values already bound by earlier matches.
        :returns: Bindings including new values, or ``None`` without match.
        """
        regex, groups = self._compile(bindings)
        found = regex.fullmatch(text)
        if found is None:
            return None
        result = dict(bindings)
        for index, parameter in enumerate(groups):
            value = found.group(index + 1)
            if not _valid_value(parameter.kind, value):
                return None
            if parameter.name is not None:
                if result.setdefault(parameter.name, value) != value:
                    return None
        return result

    def _compile(self, bindings: dict[str, str]) -> tuple[re.Pattern[str], list[Parameter]]:
        """Build a regular expression with bound values as literals."""
        pieces: list[str] = []
        groups: list[Parameter] = []
        for part in self.parts:
            if part is None:
                pieces.append(".*?")
            elif isinstance(part, str):
                pieces.append(re.escape(part))
            elif part.name is not None and part.name in bindings:
                pieces.append(re.escape(bindings[part.name]))
            else:
                pieces.append(f"({PARAMETER_TYPES.get(part.kind, WORD_PATTERN)})")
                groups.append(part)
        return re.compile("".join(pieces)), groups


@dataclass(frozen=True)
class CommandCheck:
    """Represent one ``[[command]]`` item."""

    number: int
    id: str | None
    pattern: Pattern
    after: str | None
    points: float


@dataclass(frozen=True)
class OutputCheck:
    """Represent one ``[[output]]`` item.

    :param lines: Line patterns; ``None`` stands for a ``...`` gap.
    """

    number: int
    of: str | None
    lines: tuple[Pattern | None, ...]
    order: str
    min_count: int
    points: float


@dataclass(frozen=True)
class GradeStep:
    """Represent one step of the grading scale."""

    threshold: float
    mark: str


@dataclass(frozen=True)
class CheckSpec:
    """Represent a loaded check file."""

    source_name: str
    expected_task: int | None
    expected_host: str | None
    commands: tuple[CommandCheck, ...]
    outputs: tuple[OutputCheck, ...]
    total: float
    scale: tuple[GradeStep, ...]


def normalize_spacing(text: str) -> str:
    """Trim a line and collapse whitespace runs to single spaces (D35).

    :param text: Raw line.
    :returns: Normalized line.
    """
    return _WHITESPACE.sub(" ", text).strip()


def compile_pattern(text: str, where: str = "pattern") -> Pattern:
    """Compile one line pattern.

    :param text: Pattern line.
    :param where: Location used in error messages.
    :returns: Compiled pattern.
    :raises CheckFileError: For an unknown ``<NAME-label>`` form.
    """
    normalized = normalize_spacing(text)
    parts: list[str | Parameter | None] = []
    literal: list[str] = []
    position = 0
    for token in _TOKEN_PATTERN.finditer(normalized):
        literal.append(normalized[position:token.start()])
        position = token.end()
        if token.group(0) == "\\<":
            literal.append("<")
            continue
        if literal:
            parts.append("".join(literal))
            literal = []
        parts.append(_parameter(token.group(1), where))
    literal.append(normalized[position:])
    if "".join(literal):
        parts.append("".join(literal))
    return Pattern(normalized, tuple(part for part in parts if part != ""))


def load_check(path: str | Path) -> CheckSpec:
    """Load and validate a check file.

    :param path: Path to the TOML check file.
    :returns: Validated check specification.
    :raises CheckFileError: When the file is missing, is not valid TOML or
        does not follow the check file format.
    """
    check_path = Path(path)
    if not check_path.is_file():
        raise CheckFileError(f'Check file "{check_path}" was not found.')
    try:
        data = tomllib.loads(check_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise CheckFileError(f'Check file "{check_path}" is not valid TOML: {error}') from None
    return parse_check(data, check_path.name)


def parse_check(data: dict[str, Any], source_name: str) -> CheckSpec:
    """Validate parsed check file data.

    :param data: Parsed TOML document.
    :param source_name: File name used in messages.
    :returns: Validated check specification.
    :raises CheckFileError: When the data does not follow the format.
    """
    def fail(message: str) -> CheckFileError:
        return CheckFileError(f"Check file {source_name}: {message}")

    _reject_unknown(data, _TOP_LEVEL_KEYS, "top level", fail)
    report = _table(data.get("report", {}), "[report]", fail)
    _reject_unknown(report, _REPORT_KEYS, "[report]", fail)
    expected_task = report.get("task")
    if expected_task is not None and (isinstance(expected_task, bool) or not isinstance(expected_task, int)):
        raise fail("[report] task must be an integer.")
    expected_host = report.get("host")
    if expected_host is not None and not isinstance(expected_host, str):
        raise fail("[report] host must be a string.")

    commands: list[CommandCheck] = []
    known_ids: set[str] = set()
    for number, item in enumerate(_items(data, "command", fail), start=1):
        where = f"command #{number}"
        _reject_unknown(item, _COMMAND_KEYS, where, fail)
        cmd = item.get("cmd")
        if not isinstance(cmd, str) or not normalize_spacing(cmd):
            raise fail(f"{where} needs a non-empty cmd string.")
        item_id = _optional_string(item, "id", where, fail)
        after = _optional_string(item, "after", where, fail)
        if after is not None and after not in known_ids:
            raise fail(f'{where}: after = "{after}" must name an id of an earlier command.')
        if item_id is not None:
            if item_id in known_ids:
                raise fail(f'{where}: duplicate id "{item_id}".')
            known_ids.add(item_id)
        commands.append(
            CommandCheck(
                number=number,
                id=item_id,
                pattern=_pattern(cmd, where, fail),
                after=after,
                points=_points(item, where, fail),
            )
        )

    outputs: list[OutputCheck] = []
    for number, item in enumerate(_items(data, "output", fail), start=1):
        where = f"output #{number}"
        if "after" in item:
            raise fail(f"{where}: after is allowed only for commands; use of for outputs.")
        _reject_unknown(item, _OUTPUT_KEYS, where, fail)
        of = _optional_string(item, "of", where, fail)
        if of is not None and of not in known_ids:
            raise fail(f'{where}: of = "{of}" must name an id of a command.')
        order = item.get("order", ORDER_SEQUENCE)
        if order not in (ORDER_SEQUENCE, ORDER_ANY):
            raise fail(f'{where}: order must be "{ORDER_SEQUENCE}" or "{ORDER_ANY}".')
        min_count = item.get("min_count", 1)
        if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
            raise fail(f"{where}: min_count must be a positive integer.")
        text = item.get("text")
        if not isinstance(text, str):
            raise fail(f"{where} needs a text string.")
        patterns = tuple(
            None if normalize_spacing(raw) == GAP_LINE else _pattern(raw, where, fail)
            for raw in _block_lines(text)
        )
        if all(pattern is None for pattern in patterns):
            raise fail(f"{where}: text has no lines to match.")
        if order == ORDER_ANY and any(pattern is None for pattern in patterns):
            raise fail(f'{where}: "..." lines are not allowed with order = "any".')
        outputs.append(
            OutputCheck(
                number=number,
                of=of,
                lines=patterns,
                order=order,
                min_count=min_count,
                points=_points(item, where, fail),
            )
        )
    if not commands and not outputs:
        raise fail("it defines no [[command]] or [[output]] items.")

    grading = data.get("grading")
    if grading is None:
        raise fail("the [grading] table with total and scale is required.")
    grading = _table(grading, "[grading]", fail)
    _reject_unknown(grading, _GRADING_KEYS, "[grading]", fail)
    total = grading.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total <= 0:
        raise fail("[grading] total must be a positive number.")
    scale = _scale(grading.get("scale"), fail)

    return CheckSpec(
        source_name=source_name,
        expected_task=expected_task,
        expected_host=expected_host,
        commands=tuple(commands),
        outputs=tuple(outputs),
        total=float(total),
        scale=scale,
    )


def _parameter(name: str, where: str) -> Parameter | None:
    """Translate one placeholder name."""
    if name == "*":
        return None
    kind, _, label = name.partition("-")
    if kind in PARAMETER_TYPES:
        return Parameter(kind, name if label else None)
    if label:
        raise CheckFileError(
            f"{where}: <{name}> uses a label, but labels are allowed only for "
            f"{', '.join(PARAMETER_TYPES)}."
        )
    return Parameter("WORD", name)


def _valid_value(kind: str, value: str) -> bool:
    """Check a matched value against the structure of its type."""
    if kind == "IP":
        return all(int(octet) <= 255 for octet in value.split("."))
    if kind == "MASK":
        return 0 <= int(value) <= 32
    return True


def _pattern(text: str, where: str, fail) -> Pattern:
    """Compile a pattern, wrapping errors with the file name."""
    try:
        return compile_pattern(text, where)
    except CheckFileError as error:
        raise fail(str(error)) from None


def _block_lines(text: str) -> list[str]:
    """Split a text block, dropping blank lines at its start and end."""
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _items(data: dict[str, Any], key: str, fail) -> list[dict[str, Any]]:
    """Return an array of tables."""
    items = data.get(key, [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise fail(f"{key} items must be written as [[{key}]] tables.")
    return items


def _table(value: Any, where: str, fail) -> dict[str, Any]:
    """Return a table or fail."""
    if not isinstance(value, dict):
        raise fail(f"{where} must be a table.")
    return value


def _reject_unknown(table: dict[str, Any], allowed: set[str], where: str, fail) -> None:
    """Fail on keys that the format does not define."""
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise fail(f"{where}: unknown key(s) {', '.join(unknown)}.")


def _optional_string(item: dict[str, Any], key: str, where: str, fail) -> str | None:
    """Return an optional non-empty string value."""
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise fail(f"{where}: {key} must be a non-empty string.")
    return value


def _points(item: dict[str, Any], where: str, fail) -> float:
    """Return the weight of an item (default 1)."""
    points = item.get("points", 1)
    if isinstance(points, bool) or not isinstance(points, (int, float)) or points <= 0:
        raise fail(f"{where}: points must be a positive number.")
    return float(points)


def _scale(value: Any, fail) -> tuple[GradeStep, ...]:
    """Validate the grading scale: thresholds in percent, strictly descending."""
    if not isinstance(value, list) or not value:
        raise fail('[grading] scale must be a non-empty list of [threshold, "mark"] pairs.')
    steps: list[GradeStep] = []
    for entry in value:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or isinstance(entry[0], bool)
            or not isinstance(entry[0], (int, float))
            or not isinstance(entry[1], str)
        ):
            raise fail('[grading] scale entries must be [threshold, "mark"] pairs.')
        if not 0 <= entry[0] <= 100:
            raise fail("[grading] scale thresholds must be between 0 and 100.")
        steps.append(GradeStep(float(entry[0]), entry[1]))
    thresholds = [step.threshold for step in steps]
    if thresholds != sorted(thresholds, reverse=True) or len(set(thresholds)) != len(thresholds):
        raise fail("[grading] scale thresholds must be listed from highest to lowest.")
    return tuple(steps)
