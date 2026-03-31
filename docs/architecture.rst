Architecture
============

PySnap is organized into a few focused packages.

Package Layout
--------------

- ``pysnap.cli`` contains argument parsing and terminal output formatting.
- ``pysnap.config`` contains persistent configuration helpers.
- ``pysnap.core`` contains domain models and the main application service.
- ``pysnap.runtime`` contains the live-session registry shared by runtime tools.
- ``pysnap.terminal`` contains the built-in serial terminal transport, emulator,
  key mapping, and interactive UI session logic.
- ``pysnap.vbox`` contains the ``VBoxManage`` client and output parsers.
- ``pysnap.tests`` contains unit tests.

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

Serial Port Strategy
--------------------

PySnap uses ``UART1`` for the serial TCP endpoint of clones. If no explicit
port is supplied, the service automatically assigns ``max(used_ports) + 1`` or
``1024`` when no TCP serial port has been configured yet.

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
- ``Changing`` for startup, shutdown, and other transitional states
- ``Paused`` for paused VMs
- ``Error`` for explicit error-like VirtualBox states
