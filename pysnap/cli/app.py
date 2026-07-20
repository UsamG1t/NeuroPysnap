"""Command line interface for PySnap."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence, TextIO

from pysnap.cli.formatters import (
    ImportProgressBar,
    format_groups,
    format_import_result,
    format_integration_test_result,
    format_monitor_records,
    format_vm_info,
)
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
        description="Manage VirtualBox appliance imports and linked clones.",
        epilog=(
            "Commands:\n"
            "  pysnap list\n"
            "  pysnap import IMAGE.ova|IMAGE.ovf [VMName]\n"
            "  pysnap --integration-test IMAGE.ova|IMAGE.ovf\n"
            "  pysnap protosettings BASE_VM\n"
            "  pysnap show VM\n"
            "  pysnap plug VM\n"
            "  pysnap docs [--browser BROWSER]\n"
            "  pysnap connect VM\n"
            "  pysnap monitor\n"
            "  pysnap stop [VM | --all]\n"
            "  pysnap clone BASE_VM CLONE_VM [-p PORT] [INTNET1 [INTNET2 [INTNET3]]]\n"
            "  pysnap erase [--clones-only] [--all | --group GROUP | VM]\n"
            "  pysnap full-clean [--path DIRECTORY ...]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        stdout=stdout,
        stderr=stderr,
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
        if command in {"-h", "--help"}:
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
        if command == "docs":
            return _run_docs(arguments[1:], output, error_output)
        if command == "connect":
            return _run_connect(arguments[1:], app_service, output, error_output)
        if command == "monitor":
            return _run_monitor(arguments[1:], app_service, output, error_output)
        if command == "stop":
            return _run_stop(arguments[1:], app_service, output, error_output)
        if command == "clone":
            return _run_clone(arguments[1:], app_service, output, error_output)
        if command == "erase":
            return _run_erase(arguments[1:], app_service, output, error_output)
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
    parser = CliArgumentParser(prog="pysnap list", stdout=stdout, stderr=stderr)
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
    parser = CliArgumentParser(prog="pysnap show", stdout=stdout, stderr=stderr)
    parser.add_argument("vm", help="Virtual machine name.")
    namespace = parser.parse_args(list(arguments))
    print(format_vm_info(service.show_vm(namespace.vm)), file=stdout)
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
    parser = CliArgumentParser(prog="pysnap protosettings", stdout=stdout, stderr=stderr)
    parser.add_argument("vm", help="Base virtual machine name.")
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
    parser = CliArgumentParser(prog="pysnap import", stdout=stdout, stderr=stderr)
    parser.add_argument("image", help="Path to the OVA or OVF appliance.")
    parser.add_argument(
        "vm_name",
        nargs="?",
        help="Optional name for the imported VM when the appliance contains one VM.",
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
    parser = CliArgumentParser(prog="pysnap plug", stdout=stdout, stderr=stderr)
    parser.add_argument("vm", help="Virtual machine name.")
    namespace = parser.parse_args(list(arguments))
    vm_info = service.plug_vm(namespace.vm)
    print(format_vm_info(vm_info), file=stdout)
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
    parser = CliArgumentParser(prog="pysnap clone", stdout=stdout, stderr=stderr)
    parser.add_argument("base_vm", help="Base virtual machine name.")
    parser.add_argument("clone_vm", help="Linked clone name.")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        help="TCP port assigned to UART1. If omitted, the next free port is used.",
    )
    parser.add_argument(
        "networks",
        nargs="*",
        help="Up to three internal network names.",
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
    parser = CliArgumentParser(prog="pysnap connect", stdout=stdout, stderr=stderr)
    parser.add_argument("vm", help="Virtual machine name.")
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
    parser = CliArgumentParser(prog="pysnap docs", stdout=stdout, stderr=stderr)
    parser.add_argument(
        "--browser",
        help="Browser executable used to open the bundled documentation.",
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
    parser = CliArgumentParser(prog="pysnap monitor", stdout=stdout, stderr=stderr)
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
    parser = CliArgumentParser(prog="pysnap stop", stdout=stdout, stderr=stderr)
    parser.add_argument("vm", nargs="?", help="Virtual machine name to stop.")
    parser.add_argument("--all", action="store_true", help="Stop all running VMs.")
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
    parser = CliArgumentParser(prog="pysnap erase", stdout=stdout, stderr=stderr)
    parser.add_argument("vm", nargs="?", help="Virtual machine name to remove.")
    parser.add_argument("--all", action="store_true", help="Remove all registered VMs.")
    parser.add_argument("--group", help="Remove all VMs from the specified group.")
    parser.add_argument(
        "--clones-only",
        action="store_true",
        help="Restrict the operation to VMs created as PySnap linked clones.",
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
            "Permanently delete the VirtualBox machine and configuration "
            "directories. Stop VirtualBox and all VMs before running this."
        ),
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
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("image", help="Path to the OVA or OVF appliance.")
    namespace = parser.parse_args(list(arguments))
    result = service.run_integration_test(str(Path(namespace.image).expanduser()))
    print(format_integration_test_result(result), file=stdout)
    return 0
