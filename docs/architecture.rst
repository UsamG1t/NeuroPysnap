Architecture
============

PySnap is organized into a few focused packages.

Package Layout
--------------

- ``pysnap.cli`` contains argument parsing and terminal output formatting.
- ``pysnap.config`` contains persistent configuration helpers.
- ``pysnap.core`` contains domain models and the main application service.
- ``pysnap/docs`` stores compiled HTML documentation bundled with the wheel.
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
          runtime [label="pysnap.runtime"];
          terminal [label="pysnap.terminal"];
          vbox [label="pysnap.vbox"];

          cli -> core [label="command dispatch"];
          cli -> terminal [label="connect"];
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

Documentation Packaging Strategy
--------------------------------

Sphinx builds the HTML documentation into ``docs/_build/html``. During wheel
packaging, ``doit`` copies that compiled tree into ``pysnap/docs`` so the
installed package can open its own local documentation through ``pysnap docs``
without requiring Sphinx at runtime.
