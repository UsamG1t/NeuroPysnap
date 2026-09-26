Architecture
============

PySnap is organized into a few focused packages.

Package Layout
--------------

- ``pysnap.cli`` contains argument parsing and terminal output formatting.
- ``pysnap.config`` contains persistent configuration helpers.
- ``pysnap.core`` contains domain models and the main application service.
- ``pysnap/docs`` stores compiled HTML documentation bundled with the wheel.
- ``pysnap.report`` reads session reports recorded inside VMs by the
  ``report`` utility: safe archive loading, parsing of the ``script``
  recording, rendering of the session text and commands, timed replay and
  the interactive pager and player.
- ``pysnap.runtime`` contains the live-session registry shared by runtime tools.
- ``pysnap.terminal`` contains the built-in serial terminal transport, emulator,
  key mapping, terminal-query responder, and interactive UI session logic.
- ``pysnap.vbox`` contains the ``VBoxManage`` client and output parsers.
- ``pysnap.tests`` contains unit tests.

Package Relationship Diagram
----------------------------

.. only:: graphviz

   .. graphviz::
      :caption: High-level relationships between PySnap packages.

      digraph package_relationships {
          rankdir=LR;
          node [shape=box, style="rounded,filled", fillcolor="#f6f6f6"];

          cli [label="pysnap.cli"];
          config [label="pysnap.config"];
          core [label="pysnap.core"];
          report [label="pysnap.report"];
          runtime [label="pysnap.runtime"];
          terminal [label="pysnap.terminal"];
          vbox [label="pysnap.vbox"];

          cli -> core [label="command dispatch"];
          cli -> terminal [label="connect"];
          cli -> report [label="report"];
          report -> terminal [label="emulator, UI controls"];
          core -> config [label=".ptotosettings"];
          core -> runtime [label="session registry"];
          core -> vbox [label="VBoxManage operations"];
          terminal -> core [label="prepare VM"];
          terminal -> runtime [label="live session"];
      }

.. only:: not graphviz

   Graphviz relationship diagrams are enabled automatically when the ``dot``
   executable is available on the build host.

Execution Flow
--------------

1. The CLI entry point parses command line arguments.
2. ``PySnapService`` validates the request and coordinates the workflow.
3. ``VBoxManageClient`` executes VirtualBox commands.
4. Parser helpers convert command output into Python models.
5. ``SessionRegistry`` tracks active terminal attachments across processes.
6. ``TerminalSession`` starts the headless connection workflow when interactive
   serial access is requested.
7. The CLI renders human-readable results.

Metadata Strategy
-----------------

PySnap stores management metadata in VirtualBox extra data entries with the
``pysnap/`` prefix. These values are used to:

- mark imported and cloned VMs as managed by PySnap
- record clone ancestry
- support dependency checks before erase operations
- select linked clones for ``erase --clones-only`` without a separate registry

Serial Port Strategy
--------------------

PySnap uses ``UART1`` for the serial TCP endpoint of clones. If no explicit
port is supplied, the service automatically assigns ``max(used_ports) + 1`` or
``1024`` when no TCP serial port has been configured yet.

The ``plug`` workflow reuses the same ``UART1 tcpserver`` strategy for existing
VMs. It only rewrites ``UART1`` when the VM is stopped and the current backend
is not already occupied by another mode such as ``tcpclient`` or ``file``.
When needed, the selected TCP port must be available both in VirtualBox and on
the host system.

During ``pysnap connect``, PySnap continuously tracks the outer terminal size
and resizes the local emulator to the current visible guest area. Because raw
serial TCP does not offer a PTY-style ``SIGWINCH`` path into the guest, PySnap
also replies to xterm-compatible in-band terminal queries such as ``CSI 18 t``
and ``CSI 6 n`` so guest-side Linux tools can rediscover the current geometry.

Terminal Selection Strategy
---------------------------

Mouse selection in ``pysnap connect`` only captures text; the copy itself is an
explicit ``Ctrl-Shift-C`` action. Classic terminals transmit ``Ctrl-Shift-C``
and ``Ctrl-C`` as the same ``ETX`` byte, so PySnap resolves the chord by
selection state: with a captured selection it copies to the host clipboard,
without one it forwards a real interrupt to the guest. Incoming guest output
drops only the visible highlight and keeps the captured text, which makes
copying safe while background utilities continue printing.

Proto Settings Strategy
-----------------------

PySnap stores proto-settings base VM names in ``Path.home() / ".ptotosettings"``
with one VM name per line. When a clone is created from a registered base VM,
PySnap writes additional DMI settings through ``VBoxManage setextradata``:

- ``DmiSystemVendor = <CloneVM>``
- ``DmiSystemSKU = port<Port>[.<net1>[.<net2>[.<net3>]]]``

This mode exists for educational VirtualBox images used in CMC MSU courses.

Runtime State Strategy
----------------------

PySnap translates raw VirtualBox runtime states into compact monitor labels:

- ``Working`` for running VMs with an active PySnap terminal session
- ``Active`` for running VMs without an attached PySnap terminal session
- ``Stopping`` for graceful shutdown in progress
- ``Changing`` for startup and other transitional states
- ``Paused`` for paused VMs
- ``Error`` for explicit error-like VirtualBox states

Host Cleanup Strategy
---------------------

The ``full-clean`` command removes the VirtualBox machine folder and the
platform-specific configuration directory directly from the host file system
through ``shutil.rmtree``. The CLI layer owns the double confirmation dialog,
while ``PySnapService.full_clean`` performs the removal and reports partial
failures. The ``VBOX_USER_HOME`` environment variable overrides the detected
configuration directory, and the repeated ``--path`` option replaces the
default selection entirely.

Listing Robustness Strategy
---------------------------

``VBoxManage list`` can block indefinitely when the VirtualBox configuration
directory is missing while a stale ``VBoxSVC`` process is still running, for
example right after ``pysnap full-clean``. ``VBoxManageClient`` therefore runs
the ``list`` commands with a bounded timeout and reports expired timeouts as
regular command errors. The ``list`` CLI command translates such failures into
an empty listing, so the user sees ``No virtual machines found.`` instead of a
hanging process. Other ``VBoxManage`` operations keep running without a
timeout because imports, snapshots, and clone creation are legitimately
long-running.

Report Reading Strategy
-----------------------

Students record their work inside the educational VMs with the ``report``
utility from the `vbsnap <https://github.com/FrBrGeorge/vbsnap>`_ scripts. It
runs ``script -I IN.txt -O OUT.txt -B BOTH.txt -T TIME.txt`` and packs the
logs together with ``lscpu`` output (``CPU.txt``) into a ``tar.gz`` file named
``report.<NN>.<host>``.

A report is untrusted input, so ``pysnap.report.archive`` never extracts it
to disk. The archive is streamed once through a byte counter: only regular
files with the five known names are read into memory, while links, devices,
path-traversal names and unknown entries are skipped with a diagnostic. The
compressed size, the size of one member and the total decompressed size are
bounded by ``ReportLimits``, which protects against decompression bombs.

``pysnap.report.recording`` parses the multi-stream timing log of
util-linux ``script``. ``H`` lines carry headers such as ``START_TIME``,
``COLUMNS``, ``DURATION`` and ``EXIT_CODE``; ``I`` and ``O`` lines carry the
byte sizes of input and output chunks; ``S`` lines carry signals such as
terminal resizes. The byte sizes address the stream logs between their
``Script started on`` line and ``Script done on`` trailer, so the parser can
check that the timing log and the stream logs describe exactly the same bytes.
Truncated or inconsistent reports produce diagnostics instead of errors; only
a report without ``TIME.txt`` or ``OUT.txt`` is rejected.

Report Rendering Strategy
-------------------------

``pysnap.report.render`` replays the recorded output through a ``pyte``
screen of the recorded size and applies recorded terminal resizes. Lines that
scroll off the top, lines dropped when the screen shrinks and the visible
text wiped by a full-screen erase such as ``clear`` are copied into the
transcript, so the transcript holds everything the student saw. Characters
without width that ``pyte`` would silently drop, together with control
characters and bidirectional overrides, are shown as ``?``: the reader never
sees hidden text, and nothing from the report reaches the reader's terminal
as a control sequence. Recorded screen sizes are clamped (1000 rows and
columns by default) and the transcript length is bounded, so a crafted report
cannot make rendering arbitrarily expensive.

Commands are read from the screen, not from the raw keystrokes, because the
keystrokes contain line editing, cursor movement and history recall that only
the shell resolves. The input of a line starts where the cursor stood at its
first keystroke, which also covers prompts of other programs such as
``vtysh`` or a remote shell. When Enter is pressed, the next output chunk is
fed up to its first line feed and the logical input line, including wrapped
rows, is read at that moment. ``Ctrl-C``, ``Ctrl-D`` and ``Ctrl-Z`` abandon
the current input line. Keys typed ahead while a program was running are
skipped by starting the command after the last ``report`` prompt in the line.

``pysnap report text`` highlights the ``report`` prompt parts and the
entered commands, keeps the colors produced by the guest, and emits ANSI
styles only for terminals unless ``--color`` says otherwise. The styles are
generated from the parsed cell attributes, never copied from the report.

Report Viewing Strategy
-----------------------

``pysnap.report.highlight`` turns a transcript into styled runs with the
highlighted ``report`` prompt and bold commands; the ANSI printer of
``pysnap report text`` and the pager both draw these runs. The pager opens
only when the output is a terminal and the text is longer than the screen.
A report is an image of a fixed-width terminal, so the pager cuts long lines
at the window edge like ``less -S`` instead of wrapping them: tables such as
``ip route`` or ``tcpdump -X`` dumps keep their columns, edge markers show
hidden text, and ``Left``/``Right`` shift the view by half a window.

``pysnap.report.player`` replays a recording without any user interface: it
owns a ``TerminalEmulator`` of the recorded size and moves it to any moment of
a compressed timeline on which every pause is limited to ``--max-delay``
seconds, like ``scriptreplay -m``. Seeking backwards replays from the start.
``PlaybackController`` holds the play, pause, speed and command-jump state
that the key bindings change. ``pysnap.report.viewer`` draws the player in a
full-screen window at the recorded size; when the window is smaller, the view
is clipped around the cursor and the status line says so. Both viewers reuse
the scrollable control and mouse-wheel handling of ``pysnap connect``.

Report Statistics Strategy
--------------------------

``pysnap.report.stats`` derives the information block of
``pysnap report check`` from the report alone. Commands, their prompts and
their input events come from the rendered transcript; keystrokes are counted
by splitting input chunks into keys, because the serial console may deliver
two fast keys in one chunk and an escape sequence such as an arrow key is a
single key. The pause before a command is the thinking time from the last
output to its first key, so the run time of the previous program does not
count. The thresholds, five printable characters for pasted input and two
seconds for archive times, are parameters of ``compute_stats``.

Check Matching Strategy
-----------------------

``pysnap.report.checkfile`` loads a TOML check file with the standard
``tomllib`` module, validates every key and compiles each
pattern line into literal text and placeholders. Both sides are normalized:
edges are trimmed and whitespace runs collapse to one space. A pattern is
turned into a regular expression per match attempt, with the values of
already bound labels inserted as literal text, so a label either binds a new
value or requires the old one; typed values are then validated (IPv4 octets,
prefix lengths).

``pysnap.report.matcher`` checks all command items, then all output blocks,
with a backtracking search. For each item it tries the candidates in report
order, then the possibility that the item fails, and keeps the assignment
with the highest passed weight, preferring the earliest choices on ties. The
upper bound of the remaining weight prunes the search, and a step budget
stops it on pathological check files: the best complete assignment is used
and a warning is printed. Explanations for failed items are computed after
the search from the final label values.

Report Transfer Strategy
------------------------

``pysnap.report.extract`` reuses the ``UART1`` TCP endpoint of the VM. The
service checks the VM state, the serial port and the session registry first,
because VirtualBox accepts only one client on the port. On the console the
transfer relies on the guest shell and coreutils only: an arithmetic ``echo``
proves a working shell (its answer never appears in the echoed command), one
command prints the file as a single ``base64`` line and its ``sha256sum``
between random markers, and the markers are split in the command so that the
echo of the command never looks like a marker. The host verifies the
checksum and writes the file atomically. The ``report`` prompt is detected
before any command is sent, so a running recording is never modified.

While ``pysnap connect`` is attached, the port is taken, so the terminal
session also serves a control channel (``pysnap.report.control``): it listens
on ``127.0.0.1`` on a random port and stores the port and a random secret in
its session record, which is readable only by the current user.
``pysnap report extract`` sends one JSON line with the secret and the quoted
path; the session runs the same protocol through a ``QueueConsole`` fed by
its serial reader, pauses keyboard input meanwhile, and answers with the file
as ``base64`` and its checksum, which the client verifies again. One transfer
runs at a time.

Report Comparison Strategy
--------------------------

``pysnap.report.compare`` reads every report once into a
``ReportFingerprint``: the SHA-256 of the file and of the output body, the
unicast MAC addresses, the start second and the duration, hashes of every run
of 20 keyboard delays in microseconds, ``ping`` times in output order and
``tcpdump`` timestamps, erroneous commands and ``CPU.txt``. Pairs are then compared on
these values only, so a hundred reports (4950 pairs) take a few seconds, most
of it spent rendering the transcripts. Delay runs that consist of one repeated
value are not hashed, because they carry no rhythm. The comparison reports
signals and never classifies a pair; which reports belong to one student is
known only to the teacher.

Invisible Character Strategy
----------------------------

``pyte`` stops drawing the rest of an output chunk at the first character
without width that is not a combining mark, such as U+200B or U+202E. The
``TerminalEmulator`` therefore replaces such characters before drawing: the
live ``pysnap connect`` terminal skips them like a regular terminal does,
while report views show them as ``?`` so that hidden characters in a student
report stay visible to the reader.

Documentation Packaging Strategy
--------------------------------

Sphinx builds the HTML documentation into ``docs/_build/html``. During wheel
packaging, ``doit`` copies that compiled tree into ``pysnap/docs`` so the
installed package can open its own local documentation through ``pysnap docs``
without requiring Sphinx at runtime.
