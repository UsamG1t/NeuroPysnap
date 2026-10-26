Architecture
============

VBoxLab is organized into a few focused packages.

Package Layout
--------------

- ``vboxlab.cli`` contains argument parsing and terminal output formatting.
- ``vboxlab.config`` contains persistent configuration helpers.
- ``vboxlab.core`` contains domain models and the main application service.
- ``vboxlab/docs`` stores compiled HTML documentation bundled with the wheel.
- ``vboxlab.report`` reads session reports recorded inside VMs by the
  ``report`` utility: safe archive loading, parsing of the ``script``
  recording, rendering of the session text and commands, timed replay and
  the interactive pager and player.
- ``vboxlab.runtime`` contains the live-session registry shared by runtime tools.
- ``vboxlab.terminal`` contains the built-in serial terminal transport, emulator,
  key mapping, terminal-query responder, and interactive UI session logic.
- ``vboxlab.vbox`` contains the ``VBoxManage`` client and output parsers.
- ``vboxlab.tests`` contains unit tests.

Package Relationship Diagram
----------------------------

.. only:: graphviz

   .. graphviz::
      :caption: High-level relationships between VBoxLab packages.

      digraph package_relationships {
          rankdir=LR;
          node [shape=box, style="rounded,filled", fillcolor="#f6f6f6"];

          cli [label="vboxlab.cli"];
          config [label="vboxlab.config"];
          core [label="vboxlab.core"];
          report [label="vboxlab.report"];
          runtime [label="vboxlab.runtime"];
          terminal [label="vboxlab.terminal"];
          vbox [label="vboxlab.vbox"];

          cli -> core [label="command dispatch"];
          cli -> terminal [label="connect"];
          cli -> report [label="report"];
          report -> terminal [label="emulator, UI controls"];
          core -> config [label="~/.vboxlab/protosettings"];
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
2. ``VBoxLabService`` validates the request and coordinates the workflow.
3. ``VBoxManageClient`` executes VirtualBox commands.
4. Parser helpers convert command output into Python models.
5. ``SessionRegistry`` tracks active terminal attachments across processes.
6. ``TerminalSession`` starts the headless connection workflow when interactive
   serial access is requested.
7. The CLI renders human-readable results.

Metadata Strategy
-----------------

VBoxLab stores management metadata in VirtualBox extra data entries with the
``vboxlab/`` prefix. These values are used to:

- mark imported and cloned VMs as managed by VBoxLab
- record clone ancestry
- support dependency checks before erase operations
- select linked clones for ``erase --clones-only`` without a separate registry

Serial Port Strategy
--------------------

VBoxLab uses ``UART1`` for the serial TCP endpoint of clones. If no explicit
port is supplied, the service automatically assigns ``max(used_ports) + 1`` or
``1024`` when no TCP serial port has been configured yet.

The ``plug`` workflow reuses the same ``UART1 tcpserver`` strategy for existing
VMs. It only rewrites ``UART1`` when the VM is stopped and the current backend
is not already occupied by another mode such as ``tcpclient`` or ``file``.
When needed, the selected TCP port must be available both in VirtualBox and on
the host system.

Automatic allocation counts the TCP servers of all four serial ports and the
forwarded ssh ports of all VMs as used. ``vboxlab set`` reads ``COM1``-``COM4`` and every network adapter
of the VM from ``showvminfo``; the adapter count of its chipset comes from
``VBoxManage list systemproperties`` (8 for PIIX3 and 36 for ICH9 when it is
not reported). It validates every option and TCP port before changing anything,
and applies the result with one ``modifyvm`` call using the standard PC
resources of each COM port. Host pipes are named by a short name that is
mapped to the host's pipe path format.

During ``vboxlab connect``, VBoxLab continuously tracks the outer terminal size
and resizes the local emulator to the current visible guest area. Because raw
serial TCP does not offer a PTY-style ``SIGWINCH`` path into the guest, VBoxLab
also replies to xterm-compatible in-band terminal queries such as ``CSI 18 t``
and ``CSI 6 n`` so guest-side Linux tools can rediscover the current geometry.

Terminal Selection Strategy
---------------------------

Mouse selection in ``vboxlab connect`` copies the text to the host clipboard
when the button is released: many terminal emulators keep ``Ctrl-Shift-C``
for their own copy command and never pass it on. Where it does arrive,
``Ctrl-Shift-C`` and ``Ctrl-C`` are the same ``ETX`` byte, so VBoxLab resolves
the chord by selection state: with a selected text it copies again, without
one it forwards a real interrupt to the guest. Incoming guest output drops
only the visible highlight and keeps the selected text.

Key Strategy
------------

Every key goes to the guest except ``Ctrl-]``, the VBoxLab command prefix
borrowed from ``telnet``: editors need ``Esc``, ``Ctrl-L``, ``Ctrl-Q`` and
``Alt`` combinations, which earlier versions used for detaching, redrawing
and scrolling. A ``Condition`` filter enables the command bindings (``q``,
``Ctrl-]``, the scroll keys) only after the prefix; any other key, ``Esc``
included, just leaves the mode. As no binding starts with ``Escape``, a lone
``Esc`` reaches the guest after the 50 ms the input parser waits for the rest
of an escape sequence instead of after the half-second key binding timeout.
``key_press_to_bytes`` sends a sequence that ``prompt_toolkit`` splits into
several keys, such as ``ESC [ 1 ; 3 A`` (``Alt+Up`` on an xterm), once with
the first key and skips the empty rest. On the alternate screen the mouse
wheel sends cursor keys, in the application form ``ESC O A`` when the guest
switched DECCKM on.

Proto Settings Strategy
-----------------------

VBoxLab stores proto-settings base VM names in ``Path.home() / ".vboxlab" / "protosettings"``
with one VM name per line. When a clone is created from a registered base VM,
VBoxLab writes additional DMI settings through ``VBoxManage setextradata``:

- ``DmiSystemVendor = <CloneVM>``
- ``DmiSystemSKU = port<Port>[.<net1>[.<net2>[.<net3>]]]``

This mode exists for educational VirtualBox images used in CMC MSU courses.

Access Strategy
---------------

``vboxlab.core.access.resolve_access`` decides how a guest is reached: an
explicit ``vboxlab/access`` extra data entry wins, base VMs registered with
protosettings and their clones use the ``COM1`` serial console, and every other
VM uses ssh. The ssh user is the ``vboxlab/ssh-user`` extra data entry or
``root``. The ssh port is not stored separately: it is read from the NAT
port-forwarding rule ``vboxlab-ssh`` (``127.0.0.1:PORT`` to guest port 22), so
VirtualBox remains the only source of truth. ``showvminfo --machinereadable``
prints the rules of a NAT adapter after its ``natnetN`` line, which tells
which adapter holds the rule.

ssh Strategy
------------

``vboxlab.ssh`` runs the system OpenSSH client instead of a Python ssh library,
so VBoxLab gains no dependency. Every call uses ``-F`` with the null device (no
user configuration), the VBoxLab key with ``IdentitiesOnly`` and no agent, the
known_hosts file of the VM with ``StrictHostKeyChecking=accept-new`` and batch
mode, so a missing key fails instead of asking for a password. Only
``vboxlab ssh-key`` runs ssh with password login in the user's terminal; its
guest script appends the key to ``~/.ssh/authorized_keys`` unless it is there
and then runs the same for root through ``su``. Whether the key works is
decided afterwards by key logins, not by parsing the dialogue. Interactive
terminals use ``ssh -tt`` with pipes; the guest terminal size is set with
``stty`` before the login shell starts.

``TerminalSession`` works on a ``SessionTransport``: a name, a status-line
label and an opener that returns a reader, a writer and a close coroutine.
The serial transport opens the ``UART1`` TCP connection; the ssh transport
starts ``ssh -tt``, merges its error output into the stream and terminates the
process on close. Before an ssh session the service checks the key login
(``prepare_ssh_terminal``): it waits for a booting guest, and a refused key
starts the same key setup as ``vboxlab ssh-key``. Features of the serial
console only (the waking newline, the silence hint, the report control
channel) run only for the serial transport.

Runtime State Strategy
----------------------

VBoxLab translates raw VirtualBox runtime states into compact monitor labels:

- ``Working`` for running VMs with an active VBoxLab terminal session
- ``Active`` for running VMs without an attached VBoxLab terminal session
- ``Stopping`` for graceful shutdown in progress
- ``Changing`` for startup and other transitional states
- ``Paused`` for paused VMs
- ``Error`` for explicit error-like VirtualBox states

A ``vboxlab connect`` session ends on every label except ``Working``,
``Active`` and ``Paused``. ``PauseTracker`` follows pause and resume: while
paused, keys are not forwarded, and a session that started on a paused VM
wakes the console after the resume only when no guest output arrived yet.

Host Cleanup Strategy
---------------------

The ``full-clean`` command removes the VirtualBox machine folder and the
platform-specific configuration directory directly from the host file system
through ``shutil.rmtree``. The CLI layer owns the double confirmation dialog,
while ``VBoxLabService.full_clean`` performs the removal and reports partial
failures. The ``VBOX_USER_HOME`` environment variable overrides the detected
configuration directory, and the repeated ``--path`` option replaces the
default selection entirely.

Listing Robustness Strategy
---------------------------

``VBoxManage list`` can block indefinitely when the VirtualBox configuration
directory is missing while a stale ``VBoxSVC`` process is still running, for
example right after ``vboxlab full-clean``. ``VBoxManageClient`` therefore runs
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

A report is untrusted input, so ``vboxlab.report.archive`` never extracts it
to disk. The archive is streamed once through a byte counter: only regular
files with the five known names are read into memory, while links, devices,
path-traversal names and unknown entries are skipped with a diagnostic. The
compressed size, the size of one member and the total decompressed size are
bounded by ``ReportLimits``, which protects against decompression bombs.

``vboxlab.report.recording`` parses the multi-stream timing log of
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

``vboxlab.report.render`` replays the recorded output through a ``pyte``
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

``vboxlab report text`` highlights the ``report`` prompt parts and the
entered commands, keeps the colors produced by the guest, and emits ANSI
styles only for terminals unless ``--color`` says otherwise. The styles are
generated from the parsed cell attributes, never copied from the report.

Report Viewing Strategy
-----------------------

``vboxlab.report.highlight`` turns a transcript into styled runs with the
highlighted ``report`` prompt and bold commands; the ANSI printer of
``vboxlab report text`` and the pager both draw these runs. The pager opens
only when the output is a terminal and the text is longer than the screen.
A report is an image of a fixed-width terminal, so the pager cuts long lines
at the window edge like ``less -S`` instead of wrapping them: tables such as
``ip route`` or ``tcpdump -X`` dumps keep their columns, edge markers show
hidden text, and ``Left``/``Right`` shift the view by half a window.

``vboxlab.report.player`` replays a recording without any user interface: it
owns a ``TerminalEmulator`` of the recorded size and moves it to any moment of
a compressed timeline on which every pause is limited to ``--max-delay``
seconds, like ``scriptreplay -m``. Seeking backwards replays from the start.
``PlaybackController`` holds the play, pause, speed and command-jump state
that the key bindings change. ``vboxlab.report.viewer`` draws the player in a
full-screen window at the recorded size; when the window is smaller, the view
is clipped around the cursor and the status line says so. Both viewers reuse
the scrollable control and mouse-wheel handling of ``vboxlab connect``.

Report Statistics Strategy
--------------------------

``vboxlab.report.stats`` derives the information block of
``vboxlab report check`` from the report alone. Commands, their prompts and
their input events come from the rendered transcript; keystrokes are counted
by splitting input chunks into keys, because the serial console may deliver
two fast keys in one chunk and an escape sequence such as an arrow key is a
single key. The pause before a command is the thinking time from the last
output to its first key, so the run time of the previous program does not
count. The thresholds, five printable characters for pasted input and two
seconds for archive times, are parameters of ``compute_stats``.

Check Matching Strategy
-----------------------

``vboxlab.report.checkfile`` loads a TOML check file with the standard
``tomllib`` module, validates every key and compiles each
pattern line into literal text and placeholders. Both sides are normalized:
edges are trimmed and whitespace runs collapse to one space. A pattern is
turned into a regular expression per match attempt, with the values of
already bound labels inserted as literal text, so a label either binds a new
value or requires the old one; typed values are then validated (IPv4 octets,
prefix lengths).

``vboxlab.report.matcher`` checks all command items, then all output blocks,
with a backtracking search. For each item it tries the candidates in report
order, then the possibility that the item fails, and keeps the assignment
with the highest passed weight, preferring the earliest choices on ties. The
upper bound of the remaining weight prunes the search, and a step budget
stops it on pathological check files: the best complete assignment is used
and a warning is printed. Explanations for failed items are computed after
the search from the final label values.

Report Transfer Strategy
------------------------

For a VM reached through ssh, ``vboxlab.report.extract`` runs one batch-mode
ssh command with the VBoxLab key: ``sh -c`` checks that the file is readable
(exit status 3 otherwise), prints its ``sha256sum`` and then at most one byte
more than the size limit of the file with ``head -c``. The host splits the
first line off, checks the size and the checksum and writes the file
atomically. This session is independent of ``vboxlab connect``, so the session
registry and the control channel are not used.

For other VMs ``vboxlab.report.extract`` reuses the ``UART1`` TCP endpoint of
the VM. The service checks the VM state, the serial port and the session
registry first, because VirtualBox accepts only one client on the port. On the
console the transfer relies on the guest shell and coreutils only: an
arithmetic ``echo`` proves a working shell (its answer never appears in the
echoed command), one command prints the file as a single ``base64`` line and
its ``sha256sum`` between random markers, and the markers are split in the
command so that the echo of the command never looks like a marker. The host
verifies the checksum and writes the file atomically. The ``report`` prompt is
detected before any command is sent, so a running recording is never modified.

While ``vboxlab connect`` is attached, the port is taken, so the terminal
session also serves a control channel (``vboxlab.report.control``): it listens
on ``127.0.0.1`` on a random port and stores the port and a random secret in
its session record, which is readable only by the current user.
``vboxlab report extract`` sends one JSON line with the secret and the quoted
path; the session runs the same protocol through a ``QueueConsole`` fed by
its serial reader, pauses keyboard input meanwhile, and answers with the file
as ``base64`` and its checksum, which the client verifies again. One transfer
runs at a time.

Report Comparison Strategy
--------------------------

``vboxlab.report.compare`` reads every report once into a
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
live ``vboxlab connect`` terminal skips them like a regular terminal does,
while report views show them as ``?`` so that hidden characters in a student
report stay visible to the reader.

Control Sequence Strategy
-------------------------

``pyte`` 0.8 fails on several sequences that current programs send, for
example Vim with an xterm ``TERM``: a private CSI sequence other than a DEC
mode (``ESC [ ? 4 m``) raises ``TypeError``, ``>``, ``=`` and ``<`` prefixes
are ignored so that ``ESC [ > 4 ; 2 m`` switches on underline, intermediate
bytes and colon sub-parameters end a sequence early and leave its rest on the
screen, and DCS, SOS, PM and APC payloads are drawn as text. The
``SequenceFilter`` of ``TerminalEmulator`` therefore sits in front of
``pyte``: it keeps text, C0 controls, ordinary CSI sequences, DEC modes
(``? … h``/``l``), selective erase and the other ``ESC`` sequences, rewrites
colon SGR parameters (``38:2::R:G:B``, ``4:3``) to the semicolon form and drops
the rest together with all control strings. Its state survives chunk
boundaries; a CSI sequence longer than 256 bytes is skipped to its final byte
and a control string longer than 64 KiB ends, so that garbage output cannot
hide the screen for good. Should ``pyte`` still fail, the emulator starts a
new parser and the session goes on.

``pyte`` 0.8 also lacks the scroll commands ``CSI n S`` and ``CSI n T``, with
which nano scrolls its editing area on an xterm. ``ScreenStream`` dispatches
them to ``ScrollRegionMixin``: the lines inside the scrolling margins move by
``index`` and ``reverse_index`` while the cursor stays, so screens that track
lines leaving the top see them as usual.

``pyte`` 0.8 has no alternate screen either: after vim, nano or less the
editor picture stayed on the screen and its scrolled lines filled the local
history. ``AlternateScreenMixin`` of the live terminal handles the DEC modes
``47``, ``1047`` and ``1049`` (``1049`` and ``1048`` also save and restore the
cursor): the shell buffer is kept aside while a fresh buffer is shown and comes
back on exit; lines scrolled on the alternate screen do not enter the history,
and the local scrollback is off there. A resize while the alternate screen is
shown also moves the top lines of the kept shell buffer to the history and
cuts its cells beyond the new width, as ``pyte`` does for the visible screen.

``vboxlab.report.render`` replays recordings with the same filter, stream and
scroll commands, so a report in which the student used vim or nano renders
like the live terminal; a ``pyte`` failure loses only the chunk it occurred in.

Documentation Packaging Strategy
--------------------------------

Sphinx builds the HTML documentation into ``docs/_build/html``. During wheel
packaging, ``doit`` copies that compiled tree into ``vboxlab/docs`` so the
installed package can open its own local documentation through ``vboxlab docs``
without requiring Sphinx at runtime.
