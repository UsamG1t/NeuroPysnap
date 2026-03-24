Architecture
============

PySnap is organized into a few focused packages.

Package Layout
--------------

- ``pysnap.cli`` contains argument parsing and terminal output formatting.
- ``pysnap.core`` contains domain models and the main application service.
- ``pysnap.vbox`` contains the ``VBoxManage`` client and output parsers.
- ``pysnap.tests`` contains unit tests.

Execution Flow
--------------

1. The CLI entry point parses command line arguments.
2. ``PySnapService`` validates the request and coordinates the workflow.
3. ``VBoxManageClient`` executes VirtualBox commands.
4. Parser helpers convert command output into Python models.
5. The CLI renders human-readable results.

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
