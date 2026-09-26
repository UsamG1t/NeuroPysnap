Usage Guide
===========

PySnap exposes a single command line entry point named ``pysnap``.

Help
----

Running the command without arguments shows the built-in help:

.. code-block:: text

   pysnap

Open Bundled Documentation
--------------------------

The ``docs`` command opens the compiled HTML documentation that is bundled
inside the installed PySnap package.

By default, PySnap tries to launch ``firefox``. When it is not available, pass
an explicit browser executable through ``--browser``.

.. code-block:: text

   pysnap docs
   pysnap docs --browser /usr/bin/chromium

List Virtual Machines
---------------------

The ``list`` command prints all detected VirtualBox groups and the names of the
virtual machines inside each group.

When the VirtualBox data directories are missing or the ``VBoxManage`` service
does not respond, for example right after ``pysnap full-clean``, the ``list``
command prints ``No virtual machines found.`` and exits cleanly. PySnap runs
the underlying ``VBoxManage list`` calls with a bounded timeout, so a stale
``VBoxSVC`` process cannot block the listing forever.

.. code-block:: text

   pysnap list

Run an Integration Test
-----------------------

PySnap can run a real end-to-end VirtualBox integration scenario directly from
the command line:

.. code-block:: text

   pysnap --integration-test image.ova

The integration scenario:

- imports the appliance as a temporary base VM
- creates three linked clones
- connects clone A and B with ``intnet``
- connects clone B and C with ``deepnet``
- connects clone C and A with ``virtnet``
- attaches a serial terminal probe to clone A
- starts clone B in headless mode without attaching to it
- captures ``monitor`` output and verifies that clone A is ``Working`` while
  clone B is ``Active``
- prints details for the base VM and all three clones
- removes the four VMs one by one

Register Proto Settings for a Base VM
-------------------------------------

The ``protosettings`` command registers one base VM in the persistent
proto-settings list stored at ``Path.home() / ".ptotosettings"``. The file
contains one VM name per line and is used as a set without duplicates.

This option exists for the image conventions used in the educational program of
CMC MSU. When a base VM is present in the proto-settings list, every linked
clone created from it receives additional DMI values:

- ``DmiSystemVendor = <CloneVM>``
- ``DmiSystemSKU = port<Port>[.<net1-name>[.<net2-name>[.<net3-name>]]]``

This behavior is relevant for the following CMC MSU courses:

- `LinuxNetwork <https://uneex.org/LecturesCMC/LinuxNetwork2026>`_
- `Nets: Introduction <https://asvk.cs.msu.ru/uchebnyj-process/chitaemye-kursy/vvedenie-v-seti-evm/>`_
- `Methodics of Linux Net Protocols <https://github.com/UsamG1t/Methodics_LinuxNetProtocols>`_
- `Labs of Linux Net Protocols <https://github.com/UsamG1t/Nets_ASVK_Labs>`_

.. code-block:: text

   pysnap protosettings BaseVM

Import an Appliance
-------------------

Import either an ``.ova`` or ``.ovf`` appliance.

If the appliance already defines a group, PySnap keeps it. Otherwise, the
virtual machine is placed into the ``/Others`` group.

When the appliance contains exactly one VM, an optional ``VMName`` argument can
override the imported VM name.

Before running ``VBoxManage import``, PySnap checks the final VM name. If the
name already exists, the import is aborted before creating anything. For the
default one-VM case, PySnap recommends using the optional ``VMName`` argument
to choose a different target name.

During the import, PySnap renders a live progress bar based on the percentage
output reported by ``VBoxManage import``.

.. code-block:: text

   pysnap import image.ova
   pysnap import image.ovf
   pysnap import image.ova RenamedVM

Show Virtual Machine Details
----------------------------

The ``show`` command prints the VM name, the assigned group, and the serial TCP
port configured through ``UART1``.

.. code-block:: text

   pysnap show MyVM

Plug a Virtual Machine for PySnap Connections
---------------------------------------------

The ``plug`` command prepares an existing VM for ``pysnap connect`` by
configuring ``UART1`` as a ``tcpserver`` endpoint when this can be done safely.

PySnap checks these conditions:

- the VM exists
- if ``UART1`` is already configured as ``tcpserver,<port>``, the VM is left unchanged
- if ``UART1`` is already bound to another backend, PySnap refuses to overwrite it
- if a reconfiguration is needed, the VM must be in a stopped state
- the selected TCP port must be free in both VirtualBox and the host OS

When all checks pass, PySnap assigns the same kind of serial TCP port used by
the imported ``protocols-*`` images, so the VM becomes connectable through the
built-in terminal interface.

.. code-block:: text

   pysnap plug MyVM
   pysnap connect MyVM

.. only:: graphviz

   .. graphviz::
      :caption: Plug-and-connect workflow for an existing VM.

      digraph plug_connect_workflow {
          rankdir=LR;
          node [shape=box, style="rounded,filled", fillcolor="#f6f6f6"];

          plug [label="pysnap plug VM"];
          modifyvm [label="VBoxManage modifyvm\n--uart1 0x3F8 4\n--uartmode1 tcpserver PORT"];
          vm [label="VM with UART1 tcpserver", shape=ellipse, fillcolor="#fff8dc"];
          connect [label="pysnap connect VM"];
          headless [label="VBoxManage startvm\n--type=headless"];
          terminal [label="PySnap terminal session"];

          plug -> modifyvm -> vm;
          connect -> headless -> terminal -> vm;
      }

.. only:: not graphviz

   Graphviz example diagrams are rendered automatically when the ``dot``
   executable is available on the documentation build host.

Connect to a Headless Virtual Machine
-------------------------------------

The ``connect`` command ensures that the selected VM is running in headless
mode and then attaches a built-in PySnap terminal interface to its ``UART1``
TCP console.

For VMs that do not already expose a suitable ``UART1 tcpserver`` endpoint, run
``pysnap plug <VM>`` first.

The terminal session is detached with ``Ctrl-Q``. Detaching does not stop the
virtual machine. ``Ctrl-L`` redraws the local interface.

PySnap also keeps a local scrollback buffer for the attached session:

- ``Alt-Up`` scrolls one line toward older output
- ``Alt-Down`` scrolls one line toward newer output
- ``Alt-Left`` jumps to the oldest retained output
- ``Alt-Right`` jumps back to the live output bottom
- dragging with the left mouse button selects visible terminal text and
  captures it for copying
- ``Ctrl-Shift-C`` copies the captured selection to the host clipboard
- on Linux, the mouse wheel or touchpad scroll gesture also moves through the
  local scrollback buffer

Classic terminal emulators deliver ``Ctrl-Shift-C`` and ``Ctrl-C`` as the same
byte, so PySnap disambiguates them by selection state. While a captured
selection exists, the chord only copies. Without one, it forwards a real
``Ctrl-C`` interrupt to the guest. Incoming guest output removes the visible
highlight but keeps the captured text, so copying stays possible while
utilities keep printing in the background.

While the session is attached, PySnap continuously tracks the outer terminal
size and resizes the visible guest text area to match it. This also works
after reconnecting to an already running VM.

For xterm-compatible guest-side tools, PySnap replies to standard size queries
such as ``CSI 18 t`` and cursor-position reports such as ``CSI 6 n``. On Linux
guests this makes terminal-size refresh workflows such as ``resize`` much more
practical over the raw serial console.

On macOS, PySnap also checks the standard VirtualBox application-bundle path
for ``VBoxManage`` when the command is not exported in ``PATH``.

.. code-block:: text

   pysnap connect MyVM

Monitor Active Virtual Machines
-------------------------------

The ``monitor`` command prints compact runtime records in the form
``<VM> (state: <State> ; <serial port> ; <group>)``.

PySnap currently uses these runtime labels:

- ``Working`` when the VM is running and PySnap has an active attached session
- ``Active`` when the VM is running in headless mode without an attached session
- ``Stopping`` when the VM is shutting down gracefully
- ``Changing`` when the VM is starting or otherwise transitioning
- ``Paused`` when VirtualBox reports a paused machine
- ``Error`` when VirtualBox reports an error-like runtime state

.. code-block:: text

   pysnap monitor
   srv (state: Working ; 2345 ; /Lab)
   db (state: Active ; 2346 ; /Lab)
   router (state: Stopping ; 2347 ; /Net)

Stop Running Virtual Machines
-----------------------------

PySnap stops headless virtual machines through ``VBoxManage controlvm
<VM> acpipowerbutton`` only. No automatic fallback to ``savestate`` or other
shutdown modes is used.

When a VM stops, any active ``pysnap connect`` session attached to it finishes
automatically because the VM state changes and the serial connection is closed.

.. code-block:: text

   pysnap stop MyVM
   pysnap stop --all

Create a Linked Clone
---------------------

The ``clone`` command creates a linked clone from a base VM. The clone inherits
the base VM group.

When ``-p`` is provided, its value is used as the host TCP port for
``modifyvm <VM> --uart1 0x3F8 4 --uartmode1 tcpserver <port>``.

When ``-p`` is omitted, PySnap scans existing VMs, finds the maximum already
used serial TCP port, and assigns the next value. If no serial TCP ports are
configured yet, the automatic sequence starts at ``1024``.

Up to three extra positional arguments configure internal networks. PySnap uses
two layouts depending on whether the base VM was registered through
``pysnap protosettings``:

- for ordinary base VMs, the legacy layout is used: networks are mapped
  sequentially onto ``nic1`` through ``nic3``, and omitted adapters are set to
  ``none``
- for proto-settings base VMs, ``nic1`` stays in ``nat`` mode, the requested
  networks are mapped onto ``nic2`` through ``nic4``, and omitted adapters are
  left enabled

If the base VM was previously registered through ``pysnap protosettings``,
PySnap also applies educational DMI settings to the clone.

.. code-block:: text

   pysnap clone BaseVM CloneVM -p 2345 intnet1 intnet2 intnet3
   pysnap clone BaseVM CloneVM intnet1

Erase Virtual Machines
----------------------

PySnap supports three erase modes:

- ``pysnap erase VM`` removes a single VM when no dependent linked clones exist.
- ``pysnap erase --group GROUP`` removes all VMs inside one group.
- ``pysnap erase --all`` removes all registered VirtualBox VMs.

The single-VM erase mode refuses deletion when dependent clones still exist.
The group erase mode refuses deletion when descendants outside the target group
still depend on the selected VMs.

Every erase mode accepts the ``--clones-only`` flag, which restricts the
operation to VMs created as PySnap linked clones:

- ``pysnap erase --all --clones-only`` removes every linked clone and keeps the
  imported base VMs registered.
- ``pysnap erase --group GROUP --clones-only`` removes only the linked clones
  inside one group. The operation is refused when clones outside the group
  still depend on the selected clones.
- ``pysnap erase VM --clones-only`` behaves like the normal single-VM erase for
  a clone. For a base VM, PySnap prints a warning and refuses the deletion, so
  the flag stays safe in scripted cleanups.

PySnap recognizes clones through the ``pysnap/kind`` and ``pysnap/parent``
metadata written into VirtualBox extra data during cloning, so the clone list
stays correct without a separate registry file.

When a VM is removed successfully, PySnap also removes its name from the
proto-settings file if it was registered there.

.. code-block:: text

   pysnap erase BaseVM
   pysnap erase --group /Lab
   pysnap erase --all
   pysnap erase --all --clones-only
   pysnap erase --group /Lab --clones-only
   pysnap erase CloneVM --clones-only

Remove All VirtualBox Data
--------------------------

The ``full-clean`` command permanently deletes the VirtualBox machine folder
and the VirtualBox configuration directory from the host file system. Stop
VirtualBox and all virtual machines before running it.

Without ``--path``, PySnap removes ``~/VirtualBox VMs`` together with the
OS-specific configuration directory:

- ``~/.config/VirtualBox`` on Linux and other Unix systems
- ``~/Library/VirtualBox`` on macOS
- ``~/.VirtualBox`` on Windows
- the directory from ``VBOX_USER_HOME`` when the variable is set

The repeated ``--path`` option replaces the default selection with explicit
directories. This covers custom machine-folder locations configured through
``VBoxManage setproperty machinefolder``.

The command prints the target directories, marks missing ones, and asks for two
different explicit confirmations: ``yes`` on the first prompt and ``delete`` on
the second one. Any other answer cancels the cleanup without touching the file
system. Directories are removed recursively through ``shutil.rmtree``, so the
same behavior applies on every supported operating system.

.. code-block:: text

   pysnap full-clean
   pysnap full-clean --path /data/vms --path /data/vbox-config

Read Session Reports
--------------------

Students record their work inside the educational VMs with the ``report``
utility, which produces a ``report.<NN>.<host>`` archive. The
``pysnap report`` command reads such archives safely: nothing is extracted to
disk and nothing from the report is sent to the terminal as a raw control
sequence.

The ``text`` subcommand prints the text of the session without timing, as the
student saw it. The prompt installed by ``report`` is highlighted and the
entered commands are shown in bold; colors produced inside the VM are kept.
Colors are used only when the output is a terminal; ``--color always`` or
``--color never`` overrides the detection, and the ``NO_COLOR`` environment
variable disables colors.

The ``--commands`` option prints only the numbered list of entered commands
with the time since the start of the recording. Commands typed at the prompt
of another program, for example inside ``vtysh``, are marked with that
prompt. Line editing, history recall and interrupted commands are resolved,
so the list shows what was actually executed.

Problems found in the report, such as a truncated recording, are printed as
warnings to the error stream; the available text is still shown.

When the output is a terminal and the text is longer than the screen, ``text``
opens a pager. Like ``less -S``, the pager keeps the recorded line layout:
lines longer than the window are cut at its edge, and ``>`` or ``<`` in the
edge column marks text hidden to the right or left.

- ``Up``/``Down`` or ``k``/``j`` scroll by a line
- ``PageUp``/``PageDown``, ``b`` and ``Space`` scroll by a page
- ``g``/``Home`` and ``G``/``End`` jump to the start or end
- ``Left``/``Right`` shift the view by half the window width
- the mouse wheel scrolls on Linux
- ``q`` or ``Ctrl-Q`` quits

``--no-pager`` prints the text directly.

.. code-block:: text

   pysnap report text report.01.first
   pysnap report text report.01.first --commands
   1  00:17.05  ip a show eth1
   2  00:29.86  ping -c5 10.9.0.2
   pysnap report text report.01.first --color never > report.01.first.txt
   pysnap report text report.01.first --no-pager

The ``show`` subcommand replays the session with its timing in a safe
terminal view of the recorded size. Pauses longer than ``--max-delay``
seconds (1 by default, ``0`` keeps the real pauses) are shortened, like
``scriptreplay -m``, and ``--speed`` selects the initial speed. When the
window is smaller than the recorded screen, the view is clipped around the
cursor and the status line reports the recorded size.

- ``Space`` pauses and resumes playback; resuming at the end starts over
- ``+`` and ``-`` change the speed between x0.25 and x16
- ``n`` and ``p`` jump to the start of the next or previous command
- ``Home`` and ``End`` jump to the start or the end of the recording
- while paused, ``Alt-Up``/``Alt-Down`` and the mouse wheel on Linux scroll
  the screen history
- ``q`` or ``Ctrl-Q`` quits

Characters that would be invisible, such as zero-width spaces or
bidirectional overrides, are shown as ``?`` in ``text`` and ``show``.

.. code-block:: text

   pysnap report show report.01.first
   pysnap report show report.01.first --speed 4 --max-delay 0.5

The ``check`` subcommand prints a report information block. Without a check
file it shows only this block:

- **Identity**: task and host from the ``report.<NN>.<host>`` file name and
  from the prompts recorded by ``report``; a warning is printed when they
  differ or when the prompts show several hosts
- **Timing**: start, end, duration and the exit code of the recorded shell
- **Environment**: terminal device, type and size, CPU model and hypervisor
  from ``CPU.txt``
- **Commands**: the number of commands and unique commands, commands
  interrupted with ``Ctrl-C``, recalled from history (``Up``, ``Down``,
  ``Ctrl-R``) or typed at the prompt of another program, and the pause
  before each command, measured from the last output to the first key
- **Typing**: keys per second from the first key of a command to ``Enter``,
  the share of ``Backspace`` keys and input chunks that look pasted (five or
  more printable characters at once, or a bracketed-paste marker)
- **Addresses in output**: IPv4 and MAC addresses shown by the commands,
  without the broadcast and all-zero MAC addresses
- **Integrity**: problems found while reading the report and a comparison of
  the archive member times with the recording start (``CPU.txt``) and end
  (the other members) within two seconds

.. code-block:: text

   pysnap report check report.01.first
   pysnap report check report.01.first lab02-first.check.toml

Check Files
~~~~~~~~~~~

With a check file, ``check`` also checks the expected commands and output
blocks and grades the report. A check file is written in TOML, usually with
the ``.check.toml`` extension, one file per host of a lab:

.. code-block:: toml

   [report]              # optional expectations, reported as warnings
   task = 1
   host = "first"

   [[command]]
   id = "addr"           # optional name used by "of" and "after"
   cmd = "ip a show <ETH-A>"

   [[command]]
   id = "ping"
   cmd = "ping -c5 <IP-B>"

   [[output]]
   of = "addr"           # search only in the output of that command
   text = '''
   <*>: <ETH-A>: <*>UP<*>
   ...
       inet <IP-A>/<MASK> scope global <ETH-A>
   '''

   [[output]]
   of = "ping"
   min_count = 5         # the block must occur at least five times
   text = "64 bytes from <IP-B>: icmp_seq=<*> ttl=64 time=<*>"

   [grading]
   total = 10
   scale = [[90, "5"], [75, "4"], [50, "3"], [0, "2"]]

``[[command]]`` items are compared with the entered commands as a whole.
Keys:

- ``cmd`` (required): the command pattern
- ``id``: a name for ``of`` and ``after`` references
- ``after``: the id of an earlier command; the item passes only when its
  command was entered after that command. If that command did not pass,
  this item fails too.
- ``points``: the weight of the item, 1 by default

``[[output]]`` items are blocks of output lines. Keys:

- ``text`` (required): the block; a line holding only ``...`` matches any
  number of lines, otherwise the lines must follow each other
- ``of``: the id of a command; the block is searched only in the output of
  the runs of that command, otherwise in the whole report
- ``order = "any"``: the lines may appear in any order, which suits tables
  such as ``ip route``; ``...`` is not allowed then
- ``min_count``: how many times the block must occur, 1 by default, for
  example for ping replies
- ``points``: the weight of the item, 1 by default

Patterns may contain placeholders:

- ``<IP>``, ``<ETH>`` and ``<MASK>`` match any IPv4 address, ``ethN``
  interface or prefix length from 0 to 32
- a label such as ``<IP-A>``, ``<ETH-A>`` or ``<MASK-A>`` remembers the first
  matched value; every later use of the same label, in commands and in output
  blocks, must show the same value
- any other name, such as ``<X>``, matches one word and also remembers it
- ``<*>`` matches any text inside a line and remembers nothing; use it for
  values that change, such as ping times or sequence numbers
- ``\<`` writes a literal ``<``; text like ``< /dev/ttyS1`` needs no escaping

Spaces never matter: both the report lines and the patterns are trimmed and
every run of spaces or tabs counts as one space, so aligned table columns
match patterns written with single spaces.

Commands are checked in file order, then output blocks. PySnap chooses among
several possible matches, for example several ``ping`` runs, so that as many
items as possible pass; with equal results it prefers the earliest match. An
item that fails does not make later items fail through wrong label values.

``[grading]`` is required. Every item contributes its weight; the percentage
of the passed weight is scaled to ``total`` points, and the mark is the first
``scale`` step whose threshold in percent is reached. Thresholds are listed
from the highest to the lowest.

The output lists every item as ``PASS`` or ``FAIL``. A passed command shows
the entered command it matched; a passed block shows where it was found; a
failed item shows the reason and, for commands, the closest entered command.
The values of all labels, the number of passed items, the points, the
percentage and the mark follow. When the ``[report]`` task or host differs
from the report file name or prompts, a warning is printed and the grade is
not changed.

Extract a Report from a Running VM
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``extract`` subcommand copies a report from a running VM to the host
through the same ``UART1`` serial console that ``pysnap connect`` uses, so no
second COM port and no VM restart are needed.

.. code-block:: text

   pysnap report extract first report.01.first
   Extracted "$HOME"/report.01.first from first to /home/user/report.01.first (2621 bytes, sha256 ...).
   pysnap report extract first /tmp/report.03.pc1 --output reports/pc1 --force

Requirements and behavior:

- the VM is running and has a ``UART1`` TCP port (see ``pysnap plug``)
- the command also works while ``pysnap connect`` is attached to the VM:
  VirtualBox serves one client, so ``extract`` asks the attached session to
  run the transfer on its connection. The terminal shows a message in the
  status line, keys are paused until the transfer ends, and afterwards the
  screen is cleared and a fresh prompt appears. Sessions started by an older
  PySnap cannot do this; detach them with ``Ctrl-Q`` first
- the console is logged in and at a shell prompt; PySnap checks this with
  ``echo PYSNAP_$((20+22))`` and stops when the answer does not arrive
- no ``report`` recording is running: PySnap presses ``Ctrl-U`` and Enter once, and when the
  recording prompt appears it stops before sending any command, so nothing
  else ends up in the student's report
- a name without ``/`` is looked up in the home directory of the console
  user; a path with ``/`` is used as given
- the file is sent as ``base64`` together with its ``sha256sum``; PySnap
  verifies the checksum, writes the file under the same name into the current
  directory or to ``--output``, and never replaces an existing file without
  ``--force``
- the service commands start with a space, so shells with ``HISTCONTROL``
  set to ``ignorespace`` keep them out of the history, and the guest screen is
  cleared afterwards
- after the copy PySnap reads the file as a report and warns when it is not
  one

A serial console typically runs at 115200 baud, about 11 KB/s: a report of a
few kilobytes takes well under a second. The transfer fails only when no data
arrives for ten seconds.

Compare Reports
~~~~~~~~~~~~~~~

The ``compare`` subcommand compares every pair of the given reports and lists
signs that two reports share their origin. Paths may be report files or
directories; directories are searched recursively for files named
``report.NN.HOST``, and a file reached twice is compared once. PySnap does not
know which reports belong to the same student: reports of one student
legitimately share values such as the MAC address of the same VM, so the
teacher decides which pairs matter.

.. code-block:: text

   pysnap report compare reports/
   SIGNALS  reports/ivanov/report.01.first  <->  reports/petrov/report.01.first
     strong  S3  same MAC address: 08:00:27:a9:84:3a
     medium  M3  identical CPU.txt
   OK       reports/ivanov/report.01.first  <->  reports/sidorov/report.01.first
   OK       reports/petrov/report.01.first  <->  reports/sidorov/report.01.first

   Compared 3 reports, 3 pairs: 1 with signals (strongest: 1 strong, 0 medium, 0 weak), 2 OK.

Pairs with signals come first, strongest first, and show only the signals
found; other pairs take one ``OK`` line.

.. list-table::
   :header-rows: 1

   * - Code
     - Level
     - Signal
   * - ``S1``
     - strong
     - identical report files; the other signals are then not listed
   * - ``S2``
     - strong
     - identical recorded output
   * - ``S3``
     - strong
     - the same MAC address in both outputs (all-zero, broadcast and
       multicast addresses are ignored)
   * - ``S4``
     - strong
     - the same ``START_TIME`` (to the second) and ``DURATION`` (to the
       microsecond)
   * - ``M1``
     - medium
     - at least 20 identical consecutive keyboard delays, to the
       microsecond, which points at a copied timing log
   * - ``M2``
     - medium
     - at least three consecutive ``ping`` times (``time=... ms``) in the
       same order, or at least three shared ``tcpdump`` timestamps with
       microseconds; single ``ping`` times match by chance too often, since
       ``ping`` prints only three significant digits
   * - ``M3``
     - medium
     - byte-identical ``CPU.txt``, including the BogoMIPS value measured at
       boot
   * - ``M4``
     - medium
     - at least two shared commands whose output says ``command not found``
       or ``No such file``
   * - ``W1``
     - weak
     - the same CPU model (not shown when ``CPU.txt`` is identical)

Unreadable paths are reported as warnings and skipped. The exit code is
``0`` when at least one report was read and ``1`` otherwise.

