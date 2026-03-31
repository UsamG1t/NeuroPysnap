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
- Configure `UART1` as a `tcpserver` endpoint.
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
pysnap --integration-test IMAGE.ova|IMAGE.ovf
pysnap IMAGE.ova|IMAGE.ovf
pysnap show <VM>
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
- `doit` for task automation
- `python -m build --wheel` for packaging

Typical workflow:

```bash
.pysnap/bin/doit test
.pysnap/bin/doit docs
.pysnap/bin/doit wheel
```

## Runtime Examples

Start a VM in headless mode and attach to its serial console:

```bash
pysnap connect srv
```

Monitor running or changing VMs in a compact form:

```bash
pysnap monitor
srv (state: Working ; 2345 ; /Lab)
db (state: Active ; 2346 ; /Lab)
router (state: Changing ; 2347 ; /Net)
```

Stop a single machine or all running machines:

```bash
pysnap stop srv
pysnap stop --all
```
