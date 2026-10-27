Usage Guide
===========

VBoxLab exposes a single command line entry point named ``vboxlab``.

Help
----

Running the command without arguments, or with ``--help``, lists the commands
in groups with a one-line summary each. Every command has its own help with a
description, its arguments and options, and examples:

.. code-block:: text

   vboxlab --help
   vboxlab clone --help
   vboxlab report --help
   vboxlab report check --help

Open Bundled Documentation
--------------------------

The ``docs`` command opens the compiled HTML documentation that is bundled
inside the installed VBoxLab package.

By default, VBoxLab tries to launch ``firefox``. When it is not available, pass
an explicit browser executable through ``--browser``.

.. code-block:: text

   vboxlab docs
   vboxlab docs --browser /usr/bin/chromium

List Virtual Machines
---------------------

The ``list`` command prints all detected VirtualBox groups and the names of the
virtual machines inside each group.

When the VirtualBox data directories are missing or the ``VBoxManage`` service
does not respond, for example right after ``vboxlab full-clean``, the ``list``
command prints ``No virtual machines found.`` and exits cleanly. VBoxLab runs
the underlying ``VBoxManage list`` calls with a bounded timeout, so a stale
``VBoxSVC`` process cannot block the listing forever.

.. code-block:: text

   vboxlab list

Run an Integration Test
-----------------------

VBoxLab can run a real end-to-end VirtualBox integration scenario directly from
the command line:

.. code-block:: text

   vboxlab --integration-test image.ova

The integration scenario:

- imports the appliance as a temporary lab VM, as ``--lab-vm`` does
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

Access to the Guest
-------------------

VBoxLab reaches the guest of a VM in one of two ways:

- **COM1 serial console**: ``UART1`` is a TCP server on the host
- **ssh**: a rule named ``vboxlab-ssh`` on the NAT adapter forwards a host
  port on ``127.0.0.1`` to port 22 of the guest

The access is a property of the VM, stored in VirtualBox extra data
(``vboxlab/access``). ``vboxlab import`` sets it (ssh unless another access is
chosen, see `Import an Appliance`_), linked clones copy it from their base VM
and get ports of their own, and ``vboxlab set VM access=com|ssh`` changes it
(see `Set Serial Ports and Network Adapters`_). A VM imported with
``--clean-import`` stores no access; it counts as an ssh VM without a
forwarded port until ``vboxlab set`` gives it one.

Lab VMs (``--lab-vm`` or ``vboxlab set VM lab=yes``, extra data
``vboxlab/lab``) are reached through the COM1 console and give their clones
the lab DMI settings (see `Create a Linked Clone`_).

ssh connections use the guest user ``root`` unless ``vboxlab set VM
ssh-user=NAME`` names another one; clones inherit it. ``vboxlab show`` prints
the access of a VM, marked ``(lab)`` for lab VMs and ``(default)`` for VMs that
store none, and ``vboxlab monitor`` shows ``ssh:PORT`` instead of the serial
port for ssh VMs.

.. code-block:: text

   vboxlab show first
   ...
   Access: COM1 serial console, port 1024 (lab)

   vboxlab set first access=ssh ssh-user=student

Install the ssh Key
~~~~~~~~~~~~~~~~~~~

ssh connections of VBoxLab use only key login. VBoxLab keeps its own key pair in
``~/.vboxlab/ssh`` (``vboxlab_vm_access_ed25519``, created once without a
passphrase) and one ``known_hosts`` file per VM in
``~/.vboxlab/ssh/known_hosts.d/<VM UUID>``: the first host key of a VM is
accepted, a different key later is refused. ``~/.ssh`` is never read or
changed, and existing files are never overwritten. VBoxLab runs the system
OpenSSH client ``ssh``, which is part of Linux distributions, macOS and
Windows 10 and newer.

``vboxlab ssh-key`` installs the key in a running guest:

.. code-block:: text

   vboxlab ssh-key first --user student
   student@127.0.0.1's password:
   VBoxLab: the key is installed for student.
   VBoxLab: enter the root password to install the key for root too (press Enter to skip).
   Password:
   VBoxLab: the key is installed for root.
   VBoxLab key works for: student, root

- ssh logs in with the password of the given user (by default the ssh user
  of the VM) in the current terminal and adds the key for that user
- then the guest asks for the root password through ``su``; with it the key is
  added for root as well, an empty or wrong password skips root
- many systems refuse a root password over ssh, so the login usually uses an
  ordinary user with ``--user``
- when the ssh user of the VM cannot use the key afterwards but the login user
  can, the ssh user of the VM is switched to the login user
- with ``--console`` the command that adds the key is typed on the VM screen
  through VirtualBox keyboard input instead, for a shell that is already logged
  in there; the guest needs a Latin keyboard layout

Import an Appliance
-------------------

Import either an ``.ova`` or ``.ovf`` appliance and set up how VBoxLab
reaches the imported VMs.

If the appliance already defines a group, VBoxLab keeps it. Otherwise, the
virtual machine is placed into the ``/Others`` group.

When the appliance contains exactly one VM, ``-o``/``--output-name NAME``
sets the name of the imported VM.

Before running ``VBoxManage import``, VBoxLab checks the final VM names. If a
name already exists, the import is aborted before creating anything, with a
hint to choose another name with ``--output-name``.

One of these options chooses the setup of every imported VM; they exclude
each other:

- ``--ssh-access-vm`` (the default): ssh access; a free host port is
  forwarded to the guest ssh server on ``NIC1``
- ``--com-access-vm``: access through the COM1 serial console; ``UART1``
  becomes a TCP server on a free port
- ``--lab-vm``: like ``--com-access-vm``, and the VM is a lab VM whose clones
  get the lab DMI settings (see `Create a Linked Clone`_)
- ``--clean-import``: nothing is changed; the VM keeps the settings of the
  appliance and stores no access

Except for ``--clean-import``, ``NIC1`` is attached to NAT, the port is always
a free one (also when the appliance brings its own ``UART1`` TCP server), and
an ssh forwarding rule that came with the appliance is replaced. VBoxLab
always records that it imported the VM (``vboxlab/managed``, ``vboxlab/kind``
and ``vboxlab/source`` in VirtualBox extra data).

During the import, VBoxLab renders a live progress bar based on the percentage
output reported by ``VBoxManage import``; afterwards it lists the access of
every imported VM.

.. code-block:: text

   vboxlab import image.ova
   vboxlab import protocols.ova -o first --lab-vm
   Importing [############################....]  87%
   Imported virtual machines:
   - first (/Lab): COM1 serial console, port 1024 (lab)

   vboxlab import router.ova --com-access-vm
   vboxlab import image.ova --clean-import

.. only:: graphviz

   .. graphviz::
      :caption: Import-and-connect workflow for a lab VM.

      digraph import_connect_workflow {
          rankdir=LR;
          node [shape=box, style="rounded,filled", fillcolor="#f6f6f6"];

          import [label="vboxlab import IMAGE --lab-vm"];
          modifyvm [label="VBoxManage modifyvm\n--nic1 nat\n--uart1 0x3F8 4\n--uartmode1 tcpserver PORT"];
          vm [label="VM with UART1 tcpserver", shape=ellipse, fillcolor="#fff8dc"];
          connect [label="vboxlab connect VM"];
          headless [label="VBoxManage startvm\n--type=headless"];
          terminal [label="VBoxLab terminal session"];

          import -> modifyvm -> vm;
          connect -> headless -> terminal -> vm;
      }

.. only:: not graphviz

   Graphviz example diagrams are rendered automatically when the ``dot``
   executable is available on the documentation build host.

Show Virtual Machine Details
----------------------------

The ``show`` command prints the VM name, the assigned group, the serial TCP
port configured through ``UART1`` and the access (see `Access to the Guest`_).

.. code-block:: text

   vboxlab show MyVM

Set Serial Ports and Network Adapters
-------------------------------------

The ``set`` command shows or changes the serial (COM) ports and the network
adapters of a VM. Each option is ``KEY=VALUE``; ``N`` is any number the VM
offers: VirtualBox has ``COM1``-``COM4`` and, depending on the chipset of the
VM, ``NIC1``-``NIC8`` (PIIX3) or ``NIC1``-``NIC36`` (ICH9). The output lists
``NIC1``-``NIC4`` and further adapters up to the last one in use.

- ``comN=auto``: a TCP server on a free port (an existing TCP server keeps
  its port); ``comN=PORT`` uses the given port
- ``comN=server:NAME`` and ``comN=client:NAME``: a host pipe between two VMs,
  created by the VM with ``server`` and joined by the VM with ``client``
- ``comN=off``: no serial port
- ``nicN=NETWORK``: the internal network ``NETWORK``
- ``nicN=nat``: NAT; ``nicN=off``: no network adapter
- ``access=com``: access through the COM1 console; unless ``COM1`` already is
  a TCP server (or ``com1=`` is given), it becomes one on a free port, which
  needs a stopped VM
- ``access=ssh``: access through ssh; unless a port is forwarded already, a
  free port is forwarded on the first NAT adapter, on a stopped, running or
  paused VM; without a NAT adapter a warning asks to add one
  (``nic1=nat``) and to repeat ``access=ssh``
- ``lab=yes|no``: whether clones of the VM get the lab DMI settings; the
  access does not change
- ``ssh-user=NAME``: the guest user for ssh; ``lab`` and ``ssh-user`` work on
  a VM in any state (see `Access to the Guest`_)

.. code-block:: text

   vboxlab set first  nic2=left nic3=right com2=server:link
   vboxlab set second nic2=left com2=client:link
   vboxlab set first
   first (poweroff)
     COM1  TCP server, port 2326
     COM2  pipe server, creates "link" (/tmp/vboxlab-link)
     COM3  off
     COM4  off
     NIC1  NAT
     NIC2  internal network "left"
     NIC3  internal network "right"
     NIC4  off

Without options the current settings are printed. Changes of ports and
adapters require a stopped VM; all options are checked first and then applied together, so an invalid
option changes nothing. VBoxLab checks that a TCP port is not used by another
VM, by another COM port of the same VM or on the host. Removing a TCP server
prints a warning; ``vboxlab connect`` uses ``COM1`` and cannot reach a VM
without it.

A pipe name is short; VBoxLab turns it into the path format of the host
(``\\.\pipe\vboxlab-NAME`` on Windows, a ``vboxlab-NAME`` socket in the
temporary directory elsewhere), so the same name connects two VMs on any
platform.

For clones of lab VMs, the DMI system SKU is rebuilt from the
new ``COM1`` port and the internal networks of ``NIC2``-``NIC4``, for example
``port2326.left.right``. An adapter without an internal network leaves an
empty place (``port2326..right``). When ``COM1`` is no TCP server, the
highest TCP port of the other COM ports is used; without any TCP port the
port part of the previous SKU is kept.

Connect to a Headless Virtual Machine
-------------------------------------

The ``connect`` command ensures that the selected VM is running in headless
mode and then attaches a built-in VBoxLab terminal interface to the guest: to
its ``UART1`` TCP console, or to an ssh login shell for VMs reached through
ssh (see `Access to the Guest`_).

For VMs that do not already expose a suitable ``UART1 tcpserver`` endpoint or
a forwarded ssh port, run ``vboxlab set <VM> access=com`` or ``access=ssh``
first.

Over ssh, ``connect`` logs in with the VBoxLab key as the ssh user of the VM:

- while the guest is still booting and no ssh server answers yet, VBoxLab
  waits up to three minutes and says so once; ``Ctrl-C`` stops waiting
- when the guest refuses the key, VBoxLab installs it right away in the same
  way as ``vboxlab ssh-key`` (password of the user, then optionally of root)
  and then logs in with the key
- the status line shows ``ssh:PORT`` instead of ``UART1:PORT``, no waking
  newline is sent because the login shell prints its own prompt, and the
  session ends when the login shell exits (``exit`` or ``Ctrl-D``)
- a paused VM is refused with a hint to run ``vboxlab resume``, because a
  paused guest cannot answer the login
- one ``connect`` session per VM is allowed, as on the serial console

All keys go to the guest, so editors and shells get ``Esc``, ``Ctrl-L``,
``Ctrl-Q``, ``Alt`` combinations and function keys as in a regular terminal.
VBoxLab commands start with ``Ctrl-]``, which guest programs hardly use, as in
``telnet``; the status line then lists the command keys:

- ``q`` detaches the session; detaching does not stop the virtual machine
- ``Up`` and ``Down`` scroll the local scrollback buffer by one line,
  ``PgUp`` and ``PgDn`` by one screen, ``Home`` jumps to the oldest retained
  output and ``End`` back to the live output; the command keys stay active
  for further scrolling
- ``v`` pastes the text of the host clipboard (see below)
- ``Ctrl-]`` sends ``Ctrl-]`` itself to the guest
- ``Esc`` or any other key leaves the command keys without sending anything
- on Linux, the mouse wheel or touchpad scroll gesture moves the local
  scrollback buffer by three lines while the command keys are active

Outside the command keys the mouse belongs to your terminal: select, copy
and paste text the way your terminal does it for every program, for example
by dragging and ``Ctrl-Shift-C``/``Ctrl-Shift-V`` or the middle button on
Linux, ``Ctrl-C``/``Ctrl-V`` or the right button in Windows Terminal, the
right button in the Windows console and ``Cmd-C``/``Cmd-V`` on macOS. Text
in the local scrollback can be selected after scrolling to it with the
command keys. ``Ctrl-C`` always goes to the guest as an interrupt.

A pasted text reaches the guest the way a terminal sends it: line breaks of
any system become the Enter key (``CR``), so a text copied on Windows gives no
empty lines, and when the guest asked for bracketed paste, as bash and vim
do, the text is marked as a paste; bash then waits for Enter instead of
running each pasted line, and vim does not indent it again. ``Ctrl-]`` ``v``
pastes the host clipboard in the same way for terminals without a paste key,
for example the Windows console without its Ctrl key shortcuts, where
``Ctrl-V`` reaches the guest as ``^V``. The clipboard is read only then: on
Windows through the system API, on macOS with ``pbpaste``, elsewhere with
``wl-paste`` (Wayland), ``xclip`` or ``xsel``; the status line tells when none
is installed. Over the serial console a long paste can lose characters, because
the console has no flow control.

Full-screen programs such as vim, nano and less use the alternate screen:
while they run, the local scrollback is off, and they scroll with their own
keys; when they exit, the shell screen comes back as it was, and their
screens never enter the scrollback.

While the session is attached, VBoxLab continuously tracks the outer terminal
size and resizes the visible guest text area to match it. This also works
after reconnecting to an already running VM.

For xterm-compatible guest-side tools, VBoxLab replies to standard size queries
such as ``CSI 18 t`` and cursor-position reports such as ``CSI 6 n``. On Linux
guests this makes terminal-size refresh workflows such as ``resize`` much more
practical over the raw serial console.

On macOS, VBoxLab also checks the standard VirtualBox application-bundle path
for ``VBoxManage`` when the command is not exported in ``PATH``.

.. code-block:: text

   vboxlab connect MyVM

A paused VM can be attached through ``UART1`` too: the status line shows
``Paused``, keys are held back and nothing is sent to the guest until
``vboxlab resume``; the ``Ctrl-]`` commands keep working. Then the keys work
again, and when the guest has not printed anything yet VBoxLab sends the usual
waking newline. When a VM is paused during a session, the session stays open
in the same way, so ``Ctrl-]`` ``q`` on a paused VM and a new
``connect`` lead to the same state. If the paused VM does not accept the
serial connection within three seconds, ``connect`` fails with a hint to run
``vboxlab resume``.

Monitor Active Virtual Machines
-------------------------------

The ``monitor`` command prints compact runtime records in the form
``<VM> (state: <State> ; <serial port> ; <group>)``.

VBoxLab currently uses these runtime labels:

- ``Working`` when the VM is running and VBoxLab has an active attached session
- ``Active`` when the VM is running in headless mode without an attached session
- ``Stopping`` when the VM is shutting down gracefully
- ``Changing`` when the VM is starting or otherwise transitioning
- ``Paused`` when VirtualBox reports a paused machine
- ``Error`` when VirtualBox reports an error-like runtime state

.. code-block:: text

   vboxlab monitor
   srv (state: Working ; 2345 ; /Lab)
   db (state: Active ; 2346 ; /Lab)
   router (state: Stopping ; 2347 ; /Net)

Stop Running Virtual Machines
-----------------------------

VBoxLab stops headless virtual machines through ``VBoxManage controlvm
<VM> acpipowerbutton`` only. No automatic fallback to ``savestate`` or other
shutdown modes is used.

When a VM stops, any active ``vboxlab connect`` session attached to it finishes
automatically because the VM state changes and the serial connection is closed.

A paused guest cannot handle the ACPI power button, so ``stop`` resumes a
paused VM first; ``stop --all`` also stops paused VMs.

.. code-block:: text

   vboxlab stop MyVM
   vboxlab stop --all

Pause and Resume Virtual Machines
---------------------------------

``pause`` freezes a running VM through ``VBoxManage controlvm <VM> pause``; it
stays in memory and shows up as ``Paused`` in ``vboxlab monitor``. ``resume``
continues it. With ``--all`` the commands act on every running or every paused
VM. This is not ``savestate``: nothing is written to disk and the VM does not
stop.

.. code-block:: text

   vboxlab pause MyVM
   vboxlab resume MyVM
   vboxlab pause --all
   vboxlab resume --all

Create a Linked Clone
---------------------

The ``clone`` command creates a linked clone from a base VM. The clone inherits
the base VM group.

When ``-p`` is provided, its value is used as the host TCP port for
``modifyvm <VM> --uart1 0x3F8 4 --uartmode1 tcpserver <port>``.

When ``-p`` is omitted, VBoxLab scans existing VMs, finds the maximum already
used serial TCP port, and assigns the next value. If no serial TCP ports are
configured yet, the automatic sequence starts at ``1024``.

Up to three extra positional arguments attach internal networks to ``NIC2``
through ``NIC4`` in order. ``NIC1`` and the adapters after the given networks
keep the settings of the base VM; for VMs imported with a setup ``NIC1`` is
the NAT adapter. ``COM1`` of every clone is a TCP server, also for clones
reached through ssh, where it serves as a spare console.

A clone copies the access, the lab property and the ssh user of its base VM.
It also copies the ``vboxlab-ssh`` rule of the base VM; VBoxLab removes that
copy, because its host port belongs to the base VM. A clone of a base VM with
ssh access gets its own free port on the NAT adapter of the base VM; the clone
is refused before anything is created when the base VM has no NAT adapter or
the networks would replace it. A clone of a base VM that stores no access
gets no forwarded port.

Clones of lab VMs also receive these DMI values, which the lab images use:

- ``DmiSystemVendor = <CloneVM>``
- ``DmiSystemSKU = port<Port>[.<net1-name>[.<net2-name>[.<net3-name>]]]``

This behavior is relevant for the following CMC MSU courses:

- `LinuxNetwork <https://uneex.org/LecturesCMC/LinuxNetwork2026>`_
- `Nets: Introduction <https://asvk.cs.msu.ru/uchebnyj-process/chitaemye-kursy/vvedenie-v-seti-evm/>`_
- `Methodics of Linux Net Protocols <https://github.com/UsamG1t/Methodics_LinuxNetProtocols>`_
- `Labs of Linux Net Protocols <https://github.com/UsamG1t/Nets_ASVK_Labs>`_

.. code-block:: text

   vboxlab clone BaseVM CloneVM -p 2345 intnet1 intnet2 intnet3
   vboxlab clone BaseVM CloneVM intnet1

Erase Virtual Machines
----------------------

VBoxLab supports three erase modes:

- ``vboxlab erase VM`` removes a single VM when no dependent linked clones exist.
- ``vboxlab erase --group GROUP`` removes all VMs inside one group.
- ``vboxlab erase --all`` removes all registered VirtualBox VMs.

The single-VM erase mode refuses deletion when dependent clones still exist.
The group erase mode refuses deletion when descendants outside the target group
still depend on the selected VMs.

Every erase mode accepts the ``--clones-only`` flag, which restricts the
operation to VMs created as VBoxLab linked clones:

- ``vboxlab erase --all --clones-only`` removes every linked clone and keeps the
  imported base VMs registered.
- ``vboxlab erase --group GROUP --clones-only`` removes only the linked clones
  inside one group. The operation is refused when clones outside the group
  still depend on the selected clones.
- ``vboxlab erase VM --clones-only`` behaves like the normal single-VM erase for
  a clone. For a base VM, VBoxLab prints a warning and refuses the deletion, so
  the flag stays safe in scripted cleanups.

VBoxLab recognizes clones through the ``vboxlab/kind`` and ``vboxlab/parent``
metadata written into VirtualBox extra data during cloning, so the clone list
stays correct without a separate registry file.

.. code-block:: text

   vboxlab erase BaseVM
   vboxlab erase --group /Lab
   vboxlab erase --all
   vboxlab erase --all --clones-only
   vboxlab erase --group /Lab --clones-only
   vboxlab erase CloneVM --clones-only

Remove All VirtualBox Data
--------------------------

The ``full-clean`` command permanently deletes the VirtualBox machine folder
and the VirtualBox configuration directory from the host file system. Stop
VirtualBox and all virtual machines before running it.

Without ``--path``, VBoxLab removes ``~/VirtualBox VMs`` together with the
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

   vboxlab full-clean
   vboxlab full-clean --path /data/vms --path /data/vbox-config

Read Session Reports
--------------------

Students record their work inside the educational VMs with the ``report``
utility, which produces a ``report.<NN>.<host>`` archive. The
``vboxlab report`` command reads such archives safely: nothing is extracted to
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

   vboxlab report text report.01.first
   vboxlab report text report.01.first --commands
   1  00:17.05  ip a show eth1
   2  00:29.86  ping -c5 10.9.0.2
   vboxlab report text report.01.first --color never > report.01.first.txt
   vboxlab report text report.01.first --no-pager

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

   vboxlab report show report.01.first
   vboxlab report show report.01.first --speed 4 --max-delay 0.5

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

   vboxlab report check report.01.first
   vboxlab report check report.01.first lab02-first.check.toml

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

Commands are checked in file order, then output blocks. VBoxLab chooses among
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

The ``extract`` subcommand copies a report from a running VM to the host.
A VM reached through ssh (see `Access to the Guest`_) is read through a
separate ssh session; every other VM through the same ``UART1`` serial
console that ``vboxlab connect`` uses, so no second COM port and no VM restart
are needed.

.. code-block:: text

   vboxlab report extract first report.01.first
   Extracted "$HOME"/report.01.first from first to /home/user/report.01.first (2621 bytes, sha256 ...).
   vboxlab report extract first /tmp/report.03.pc1 --output reports/pc1 --force

Over ssh:

- the VM is running, has a forwarded ssh port (see ``vboxlab set VM access=ssh``) and the
  VBoxLab key is installed for its ssh user (see ``vboxlab ssh-key``); unlike
  ``connect``, ``extract`` never asks for a password
- one command prints the ``sha256sum`` of the file and the file itself;
  VBoxLab verifies the checksum and writes the file as described below
- the student's terminal is not touched: nothing is typed into it, so the
  command works the same with or without an attached ``vboxlab connect`` and
  needs no shell prompt there. A report that is still being recorded is
  copied as it is at that moment
- a name without ``/`` becomes ``"$HOME"/NAME`` and the guest shell of the
  ssh user expands ``$HOME``: the name is looked up in the home directory of
  the ssh user of the VM (``vboxlab set VM ssh-user=NAME``, ``root`` by
  default) and nowhere else. A path with ``/`` is used as given; a relative
  path starts in that home directory too. With the default user,
  ``report.01.first`` means ``/root/report.01.first``; with
  ``ssh-user=student`` it means ``/home/student/report.01.first``. A report
  recorded by another user needs its full path, for example
  ``vboxlab report extract first /home/student/report.01.first``, and the ssh
  user must be allowed to read it
- the transfer fails when it takes more than two minutes

On the serial console:

- the VM is running and has a ``UART1`` TCP port (see ``vboxlab set VM access=com``)
- the command also works while ``vboxlab connect`` is attached to the VM:
  VirtualBox serves one client, so ``extract`` asks the attached session to
  run the transfer on its connection. The terminal shows a message in the
  status line, keys are paused until the transfer ends, and afterwards the
  screen is cleared and a fresh prompt appears. Sessions started by an older
  VBoxLab cannot do this; detach them with ``Ctrl-]`` ``q`` first
- the console is logged in and at a shell prompt; VBoxLab checks this with
  ``echo VBOXLAB_$((20+22))`` and stops when the answer does not arrive
- no ``report`` recording is running: VBoxLab presses ``Ctrl-U`` and Enter once, and when the
  recording prompt appears it stops before sending any command, so nothing
  else ends up in the student's report
- a name without ``/`` is looked up in the home directory (``$HOME``) of the
  shell running on the console, that is of the user logged in there; a path
  with ``/`` is used as given, and a relative one starts in the current
  directory of that shell
- the file is sent as ``base64`` together with its ``sha256sum``; VBoxLab
  verifies the checksum, writes the file under the same name into the current
  directory or to ``--output``, and never replaces an existing file without
  ``--force``
- the service commands start with a space, so shells with ``HISTCONTROL``
  set to ``ignorespace`` keep them out of the history, and the guest screen is
  cleared afterwards
- after the copy VBoxLab reads the file as a report and warns when it is not
  one

A serial console typically runs at 115200 baud, about 11 KB/s: a report of a
few kilobytes takes well under a second. The transfer fails only when no data
arrives for ten seconds.

Compare Reports
~~~~~~~~~~~~~~~

The ``compare`` subcommand compares every pair of the given reports and lists
signs that two reports share their origin. Paths may be report files or
directories; directories are searched recursively for files named
``report.NN.HOST``, and a file reached twice is compared once. VBoxLab does not
know which reports belong to the same student: reports of one student
legitimately share values such as the MAC address of the same VM, so the
teacher decides which pairs matter.

.. code-block:: text

   vboxlab report compare reports/
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

