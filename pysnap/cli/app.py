"""Command line interface for PySnap."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import textwrap
from typing import Sequence, TextIO

from pysnap.cli.formatters import (
    ImportProgressBar,
    format_groups,
    format_import_result,
    format_integration_test_result,
    format_monitor_records,
    format_vm_info,
    format_vm_settings,
)
from pysnap import __version__
from pysnap.cli.report import run_report_command
from pysnap.core.service import PySnapService
from pysnap.docview import open_bundled_documentation
from pysnap.errors import CommandExecutionError, PySnapError
from pysnap.terminal.session import TerminalSession


class ParserExit(Exception):
    """Represent a controlled parser exit."""

    def __init__(self, status: int = 0) -> None:
        """Initialize the parser exit.

        :param status: Exit status requested by the parser.
        """
        self.status = status
        super().__init__(status)


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Wrap help paragraphs and keep indented lines such as examples.

    Descriptions and epilogs are split into blank-line separated blocks. A
    block without indented lines is wrapped as one paragraph; a block with
    indented lines (an example list or an option table) is kept as written.
    """

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        blocks = []
        for block in textwrap.dedent(text).strip("\n").split("\n\n"):
            lines = block.splitlines()
            if any(line.startswith(" ") for line in lines):
                blocks.append("\n".join(indent + line for line in lines))
            else:
                blocks.append(
                    textwrap.fill(
                        " ".join(line.strip() for line in lines),
                        width,
                        initial_indent=indent,
                        subsequent_indent=indent,
                    )
                )
        return "\n\n".join(blocks)


# Command groups and one-line summaries shown by ``pysnap --help``.
COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Virtual machines",
        (
            ("list", "List VirtualBox groups and their virtual machines."),
            ("show", "Show the group, serial TCP port and parent of a VM."),
            ("import", "Import an OVA or OVF appliance."),
            ("protosettings", "Register a base VM whose clones get lab settings."),
            ("clone", "Create a linked clone with a serial port and networks."),
            ("set", "Show or change the COM ports and network adapters of a VM."),
            ("plug", "Give an existing VM a TCP serial port for connect."),
            ("erase", "Delete VMs, with dependency checks for linked clones."),
        ),
    ),
    (
        "Running virtual machines",
        (
            ("connect", "Start a VM headless and attach to its console or ssh."),
            ("ssh-key", "Install the PySnap ssh key in a guest reached through ssh."),
            ("monitor", "List VMs that are running, paused or changing state."),
            ("pause", "Pause running VMs; they stay in memory."),
            ("resume", "Resume paused VMs."),
            ("stop", "Shut VMs down through the ACPI power button."),
        ),
    ),
    (
        "Session reports",
        (("report", "Read, check, extract and compare student session reports."),),
    ),
    (
        "Maintenance",
        (
            ("docs", "Open the bundled HTML documentation."),
            ("full-clean", "Delete the VirtualBox machine and configuration directories."),
            ("--integration-test", "Run the end-to-end check on a real VirtualBox."),
        ),
    ),
)


def _commands_epilog() -> str:
    """Return the grouped command list of the root help."""
    width = max(len(name) for _, commands in COMMAND_GROUPS for name, _ in commands)
    lines = []
    for title, commands in COMMAND_GROUPS:
        lines.append(f"{title}:")
        lines.extend(f"  {name:<{width}}  {summary}" for name, summary in commands)
        lines.append("")
    lines.append('Run "pysnap COMMAND --help" for the arguments and examples of a command.')
    return "\n".join(lines)


def _examples(*commands: str) -> str:
    """Return an examples block for a command epilog."""
    return "Examples:\n" + "\n".join(f"  {command}" for command in commands)


class CliArgumentParser(argparse.ArgumentParser):
    """Route argparse output through caller-provided streams."""

    def __init__(
        self,
        *args: object,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize the custom argument parser.

        :param stdout: Stream used for help output.
        :param stderr: Stream used for parser errors.
        """
        self.stdout = stdout
        self.stderr = stderr
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)

    def _print_message(self, message: str, file: TextIO | None = None) -> None:
        """Print parser output to redirected streams when provided.

        :param message: Message to print.
        :param file: Original argparse target stream.
        """
        if not message:
            return

        target = file
        if target is sys.stdout and self.stdout is not None:
            target = self.stdout
        elif target is sys.stderr and self.stderr is not None:
            target = self.stderr
        elif target is None:
            target = self.stderr or sys.stderr

        if target is not None:
            target.write(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        """Raise a controlled parser exit instead of terminating the process.

        :param status: Exit status code.
        :param message: Optional exit message.
        :raises ParserExit: Always raised with the requested status.
        """
        if message:
            self._print_message(message, sys.stderr if status else sys.stdout)
        raise ParserExit(status)


def build_root_parser(
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> CliArgumentParser:
    """Build the root CLI parser used for help output.

    :param stdout: Stream used for help output.
    :param stderr: Stream used for parser errors.
    :returns: Configured argument parser.
    """
    parser = CliArgumentParser(
        prog="pysnap",
        usage="pysnap COMMAND [ARGUMENTS] | pysnap COMMAND --help",
        description=(
            "Manage VirtualBox appliance imports and linked clones for network "
            "labs: import images, clone and configure virtual machines, attach "
            "to their serial consoles or through ssh and read the session reports recorded "
            "inside them."
        ),
        epilog=_commands_epilog(),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"pysnap {__version__}",
        help="show the PySnap version and exit",
    )
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    service: PySnapService | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Execute the CLI.

    :param argv: Command line arguments without the executable name.
    :param service: Optional application service.
    :param stdout: Output stream for regular messages.
    :param stderr: Output stream for errors.
    :param stdin: Input stream for interactive confirmations.
    :returns: Process exit code.
    """
    arguments = list(argv if argv is not None else sys.argv[1:])
    app_service = service or PySnapService()
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    input_stream = stdin or sys.stdin
    root_parser = build_root_parser(stdout=output, stderr=error_output)

    try:
        if not arguments:
            root_parser.print_help(file=output)
            return 0

        command = arguments[0]
        if command == "--integration-test":
            return _run_integration_test(arguments[1:], app_service, output, error_output)
        if command in {"-h", "--help", "-v", "--version"}:
            root_parser.parse_args([command])
            return 0
        if command == "list":
            return _run_list(arguments[1:], app_service, output, error_output)
        if command == "full-clean":
            return _run_full_clean(
                arguments[1:], app_service, output, error_output, input_stream
            )
        if command == "import":
            return _run_import(arguments[1:], app_service, output, error_output)
        if command == "protosettings":
            return _run_protosettings(arguments[1:], app_service, output, error_output)
        if command == "show":
            return _run_show(arguments[1:], app_service, output, error_output)
        if command == "plug":
            return _run_plug(arguments[1:], app_service, output, error_output)
        if command == "set":
            return _run_set(arguments[1:], app_service, output, error_output)
        if command == "docs":
            return _run_docs(arguments[1:], output, error_output)
        if command == "connect":
            return _run_connect(arguments[1:], app_service, output, error_output)
        if command == "monitor":
            return _run_monitor(arguments[1:], app_service, output, error_output)
        if command == "stop":
            return _run_stop(arguments[1:], app_service, output, error_output)
        if command == "ssh-key":
            return _run_ssh_key(arguments[1:], app_service, output, error_output)
        if command in {"pause", "resume"}:
            return _run_pause_resume(command, arguments[1:], app_service, output, error_output)
        if command == "clone":
            return _run_clone(arguments[1:], app_service, output, error_output)
        if command == "erase":
            return _run_erase(arguments[1:], app_service, output, error_output)
        if command == "report":
            return run_report_command(arguments[1:], app_service, output, error_output)
        root_parser.error("unknown command. Run `pysnap --help` to see available commands.")
    except ParserExit as error:
        return error.status
    except PySnapError as error:
        print(f"Error: {error}", file=error_output)
        return 1

    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point.

    :param argv: Optional command line arguments.
    :returns: Process exit code.
    """
    return run_cli(argv=argv)


def _run_list(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``list`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap list",
        description=(
            "List the VirtualBox groups and the virtual machines in each group. "
            "VMs without a group are listed under /Others."
        ),
        epilog=_examples(
            "pysnap list",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.parse_args(list(arguments))
    try:
        groups = service.list_groups()
    except CommandExecutionError:
        # ``VBoxManage list`` fails or times out when the VirtualBox data
        # directories are missing or corrupted, for example right after
        # ``pysnap full-clean``. An empty listing is the correct answer for
        # the user in that situation, so report the stub and exit cleanly.
        print(format_groups([]), file=stdout)
        return 0
    print(format_groups(groups), file=stdout)
    return 0


def _run_show(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``show`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap show",
        description=(
            "Show the name, group, serial TCP port (COM1) and, for linked "
            "clones, the parent of a virtual machine, and how PySnap reaches its "
            "guest: through the COM1 serial console or through ssh."
        ),
        epilog=_examples(
            "pysnap show first",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", metavar="VM", help="Name of the virtual machine.")
    namespace = parser.parse_args(list(arguments))
    vm_info = service.show_vm(namespace.vm)
    print(format_vm_info(vm_info, service.get_vm_access(namespace.vm)), file=stdout)
    return 0


def _run_protosettings(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``protosettings`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap protosettings",
        description=(
            "Register a base VM whose linked clones get lab settings: NIC1 "
            "stays on NAT, the internal networks of pysnap clone go to "
            "NIC2-NIC4, and the clone name, serial port and networks are "
            "written into its DMI system information. These VMs and their clones "
            "are reached through the COM1 serial console; other VMs through ssh. "
            "The list of such base VMs is kept in ~/.ptotosettings."
        ),
        epilog=_examples(
            "pysnap protosettings protocols-jeos-20251218-x86_64",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", metavar="BASE_VM", help="Name of the base virtual machine.")
    namespace = parser.parse_args(list(arguments))
    configured = service.register_proto_settings_vm(namespace.vm)
    print(f"Registered proto-settings VM: {namespace.vm}", file=stdout)
    print(
        "Configured proto-settings VMs: "
        + (", ".join(configured) if configured else "none"),
        file=stdout,
    )
    return 0


def _run_import(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``import`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap import",
        description=(
            "Import an OVA or OVF appliance into VirtualBox. The imported VMs "
            "keep the group of the appliance or go to /Others. Nothing is "
            "created when a VM with the same name already exists."
        ),
        epilog=_examples(
            "pysnap import image.ova",
            "pysnap import image.ova RenamedVM",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("image", metavar="IMAGE", help="Path to the .ova or .ovf appliance.")
    parser.add_argument(
        "vm_name",
        nargs="?",
        metavar="VM_NAME",
        help="New name of the imported VM; only for appliances with one VM.",
    )
    namespace = parser.parse_args(list(arguments))
    progress_bar = ImportProgressBar(stream=stdout)
    try:
        imported = service.import_image(
            namespace.image,
            vm_name=namespace.vm_name,
            progress_callback=progress_bar.update,
        )
    finally:
        progress_bar.finish()
    print(format_import_result(imported), file=stdout)
    return 0


def _run_plug(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``plug`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap plug",
        description=(
            "Prepare an existing VM for pysnap connect. A VM reached through the "
            "COM1 console gets COM1 (UART1) as a TCP server on a free port; a VM "
            "whose COM1 already is a TCP server is left unchanged, otherwise it "
            "must be stopped. A VM reached through ssh gets a rule on its NAT "
            "adapter that forwards a free port on 127.0.0.1 to port 22 of the "
            "guest, on a stopped, running or paused VM. Use pysnap set for other "
            "ports and adapters and to choose the access."
        ),
        epilog=_examples(
            "pysnap plug MyVM",
            "pysnap connect MyVM",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", metavar="VM", help="Name of the virtual machine.")
    namespace = parser.parse_args(list(arguments))
    vm_info = service.plug_vm(namespace.vm)
    print(format_vm_info(vm_info, service.get_vm_access(namespace.vm)), file=stdout)
    return 0


def _run_set(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``set`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap set",
        description=(
            "Show or change the serial (COM) ports and network adapters of a VM: "
            "COM1-COM4, and NIC1-NIC8 or NIC1-NIC36 depending on the chipset of the "
            "VM, and how PySnap reaches the guest. Without options the current "
            "settings are shown. Ports and adapters change only on a stopped VM; "
            "access and ssh-user can change at any time. All options are checked "
            "first and then applied together, so an invalid option changes "
            "nothing. Removing a TCP server prints a warning."
        ),
        epilog=(
            "Options (N is the port or adapter number):\n"
            "  comN=auto        TCP server on a free port (keeps an existing one)\n"
            "  comN=PORT        TCP server on the given port\n"
            "  comN=server:NAME host pipe NAME, this VM creates it\n"
            "  comN=client:NAME host pipe NAME, this VM connects to it\n"
            "  comN=off         no serial port\n"
            "  nicN=NETWORK     internal network NETWORK\n"
            "  nicN=nat         NAT\n"
            "  nicN=off         no network adapter\n"
            "  access=com       reach the guest through the COM1 console\n"
            "  access=ssh       reach the guest through ssh\n"
            "  access=auto      COM1 for protosettings VMs, ssh otherwise\n"
            "  ssh-user=NAME    guest user for ssh (default: root)\n"
            "\n"
            + _examples(
                "pysnap set first",
                "pysnap set first nic2=left nic3=right com2=server:link",
                "pysnap set second nic2=left com2=client:link",
                "pysnap set first com1=2330 nic4=off",
                "pysnap set first access=ssh ssh-user=student",
            )
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", metavar="VM", help="Name of the virtual machine.")
    parser.add_argument(
        "options", nargs="*", metavar="OPTION", help="KEY=VALUE option, see below."
    )
    namespace = parser.parse_args(list(arguments))
    result = service.set_vm_settings(namespace.vm, namespace.options)
    for warning in result.warnings:
        print(f"Warning: {warning}", file=stderr)
    print(format_vm_settings(result), file=stdout)
    return 0


def _run_clone(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``clone`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap clone",
        description=(
            "Create a linked clone of a base VM in the same group. The clone "
            "gets COM1 as a TCP server and up to three internal networks on "
            "NIC2-NIC4, while NIC1 stays on NAT. A clone reached through ssh "
            "also gets a forwarded ssh port; only base VMs set to COM1 access "
            "with pysnap set keep the old layout with networks on NIC1-NIC3. A "
            "snapshot of the base VM is taken when it has none."
        ),
        epilog=_examples(
            "pysnap clone base first",
            "pysnap clone base first -p 2330 left right",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("base_vm", metavar="BASE_VM", help="Name of the VM to clone.")
    parser.add_argument("clone_vm", metavar="CLONE_VM", help="Name of the new linked clone.")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        metavar="PORT",
        help="TCP port of COM1 (UART1); default: the next free port.",
    )
    parser.add_argument(
        "networks",
        nargs="*",
        metavar="NETWORK",
        help="Internal network names in adapter order, at most three.",
    )
    # ``pysnap clone BASE CLONE -p PORT NET...`` places an optional between
    # positional groups. Plain ``parse_args`` supports this only from the
    # argparse shipped with Python 3.13, so intermixed parsing keeps the
    # documented syntax working on every supported interpreter.
    namespace = parser.parse_intermixed_args(list(arguments))
    if len(namespace.networks) > 3:
        parser.error("at most three internal network names may be provided")
    vm_info = service.clone_vm(
        base_vm=namespace.base_vm,
        clone_vm=namespace.clone_vm,
        serial_port=namespace.port,
        networks=tuple(namespace.networks),
    )
    print(format_vm_info(vm_info), file=stdout)
    return 0


def _run_connect(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``connect`` command.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap connect",
        description=(
            "Start a VM in headless mode when needed and attach the PySnap "
            "terminal to its serial console (COM1) or, for VMs reached through "
            "ssh (see pysnap show), to an ssh login shell. All keys go to the "
            "guest except Ctrl-], which starts a PySnap command: q detaches "
            "without stopping the VM, Up/Down/PgUp/PgDn/Home/End scroll the local "
            "history, Ctrl-] sends Ctrl-] itself and Esc goes back; on Linux the "
            "mouse wheel scrolls the history in this mode too. Otherwise the mouse "
            "belongs to your terminal: select, copy and paste with its own keys "
            "and buttons. A paused VM can be attached through COM1: keys wait until "
            "pysnap resume. Over ssh, PySnap waits for the guest ssh server, "
            "installs its key on the first connect when the guest refuses it "
            "(see pysnap ssh-key) and ends the session when the login shell exits."
        ),
        epilog=_examples(
            "pysnap connect first",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", metavar="VM", help="Name of the virtual machine.")
    namespace = parser.parse_args(list(arguments))
    session = TerminalSession(service=service)
    return session.run(namespace.vm)


def _run_docs(
    arguments: Sequence[str],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``docs`` command.

    :param arguments: Subcommand arguments.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap docs",
        description=(
            "Open the HTML documentation bundled with PySnap in a web browser "
            "(firefox unless --browser names another one)."
        ),
        epilog=_examples(
            "pysnap docs",
            "pysnap docs --browser chromium",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument(
        "--browser",
        metavar="BROWSER",
        help="Browser executable to use; default: firefox.",
    )
    namespace = parser.parse_args(list(arguments))
    open_bundled_documentation(browser=namespace.browser)
    return 0


def _run_monitor(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``monitor`` command.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap monitor",
        description=(
            "List the virtual machines that are not stopped, with their state, "
            "port and group; the port is the COM1 TCP port or ssh:PORT for VMs "
            "reached through ssh. States: Working (a pysnap connect "
            "session is attached), Active (running without a session), Paused, "
            "Stopping, Changing and Error."
        ),
        epilog=_examples(
            "pysnap monitor",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.parse_args(list(arguments))
    print(format_monitor_records(service.list_monitored_vms()), file=stdout)
    return 0


def _run_stop(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``stop`` command.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap stop",
        description=(
            "Shut a VM down through the ACPI power button and wait until it "
            "stops. A paused VM is resumed first, because a paused guest "
            "ignores the power button. Give either a VM name or --all."
        ),
        epilog=_examples(
            "pysnap stop first",
            "pysnap stop --all",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", nargs="?", metavar="VM", help="Name of the virtual machine to stop.")
    parser.add_argument("--all", action="store_true", help="Stop all running and paused VMs.")
    namespace = parser.parse_args(list(arguments))

    if sum(bool(value) for value in (namespace.all, namespace.vm)) != 1:
        parser.error('exactly one of "--all" or "VM" must be provided')

    if namespace.all:
        stopped = service.stop_all_runtime_vms()
        print(
            f"Stopped virtual machines: {', '.join(stopped) if stopped else 'none'}",
            file=stdout,
        )
        return 0

    service.stop_runtime_vm(namespace.vm)
    print(f'Stopped virtual machine: {namespace.vm}', file=stdout)
    return 0


def _run_ssh_key(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``ssh-key`` command.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap ssh-key",
        description=(
            "Install the PySnap ssh key in a running guest, so that ssh "
            "connections need no password. PySnap keeps its own key pair in "
            "~/.pysnap/ssh (pysnap_vm_access_ed25519) and never touches ~/.ssh.\n\n"
            "By default ssh logs in with the password of USER (the ssh user of "
            "the VM, see pysnap set ssh-user) in this terminal and adds the key "
            "for that user; then the guest asks for the root password to add it "
            "for root too, which may be skipped with Enter. Many systems refuse "
            "a root password over ssh, so log in as an ordinary user with --user.\n\n"
            "With --console the command that adds the key is typed on the VM "
            "screen instead (VirtualBox keyboard input, Latin layout), for a "
            "shell that is already logged in there.\n\n"
            "When the ssh user of the VM cannot use the key afterwards but USER "
            "can, the ssh user of the VM is switched to USER."
        ),
        epilog=_examples(
            "pysnap ssh-key first",
            "pysnap ssh-key first --user student",
            "pysnap ssh-key first --console",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", metavar="VM", help="Name of the running virtual machine.")
    parser.add_argument(
        "--user", metavar="USER", help="Guest user for the password login; default: the ssh user."
    )
    parser.add_argument(
        "--console", action="store_true", help="Type the command on the VM screen instead of ssh."
    )
    namespace = parser.parse_args(list(arguments))
    result = service.setup_ssh_key(namespace.vm, namespace.user, console=namespace.console)
    if result.already_installed:
        print(f"The PySnap key already works for {result.users[0]}.", file=stdout)
    print(f"PySnap key works for: {', '.join(result.users)}", file=stdout)
    if result.changed_user:
        print(f"ssh user of {result.vm_name} set to {result.ssh_user}.", file=stdout)
    return 0


def _run_pause_resume(
    command: str,
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``pause`` or ``resume`` command.

    :param command: ``"pause"`` or ``"resume"``.
    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    pausing = command == "pause"
    parser = CliArgumentParser(
        prog=f"pysnap {command}",
        description=(
            "Pause a running VM. It stays in memory and is shown as Paused in "
            "pysnap monitor until pysnap resume; nothing is written to disk. An "
            "attached pysnap connect session stays open and holds keys back. "
            "Give either a VM name or --all."
            if pausing
            else "Resume a paused VM; an attached pysnap connect session takes "
            "keys again. Give either a VM name or --all."
        ),
        epilog=_examples(f"pysnap {command} first", f"pysnap {command} --all"),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument(
        "vm", nargs="?", metavar="VM", help=f"Name of the virtual machine to {command}."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Pause all running VMs." if pausing else "Resume all paused VMs.",
    )
    namespace = parser.parse_args(list(arguments))

    if sum(bool(value) for value in (namespace.all, namespace.vm)) != 1:
        parser.error('exactly one of "--all" or "VM" must be provided')

    label = "Paused" if pausing else "Resumed"
    if namespace.all:
        names = service.pause_all_runtime_vms() if pausing else service.resume_all_runtime_vms()
        print(f"{label} virtual machines: {', '.join(names) if names else 'none'}", file=stdout)
        return 0

    if pausing:
        service.pause_runtime_vm(namespace.vm)
    else:
        service.resume_runtime_vm(namespace.vm)
    print(f"{label} virtual machine: {namespace.vm}", file=stdout)
    return 0


def _run_erase(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the ``erase`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap erase",
        description=(
            "Unregister virtual machines and delete their files. A base VM is "
            "not deleted while linked clones outside the selection depend on "
            "it; clones are deleted before their base VMs. Give exactly one of "
            "a VM name, --all or --group."
        ),
        epilog=_examples(
            "pysnap erase first",
            "pysnap erase --group /Lab",
            "pysnap erase --clones-only --all",
        ),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("vm", nargs="?", metavar="VM", help="Name of the virtual machine to delete.")
    parser.add_argument("--all", action="store_true", help="Delete all registered VMs.")
    parser.add_argument(
        "--group", metavar="GROUP", help="Delete all VMs of a group, for example /Lab."
    )
    parser.add_argument(
        "--clones-only",
        action="store_true",
        help="Delete only linked clones made by pysnap clone and keep base VMs.",
    )
    namespace = parser.parse_args(list(arguments))

    if sum(bool(value) for value in (namespace.all, namespace.group, namespace.vm)) != 1:
        parser.error('exactly one of "--all", "--group", or "VM" must be provided')

    if namespace.all:
        deleted = service.erase_clones() if namespace.clones_only else service.erase_all()
        print(f"Erased virtual machines: {', '.join(deleted) if deleted else 'none'}", file=stdout)
        return 0
    if namespace.group:
        if namespace.clones_only:
            deleted = service.erase_clones(namespace.group)
        else:
            deleted = service.erase_group(namespace.group)
        print(f"Erased virtual machines: {', '.join(deleted) if deleted else 'none'}", file=stdout)
        return 0

    if namespace.clones_only and not service.is_clone_vm(namespace.vm):
        print(
            f'Virtual machine "{namespace.vm}" is not a clone. Verify the '
            "operation or remove the --clones-only flag.",
            file=stderr,
        )
        return 1

    service.erase_vm(namespace.vm)
    print(f'Erased virtual machine: {namespace.vm}', file=stdout)
    return 0


def _default_virtualbox_directories() -> tuple[Path, ...]:
    """Return the default VirtualBox data directories for the current OS.

    The machine folder defaults to ``~/VirtualBox VMs`` on every platform,
    while the configuration directory follows the VirtualBox conventions:
    ``~/.config/VirtualBox`` on Linux and other Unix systems,
    ``~/Library/VirtualBox`` on macOS, and ``~/.VirtualBox`` on Windows.
    An explicit ``VBOX_USER_HOME`` environment variable overrides the
    configuration directory on every platform.

    :returns: Directories removed by ``pysnap full-clean`` by default.
    """
    machine_folder = Path.home() / "VirtualBox VMs"
    vbox_user_home = os.environ.get("VBOX_USER_HOME")
    if vbox_user_home:
        config_folder = Path(vbox_user_home).expanduser()
    elif sys.platform == "win32":
        config_folder = Path.home() / ".VirtualBox"
    elif sys.platform == "darwin":
        config_folder = Path.home() / "Library" / "VirtualBox"
    else:
        config_folder = Path.home() / ".config" / "VirtualBox"
    return (machine_folder, config_folder)


def _read_confirmation(prompt: str, stdout: TextIO, stdin: TextIO) -> str:
    """Prompt for one confirmation line on the interactive input stream.

    :param prompt: Prompt text shown before reading.
    :param stdout: Output stream for the prompt.
    :param stdin: Input stream for the answer.
    :returns: Stripped answer text, empty on end of input.
    """
    stdout.write(prompt)
    stdout.flush()
    return stdin.readline().strip()


def _run_full_clean(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
    stdin: TextIO,
) -> int:
    """Run the ``full-clean`` subcommand.

    :param arguments: Subcommand arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :param stdin: Input stream for interactive confirmations.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap full-clean",
        description=(
            "Permanently delete the VirtualBox machine directory (~/VirtualBox "
            "VMs) and the VirtualBox configuration directory of this system "
            "(VBOX_USER_HOME overrides it). Two confirmations are asked first. "
            "Stop VirtualBox and all VMs before running this."
        ),
        epilog=_examples("pysnap full-clean", "pysnap full-clean --path /data/vms"),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument(
        "--path",
        action="append",
        metavar="DIRECTORY",
        help=(
            "Directory to delete instead of the default VirtualBox "
            "directories. May be repeated for several directories."
        ),
    )
    namespace = parser.parse_args(list(arguments))

    if namespace.path:
        targets = tuple(Path(raw_path).expanduser() for raw_path in namespace.path)
    else:
        targets = _default_virtualbox_directories()

    print("The following directories will be permanently deleted:", file=stdout)
    for target in targets:
        suffix = "" if target.exists() else " (missing)"
        print(f"- {target}{suffix}", file=stdout)

    first_answer = _read_confirmation(
        'Type "yes" to continue: ', stdout=stdout, stdin=stdin
    )
    if first_answer != "yes":
        print("Full clean cancelled.", file=stdout)
        return 1
    second_answer = _read_confirmation(
        'This cannot be undone. Type "delete" to confirm: ',
        stdout=stdout,
        stdin=stdin,
    )
    if second_answer != "delete":
        print("Full clean cancelled.", file=stdout)
        return 1

    removed = service.full_clean(targets)
    print(
        f"Removed directories: {', '.join(removed) if removed else 'none'}",
        file=stdout,
    )
    return 0


def _run_integration_test(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the real integration-test scenario.

    :param arguments: Command arguments following ``--integration-test``.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    parser = CliArgumentParser(
        prog="pysnap --integration-test",
        description=(
            "Run the end-to-end check on a real VirtualBox: import the appliance, "
            "create three linked clones in a triangle of internal networks, start "
            "and attach to them, check pysnap monitor and delete everything "
            "created."
        ),
        epilog=_examples("pysnap --integration-test image.ova"),
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("image", metavar="IMAGE", help="Path to the .ova or .ovf appliance.")
    namespace = parser.parse_args(list(arguments))
    result = service.run_integration_test(str(Path(namespace.image).expanduser()))
    print(format_integration_test_result(result), file=stdout)
    return 0
