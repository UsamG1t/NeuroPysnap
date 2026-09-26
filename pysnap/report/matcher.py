"""Match a check file against a rendered report and grade the result.

Items are checked in file order: all ``[[command]]`` items, then all
``[[output]]`` items, so labels bound by commands reach the output blocks
(D12). A backtracking search chooses the matches: for every item it tries the
candidates in report order and also the possibility that the item fails, and
keeps the assignment with the most points; ties keep the earliest choices, so
the result is deterministic. A failing item therefore never takes the labels
of later items down with it.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from functools import lru_cache

from pysnap.report.checkfile import (
    ORDER_ANY,
    CheckSpec,
    CommandCheck,
    OutputCheck,
    Pattern,
    normalize_spacing,
)
from pysnap.report.models import ReportName, Transcript

DEFAULT_SEARCH_BUDGET = 200_000


@dataclass(frozen=True)
class ItemResult:
    """Represent the outcome of one check item.

    :param kind: ``"command"`` or ``"output"``.
    :param number: Position among the items of its kind, starting at 1.
    :param label: Item id or its first pattern line.
    :param pattern: Pattern text shown to the reader.
    :param passed: Whether the item passed.
    :param points: Weight of the item.
    :param command: Index of the matched report command, for commands.
    :param lines: Transcript lines of the first match, for outputs.
    :param count: Number of block occurrences found, for outputs.
    :param reason: Explanation of a failure.
    :param closest: Closest entered command for a failed command item.
    """

    kind: str
    number: int
    label: str
    pattern: str
    passed: bool
    points: float
    command: int | None = None
    lines: tuple[int, ...] = ()
    count: int = 0
    reason: str | None = None
    closest: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """Represent the graded result of a check file."""

    check_name: str
    items: tuple[ItemResult, ...]
    bindings: dict[str, str]
    earned_weight: float
    total_weight: float
    percent: float
    points: float
    total_points: float
    mark: str | None
    warnings: tuple[str, ...]
    search_exhausted: bool = False


@dataclass(frozen=True)
class _Match:
    """One way an item can pass."""

    bindings: dict[str, str]
    command: int | None = None
    lines: tuple[int, ...] = ()
    count: int = 0


class _Matcher:
    """Hold the report data and the search state."""

    def __init__(self, spec: CheckSpec, transcript: Transcript, budget: int) -> None:
        self.spec = spec
        self.transcript = transcript
        self.budget = budget
        self.steps = 0
        self.exhausted = False
        self.command_texts = [normalize_spacing(command.text) for command in transcript.commands]
        self.line_texts = [normalize_spacing(line.text) for line in transcript.lines]
        self.items: list[CommandCheck | OutputCheck] = [*spec.commands, *spec.outputs]
        self.command_by_id = {item.id: item for item in spec.commands if item.id}
        self.best_weight = -1.0
        self.best: dict[int, _Match] = {}

    def solve(self) -> dict[int, _Match]:
        """Return the best assignment of matches to item positions."""
        suffix = [0.0] * (len(self.items) + 1)
        for position in range(len(self.items) - 1, -1, -1):
            suffix[position] = suffix[position + 1] + self.items[position].points
        self._suffix = suffix
        self._search(0, {}, {}, 0.0)
        return self.best

    def _search(self, position: int, bindings: dict[str, str], chosen: dict[int, _Match], weight: float) -> None:
        if weight + self._suffix[position] <= self.best_weight and self.best_weight >= 0:
            return
        if position == len(self.items):
            self.best_weight = weight
            self.best = dict(chosen)
            return
        self.steps += 1
        if self.steps > self.budget:
            self.exhausted = True
        item = self.items[position]
        candidates = self._candidates(item, bindings, chosen)
        if self.exhausted:
            # Out of budget: finish this path greedily with the first
            # candidate so that a complete assignment is always available.
            if candidates:
                chosen[position] = candidates[0]
                self._search(position + 1, candidates[0].bindings, chosen, weight + item.points)
                del chosen[position]
            else:
                self._search(position + 1, bindings, chosen, weight)
            return
        for match in candidates:
            chosen[position] = match
            self._search(position + 1, match.bindings, chosen, weight + item.points)
            del chosen[position]
            if self.exhausted:
                return
        self._search(position + 1, bindings, chosen, weight)

    def _candidates(self, item, bindings: dict[str, str], chosen: dict[int, _Match]) -> list[_Match]:
        if isinstance(item, CommandCheck):
            return self._command_candidates(item, bindings, chosen)
        return self._output_candidates(item, bindings, chosen)

    def _command_candidates(self, item: CommandCheck, bindings, chosen) -> list[_Match]:
        earliest = 0
        if item.after is not None:
            parent = self._chosen_command(item.after, chosen)
            if parent is None:
                return []
            earliest = parent.command + 1
        matches = []
        for index in range(earliest, len(self.command_texts)):
            new_bindings = _match(item.pattern, self.command_texts[index], bindings)
            if new_bindings is not None:
                matches.append(_Match(new_bindings, command=index))
        return matches

    def _output_candidates(self, item: OutputCheck, bindings, chosen) -> list[_Match]:
        segments = self._segments(item, bindings, chosen)
        if segments is None:
            return []
        outcomes: dict[tuple, _Match] = {}
        for segment in segments:
            for found_bindings, lines in _block_matches(item, segment, self.line_texts, bindings):
                key = tuple(sorted(found_bindings.items()))
                if key in outcomes:
                    continue
                count = _count_blocks(item, segments, self.line_texts, found_bindings)
                if count >= item.min_count:
                    outcomes[key] = _Match(found_bindings, lines=lines, count=count)
        return list(outcomes.values())

    def _segments(self, item: OutputCheck, bindings, chosen) -> list[range] | None:
        """Return the transcript ranges an output block may be found in."""
        if item.of is None:
            return [range(len(self.line_texts))]
        parent_position = self.items.index(self.command_by_id[item.of])
        if parent_position not in chosen:
            return None
        parent = self.command_by_id[item.of]
        segments = []
        for command, text in zip(self.transcript.commands, self.command_texts):
            if _match(parent.pattern, text, bindings) is not None:
                segments.append(range(command.output_start, command.output_end))
        return segments

    def _chosen_command(self, item_id: str, chosen: dict[int, _Match]) -> _Match | None:
        position = self.items.index(self.command_by_id[item_id])
        return chosen.get(position)

    def explain(self, position: int, bindings: dict[str, str], chosen: dict[int, _Match]) -> tuple[str, str | None]:
        """Explain why an item failed in the final assignment."""
        item = self.items[position]
        if isinstance(item, CommandCheck):
            closest = difflib.get_close_matches(item.pattern.source, self.command_texts, n=1, cutoff=0.5)
            closest_text = closest[0] if closest else None
            if item.after is not None and self._chosen_command(item.after, chosen) is None:
                return f'its "after" command "{item.after}" did not pass', closest_text
            unbound = [index for index, text in enumerate(self.command_texts) if _match(item.pattern, text, {}) is not None]
            bound = [index for index in unbound if _match(item.pattern, self.command_texts[index], bindings) is not None]
            if bound and item.after is not None:
                return f'it was entered only before command "{item.after}"', closest_text
            if unbound:
                text = self.command_texts[unbound[0]]
                return f'"{text}" has other values than the labels bound earlier ({_describe(item.pattern, bindings)})', text
            return "no entered command matches", closest_text
        if item.of is not None and self._chosen_command(item.of, chosen) is None:
            return f'command "{item.of}" did not pass', None
        segments = self._segments(item, bindings, chosen) or []
        where = f'the output of "{item.of}"' if item.of else "the report"
        found = [match for segment in segments for match in _block_matches(item, segment, self.line_texts, bindings)]
        if found:
            count = _count_blocks(item, segments, self.line_texts, found[0][0])
            return f"found {count} time(s) in {where}, {item.min_count} required", None
        loose = [match for segment in segments for match in _block_matches(item, segment, self.line_texts, {})]
        if loose:
            return f"found in {where}, but with other values than the labels bound earlier ({_describe_block(item, bindings)})", None
        return f"not found in {where}", None


def run_check(
    spec: CheckSpec,
    transcript: Transcript,
    *,
    report_name: ReportName | None = None,
    budget: int = DEFAULT_SEARCH_BUDGET,
) -> CheckResult:
    """Check a transcript against a check file and grade it.

    :param spec: Loaded check file.
    :param transcript: Rendered report.
    :param report_name: Task and host from the report file name, used for the
        ``[report]`` expectations.
    :param budget: Largest number of search steps before the best assignment
        found so far is used.
    :returns: Graded result.
    """
    matcher = _Matcher(spec, transcript, budget)
    best = matcher.solve()

    bindings: dict[str, str] = {}
    for position in sorted(best):
        bindings = best[position].bindings
    results: list[ItemResult] = []
    for position, item in enumerate(matcher.items):
        kind = "command" if isinstance(item, CommandCheck) else "output"
        pattern = item.pattern.source if kind == "command" else _block_source(item)
        label = item.id if kind == "command" and item.id else pattern.split("\n")[0]
        match = best.get(position)
        if match is not None:
            results.append(
                ItemResult(kind, item.number, label, pattern, True, item.points,
                           command=match.command, lines=match.lines, count=match.count)
            )
        else:
            reason, closest = matcher.explain(position, bindings, best)
            results.append(
                ItemResult(kind, item.number, label, pattern, False, item.points,
                           reason=reason, closest=closest)
            )

    total_weight = sum(item.points for item in matcher.items)
    earned = sum(result.points for result in results if result.passed)
    percent = 100.0 * earned / total_weight if total_weight else 0.0
    mark = next((step.mark for step in spec.scale if percent + 1e-9 >= step.threshold), None)
    return CheckResult(
        check_name=spec.source_name,
        items=tuple(results),
        bindings=bindings,
        earned_weight=earned,
        total_weight=total_weight,
        percent=percent,
        points=spec.total * earned / total_weight if total_weight else 0.0,
        total_points=spec.total,
        mark=mark,
        warnings=_expectation_warnings(spec, transcript, report_name),
        search_exhausted=matcher.exhausted,
    )


def _match(pattern: Pattern, text: str, bindings: dict[str, str]) -> dict[str, str] | None:
    """Match one line, reusing compiled expressions for equal binding sets."""
    relevant = tuple(sorted((name, bindings[name]) for name in pattern.names if name in bindings))
    found = _cached_match(pattern, text, relevant)
    if found is None:
        return None
    merged = dict(bindings)
    merged.update(found)
    return merged


@lru_cache(maxsize=65536)
def _cached_match(pattern: Pattern, text: str, relevant: tuple) -> dict[str, str] | None:
    """Match with only the bindings the pattern uses."""
    return pattern.match(text, dict(relevant))


def _block_matches(item: OutputCheck, segment: range, lines: list[str], bindings):
    """Yield ``(bindings, line numbers)`` for block matches in a segment."""
    if item.order == ORDER_ANY:
        found = _match_any(list(item.lines), list(segment), lines, bindings)
        if found is not None:
            yield found
        return
    for start in segment:
        found = _match_sequence(list(item.lines), start, segment.stop, lines, bindings, [])
        if found is not None:
            yield found


def _match_sequence(patterns, index, stop, lines, bindings, used):
    """Match patterns from a line index; ``None`` patterns are gaps."""
    if not patterns:
        return bindings, tuple(used)
    head, rest = patterns[0], patterns[1:]
    if head is None:
        for skip in range(index, stop + 1):
            found = _match_sequence(rest, skip, stop, lines, bindings, used)
            if found is not None:
                return found
        return None
    if index >= stop:
        return None
    new_bindings = _match(head, lines[index], bindings)
    if new_bindings is None:
        return None
    return _match_sequence(rest, index + 1, stop, lines, new_bindings, used + [index])


def _match_any(patterns, candidates, lines, bindings):
    """Match every pattern to a distinct line in any order."""
    if not patterns:
        return bindings, ()
    head, rest = patterns[0], patterns[1:]
    for position, index in enumerate(candidates):
        new_bindings = _match(head, lines[index], bindings)
        if new_bindings is None:
            continue
        found = _match_any(rest, candidates[:position] + candidates[position + 1:], lines, new_bindings)
        if found is not None:
            return found[0], tuple(sorted((index, *found[1])))
    return None


def _count_blocks(item: OutputCheck, segments: list[range], lines: list[str], bindings) -> int:
    """Count non-overlapping block occurrences with fixed bindings."""
    count = 0
    for segment in segments:
        if item.order == ORDER_ANY:
            remaining = list(segment)
            while True:
                found = _match_any(list(item.lines), remaining, lines, bindings)
                if found is None:
                    break
                count += 1
                remaining = [index for index in remaining if index not in found[1]]
            continue
        start = segment.start
        while start < segment.stop:
            found = _match_sequence(list(item.lines), start, segment.stop, lines, bindings, [])
            if found is None or not found[1]:
                start += 1
                continue
            count += 1
            start = found[1][-1] + 1
    return count


def _block_source(item: OutputCheck) -> str:
    """Return the pattern text of a block."""
    return "\n".join("..." if line is None else line.source for line in item.lines)


def _describe(pattern: Pattern, bindings: dict[str, str]) -> str:
    """Describe the bound labels a pattern depends on."""
    names = [name for name in dict.fromkeys(pattern.names) if name in bindings]
    return ", ".join(f"{name}={bindings[name]}" for name in names) or "no labels"


def _describe_block(item: OutputCheck, bindings: dict[str, str]) -> str:
    """Describe the bound labels an output block depends on."""
    names = [name for line in item.lines if line is not None for name in line.names]
    names = [name for name in dict.fromkeys(names) if name in bindings]
    return ", ".join(f"{name}={bindings[name]}" for name in names) or "no labels"


def _expectation_warnings(spec: CheckSpec, transcript: Transcript, report_name: ReportName | None) -> tuple[str, ...]:
    """Compare the ``[report]`` expectations with the report (D36)."""
    if spec.expected_task is None and spec.expected_host is None:
        return ()
    identities = []
    if report_name is not None:
        identities.append(("file name", report_name.task, report_name.host))
    for command in transcript.commands:
        if command.prompt_info is not None:
            identities.append(("prompt", command.prompt_info.task, command.prompt_info.host))
    warnings = []
    for source, task, host in dict.fromkeys(identities):
        if spec.expected_task is not None and task != spec.expected_task:
            warnings.append(f"The check file expects task {spec.expected_task:02d}, but the report {source} shows task {task:02d}.")
        if spec.expected_host is not None and host != spec.expected_host:
            warnings.append(f'The check file expects host "{spec.expected_host}", but the report {source} shows host "{host}".')
    return tuple(warnings)
