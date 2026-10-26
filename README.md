# VBoxLab

VBoxLab 2.2.1 is a Python 3.11+ command line package for importing VirtualBox OVA/OVF
appliances, creating linked clones, configuring internal networks, starting
virtual machines in headless mode, attaching to them through a built-in
terminal over the serial console or ssh, and removing virtual machines with dependency checks.

## Features

- Import `.ova` and `.ovf` appliances with VirtualBox group normalization.
- List VirtualBox groups and their member virtual machines.
- Show VM name, group, and configured serial TCP port.
- Create linked clones with inherited groups and internal network mapping.
- Support two clone network layouts: the legacy sequential mapping for ordinary
  base VMs, and the NAT-preserving ``NIC2``-``NIC4`` mapping for base VMs
  registered through ``vboxlab protosettings``.
- Register special base VMs whose clones receive additional DMI settings.
- Configure `UART1` as a `tcpserver` endpoint.
- Plug a stopped VM into the VBoxLab connection model by assigning a safe
  `UART1 tcpserver` port.
- Allocate the next available serial TCP port automatically when `-p` is omitted.
- Show or change the COM ports and network adapters of a stopped VM with
  `vboxlab set`: TCP servers, host pipes between VMs, internal networks, NAT.
- Reach protosettings VMs and their clones through the COM1 serial console and
  every other VM through ssh over a forwarded NAT port; `vboxlab set` can
  override the choice and the ssh user.
- Install a VBoxLab-only ssh key in a guest with `vboxlab ssh-key`, through a
  password login (with an optional `su` for root) or typed on the VM screen.
- Start virtual machines in headless mode and attach to them through a
  cross-platform Python terminal interface.
- Track outer-terminal resize events during ``connect`` sessions and answer
  xterm-compatible size queries for guest-side tools.
- Select terminal text with the mouse; it is copied to the host clipboard
  when the button is released.
- Open the bundled HTML documentation directly from the installed package.
- Monitor active and changing virtual machines with compact runtime states.
- Stop one running VM or all running VMs through `acpipowerbutton`.
- Pause and resume one VM or all VMs; `connect` stays attached to a paused VM
  with keys held back until it is resumed.
- Discover `VBoxManage` automatically on macOS through the standard
  `/Applications/VirtualBox.app/...` bundle path when needed.
- Delete one VM, a group of VMs, or all registered VMs with dependency checks,
  optionally restricted to linked clones with `--clones-only`.
- Wipe the VirtualBox machine and configuration directories with
  `vboxlab full-clean` after a double interactive confirmation, with
  OS-specific defaults and explicit `--path` overrides.
- Read student session reports recorded with the ``report`` utility safely:
  print or page their text with highlighted prompts and commands, or replay
  them with timing, speed control and jumps between commands; show report
  information and statistics: identity, timing, commands, pauses, typing,
  pasted input, addresses and integrity checks; grade a report against a
  TOML check file with expected commands, output blocks and labeled values;
  copy reports out of running VMs through the serial console or ssh; compare
  reports pairwise for signs of a shared origin.
- Run an end-to-end integration test that now verifies VM startup and monitor
  state transitions in addition to creation and cleanup.

## Command Summary

`vboxlab --help` lists the commands; `vboxlab COMMAND --help` describes one
command with its arguments, options and examples.

```text
vboxlab
vboxlab list
vboxlab import IMAGE.ova|IMAGE.ovf [VMName]
vboxlab --integration-test IMAGE.ova|IMAGE.ovf
vboxlab protosettings BASE_VM
vboxlab docs [--browser BROWSER]
vboxlab show <VM>
vboxlab plug <VM>
vboxlab ssh-key <VM> [--user USER] [--console]
vboxlab set <VM> [comN=auto|PORT|server:NAME|client:NAME|off] [nicN=NETWORK|nat|off] ...
vboxlab connect <VM>
vboxlab monitor
vboxlab stop [<VM> | --all]
vboxlab pause [<VM> | --all]
vboxlab resume [<VM> | --all]
vboxlab clone <BaseVM> <CloneVM> [-p PORT] [<int1-net> [<int2-net> [<int3-net>]]]
vboxlab erase [--clones-only] [--all | --group GROUP | <VM>]
vboxlab full-clean [--path DIRECTORY ...]
vboxlab report text REPORT [--commands] [--color WHEN] [--no-pager]
vboxlab report show REPORT [--speed X] [--max-delay SECONDS]
vboxlab report check REPORT [CHECK]
vboxlab report extract VM NAME [--output PATH] [--force]
vboxlab report compare PATH...
```

## Development

The project uses:

- `pyproject.toml` for package metadata
- `Sphinx` as a build-time dependency for compiled documentation
- `Graphviz` on the build host for documentation diagrams
- `doit` for task automation
- `python -m build --wheel` for packaging

Typical workflow:

```bash
.vboxlab/bin/doit test
.vboxlab/bin/doit docs
.vboxlab/bin/doit wheel
```

## Runtime Examples

Import an appliance with a live progress bar:

```bash
vboxlab import image.ova
vboxlab import image.ova renamed-vm
Importing [############################....]  87%
```

Open the bundled HTML documentation in Firefox by default or in an explicit
browser:

```bash
vboxlab docs
vboxlab docs --browser /usr/bin/chromium
```

Start a VM in headless mode and attach to its serial console or, for VMs
reached through ssh, to an ssh login shell:

```bash
vboxlab connect srv
```

While attached, VBoxLab keeps the guest-visible area synchronized with the
current terminal size. On Linux guests, xterm-compatible tools such as
``resize`` can use the serial connection to rediscover the updated geometry.
All keys go to the guest, so editors such as vim and nano work as in a
regular terminal. VBoxLab commands start with ``Ctrl+]``: then ``q`` detaches,
``Up``/``Down``/``PgUp``/``PgDn``/``Home``/``End`` scroll the local scrollback
buffer, ``Ctrl+]`` sends ``Ctrl+]`` to the guest and ``Esc`` goes back. On
Linux, the mouse wheel drives the same local scrollback, or sends cursor keys
to full-screen programs such as less. Dragging with the left mouse button
selects visible terminal text, which is copied to the host clipboard when the
button is released.

Plug a stopped VM into the same serial-console workflow used by imported
protocol images:

```bash
vboxlab plug srv
Name: srv
Group: /Lab
Serial port: 2345
```

Connect two stopped clones through internal networks and a COM2 host pipe:

```bash
vboxlab set first nic2=left com2=server:link
vboxlab set second nic2=left com2=client:link
```

Monitor running or changing VMs in a compact form:

```bash
vboxlab monitor
srv (state: Working ; 2345 ; /Lab)
db (state: Active ; 2346 ; /Lab)
router (state: Stopping ; 2347 ; /Net)
```

Register a base VM for educational protocol settings:

```bash
vboxlab protosettings protocols-jeos-20251218-x86_64
```

Stop a single machine or all running machines:

```bash
vboxlab stop srv
vboxlab stop --all
```

Pause a VM and continue it later; it stays in memory meanwhile:

```bash
vboxlab pause srv
vboxlab resume srv
```

Delete only linked clones, keeping the imported base VMs. VBoxLab recognizes
clones through the `pysnap/kind` metadata it writes into VirtualBox extra data
during cloning, so the clone list stays correct without a separate registry:

```bash
vboxlab erase --all --clones-only
vboxlab erase --group Lab --clones-only
vboxlab erase clone-vm --clones-only
```

Erasing a base VM with `--clones-only` is refused with a warning and no VM is
deleted, so the flag can be used safely in scripted cleanups.

Wipe every VirtualBox data directory after two explicit confirmations. Without
`--path`, VBoxLab removes `~/VirtualBox VMs` plus the OS-specific configuration
directory (`~/.config/VirtualBox` on Linux, `~/Library/VirtualBox` on macOS,
`~/.VirtualBox` on Windows, or `VBOX_USER_HOME` when set):

```bash
vboxlab full-clean
vboxlab full-clean --path /data/vms --path /data/vbox-config
```

When the VirtualBox data directories are missing, for example right after a
full clean, `vboxlab list` reports `No virtual machines found.` and exits
cleanly instead of waiting on an unresponsive `VBoxManage`.

Print a student report as text or list the entered commands:

```bash
vboxlab report text report.01.first
vboxlab report text report.01.first --commands
1  00:17.05  ip a show eth1
2  00:29.86  ping -c5 10.9.0.2
```

Replay a report with its timing (``Space`` pauses, ``n``/``p`` jump between
commands, ``q`` quits):

```bash
vboxlab report show report.01.first --speed 2
```

Compare all reports of a group pairwise; pairs with signals of a shared
origin are listed with those signals, the others as ``OK``:

```bash
vboxlab report compare reports/
```

## Proto Settings

The ``vboxlab protosettings <BaseVM>`` command marks a base VM so that every
linked clone created from it receives additional DMI settings. VBoxLab stores
this base-VM list in ``Path.home() / ".ptotosettings"``, which resolves to the
current user's home directory on Linux, macOS, and Windows.

This mode exists for the appliance conventions used in the educational program
of CMC MSU. When the base VM is present in the proto-settings list, VBoxLab
applies these clone-time changes:

- ``DmiSystemVendor = <CloneVM>``
- ``DmiSystemSKU = port<Port>[.<net1-name>[.<net2-name>[.<net3-name>]]]``

The setting is intended for images and labs used in these CMC MSU courses:

- [LinuxNetwork](https://uneex.org/LecturesCMC/LinuxNetwork2026)
- [Nets: Introduction](https://asvk.cs.msu.ru/uchebnyj-process/chitaemye-kursy/vvedenie-v-seti-evm/)
- [Methodics of Linux Net Protocols](https://github.com/UsamG1t/Methodics_LinuxNetProtocols)
- [Labs of Linux Net Protocols](https://github.com/UsamG1t/Nets_ASVK_Labs)
