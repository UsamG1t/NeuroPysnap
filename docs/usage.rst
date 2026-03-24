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
- prints details for the base VM and all three clones
- removes the four VMs one by one

Import an Appliance
-------------------

Import either an ``.ova`` or ``.ovf`` appliance.

If the appliance already defines a group, PySnap keeps it. Otherwise, the
virtual machine is placed into the ``/Others`` group.

.. code-block:: text

   pysnap image.ova
   pysnap image.ovf

Show Virtual Machine Details
----------------------------

The ``show`` command prints the VM name, the assigned group, and the serial TCP
port configured through ``UART1``.

.. code-block:: text

   pysnap show MyVM

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

.. code-block:: text

   pysnap erase BaseVM
   pysnap erase --group /Lab
   pysnap erase --all
