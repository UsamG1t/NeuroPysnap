"""Unit tests for replaying recordings into transcripts and commands."""

from __future__ import annotations

import unittest

from pysnap.report.models import CellStyle, PromptInfo, ReportArchive
from pysnap.report.recording import parse_recording
from pysnap.report.render import (
    DIAG_SCREEN_SIZE,
    DIAG_TRANSCRIPT_TRUNCATED,
    parse_prompt,
    render_transcript,
)
from pysnap.tests.report_factory import ReportBuilder


def _render(builder: ReportBuilder, **kwargs):
    """Finish a scripted session and render it."""
    archive = ReportArchive(members=builder.finish(), mtimes={})
    return render_transcript(parse_recording(archive, "report.01.first"), **kwargs)


def _texts(transcript) -> list[str]:
    """Return the entered commands of a transcript."""
    return [command.text for command in transcript.commands]


class PromptTests(unittest.TestCase):
    """Verify recognition of the prompt installed by ``report``."""

    def test_parses_report_prompts(self) -> None:
        """Split user, task, host and directory."""
        self.assertEqual(
            parse_prompt("[root@01-first ~]#"),
            PromptInfo(user="root", task=1, host="first", directory="~"),
        )
        self.assertEqual(
            parse_prompt("[student@12-R1.lab /etc/frr]$"),
            PromptInfo(user="student", task=12, host="R1.lab", directory="/etc/frr"),
        )

    def test_rejects_other_prompts(self) -> None:
        """Return ``None`` for prompts of other programs and plain prompts."""
        for prompt in ("R1#", "[root@first ~]#", "root@first:~#", ""):
            with self.subTest(prompt=prompt):
                self.assertIsNone(parse_prompt(prompt))


class RenderTranscriptTests(unittest.TestCase):
    """Verify the transcript text and command recovery."""

    def test_recovers_commands_and_their_output_ranges(self) -> None:
        """Capture each submitted line with its prompt, time and output."""
        builder = ReportBuilder().show_prompt()
        builder.run("ip a show eth1", "3: eth1: <UP>\n    inet 10.9.0.1/24\n")
        builder.run("ping -c1 10.9.0.2", "1 packets transmitted, 1 received\n")
        transcript = _render(builder)

        self.assertEqual(_texts(transcript), ["ip a show eth1", "ping -c1 10.9.0.2"])
        first, second = transcript.commands
        self.assertEqual(first.prompt, "[root@01-first ~]#")
        self.assertEqual(first.prompt_info, PromptInfo("root", 1, "first", "~"))
        self.assertEqual((first.line, first.column), (0, 19))
        self.assertEqual((first.output_start, first.output_end), (1, 3))
        self.assertEqual(second.line, 3)
        self.assertLess(first.time, second.time)
        self.assertEqual(
            [line.text for line in transcript.lines],
            [
                "[root@01-first ~]# ip a show eth1",
                "3: eth1: <UP>",
                "    inet 10.9.0.1/24",
                "[root@01-first ~]# ping -c1 10.9.0.2",
                "1 packets transmitted, 1 received",
                "[root@01-first ~]#",
                "exit",
            ],
        )

    def test_resolves_backspace_and_cursor_editing(self) -> None:
        """Take the command as the shell displays it after line editing."""
        builder = ReportBuilder().show_prompt()
        builder.type_text("ip link s")
        for _ in range(6):
            builder.key("\x7f", echo="\b \b")
        builder.type_text("a show eth1")
        builder.enter()
        builder.show_prompt()
        builder.type_text("ping 10.9.0.1")
        for _ in range(8):
            builder.key("\x1b[D", echo="\b")
        for character in "-c5 ":
            builder.key(character, echo=f"\x1b[1@{character}")
        builder.enter()
        builder.show_prompt()

        self.assertEqual(
            _texts(_render(builder)), ["ip a show eth1", "ping -c5 10.9.0.1"]
        )

    def test_recovers_commands_recalled_from_history_after_interrupt(self) -> None:
        """Ignore ``Ctrl-C`` input and read a recalled command from screen."""
        builder = ReportBuilder().show_prompt()
        builder.run("ping 10.9.0.1", "64 bytes from 10.9.0.1\n64 bytes from 10.9.0.1\n")
        # The run above already printed the next prompt; interrupt a second
        # ping instead to exercise Ctrl-C while a program is running.
        builder.type_text("ping 10.9.0.2")
        builder.enter()
        builder.output("64 bytes from 10.9.0.2\r\n")
        builder.key("\x03", echo="^C")
        builder.output("\r\n--- 10.9.0.2 ping statistics ---\r\n")
        builder.show_prompt()
        builder.key("\x1b[A", echo="ping 10.9.0.2")
        builder.enter()
        builder.show_prompt()

        self.assertEqual(
            _texts(_render(builder)), ["ping 10.9.0.1", "ping 10.9.0.2", "ping 10.9.0.2"]
        )

    def test_ignores_keys_typed_while_a_program_runs(self) -> None:
        """Start the command after the prompt printed behind typed-ahead keys."""
        builder = ReportBuilder().show_prompt()
        builder.type_text("sleep 1")
        builder.enter()
        builder.type_text("ls")  # Echoed by the tty while sleep runs.
        builder.show_prompt()
        builder.output("ls")  # Readline redisplays the pending input.
        builder.enter()
        builder.show_prompt()

        self.assertEqual(_texts(_render(builder)), ["sleep 1", "ls"])

    def test_joins_commands_wrapped_over_several_lines(self) -> None:
        """Read a long command across the rows it wraps onto."""
        builder = ReportBuilder(columns=30).show_prompt()
        long_command = "echo " + "0123456789" * 4
        builder.run(long_command, "0123456789" * 4 + "\n")
        transcript = _render(builder)

        self.assertEqual(_texts(transcript), [long_command])
        self.assertEqual(transcript.commands[0].output_start, 3)  # 19 + 45 chars

    def test_handles_prompts_of_other_programs(self) -> None:
        """Capture input typed at a foreign prompt such as ``vtysh``."""
        builder = ReportBuilder().show_prompt()
        builder.type_text("vtysh")
        builder.enter()
        builder.output("\r\nHello, this is FRRouting\r\n\r\nR1# ")
        builder.type_text("show ip route")
        builder.enter()
        builder.output("C>* 10.0.0.0/24 is directly connected\r\nR1# ")
        transcript = _render(builder)

        self.assertEqual(_texts(transcript), ["vtysh", "show ip route"])
        foreign = transcript.commands[1]
        self.assertEqual(foreign.prompt, "R1#")
        self.assertIsNone(foreign.prompt_info)

    def test_skips_empty_input_lines(self) -> None:
        """Do not count a bare Enter as a command."""
        builder = ReportBuilder().show_prompt()
        builder.enter()
        builder.show_prompt()
        builder.run("hostname", "first\n")

        self.assertEqual(_texts(_render(builder)), ["hostname"])

    def test_keeps_lines_scrolled_off_and_cleared_from_the_screen(self) -> None:
        """Retain the whole session text across scrolling and ``clear``."""
        builder = ReportBuilder(lines=5).show_prompt()
        builder.run("seq 8", "".join(f"{n}\n" for n in range(1, 9)))
        builder.type_text("clear")
        builder.enter()
        builder.output("\x1b[H\x1b[2J\x1b[3J")
        builder.show_prompt()
        builder.run("hostname", "first\n")
        transcript = _render(builder)

        texts = [line.text for line in transcript.lines]
        self.assertEqual(texts[:10], ["[root@01-first ~]# seq 8", *map(str, range(1, 9)), "[root@01-first ~]# clear"])
        self.assertEqual(texts[10:], ["[root@01-first ~]# hostname", "first", "[root@01-first ~]#", "exit"])
        hostname = transcript.commands[2]
        self.assertEqual((hostname.text, hostname.line), ("hostname", 10))
        self.assertEqual(transcript.lines[hostname.output_start].text, "first")

    def test_applies_recorded_terminal_resizes(self) -> None:
        """Keep lines when a resize shrinks the screen and use the new width."""
        builder = ReportBuilder(columns=40, lines=6).show_prompt()
        builder.run("seq 3", "1\n2\n3\n")
        builder.signal("SIGWINCH ROWS=3 COLS=20")
        builder.run("echo " + "x" * 30, "x" * 30 + "\n")
        transcript = _render(builder)

        texts = [line.text for line in transcript.lines]
        self.assertEqual(texts[:4], ["[root@01-first ~]# seq 3", "1", "2", "3"])
        self.assertEqual(_texts(transcript), ["seq 3", "echo " + "x" * 30])

    def test_keeps_guest_colors_and_replaces_unsafe_characters(self) -> None:
        """Preserve SGR colors as styles and neutralize bidi overrides."""
        builder = ReportBuilder().show_prompt()
        builder.run("ls", "\x1b[1;34mdir\x1b[0m file‮txt.exe\n")
        transcript = _render(builder)

        line = transcript.lines[1]
        self.assertEqual(line.text, "dir file?txt.exe")
        self.assertEqual(line.runs[0].text, "dir")
        self.assertEqual(line.runs[0].style, CellStyle(fg="blue", bold=True))

    def test_clamps_hostile_screen_sizes(self) -> None:
        """Limit huge recorded sizes instead of rendering giant screens."""
        builder = ReportBuilder(columns=100_000).show_prompt()
        builder.run("hostname", "first\n")
        builder.signal("SIGWINCH ROWS=500000 COLS=40")
        transcript = _render(builder, max_screen_size=200)

        self.assertEqual(_texts(transcript), ["hostname"])
        self.assertEqual(
            [item.code for item in transcript.diagnostics], [DIAG_SCREEN_SIZE]
        )

    def test_limits_the_transcript_length(self) -> None:
        """Cut very long transcripts with a diagnostic."""
        builder = ReportBuilder(lines=5).show_prompt()
        builder.run("seq 50", "".join(f"{n}\n" for n in range(1, 51)))
        transcript = _render(builder, max_lines=10)

        self.assertEqual(len(transcript.lines), 10)
        self.assertEqual(
            [item.code for item in transcript.diagnostics], [DIAG_TRANSCRIPT_TRUNCATED]
        )
        self.assertEqual(_texts(transcript), ["seq 50"])


if __name__ == "__main__":
    unittest.main()
