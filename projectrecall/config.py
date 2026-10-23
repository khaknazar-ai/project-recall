# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import os
import secrets
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from projectrecall.controller_transition_lock_v1 import ControllerTransitionLeaseV1
from projectrecall.controller_transition_lock_v1 import ControllerTransitionLockErrorV1
from projectrecall.durable_io import DURABLE_WRITE_LOCK
from projectrecall.paths import data_dir
from projectrecall.paths import default_cache_dir
from projectrecall.paths import default_npm_cache_dir
from projectrecall.paths import default_pip_cache_dir
from projectrecall.paths import default_tmp_dir
from projectrecall.paths import default_uv_cache_dir
from projectrecall.paths import ensure_allowed_storage_path
from projectrecall.paths import ensure_required_cache_path
from projectrecall.paths import questmemory_dir
from projectrecall.paths import resolve_project_root
from projectrecall.paths import sqlite_path
from projectrecall.storage_policy import SHARED_PIP_CACHE_ROOT
from projectrecall.storage_policy import SHARED_UV_CACHE_ROOT
from projectrecall.storage_policy import cache_root_for_project
from projectrecall.storage_policy import is_approved_windows_project_path
CONFIG_FILE = 'questmemory.toml'
MAX_CONFIG_BYTES = 1024 * 1024
GENERIC_CONTROLLER_KIND = 'generic'
FUTURE_PROJECT_CONTROLLER_KIND = 'future_project_v1'

@dataclass(frozen=True)
class QuestMemoryConfig:
    project_root: Path
    questmemory_dir: Path
    data_dir: Path
    sqlite_path: Path
    cache_dir: Path
    pip_cache_dir: Path
    uv_cache_dir: Path
    npm_cache_dir: Path
    tmp_dir: Path
    sqlite_owner_token: str | None = None
    controller_kind: str = GENERIC_CONTROLLER_KIND
    bootstrap_id: str | None = None
    bootstrap_spec_sha256: str | None = None
    bootstrap_receipt_sha256: str | None = None
    controller_marker_sha256: str | None = None

    @classmethod
    def default(cls, project_root: str | Path | None=None) -> QuestMemoryConfig:
        root = resolve_project_root(project_root)
        config = cls(project_root=root, questmemory_dir=questmemory_dir(root), data_dir=data_dir(root), sqlite_path=sqlite_path(root), cache_dir=default_cache_dir(root), pip_cache_dir=default_pip_cache_dir(root), uv_cache_dir=default_uv_cache_dir(root), npm_cache_dir=default_npm_cache_dir(root), tmp_dir=default_tmp_dir(root))
        config.validate_paths()
        return config

    def configured_paths(self) -> tuple[Path, ...]:
        return (self.project_root, self.questmemory_dir, self.data_dir, self.sqlite_path, self.cache_dir, self.pip_cache_dir, self.uv_cache_dir, self.npm_cache_dir, self.tmp_dir)

    def cache_paths(self) -> tuple[Path, ...]:
        return (self.cache_dir, self.pip_cache_dir, self.uv_cache_dir, self.npm_cache_dir, self.tmp_dir)

    def validate_paths(self) -> None:
        if self.sqlite_owner_token is not None and (len(self.sqlite_owner_token) != 64 or any((character not in '0123456789abcdef' for character in self.sqlite_owner_token))):
            raise ValueError('sqlite_owner_token must be a lowercase 256-bit token')
        if self.controller_kind not in {GENERIC_CONTROLLER_KIND, FUTURE_PROJECT_CONTROLLER_KIND}:
            raise ValueError('controller_kind is unsupported')
        bootstrap_values = (self.bootstrap_id, self.bootstrap_spec_sha256, self.bootstrap_receipt_sha256, self.controller_marker_sha256)
        if self.controller_kind == GENERIC_CONTROLLER_KIND:
            if any((value is not None for value in bootstrap_values)):
                raise ValueError('generic controller configuration cannot carry bootstrap identity')
        else:
            if not isinstance(self.bootstrap_id, str) or not self.bootstrap_id.startswith('future-project-bootstrap-v1-'):
                raise ValueError('future_project_v1 controller requires an exact bootstrap_id')
            _validate_sha256(self.bootstrap_spec_sha256, 'bootstrap_spec_sha256')
            _validate_sha256(self.bootstrap_receipt_sha256, 'bootstrap_receipt_sha256')
            if self.controller_marker_sha256 is not None:
                _validate_sha256(self.controller_marker_sha256, 'controller_marker_sha256')
        lexical_root = _absolute_lexical(self.project_root)
        if not is_approved_windows_project_path(lexical_root):
            raise ValueError('project_root must remain in approved local storage')
        lexical_memory = _absolute_lexical(self.questmemory_dir)
        lexical_data = _absolute_lexical(self.data_dir)
        lexical_sqlite = _absolute_lexical(self.sqlite_path)
        expected_memory_lexical = lexical_root / '.questmemory'
        expected_data_lexical = lexical_root / 'data'
        disposable_root_lexical = lexical_root / '.tmp'
        if lexical_memory != expected_memory_lexical:
            raise ValueError("questmemory_dir must be the controller's .questmemory directory")
        if lexical_data != expected_data_lexical:
            raise ValueError("data_dir must be the controller's data directory")
        if not (lexical_sqlite == expected_data_lexical / 'questmemory.sqlite' or lexical_sqlite.is_relative_to(expected_data_lexical) or lexical_sqlite.is_relative_to(disposable_root_lexical)):
            raise ValueError('sqlite_path must remain under controller data/ or explicit .tmp/')
        lexical_cache_root = _absolute_lexical(cache_root_for_project(lexical_root))
        lexical_uv_root = lexical_cache_root if lexical_root.drive.upper() == 'D:' else _absolute_lexical(Path(str(SHARED_UV_CACHE_ROOT)))
        lexical_pip_root = lexical_cache_root if lexical_root.drive.upper() == 'D:' else _absolute_lexical(Path(str(SHARED_PIP_CACHE_ROOT)))
        for path in self.configured_paths():
            lexical = _absolute_lexical(path)
            if not (lexical == lexical_root or lexical.is_relative_to(lexical_root) or lexical == lexical_cache_root or lexical.is_relative_to(lexical_cache_root) or (lexical == lexical_uv_root) or lexical.is_relative_to(lexical_uv_root) or (lexical == lexical_pip_root) or lexical.is_relative_to(lexical_pip_root)):
                raise ValueError(f'Path is outside allowed QuestMemory storage roots: {lexical.as_posix()}')
        for name in ('cache_dir', 'pip_cache_dir', 'uv_cache_dir', 'npm_cache_dir', 'tmp_dir'):
            path = getattr(self, name)
            required_cache_root = lexical_uv_root if name == 'uv_cache_dir' else lexical_pip_root if name == 'pip_cache_dir' else lexical_cache_root
            lexical = _absolute_lexical(path)
            if not (lexical == required_cache_root or lexical.is_relative_to(required_cache_root)):
                raise ValueError(f'Cache path must be under {required_cache_root.as_posix()}: {lexical.as_posix()}')
        expected_memory = questmemory_dir(self.project_root).resolve()
        expected_data = data_dir(self.project_root).resolve()
        if self.questmemory_dir.resolve() != expected_memory:
            raise ValueError("questmemory_dir must be the controller's .questmemory directory")
        if self.data_dir.resolve() != expected_data:
            raise ValueError("data_dir must be the controller's data directory")
        resolved_sqlite = self.sqlite_path.resolve()
        disposable_root = (self.project_root / '.tmp').resolve()
        if not (resolved_sqlite == expected_data / 'questmemory.sqlite' or resolved_sqlite.is_relative_to(expected_data) or resolved_sqlite.is_relative_to(disposable_root)):
            raise ValueError('sqlite_path must remain under controller data/ or explicit .tmp/')
        for path in self.configured_paths():
            ensure_allowed_storage_path(path, self.project_root)
        for path in self.cache_paths():
            ensure_required_cache_path(path, self.project_root, shared_uv=path == self.uv_cache_dir, shared_pip=path == self.pip_cache_dir)

    def to_toml(self) -> str:
        lines = ['[paths]', f'project_root = "{_portable_path(self.project_root, self.project_root)}"', f'questmemory_dir = "{_portable_path(self.questmemory_dir, self.project_root)}"', f'data_dir = "{_portable_path(self.data_dir, self.project_root)}"', f'sqlite_path = "{_portable_path(self.sqlite_path, self.project_root)}"', f'cache_dir = "{self.cache_dir.as_posix()}"', '', '[cache]', f'pip_cache_dir = "{self.pip_cache_dir.as_posix()}"', f'uv_cache_dir = "{self.uv_cache_dir.as_posix()}"', f'npm_cache_dir = "{self.npm_cache_dir.as_posix()}"', f'tmp_dir = "{self.tmp_dir.as_posix()}"', '']
        if self.sqlite_owner_token is not None:
            lines.extend(['[identity]', f'sqlite_owner_token = "{self.sqlite_owner_token}"', ''])
        if self.controller_kind == FUTURE_PROJECT_CONTROLLER_KIND:
            lines.extend(['[controller]', f'kind = "{self.controller_kind}"', f'bootstrap_id = "{self.bootstrap_id}"', f'bootstrap_spec_sha256 = "{self.bootstrap_spec_sha256}"', f'bootstrap_receipt_sha256 = "{self.bootstrap_receipt_sha256}"', *([f'controller_marker_sha256 = "{self.controller_marker_sha256}"'] if self.controller_marker_sha256 is not None else []), ''])
        return '\n'.join(lines)

def config_path(project_root: str | Path | None=None) -> Path:
    return resolve_project_root(project_root) / CONFIG_FILE

def load_config(project_root: str | Path | None=None, *, validate: bool=True) -> QuestMemoryConfig:
    root = resolve_project_root(project_root)
    raw_bytes = read_config_bytes_exact(root)
    if raw_bytes is None:
        return QuestMemoryConfig.default(root)
    try:
        raw_text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('questmemory.toml must be valid UTF-8') from exc
    raw = tomllib.loads(raw_text)
    paths = raw.get('paths', {})
    cache = raw.get('cache', {})
    identity = raw.get('identity', {})
    controller = raw.get('controller', {})
    if not all((isinstance(value, Mapping) for value in (paths, cache, identity, controller))):
        raise ValueError('questmemory.toml [paths], [cache], [identity], and [controller] must be tables')
    owner_token = identity.get('sqlite_owner_token')
    if owner_token is not None and (not isinstance(owner_token, str)):
        raise ValueError('questmemory.toml sqlite_owner_token must be a string')
    controller_kind = controller.get('kind', GENERIC_CONTROLLER_KIND)
    bootstrap_id = controller.get('bootstrap_id')
    bootstrap_spec_sha256 = controller.get('bootstrap_spec_sha256')
    bootstrap_receipt_sha256 = controller.get('bootstrap_receipt_sha256')
    controller_marker_sha256 = controller.get('controller_marker_sha256')
    if not isinstance(controller_kind, str):
        raise ValueError('questmemory.toml controller kind must be a string')
    if any((value is not None and (not isinstance(value, str)) for value in (bootstrap_id, bootstrap_spec_sha256, bootstrap_receipt_sha256, controller_marker_sha256))):
        raise ValueError('questmemory.toml bootstrap identity values must be strings')
    configured_root = _configured_path(paths.get('project_root', '.'), root)
    if configured_root != root:
        raise ValueError(f'questmemory.toml is bound to a different project_root: configured={configured_root.as_posix()} requested={root.as_posix()}')
    config = QuestMemoryConfig(project_root=configured_root, questmemory_dir=_configured_path(paths.get('questmemory_dir', '.questmemory'), root), data_dir=_configured_path(paths.get('data_dir', 'data'), root), sqlite_path=_configured_path(paths.get('sqlite_path', 'data/questmemory.sqlite'), root), cache_dir=_configured_path(paths.get('cache_dir', default_cache_dir(root)), root), pip_cache_dir=_configured_path(cache.get('pip_cache_dir', default_pip_cache_dir(root)), root), uv_cache_dir=_configured_path(cache.get('uv_cache_dir', default_uv_cache_dir(root)), root), npm_cache_dir=_configured_path(cache.get('npm_cache_dir', default_npm_cache_dir(root)), root), tmp_dir=_configured_path(cache.get('tmp_dir', default_tmp_dir(root)), root), sqlite_owner_token=owner_token, controller_kind=controller_kind, bootstrap_id=bootstrap_id, bootstrap_spec_sha256=bootstrap_spec_sha256, bootstrap_receipt_sha256=bootstrap_receipt_sha256, controller_marker_sha256=controller_marker_sha256)
    if validate:
        config.validate_paths()
    return config

def _ensure_config_under_transition_lock(project_root: str | Path, *, transition_lease: ControllerTransitionLeaseV1, controller_kind: str | None=None, bootstrap_id: str | None=None, bootstrap_spec_sha256: str | None=None, bootstrap_receipt_sha256: str | None=None, controller_marker_sha256: str | None=None) -> tuple[QuestMemoryConfig, bool]:
    """Ensure config while the caller owns the root transition lease."""
    config = QuestMemoryConfig.default(project_root)
    _require_transition_lease_for_config(config.project_root, transition_lease)
    path = config_path(config.project_root)
    if read_config_bytes_exact(config.project_root) is not None:
        loaded = load_config(config.project_root)
        if controller_kind is not None:
            _require_controller_binding(loaded, controller_kind=controller_kind, bootstrap_id=bootstrap_id, bootstrap_spec_sha256=bootstrap_spec_sha256, bootstrap_receipt_sha256=bootstrap_receipt_sha256, controller_marker_sha256=controller_marker_sha256)
        transition_lease.verify()
        return (loaded, False)
    requested_kind = controller_kind or GENERIC_CONTROLLER_KIND
    config = replace(config, sqlite_owner_token=secrets.token_hex(32), controller_kind=requested_kind, bootstrap_id=bootstrap_id, bootstrap_spec_sha256=bootstrap_spec_sha256, bootstrap_receipt_sha256=bootstrap_receipt_sha256, controller_marker_sha256=controller_marker_sha256)
    config.validate_paths()
    encoded = config.to_toml().encode('utf-8')
    temporary: Path | None = None
    try:
        transition_lease.verify()
        with DURABLE_WRITE_LOCK:
            transition_lease.verify()
            with tempfile.NamedTemporaryFile(mode='wb', dir=config.project_root, prefix='.questmemory-config-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                transition_lease.verify()
                os.link(temporary, path)
                created = True
            except FileExistsError:
                created = False
            loaded = load_config(config.project_root)
            if created and read_config_bytes_exact(config.project_root) != encoded:
                raise ValueError('questmemory.toml publication bytes changed')
            if controller_kind is not None:
                _require_controller_binding(loaded, controller_kind=controller_kind, bootstrap_id=bootstrap_id, bootstrap_spec_sha256=bootstrap_spec_sha256, bootstrap_receipt_sha256=bootstrap_receipt_sha256, controller_marker_sha256=controller_marker_sha256)
            transition_lease.verify()
            return (loaded, created)
    except OSError as exc:
        raise ValueError('questmemory.toml could not be published atomically') from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

def _require_transition_lease_for_config(project_root: Path, transition_lease: ControllerTransitionLeaseV1) -> None:
    transition_lease.verify()
    if transition_lease.controller_root != project_root:
        raise ControllerTransitionLockErrorV1('config transition lease is bound to a different controller root')

def read_config_bytes_exact(project_root: str | Path | None=None) -> bytes | None:
    """Read a bounded regular config file and reject mutation/reparse races."""
    path = config_path(project_root)
    try:
        before_path = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError('questmemory.toml metadata cannot be read') from exc
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)
    if not stat.S_ISREG(before_path.st_mode) or path.is_symlink() or bool(getattr(before_path, 'st_file_attributes', 0) & reparse_flag):
        raise ValueError('questmemory.toml must be a regular non-reparse file')
    if before_path.st_size > MAX_CONFIG_BYTES:
        raise ValueError('questmemory.toml exceeds its bounded byte limit')
    content = bytearray()
    try:
        with path.open('rb') as handle:
            before_handle = os.fstat(handle.fileno())
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_CONFIG_BYTES:
                    raise ValueError('questmemory.toml exceeds its bounded byte limit')
            after_handle = os.fstat(handle.fileno())
        after_path = path.stat(follow_symlinks=False)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError('questmemory.toml cannot be read completely') from exc
    fingerprint = _stat_fingerprint(before_handle)
    if _stat_fingerprint(before_path) != fingerprint or _stat_fingerprint(after_handle) != fingerprint or _stat_fingerprint(after_path) != fingerprint or (len(content) != fingerprint[2]):
        raise ValueError('questmemory.toml changed while it was being read')
    return bytes(content)

def _configured_path(value: object, root: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError('questmemory.toml path values must be strings')
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return _absolute_lexical(candidate)

def _absolute_lexical(path: Path) -> Path:
    """Normalize a path without touching the filesystem."""
    return Path(os.path.abspath(os.fspath(path)))

def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any((character not in '0123456789abcdef' for character in value)):
        raise ValueError(f'{label} must be an exact lowercase SHA-256')
    return value

def _require_controller_binding(config: QuestMemoryConfig, *, controller_kind: str, bootstrap_id: str | None, bootstrap_spec_sha256: str | None, bootstrap_receipt_sha256: str | None, controller_marker_sha256: str | None) -> None:
    expected = (controller_kind, bootstrap_id, bootstrap_spec_sha256, bootstrap_receipt_sha256, controller_marker_sha256)
    observed = (config.controller_kind, config.bootstrap_id, config.bootstrap_spec_sha256, config.bootstrap_receipt_sha256, config.controller_marker_sha256)
    if observed != expected:
        raise ValueError('questmemory.toml is bound to a different controller/bootstrap identity')

def _portable_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    project = root.resolve()
    if resolved == project:
        return '.'
    try:
        return resolved.relative_to(project).as_posix()
    except ValueError:
        return resolved.as_posix()
