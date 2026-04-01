# PySnap

PySnap is a Python 3.13 command line package for importing VirtualBox OVA/OVF
appliances, creating linked clones, configuring internal networks, starting
virtual machines in headless mode, attaching to them through a built-in serial
terminal, and removing virtual machines with dependency checks.

## Features

- Import `.ova` and `.ovf` appliances with VirtualBox group normalization.
- List VirtualBox groups and their member virtual machines.
- Show VM name, group, and configured serial TCP port.
- Create linked clones with inherited groups and internal network mapping.
- Register special base VMs whose clones receive additional DMI settings.
- Configure `UART1` as a `tcpserver` endpoint.
- Plug a stopped VM into the PySnap connection model by assigning a safe
  `UART1 tcpserver` port.
- Allocate the next available serial TCP port automatically when `-p` is omitted.
- Start virtual machines in headless mode and attach to them through a
  cross-platform Python terminal interface.
- Monitor active and changing virtual machines with compact runtime states.
- Stop one running VM or all running VMs through `acpipowerbutton`.
- Discover `VBoxManage` automatically on macOS through the standard
  `/Applications/VirtualBox.app/...` bundle path when needed.
- Delete one VM, a group of VMs, or all registered VMs with dependency checks.
- Run an end-to-end integration test that now verifies VM startup and monitor
  state transitions in addition to creation and cleanup.

## Command Summary

```text
pysnap
pysnap list
pysnap import IMAGE.ova|IMAGE.ovf [VMName]
pysnap --integration-test IMAGE.ova|IMAGE.ovf
pysnap protosettings BASE_VM
pysnap show <VM>
pysnap plug <VM>
pysnap connect <VM>
pysnap monitor
pysnap stop [<VM> | --all]
pysnap clone <BaseVM> <CloneVM> [-p PORT] [<int1-net> [<int2-net> [<int3-net>]]]
pysnap erase [--all | --group GROUP | <VM>]
```

## Development

The project uses:

- `pyproject.toml` for package metadata
- `Sphinx` for documentation
- `Graphviz` on the build host for documentation diagrams
- `doit` for task automation
- `python -m build --wheel` for packaging

Typical workflow:

```bash
.pysnap/bin/doit test
.pysnap/bin/doit docs
.pysnap/bin/doit wheel
```

## Runtime Examples

Import an appliance with a live progress bar:

```bash
pysnap import image.ova
pysnap import image.ova renamed-vm
Importing [############################....]  87%
```

Start a VM in headless mode and attach to its serial console:

```bash
pysnap connect srv
```

Plug a stopped VM into the same serial-console workflow used by imported
protocol images:

```bash
pysnap plug srv
Name: srv
Group: /Lab
Serial port: 2345
```

Monitor running or changing VMs in a compact form:

```bash
pysnap monitor
srv (state: Working ; 2345 ; /Lab)
db (state: Active ; 2346 ; /Lab)
router (state: Changing ; 2347 ; /Net)
```

Register a base VM for educational protocol settings:

```bash
pysnap protosettings protocols-jeos-20251218-x86_64
```

Stop a single machine or all running machines:

```bash
pysnap stop srv
pysnap stop --all
```

## Proto Settings

The ``pysnap protosettings <BaseVM>`` command marks a base VM so that every
linked clone created from it receives additional DMI settings. PySnap stores
this base-VM list in ``Path.home() / ".ptotosettings"``, which resolves to the
current user's home directory on Linux, macOS, and Windows.

This mode exists for the appliance conventions used in the educational program
of CMC MSU. When the base VM is present in the proto-settings list, PySnap
applies these clone-time changes:

- ``DmiSystemVendor = <CloneVM>``
- ``DmiSystemSKU = port<Port>[.<net1-name>[.<net2-name>[.<net3-name>]]]``

The setting is intended for images and labs used in these CMC MSU courses:

- `LinuxNetwork <https://uneex.org/LecturesCMC/LinuxNetwork2026>`_
- `Nets: Introduction <https://asvk.cs.msu.ru/uchebnyj-process/chitaemye-kursy/vvedenie-v-seti-evm/>`_
- `Methodics of Linux Net Protocols <https://github.com/UsamG1t/Methodics_LinuxNetProtocols>`_
- `Labs of Linux Net Protocols <https://github.com/UsamG1t/Nets_ASVK_Labs>`_
