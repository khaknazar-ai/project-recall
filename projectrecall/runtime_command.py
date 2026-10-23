# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from projectrecall.storage_policy import is_approved_windows_runtime_executable

class RuntimeCommandError(RuntimeError):
    """The active QuestMemory runtime cannot be represented safely."""

def isolated_questmemory_argv(*arguments: str) -> tuple[str, ...]:
    """Return the current installed runtime without trusting the caller cwd.

    ``-I`` prevents an external controller from shadowing ``questmemory`` or
    its dependencies through the working directory, PYTHONPATH, or user site.
    The command remains host-runtime-bound; it is not claimed to be portable to
    another machine.
    """
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise RuntimeCommandError('QuestMemory runtime executable is not absolute')
    try:
        resolved = executable.resolve(strict=True)
    except OSError as exc:
        raise RuntimeCommandError('QuestMemory runtime executable cannot be resolved') from exc
    if not resolved.is_file():
        raise RuntimeCommandError('QuestMemory runtime executable is not a file')
    if os.name == 'nt' and (not is_approved_windows_runtime_executable(resolved)):
        raise RuntimeCommandError('QuestMemory runtime executable must remain in an approved local runtime namespace')
    cleaned: list[str] = []
    for argument in arguments:
        if not isinstance(argument, str) or any((ord(character) < 32 or ord(character) == 127 for character in argument)):
            raise RuntimeCommandError('QuestMemory runtime argument is invalid')
        cleaned.append(argument)
    return (str(resolved), '-I', '-B', '-S', str(Path(__file__).resolve().parent.parent / 'project_recall.py'), *cleaned)

def powershell_argv(arguments: Iterable[str]) -> str:
    if isinstance(arguments, (str, bytes)):
        raise RuntimeCommandError('PowerShell command arguments must be a sequence')
    values = tuple(arguments)
    if not values:
        raise RuntimeCommandError('PowerShell command cannot be empty')
    if values[0] == '':
        raise RuntimeCommandError('PowerShell command executable cannot be empty')
    if any((not isinstance(value, str) or any((ord(character) < 32 or ord(character) == 127 for character in value)) for value in values)):
        raise RuntimeCommandError('PowerShell command argument is invalid')
    return '& ' + ' '.join((_powershell_literal(value) for value in values))

def questmemory_powershell_command(*arguments: str) -> str:
    return powershell_argv(isolated_questmemory_argv(*arguments))

def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
