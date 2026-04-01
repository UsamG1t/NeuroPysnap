Usage Guide
===========

PySnap exposes a single command line entry point named ``pysnap``.

Help
----

Running the command without arguments shows the built-in help:

.. code-block:: text

   pysnap

List Virtual Machines
---------------------

The ``list`` command prints all detected VirtualBox groups and the names of the
virtual machines inside each group.

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
- ``Changing`` when the VM is starting, stopping, or otherwise transitioning
- ``Paused`` when VirtualBox reports a paused machine
- ``Error`` when VirtualBox reports an error-like runtime state

.. code-block:: text

   pysnap monitor
   srv (state: Working ; 2345 ; /Lab)
   db (state: Active ; 2346 ; /Lab)
   router (state: Changing ; 2347 ; /Net)

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

Up to three extra positional arguments configure internal networks for
``nic1`` through ``nic3`` with the ``intnet`` attachment type.

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

When a VM is removed successfully, PySnap also removes its name from the
proto-settings file if it was registered there.

.. code-block:: text

   pysnap erase BaseVM
   pysnap erase --group /Lab
   pysnap erase --all
