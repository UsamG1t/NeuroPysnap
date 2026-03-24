# PySnap

PySnap is a Python 3.13 command line package for importing VirtualBox OVA/OVF
appliances, creating linked clones, configuring internal networks, and removing
virtual machines with dependency checks.

## Features

- Import `.ova` and `.ovf` appliances with VirtualBox group normalization.
- List VirtualBox groups and their member virtual machines.
- Show VM name, group, and configured serial TCP port.
- Create linked clones with inherited groups and internal network mapping.
- Configure `UART1` as a `tcpserver` endpoint.
- Allocate the next available serial TCP port automatically when `-p` is omitted.
- Delete one VM, a group of VMs, or all registered VMs with dependency checks.

## Command Summary

```text
pysnap
pysnap list
pysnap --integration-test IMAGE.ova|IMAGE.ovf
pysnap IMAGE.ova|IMAGE.ovf
pysnap show <VM>
pysnap clone <BaseVM> <CloneVM> [-p PORT] [<int1-net> [<int2-net> [<int3-net>]]]
pysnap erase [--all | --group GROUP | <VM>]
```

## Development

The project uses:

- `pyproject.toml` for package metadata
- `Sphinx` for documentation
- `doit` for task automation
- `wheel`-based build commands for packaging

Typical workflow:

```bash
.pysnap/bin/doit test
.pysnap/bin/doit docs
.pysnap/bin/doit wheel
```
