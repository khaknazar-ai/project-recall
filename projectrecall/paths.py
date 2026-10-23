# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import os
import stat
from pathlib import Path
from projectrecall.storage_policy import SHARED_PIP_CACHE_ROOT
from projectrecall.storage_policy import SHARED_UV_CACHE_ROOT
from projectrecall.storage_policy import cache_root_for_project
from projectrecall.storage_policy import is_approved_windows_project_path
from projectrecall.storage_policy import is_approved_windows_storage_path
PROJECT_ROOT_ENV = 'QUESTMEMORY_PROJECT_ROOT'

def resolve_project_root(project_root: str | Path | None=None) -> Path:
    """Resolve the project root without creating any files."""
    if project_root is not None:
        candidate = Path(project_root).expanduser()
        return _resolve_safe_project_root(candidate)
    env_root = os.environ.get(PROJECT_ROOT_ENV)
    if env_root:
        return _resolve_safe_project_root(Path(env_root).expanduser())
    return _resolve_safe_project_root(Path.cwd())

def _resolve_safe_project_root(candidate: Path) -> Path:
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if os.name == 'nt' and (not is_approved_windows_project_path(candidate) or str(candidate).startswith(('\\\\', '//')) or '..' in candidate.parts):
        raise ValueError('QuestMemory project_root must be an approved absolute local storage path')
    if os.name == 'nt':
        current = Path(candidate.anchor)
        reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)
        for part in candidate.parts[1:]:
            current = current / part
            try:
                observed = os.lstat(current)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ValueError('QuestMemory project_root cannot be inspected safely') from exc
            if current.is_symlink() or bool(getattr(observed, 'st_file_attributes', 0) & reparse_flag):
                raise ValueError('QuestMemory project_root contains a symlink or reparse point')
    resolved = candidate.resolve()
    if os.name == 'nt' and (not is_approved_windows_project_path(resolved)):
        raise ValueError('QuestMemory project_root resolved outside approved local storage')
    return resolved

def default_cache_root(project_root: Path | None=None) -> Path:
    root = cache_root_for_project(project_root)
    _require_plain_storage_ancestry(root)
    return root.resolve()

def default_cache_dir(project_root: Path | None=None) -> Path:
    return default_cache_root(project_root)

def default_pip_cache_dir(project_root: Path | None=None) -> Path:
    if project_root is not None and project_root.drive.upper() == 'D:':
        return default_cache_root(project_root) / 'pip'
    root = Path(str(SHARED_PIP_CACHE_ROOT))
    _require_plain_storage_ancestry(root)
    return root.resolve()

def default_uv_cache_dir(project_root: Path | None=None) -> Path:
    if project_root is not None and project_root.drive.upper() == 'D:':
        return default_cache_root(project_root) / 'uv'
    root = Path(str(SHARED_UV_CACHE_ROOT))
    _require_plain_storage_ancestry(root)
    return root.resolve()

def default_npm_cache_dir(project_root: Path | None=None) -> Path:
    return default_cache_root(project_root) / 'npm'

def default_tmp_dir(project_root: Path | None=None) -> Path:
    return default_cache_root(project_root) / 'tmp'

def questmemory_dir(project_root: Path) -> Path:
    return project_root / '.questmemory'

def data_dir(project_root: Path) -> Path:
    return project_root / 'data'

def sqlite_path(project_root: Path) -> Path:
    return data_dir(project_root) / 'questmemory.sqlite'

def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True

def ensure_allowed_storage_path(path: Path, project_root: Path) -> None:
    """Keep generated paths under the exact controller or its approved cache."""
    _require_plain_storage_ancestry(path)
    resolved = path.resolve()
    allowed_roots = [project_root.resolve(), default_cache_root(project_root), default_uv_cache_dir(project_root), default_pip_cache_dir(project_root)]
    if any((is_relative_to(resolved, root) or resolved == root for root in allowed_roots)):
        return
    roots = ', '.join((root.as_posix() for root in allowed_roots))
    raise ValueError(f'Path is outside allowed QuestMemory storage roots: {resolved.as_posix()} (allowed: {roots})')

def ensure_required_cache_path(path: Path, project_root: Path | None=None, *, shared_uv: bool=False, shared_pip: bool=False) -> None:
    _require_plain_storage_ancestry(path)
    if project_root is not None and project_root.drive.upper() == 'D:':
        required_root = default_cache_root(project_root)
    else:
        required_root = default_uv_cache_dir(project_root) if shared_uv else default_pip_cache_dir(project_root) if shared_pip else default_cache_root(project_root)
    resolved = path.resolve()
    if resolved == required_root or is_relative_to(resolved, required_root):
        return
    raise ValueError(f'Cache path must be under {required_root.as_posix()}: {resolved.as_posix()}')

def _require_plain_storage_ancestry(path: Path) -> None:
    if os.name != 'nt':
        return
    if not is_approved_windows_storage_path(path):
        raise ValueError('Path is outside approved local storage')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError('Storage path cannot be inspected safely') from exc
        if stat.S_ISLNK(observed.st_mode) or bool(getattr(observed, 'st_file_attributes', 0) & 1024):
            raise ValueError('Storage path contains a symlink or reparse point')
