"""Timed replay of a recording for ``pysnap report show``.

The player runs without any user interface: it owns a terminal emulator of
the recorded size and moves it to any moment of the recording. Playback uses a
compressed timeline in which every pause is limited to ``max_delay`` seconds,
like ``scriptreplay -m``. Seeking backwards replays from the start, which is
fast because the output is only parsed, never drawn.
"""

from __future__ import annotations

from pysnap.report.models import Recording, Transcript
from pysnap.report.render import DEFAULT_MAX_SCREEN_SIZE, parse_resize
from pysnap.terminal.emulator import TerminalEmulator

SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
DEFAULT_MAX_DELAY = 1.0
DEFAULT_HISTORY = 10_000
# Pressing "previous command" shortly after a command started goes to the
# command before it, as media players do with tracks.
PREVIOUS_COMMAND_GRACE = 0.5


class ReplayPlayer:
    """Move a terminal emulator along the timeline of a recording."""

    def __init__(
        self,
        recording: Recording,
        transcript: Transcript,
        *,
        max_delay: float | None = DEFAULT_MAX_DELAY,
        max_screen_size: int = DEFAULT_MAX_SCREEN_SIZE,
        history: int = DEFAULT_HISTORY,
    ) -> None:
        """Prepare the timeline.

        :param recording: Parsed recording.
        :param transcript: Rendered transcript, used for command positions.
        :param max_delay: Longest pause kept on the timeline in seconds;
            ``None`` or a non-positive value keeps the real pauses.
        :param max_screen_size: Largest accepted screen width and height.
        :param history: Scrollback lines kept by the emulator.
        """
        self.recording = recording
        self.max_screen_size = max_screen_size
        self.history = history
        self.columns = min(recording.columns or 80, max_screen_size)
        self.lines = min(recording.lines or 24, max_screen_size)

        elapsed = 0.0
        self.event_times: list[float] = []
        for event in recording.events:
            delay = event.delay
            if max_delay is not None and max_delay > 0:
                delay = min(delay, max_delay)
            elapsed += delay
            self.event_times.append(elapsed)
        self.duration = elapsed
        self.command_times = [
            self.event_times[command.first_event]
            for command in transcript.commands
            if command.first_event < len(self.event_times)
        ]
        self._reset()

    @property
    def finished(self) -> bool:
        """Return whether the player stands at the end of the recording."""
        return self.time >= self.duration

    @property
    def current_command(self) -> int:
        """Return how many commands have started up to the current moment."""
        return sum(1 for time in self.command_times if time <= self.time)

    def seek(self, time: float) -> None:
        """Show the screen as it was at a moment of the compressed timeline.

        :param time: Target moment in seconds, clamped to the recording.
        """
        target = min(max(time, 0.0), self.duration)
        if target < self.time:
            self._reset()
        events = self.recording.events
        while self._position < len(events) and self.event_times[self._position] <= target:
            self._apply(self._position)
            self._position += 1
        self.time = target

    def next_command_time(self) -> float | None:
        """Return the start of the first command after the current moment."""
        return next((time for time in self.command_times if time > self.time), None)

    def previous_command_time(self) -> float:
        """Return the start of the command before the current one, or 0."""
        earlier = [
            time for time in self.command_times if time < self.time - PREVIOUS_COMMAND_GRACE
        ]
        return earlier[-1] if earlier else 0.0

    def _reset(self) -> None:
        """Start over with an empty screen."""
        self.emulator = TerminalEmulator(
            self.columns,
            self.lines,
            history=self.history,
            invisible_replacement="?",
        )
        self._position = 0
        self.time = 0.0

    def _apply(self, index: int) -> None:
        """Apply one recorded event to the emulator."""
        event = self.recording.events[index]
        if event.kind == "O":
            self.emulator.feed(event.data)
        elif event.kind == "S":
            size = parse_resize(event.data)
            if size is not None:
                rows, columns = size
                self.emulator.resize(
                    min(columns, self.max_screen_size), min(rows, self.max_screen_size)
                )


class PlaybackController:
    """Hold the playback state driven by the ``show`` key bindings."""

    def __init__(self, player: ReplayPlayer, speed: float = 1.0) -> None:
        """Initialize a controller that plays from the start.

        :param player: Replay player.
        :param speed: Initial speed; the nearest supported speed is used.
        """
        self.player = player
        self.playing = True
        self.speed_index = min(
            range(len(SPEEDS)), key=lambda index: abs(SPEEDS[index] - speed)
        )

    @property
    def speed(self) -> float:
        """Return the current playback speed."""
        return SPEEDS[self.speed_index]

    def tick(self, seconds: float) -> None:
        """Advance playback by real elapsed time.

        :param seconds: Real seconds since the previous tick.
        """
        if not self.playing:
            return
        self.player.seek(self.player.time + seconds * self.speed)
        if self.player.finished:
            self.playing = False

    def toggle(self) -> None:
        """Pause or resume; resuming at the end starts over."""
        if not self.playing and self.player.finished:
            self.player.seek(0.0)
        self.playing = not self.playing

    def faster(self) -> None:
        """Select the next higher speed."""
        self.speed_index = min(self.speed_index + 1, len(SPEEDS) - 1)

    def slower(self) -> None:
        """Select the next lower speed."""
        self.speed_index = max(self.speed_index - 1, 0)

    def next_command(self) -> None:
        """Jump to the start of the next command, or to the end."""
        target = self.player.next_command_time()
        self.player.seek(self.player.duration if target is None else target)

    def previous_command(self) -> None:
        """Jump to the start of the previous command, or to the start."""
        self.player.seek(self.player.previous_command_time())

    def to_start(self) -> None:
        """Jump to the start of the recording."""
        self.player.seek(0.0)

    def to_end(self) -> None:
        """Jump to the end of the recording and pause."""
        self.player.seek(self.player.duration)
        self.playing = False
