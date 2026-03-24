"""Custom exceptions used by PySnap."""


class PySnapError(Exception):
    """Base exception for application-level failures."""


class CommandExecutionError(PySnapError):
    """Represent a failed ``VBoxManage`` command execution."""

    def __init__(self, command: list[str], stdout: str, stderr: str) -> None:
        """Initialize the command execution error.

        :param command: Executed command line.
        :param stdout: Captured standard output.
        :param stderr: Captured standard error.
        """
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        message = stderr.strip() or stdout.strip() or "VBoxManage command failed."
        super().__init__(message)


class VMNotFoundError(PySnapError):
    """Report that a requested virtual machine does not exist."""

    def __init__(self, vm_name: str) -> None:
        """Initialize the missing VM error.

        :param vm_name: Name of the missing virtual machine.
        """
        super().__init__(f'Virtual machine "{vm_name}" was not found.')


class VMDependencyError(PySnapError):
    """Report that a VM cannot be deleted because dependent clones exist."""

    def __init__(self, subject: str, dependents: list[str]) -> None:
        """Initialize the dependency error.

        :param subject: Name of the VM that cannot be deleted.
        :param dependents: Names of dependent clone VMs.
        """
        dependent_list = ", ".join(sorted(dependents))
        super().__init__(
            f'Cannot erase "{subject}" because dependent clones exist: '
            f"{dependent_list}."
        )

