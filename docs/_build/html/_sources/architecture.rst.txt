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
- select linked clones for ``erase --clones-only`` without a separate registry

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

Terminal Selection Strategy
---------------------------

Mouse selection in ``pysnap connect`` only captures text; the copy itself is an
explicit ``Ctrl-Shift-C`` action. Classic terminals transmit ``Ctrl-Shift-C``
and ``Ctrl-C`` as the same ``ETX`` byte, so PySnap resolves the chord by
selection state: with a captured selection it copies to the host clipboard,
without one it forwards a real interrupt to the guest. Incoming guest output
drops only the visible highlight and keeps the captured text, which makes
copying safe while background utilities continue printing.

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

Host Cleanup Strategy
---------------------

The ``full-clean`` command removes the VirtualBox machine folder and the
platform-specific configuration directory directly from the host file system
through ``shutil.rmtree``. The CLI layer owns the double confirmation dialog,
while ``PySnapService.full_clean`` performs the removal and reports partial
failures. The ``VBOX_USER_HOME`` environment variable overrides the detected
configuration directory, and the repeated ``--path`` option replaces the
default selection entirely.

Listing Robustness Strategy
---------------------------

``VBoxManage list`` can block indefinitely when the VirtualBox configuration
directory is missing while a stale ``VBoxSVC`` process is still running, for
example right after ``pysnap full-clean``. ``VBoxManageClient`` therefore runs
the ``list`` commands with a bounded timeout and reports expired timeouts as
regular command errors. The ``list`` CLI command translates such failures into
an empty listing, so the user sees ``No virtual machines found.`` instead of a
hanging process. Other ``VBoxManage`` operations keep running without a
timeout because imports, snapshots, and clone creation are legitimately
long-running.

Documentation Packaging Strategy
--------------------------------

Sphinx builds the HTML documentation into ``docs/_build/html``. During wheel
packaging, ``doit`` copies that compiled tree into ``pysnap/docs`` so the
installed package can open its own local documentation through ``pysnap docs``
without requiring Sphinx at runtime.
