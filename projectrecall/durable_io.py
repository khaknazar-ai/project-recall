# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
DURABLE_WRITE_LOCK = threading.RLock()

def _native_file_path(path: Path) -> Path:
    """Use extended Windows I/O syntax for safe absolute logical paths only.

    Do not resolve links, reinterpret caller aliases or change relative-path
    semantics. This is portability, not permission or containment admission.
    """
    if os.name != 'nt' or not path.is_absolute() or str(path).startswith(('\\\\?\\', '\\\\.\\')):
        return path
    if any((part == '..' or part.endswith(('.', ' ')) or ':' in part or Path(part).is_reserved() for part in path.parts[1:])):
        return path
    value = str(path)
    return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)

def atomic_write_text(path: Path, content: str) -> None:
    """Flush and atomically replace one UTF-8 text file.

    The process-wide re-entrant lock also lets callers group several related
    replacements under the same boundary without deadlocking when this helper
    reacquires it.
    """
    with DURABLE_WRITE_LOCK:
        path = _native_file_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

def atomic_write_texts(updates: Mapping[Path, str]) -> None:
    """Replace a bounded set of related UTF-8 files with rollback on failure.

    Each destination replacement is atomic.  The shared lock prevents
    in-process writers from observing the read/modify/write sequence halfway
    through, while rollback restores every already-replaced destination when
    a later replacement fails.  Callers should keep the set small and derive
    all content before entering this boundary.
    """
    normalized = tuple(((Path(path), content) for path, content in updates.items()))
    if not normalized:
        return
    if len(normalized) > 64:
        raise ValueError('A durable multi-file write is limited to 64 files.')
    if len({path.resolve() for path, _content in normalized}) != len(normalized):
        raise ValueError('A durable multi-file write contains duplicate destinations.')
    if any((not isinstance(content, str) for _path, content in normalized)):
        raise TypeError('Durable text file content must be a string.')
    with DURABLE_WRITE_LOCK:
        originals: dict[Path, tuple[bool, str]] = {}
        for path, _content in normalized:
            native = _native_file_path(path)
            originals[path] = (native.exists(), native.read_bytes().decode('utf-8') if native.exists() else '')
        replaced: list[Path] = []
        try:
            for path, content in normalized:
                replaced.append(path)
                atomic_write_text(path, content)
        except BaseException as write_error:
            rollback_errors: list[str] = []
            for path in reversed(replaced):
                existed, original = originals[path]
                try:
                    if existed:
                        atomic_write_text(path, original)
                    elif _native_file_path(path).exists():
                        _native_file_path(path).unlink()
                except BaseException as rollback_error:
                    rollback_errors.append(f'{path.name}:{rollback_error.__class__.__name__}')
            if rollback_errors:
                raise OSError('Durable multi-file rollback failed for ' + ', '.join(rollback_errors)) from write_error
            raise
