# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
from pathlib import Path
from pathlib import PureWindowsPath
LEGACY_CACHE_ROOT = PureWindowsPath('D:/projects/.cache')
CURRENT_CACHE_ROOT = PureWindowsPath('C:/AI/cache/questmemory')
SHARED_UV_CACHE_ROOT = PureWindowsPath('C:/AI/cache/uv')
SHARED_PIP_CACHE_ROOT = PureWindowsPath('C:/AI/cache/pip')
CURRENT_PROJECT_ROOT = PureWindowsPath('C:/AI/projects')
RECOVERY_TEMP_ROOT = PureWindowsPath('C:/AI/temp/questmemory-recovery')
CURRENT_ENVIRONMENT_ROOT = PureWindowsPath('C:/AI/environments')

def _plain_absolute_windows_path(value: str | Path | PureWindowsPath) -> PureWindowsPath | None:
    raw = str(value)
    if not raw or raw.startswith(('\\\\', '//')) or any((ord(c) < 32 or ord(c) == 127 for c in raw)):
        return None
    path = PureWindowsPath(raw)
    if not path.is_absolute() or '..' in path.parts:
        return None
    for part in path.parts[1:]:
        if ':' in part or part.endswith(('.', ' ')) or any((c in part for c in '<>"|?*')) or PureWindowsPath(part).is_reserved():
            return None
    return path

def is_approved_windows_storage_path(value: str | Path | PureWindowsPath) -> bool:
    """Admit only legacy local D: or exact C: storage namespaces lexically."""
    path = _plain_absolute_windows_path(value)
    if path is None:
        return False
    if path.drive.upper() == 'D:':
        return True
    return path.drive.upper() == 'C:' and any((path == root or path.is_relative_to(root) for root in (CURRENT_PROJECT_ROOT, CURRENT_CACHE_ROOT, SHARED_UV_CACHE_ROOT, SHARED_PIP_CACHE_ROOT, RECOVERY_TEMP_ROOT)))

def is_approved_windows_runtime_executable(value: str | Path | PureWindowsPath) -> bool:
    """Allow a central environment or an owned legacy/project test runtime."""
    path = _plain_absolute_windows_path(value)
    if path is None:
        return False
    return path.drive.upper() == 'D:' or any((path.is_relative_to(root) for root in (CURRENT_PROJECT_ROOT, CURRENT_ENVIRONMENT_ROOT, RECOVERY_TEMP_ROOT)))

def is_approved_windows_project_path(value: str | Path | PureWindowsPath) -> bool:
    """Admit roots only inside the existing host policy's project namespaces.

    Lexical admission grants no filesystem, ownership or execution authority.
    Callers retain their native reparse, identity and permission checks.
    """
    path = _plain_absolute_windows_path(value)
    if path is None or not is_approved_windows_storage_path(path):
        return False
    if path.drive.upper() == 'D:':
        return True
    return any((path != root and path.is_relative_to(root) for root in (CURRENT_PROJECT_ROOT, RECOVERY_TEMP_ROOT)))

def cache_root_for_project(project_root: str | Path | None=None) -> Path:
    """Select the active host cache without following caller-supplied paths."""
    legacy = project_root is not None and PureWindowsPath(str(project_root)).drive.upper() == 'D:'
    return Path(str(LEGACY_CACHE_ROOT if legacy else CURRENT_CACHE_ROOT))
