"""Unit tests for the report player and the viewer helpers."""

from __future__ import annotations

import unittest

from pysnap.report.models import CellStyle, ReportArchive, StyledRun
from pysnap.report.player import PlaybackController, ReplayPlayer, SPEEDS
from pysnap.report.recording import parse_recording
from pysnap.report.render import render_transcript
from pysnap.report.viewer import (
    MARKER_STYLE,
    PagerState,
    clip_runs,
    cell_style_to_prompt_toolkit,
    format_player_status,
    screen_viewport,
    split_rows,
    style_runs_to_fragments,
)
from pysnap.tests.report_factory import ReportBuilder


def _player(builder: ReportBuilder, **kwargs) -> ReplayPlayer:
    """Build a player for a scripted session."""
    recording = parse_recording(
        ReportArchive(members=builder.finish(), mtimes={}), "report.01.first"
    )
    return ReplayPlayer(recording, render_transcript(recording), **kwargs)


def _screen(player: ReplayPlayer) -> list[str]:
    """Return the non-empty screen rows of the player."""
    return [row.rstrip() for row in player.emulator.screen.display if row.strip()]


def _session() -> ReportBuilder:
    """Return a session with long pauses before each command."""
    builder = ReportBuilder(columns=40, lines=6).show_prompt(delay=0.0)
    builder.run("hostname", "first\n", key_delay=0.1, think_delay=10.0)
    builder.run("whoami", "root\n", key_delay=0.1, think_delay=20.0)
    return builder


class ReplayPlayerTests(unittest.TestCase):
    """Verify the compressed timeline and seeking."""

    def test_compresses_pauses_to_the_maximum_delay(self) -> None:
        """Cap each pause at ``max_delay`` like ``scriptreplay -m``."""
        compressed = _player(_session(), max_delay=1.0)
        real = _player(_session(), max_delay=0)

        self.assertGreater(real.duration, 30.0)
        self.assertLess(compressed.duration, real.duration - 25.0)
        self.assertEqual(len(compressed.command_times), 2)

    def test_seeks_forward_and_backward(self) -> None:
        """Show the screen of any moment, replaying from the start if needed."""
        player = _player(_session())

        player.seek(player.duration)
        self.assertIn("root", _screen(player))
        self.assertTrue(player.finished)

        player.seek(player.command_times[1] - 0.01)
        self.assertIn("first", _screen(player))
        self.assertNotIn("root", _screen(player))
        self.assertEqual(player.current_command, 1)

        player.seek(-5)
        self.assertEqual(player.time, 0.0)
        self.assertEqual(_screen(player), ["[root@01-first ~]#"])

    def test_finds_neighbouring_commands(self) -> None:
        """Jump to the next command and back to the previous one."""
        player = _player(_session())
        first, second = player.command_times

        self.assertEqual(player.next_command_time(), first)
        player.seek(first)
        self.assertEqual(player.next_command_time(), second)
        self.assertEqual(player.previous_command_time(), 0.0)
        player.seek(second + 1.0)
        self.assertEqual(player.previous_command_time(), second)
        player.seek(second + 0.1)  # Within the grace period.
        self.assertEqual(player.previous_command_time(), first)

    def test_shows_invisible_characters_as_placeholders(self) -> None:
        """Keep the report rule of showing invisible characters as ``?``."""
        builder = ReportBuilder().show_prompt()
        builder.run("ls", "a‮b\n")
        player = _player(builder)
        player.seek(player.duration)

        self.assertIn("a?b", _screen(player))

    def test_applies_recorded_resizes_with_limits(self) -> None:
        """Follow ``SIGWINCH`` entries without accepting huge screens."""
        builder = ReportBuilder(columns=40, lines=6).show_prompt()
        builder.signal("SIGWINCH ROWS=900000 COLS=30")
        player = _player(builder, max_screen_size=100)
        player.seek(player.duration)

        self.assertEqual((player.emulator.screen.columns, player.emulator.screen.lines), (30, 100))


class PlaybackControllerTests(unittest.TestCase):
    """Verify the state driven by the ``show`` key bindings."""

    def test_ticks_advance_time_by_speed_and_stop_at_the_end(self) -> None:
        """Advance real time times speed and pause at the end."""
        controller = PlaybackController(_player(_session()), speed=2.0)

        controller.tick(0.5)
        self.assertAlmostEqual(controller.player.time, 1.0)
        controller.tick(10_000)
        self.assertTrue(controller.player.finished)
        self.assertFalse(controller.playing)

    def test_pause_speed_and_restart(self) -> None:
        """Pause and resume, clamp speed changes and restart after the end."""
        controller = PlaybackController(_player(_session()), speed=3.0)
        self.assertEqual(controller.speed, 2.0)  # Nearest supported speed.

        controller.toggle()
        controller.tick(5.0)
        self.assertEqual(controller.player.time, 0.0)

        for _ in range(10):
            controller.faster()
        self.assertEqual(controller.speed, SPEEDS[-1])
        for _ in range(10):
            controller.slower()
        self.assertEqual(controller.speed, SPEEDS[0])

        controller.to_end()
        self.assertFalse(controller.playing)
        controller.toggle()
        self.assertTrue(controller.playing)
        self.assertEqual(controller.player.time, 0.0)

    def test_command_navigation(self) -> None:
        """Move between command starts, the beginning and the end."""
        controller = PlaybackController(_player(_session()))
        first, second = controller.player.command_times

        controller.next_command()
        self.assertEqual(controller.player.time, first)
        controller.next_command()
        self.assertEqual(controller.player.time, second)
        controller.next_command()
        self.assertTrue(controller.player.finished)
        controller.previous_command()
        self.assertEqual(controller.player.time, second)
        controller.to_start()
        self.assertEqual(controller.player.time, 0.0)


class ViewerHelperTests(unittest.TestCase):
    """Verify the pure helpers behind the pager and the player window."""

    def test_pager_state_scrolls_within_bounds(self) -> None:
        """Clamp line and page scrolling to the text."""
        state = PagerState(line_count=30, height=10)

        state.page_down()
        self.assertEqual(state.visible_range(), range(10, 20))
        state.page_down()
        state.page_down()
        self.assertEqual(state.offset, 20)
        state.scroll(-25)
        self.assertEqual(state.offset, 0)
        state.to_bottom()
        state.set_height(28)
        self.assertEqual(state.offset, 2)
        state.set_height(40)
        self.assertEqual(state.visible_range(), range(0, 30))

    def test_pager_state_shifts_horizontally_by_half_windows(self) -> None:
        """Move the view like ``less -S`` and stop at the longest line."""
        state = PagerState(line_count=3, line_width=133, width=60)

        state.scroll_right()
        self.assertEqual(state.column, 30)
        state.scroll_right()
        state.scroll_right()
        self.assertEqual(state.column, 73)
        state.scroll_left()
        self.assertEqual(state.column, 43)
        state.set_width(140)
        self.assertEqual(state.column, 0)
        state.scroll_right()
        self.assertEqual(state.column, 0)

    def test_clip_runs_marks_hidden_text_on_both_sides(self) -> None:
        """Cut lines to the window and mark text beyond each edge."""
        bold = CellStyle(bold=True)
        runs = [StyledRun("abc", bold), StyledRun("defghij")]

        self.assertEqual(clip_runs(runs, 0, 20), [StyledRun("abc", bold), StyledRun("defghij")])
        self.assertEqual(
            clip_runs(runs, 0, 5),
            [StyledRun("abc", bold), StyledRun("d"), StyledRun(">", MARKER_STYLE)],
        )
        self.assertEqual(
            clip_runs(runs, 2, 4),
            [StyledRun("<", MARKER_STYLE), StyledRun("de"), StyledRun(">", MARKER_STYLE)],
        )
        self.assertEqual(clip_runs(runs, 6, 10), [StyledRun("<", MARKER_STYLE), StyledRun("hij")])
        self.assertEqual(clip_runs(runs, 20, 5), [])

    def test_viewport_follows_the_cursor_in_small_windows(self) -> None:
        """Clip the recorded screen around the cursor, preferring the bottom."""
        self.assertEqual(screen_viewport(24, 23, 30), range(0, 24))
        self.assertEqual(screen_viewport(24, 23, 10), range(14, 24))
        self.assertEqual(screen_viewport(24, 3, 10), range(0, 10))
        self.assertEqual(screen_viewport(24, 12, 10), range(3, 13))
        self.assertEqual(screen_viewport(24, 5, 0), range(5, 6))

    def test_split_rows_uses_newline_fragments(self) -> None:
        """Split screen fragments into rows."""
        rows = split_rows([("bold", "a"), ("", "b"), ("", "\n"), ("", "c")])

        self.assertEqual(rows, [[("bold", "a"), ("", "b")], [("", "c")]])

    def test_style_conversion_matches_the_connect_terminal(self) -> None:
        """Translate cell styles the same way ``pysnap connect`` does."""
        style = CellStyle(fg="brightred", bg="8a8a8a", bold=True, underscore=True)

        self.assertEqual(
            cell_style_to_prompt_toolkit(style),
            "fg:ansibrightred bg:#8a8a8a bold underline",
        )
        self.assertEqual(
            style_runs_to_fragments([StyledRun("x"), StyledRun("y", style)]),
            [("", "x"), ("fg:ansibrightred bg:#8a8a8a bold underline", "y")],
        )

    def test_player_status_mentions_clipping_first(self) -> None:
        """Place the clipping hint before the long key help."""
        controller = PlaybackController(_player(_session()))
        status = format_player_status(controller, "report.01.first", clipped=True)

        self.assertTrue(
            status.startswith(" report.01.first | window smaller than recording 40x6 | Playing x1")
        )
        self.assertIn("command 0/2", status)
        self.assertNotIn("window smaller", format_player_status(controller, "r", clipped=False))


if __name__ == "__main__":
    unittest.main()
