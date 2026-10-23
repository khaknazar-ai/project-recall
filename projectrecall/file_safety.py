# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import os
import stat
from dataclasses import dataclass
from pathlib import Path

class PrepareCodexSecurityError(RuntimeError):
    """A context input or publication boundary could not be proven safe."""

@dataclass(frozen=True, slots=True)
class _DirectoryBinding:
    path: Path
    identity: tuple[int, int, int, int]

def _capture_root_chain(root: Path) -> tuple[_DirectoryBinding, ...]:
    return tuple((_directory_binding(path, label='project-root path component') for path in reversed((root, *root.parents))))

def _directory_binding(path: Path, *, label: str, containment_root: Path | None=None) -> _DirectoryBinding:
    try:
        metadata = path.lstat()
        if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise PrepareCodexSecurityError(f'{label} must be a regular non-reparse directory')
        absolute = path.absolute()
        resolved = path.resolve(strict=True)
        if resolved != absolute:
            raise PrepareCodexSecurityError(f'{label} changed containment')
        if containment_root is not None:
            resolved.relative_to(containment_root)
    except PrepareCodexSecurityError:
        raise
    except (OSError, ValueError) as exc:
        raise PrepareCodexSecurityError(f'{label} cannot be inspected within the controller') from exc
    return _DirectoryBinding(path=path, identity=(metadata.st_dev, metadata.st_ino, metadata.st_mode, int(getattr(metadata, 'st_file_attributes', 0))))

def _verify_directory_bindings(bindings: tuple[_DirectoryBinding, ...]) -> None:
    for binding in bindings:
        containment = None
        observed = _directory_binding(binding.path, label='captured directory', containment_root=containment)
        if observed.identity != binding.identity:
            raise PrepareCodexSecurityError('directory identity changed while the context package was prepared')

def _read_regular_bytes(path: Path, *, label: str, maximum: int) -> tuple[bytes, tuple[int, ...]]:
    try:
        before_path = path.lstat()
        _require_regular_metadata(before_path, label, maximum=maximum)
        with path.open('rb') as handle:
            before_handle = os.fstat(handle.fileno())
            raw = handle.read(maximum + 1)
            after_handle = os.fstat(handle.fileno())
        after_path = path.lstat()
    except PrepareCodexSecurityError:
        raise
    except FileNotFoundError as exc:
        raise PrepareCodexSecurityError(f'required file is missing: {label}') from exc
    except OSError as exc:
        raise PrepareCodexSecurityError(f'file cannot be read safely: {label}') from exc
    expected = _file_fingerprint(before_path)
    if _file_fingerprint(before_handle) != expected or _file_fingerprint(after_handle) != expected or _file_fingerprint(after_path) != expected or (len(raw) != before_path.st_size) or (len(raw) > maximum):
        raise PrepareCodexSecurityError(f'file changed while being read: {label}')
    return (raw, expected)

def _require_regular_metadata(metadata: os.stat_result, label: str, *, maximum: int) -> None:
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or (metadata.st_size > maximum):
        raise PrepareCodexSecurityError(f'file is not a distinct bounded non-reparse file: {label}')

def _file_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_nlink, int(getattr(value, 'st_file_attributes', 0)))

def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, 'st_file_attributes', 0) & reparse_flag)
