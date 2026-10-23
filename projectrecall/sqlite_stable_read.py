# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import ctypes
import hashlib
import os
import secrets
import sqlite3
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from typing import Self
from urllib.parse import quote
SQLiteReadErrorFactory = Callable[[str], Exception]
SQLiteWriteExpectation = Literal['new', 'existing']
MAX_STABLE_SQLITE_BYTES = 512 * 1024 * 1024
_SIDECAR_SUFFIXES = ('-wal', '-journal', '-shm')
_REPARSE_FLAG = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)
_WRITE_MUTEXES: dict[str, threading.Lock] = {}
_WRITE_MUTEXES_GUARD = threading.Lock()
_SQLITE_OWNER_TABLE = 'questmemory_sqlite_owner_v1'
_SQLITE_OWNER_SCHEMA = 'CREATE TABLE questmemory_sqlite_owner_v1 (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), schema_version INTEGER NOT NULL CHECK(schema_version = 1), owner_key TEXT NOT NULL CHECK(length(owner_key) > 0)) WITHOUT ROWID'

@dataclass(frozen=True, slots=True)
class _DatabaseFingerprint:
    metadata: tuple[int, ...]
    sha256: str

@dataclass(frozen=True, slots=True)
class _StableImageGuard:
    database_path: Path
    database_fingerprint: _DatabaseFingerprint
    parent_fingerprint: tuple[int, ...]
    error_factory: SQLiteReadErrorFactory

    def verify_metadata(self, error_factory: SQLiteReadErrorFactory | None=None) -> None:
        factory = error_factory or self.error_factory
        _reject_lexical_reparse_chain(self.database_path, factory)
        _reject_sidecars(self.database_path, factory)
        current_parent = _metadata_fingerprint(self.database_path.parent, require_directory=True, error_factory=factory)
        current_database = _database_metadata_fingerprint(self.database_path, factory)
        _reject_sidecars(self.database_path, factory)
        if current_parent != self.parent_fingerprint:
            _fail(factory, 'SQLite database parent changed during stable read')
        if current_database != self.database_fingerprint.metadata:
            _fail(factory, 'SQLite database metadata or identity changed during stable read')

    def verify_full(self, error_factory: SQLiteReadErrorFactory | None=None) -> None:
        factory = error_factory or self.error_factory
        _reject_lexical_reparse_chain(self.database_path, factory)
        _reject_sidecars(self.database_path, factory)
        current_parent = _metadata_fingerprint(self.database_path.parent, require_directory=True, error_factory=factory)
        current_database = _measure_database(self.database_path, factory)
        _reject_sidecars(self.database_path, factory)
        if current_parent != self.parent_fingerprint:
            _fail(factory, 'SQLite database parent changed during stable read')
        if current_database != self.database_fingerprint:
            _fail(factory, 'SQLite database bytes or identity changed during stable read')

@dataclass(slots=True)
class _StableReadSessionState:
    guard: _StableImageGuard
    open_connections: int = 0

    def acquire(self, error_factory: SQLiteReadErrorFactory) -> None:
        self.guard.verify_metadata(error_factory)
        self.open_connections += 1

    def release(self, error_factory: SQLiteReadErrorFactory) -> None:
        if self.open_connections < 1:
            _fail(error_factory, 'SQLite stable-read session lease underflow')
        self.open_connections -= 1
        self.guard.verify_metadata(error_factory)

@dataclass(slots=True)
class _PinnedDirectory:
    path: Path
    identity: tuple[int, int, int, int]
    handle: int

@dataclass(slots=True)
class _PinnedDirectoryChain:
    entries: tuple[_PinnedDirectory, ...]
    error_factory: SQLiteReadErrorFactory
    closed: bool = False

    def verify(self) -> None:
        if self.closed:
            _fail(self.error_factory, 'SQLite write directory guard is closed')
        for entry in self.entries:
            _reject_one_directory_component(entry.path, self.error_factory)
            if _directory_identity(entry.path, self.error_factory) != entry.identity:
                _fail(self.error_factory, 'SQLite write-target directory identity changed')

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for entry in reversed(self.entries):
            _close_directory_handle(entry.handle)

@dataclass(slots=True)
class _WriteMutex:
    key: str
    thread_lock: threading.Lock
    windows_handle: int | None
    error_factory: SQLiteReadErrorFactory
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.windows_handle is not None:
            _release_windows_mutex(self.windows_handle)
        self.thread_lock.release()

@dataclass(slots=True)
class _PublishedWriteImage:
    exists: bool
    fingerprint: _DatabaseFingerprint | None

@dataclass(slots=True)
class _PinnedStage:
    path: Path
    fingerprint: _DatabaseFingerprint
    identity: tuple[int, int, int, int]
    windows_handle: int | None
    moved: bool = False
    closed: bool = False

    def verify_exact(self, error_factory: SQLiteReadErrorFactory) -> None:
        current = _database_write_identity(self.path, error_factory)
        if current != self.identity:
            _fail(error_factory, 'SQLite pinned staging identity changed')
        if self.windows_handle is not None:
            digest = _hash_windows_file_handle(self.windows_handle, error_factory)
            if digest != self.fingerprint.sha256:
                _fail(error_factory, 'SQLite pinned staging bytes changed')
        else:
            measured = _measure_database(self.path, error_factory)
            if measured.sha256 != self.fingerprint.sha256:
                _fail(error_factory, 'SQLite pinned staging image changed')

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.windows_handle is not None:
            _close_windows_handle(self.windows_handle)

    def discard(self) -> None:
        if self.closed:
            return
        if self.moved:
            self.close()
            return
        if self.windows_handle is not None:
            _delete_windows_file_by_handle(self.windows_handle)
            self.close()
            return
        try:
            measured = _measure_database(self.path, ValueError)
            if measured == self.fingerprint:
                self.path.unlink()
        except (OSError, ValueError):
            pass
        self.close()

@dataclass(slots=True)
class _PinnedPublishedImage:
    path: Path
    fingerprint: _DatabaseFingerprint
    identity: tuple[int, int, int, int]
    windows_handle: int | None
    closed: bool = False

    def verify_exact(self, error_factory: SQLiteReadErrorFactory) -> None:
        if self.closed:
            _fail(error_factory, 'SQLite published-image lease is closed')
        current = _database_write_identity(self.path, error_factory)
        if current != self.identity:
            _fail(error_factory, 'SQLite published-image identity changed')
        measured = _measure_database(self.path, error_factory)
        if measured != self.fingerprint:
            _fail(error_factory, 'SQLite published image changed')

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.windows_handle is not None:
            _close_windows_handle(self.windows_handle)

@dataclass(slots=True)
class _ValidatedWriteGuard:
    database_path: Path
    directory_chain: _PinnedDirectoryChain
    mutex: _WriteMutex
    published: _PublishedWriteImage
    published_lease: _PinnedPublishedImage | None
    owner_key: str | None
    working_sha256: str
    error_factory: SQLiteReadErrorFactory
    failed: bool = False
    closed: bool = False

    def checkpoint(self, connection: ValidatedSQLiteWriteConnection) -> None:
        if self.closed:
            _fail(self.error_factory, 'SQLite validated-write guard is closed')
        if self.failed:
            _fail(self.error_factory, 'SQLite validated-write connection is unusable after a failed publish')
        try:
            self._verify_owner_invariant(connection)
            image = _serialize_write_database(connection, self.error_factory)
            self._verify_owner_invariant(connection)
            if len(image) > MAX_STABLE_SQLITE_BYTES:
                _fail(self.error_factory, 'SQLite database exceeds the validated-write byte budget')
            self._verify_published_image()
        except BaseException:
            self.failed = True
            raise

    def publish(self, connection: ValidatedSQLiteWriteConnection) -> None:
        if self.closed:
            _fail(self.error_factory, 'SQLite validated-write guard is closed')
        if self.failed:
            _fail(self.error_factory, 'SQLite validated-write connection is unusable after a failed publish')
        try:
            self._verify_owner_invariant(connection)
            image = _serialize_write_database(connection, self.error_factory)
            self._verify_owner_invariant(connection)
            image_sha256 = hashlib.sha256(image).hexdigest()
            self._verify_published_image()
            if self.published.exists and image_sha256 == self.working_sha256:
                return
            self._publish_image(image, image_sha256)
        except BaseException:
            self.failed = True
            raise

    def _verify_owner_invariant(self, connection: ValidatedSQLiteWriteConnection) -> None:
        if self.owner_key is not None:
            _require_sqlite_owner_anchor(connection, self.owner_key, self.error_factory)

    def _verify_published_image(self) -> None:
        self.directory_chain.verify()
        _reject_sidecars(self.database_path, self.error_factory)
        exists = _path_present(self.database_path, self.error_factory)
        if not self.published.exists:
            if exists:
                _fail(self.error_factory, 'SQLite write target appeared after validated open')
            return
        if not exists or self.published.fingerprint is None:
            _fail(self.error_factory, 'SQLite write target disappeared after validated open')
        if self.published_lease is None:
            _fail(self.error_factory, 'SQLite existing write target has no published-image lease')
        _reject_lexical_reparse_chain(self.database_path, self.error_factory)
        self.published_lease.verify_exact(self.error_factory)
        if self.published_lease.fingerprint != self.published.fingerprint:
            _fail(self.error_factory, 'SQLite published-image lease does not match validated generation')
        _reject_sidecars(self.database_path, self.error_factory)

    def _publish_image(self, image: bytes, image_sha256: str) -> None:
        if len(image) > MAX_STABLE_SQLITE_BYTES:
            _fail(self.error_factory, 'SQLite database exceeds the validated-write byte budget')
        stage = _write_staged_image(self.database_path, image, image_sha256, self.error_factory)
        replacement_lease: _PinnedPublishedImage | None = None
        try:
            self._verify_published_image()
            self.directory_chain.verify()
            stage.verify_exact(self.error_factory)
            _publish_pinned_stage(stage, self.database_path, replace=self.published.exists, parent_handle=self.directory_chain.entries[-1].handle, error_factory=self.error_factory)
            self.directory_chain.verify()
            stage.verify_exact(self.error_factory)
            _reject_lexical_reparse_chain(self.database_path, self.error_factory)
            _reject_sidecars(self.database_path, self.error_factory)
            if stage.windows_handle is not None:
                _flush_windows_handle(stage.windows_handle, self.error_factory)
                stage.verify_exact(self.error_factory)
            else:
                _sync_file(self.database_path, self.error_factory)
                stage.verify_exact(self.error_factory)
            stage.close()
            replacement_lease = _pin_published_image(self.database_path, self.error_factory)
            replacement_lease.verify_exact(self.error_factory)
            if replacement_lease.fingerprint.sha256 != image_sha256:
                _fail(self.error_factory, 'SQLite published-image lease does not match staged bytes')
            old_lease = self.published_lease
            self.published_lease = replacement_lease
            replacement_lease = None
            if old_lease is not None:
                old_lease.close()
            measured = _measure_database(self.database_path, self.error_factory)
            if measured.sha256 != image_sha256:
                _fail(self.error_factory, 'SQLite published write image does not match staged bytes')
            self.directory_chain.verify()
            _reject_sidecars(self.database_path, self.error_factory)
            self.published = _PublishedWriteImage(True, measured)
            self.working_sha256 = image_sha256
        except OSError as exc:
            raise self.error_factory('SQLite validated write image could not be published') from exc
        finally:
            if replacement_lease is not None:
                replacement_lease.close()
            if not stage.closed:
                if stage.moved:
                    stage.close()
                else:
                    stage.discard()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.published_lease is not None:
                self.published_lease.close()
        finally:
            try:
                self.directory_chain.close()
            finally:
                self.mutex.close()
_ACTIVE_STABLE_READ_SESSIONS: ContextVar[dict[str, _StableReadSessionState] | None] = ContextVar('questmemory_active_stable_read_sessions', default=None)

class StableReadOnlySQLiteConnection(sqlite3.Connection):
    """A sqlite3 connection whose first close enforces the stable-image guard."""
    _close_validator: Callable[[], None] | None = None
    _close_error_factory: SQLiteReadErrorFactory | None = None
    _read_lease_release: Callable[[], None] | None = None
    _stable_physically_closed = False
    _stable_authorizer_locked = False

    def install_close_validator(self, validator: Callable[[], None], error_factory: SQLiteReadErrorFactory) -> None:
        if self._close_validator is not None:
            _fail(error_factory, 'SQLite stable-image guard was installed more than once')
        self._close_validator = validator
        self._close_error_factory = error_factory

    def lock_stable_authorizer(self) -> None:
        super().set_authorizer(_stable_read_authorizer)
        self._stable_authorizer_locked = True

    def set_authorizer(self, authorizer_callback) -> None:
        if self._stable_authorizer_locked:
            raise sqlite3.ProgrammingError('stable read authorizer cannot be replaced')
        super().set_authorizer(authorizer_callback)

    def close(self) -> None:
        validator = self._close_validator
        error_factory = self._close_error_factory
        try:
            super().close()
        except sqlite3.Error as exc:
            if error_factory is None:
                raise
            raise error_factory('SQLite stable read connection could not be closed') from exc
        self._stable_physically_closed = True
        self._close_validator = None
        self._close_error_factory = None
        release = self._read_lease_release
        self._read_lease_release = None
        try:
            if validator is not None:
                validator()
        finally:
            if release is not None:
                release()

class ValidatedSQLiteWriteConnection(sqlite3.Connection):
    """In-memory SQLite writer that publishes verified whole-file generations."""
    _write_guard: _ValidatedWriteGuard | None = None
    _validated_authorizer_locked = False

    def install_write_guard(self, guard: _ValidatedWriteGuard) -> None:
        if self._write_guard is not None:
            _fail(guard.error_factory, 'SQLite validated-write guard was installed more than once')
        self._write_guard = guard

    def lock_validated_authorizer(self) -> None:
        super().set_authorizer(_validated_write_authorizer)
        self._validated_authorizer_locked = True

    def set_authorizer(self, authorizer_callback) -> None:
        if self._validated_authorizer_locked:
            raise sqlite3.ProgrammingError('validated write authorizer cannot be replaced')
        super().set_authorizer(authorizer_callback)

    def enable_load_extension(self, enabled: bool) -> None:
        if self._validated_authorizer_locked and enabled:
            raise sqlite3.ProgrammingError('validated write extensions cannot be enabled')
        super().enable_load_extension(enabled)

    def load_extension(self, path: str, *args, **kwargs) -> None:
        if self._validated_authorizer_locked:
            raise sqlite3.ProgrammingError('validated write extensions cannot be loaded')
        super().load_extension(path, *args, **kwargs)

    def backup(self, target, *args, **kwargs) -> None:
        if self._write_guard is not None:
            raise sqlite3.ProgrammingError('validated write image cannot be backed up to another database')
        super().backup(target, *args, **kwargs)

    def deserialize(self, data, /, *, name: str='main') -> None:
        if self._write_guard is not None:
            raise sqlite3.ProgrammingError('validated write image cannot be replaced by deserialization')
        super().deserialize(data, name=name)

    def abort_without_publish(self) -> None:
        """Close a partially initialized writer without publishing any bytes."""
        guard = self._write_guard
        self._write_guard = None
        primary_error: BaseException | None = None
        try:
            super().close()
        except BaseException as exc:
            primary_error = exc
        if guard is not None:
            try:
                guard.close()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
        if primary_error is not None:
            raise primary_error

    def commit(self) -> None:
        guard = self._write_guard
        try:
            super().commit()
        except sqlite3.Error as exc:
            if guard is None:
                raise
            raise guard.error_factory('SQLite in-memory write transaction could not be committed') from exc
        if guard is not None:
            guard.checkpoint(self)

    def rollback(self) -> None:
        guard = self._write_guard
        try:
            super().rollback()
        except sqlite3.Error as exc:
            if guard is None:
                raise
            raise guard.error_factory('SQLite in-memory write transaction could not be rolled back') from exc

    def close(self) -> None:
        guard = self._write_guard
        self._write_guard = None
        if guard is None:
            super().close()
            return
        primary_error: BaseException | None = None
        try:
            if not guard.failed:
                if self.in_transaction:
                    super().rollback()
                guard.publish(self)
        except BaseException as exc:
            primary_error = exc
        try:
            super().close()
        except BaseException as exc:
            if primary_error is None:
                primary_error = guard.error_factory('SQLite in-memory write connection could not be closed')
                primary_error.__cause__ = exc
        try:
            guard.close()
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
        if primary_error is not None:
            raise primary_error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

def open_stable_read_only_sqlite(database_path: str | Path, *, error_factory: SQLiteReadErrorFactory, timeout: float=30.0) -> StableReadOnlySQLiteConnection:
    resolved = _resolve_database_path(Path(database_path), error_factory)
    lease = _read_file_lease(resolved)
    try:
        stream = lease.__enter__()
    except OSError as exc:
        raise error_factory('SQLite stable read could not acquire a read lease') from exc
    released = False
    created_connection: StableReadOnlySQLiteConnection | None = None

    def release_lease() -> None:
        nonlocal released
        if not released:
            released = True
            lease.__exit__(None, None, None)

    def attach_lease(connection: StableReadOnlySQLiteConnection) -> None:
        nonlocal created_connection
        created_connection = connection
        connection._read_lease_release = release_lease
    try:
        return _open_stable_read_only_sqlite_leased(resolved, error_factory=error_factory, timeout=timeout, attach_lease=attach_lease, read_lease=stream)
    except BaseException:
        if created_connection is None or created_connection._stable_physically_closed:
            release_lease()
        raise

def _require_read_lease_matches_guard(stream, guard: _StableImageGuard, error_factory: SQLiteReadErrorFactory) -> None:
    if os.name == 'nt' and stream is None:
        _fail(error_factory, 'SQLite guarded image requires an actual read lease')
    if stream is not None and _stat_fingerprint(_handle_stat(stream.fileno())) != guard.database_fingerprint.metadata:
        _fail(error_factory, 'SQLite read lease does not match the guarded database image')

def _open_stable_read_only_sqlite_leased(database_path: str | Path, *, error_factory: SQLiteReadErrorFactory, timeout: float=30.0, attach_lease: Callable[[StableReadOnlySQLiteConnection], None], read_lease=None) -> StableReadOnlySQLiteConnection:
    """Open a sidecar-free immutable image and verify it again on close."""
    resolved = _resolve_database_path(Path(database_path), error_factory)
    state = (_ACTIVE_STABLE_READ_SESSIONS.get() or {}).get(_session_key(resolved))
    guard = state.guard if state is not None else _build_guard(resolved, error_factory)
    _require_read_lease_matches_guard(read_lease, guard, error_factory)
    connection: StableReadOnlySQLiteConnection | None = None
    session_acquired = False
    session_release_installed = False
    try:
        if os.name == 'nt':
            uri_path = quote(_windows_extended_path(resolved), safe='')
            uri = f'file:{uri_path}?mode=ro&immutable=1&cache=private'
        else:
            uri = f'{resolved.as_uri()}?mode=ro&immutable=1&cache=private'
        connection = sqlite3.connect(uri, uri=True, timeout=timeout, factory=StableReadOnlySQLiteConnection)
        attach_lease(connection)
        if state is None:
            connection.install_close_validator(lambda: guard.verify_full(error_factory), error_factory)
        else:
            state.acquire(error_factory)
            session_acquired = True
            connection.install_close_validator(lambda: state.release(error_factory), error_factory)
            session_release_installed = True
        connection.execute('PRAGMA query_only=ON')
        if connection.execute('PRAGMA query_only').fetchone() != (1,):
            _fail(error_factory, 'SQLite query_only mode could not be enforced')
        connection.execute('PRAGMA temp_store=MEMORY')
        if connection.execute('PRAGMA temp_store').fetchone() != (2,):
            _fail(error_factory, 'SQLite memory-only temp_store could not be enforced')
        connection.lock_stable_authorizer()
        guard.verify_metadata(error_factory)
        _require_read_lease_matches_guard(read_lease, guard, error_factory)
        return connection
    except sqlite3.Error as exc:
        primary = error_factory('SQLite stable read could not be opened')
        _close_failed_stable_read_open(connection, state=state, manual_session_release=session_acquired and (not session_release_installed), error_factory=error_factory, primary_error=primary)
        raise primary from exc
    except BaseException as exc:
        _close_failed_stable_read_open(connection, state=state, manual_session_release=session_acquired and (not session_release_installed), error_factory=error_factory, primary_error=exc)
        raise

def open_owned_stable_read_only_sqlite(database_path: str | Path, *, owner_key: str, error_factory: SQLiteReadErrorFactory, timeout: float=30.0) -> StableReadOnlySQLiteConnection:
    """Open one stable image and require its exact controller owner anchor."""
    _validate_owner_key(owner_key, error_factory)
    connection = open_stable_read_only_sqlite(database_path, error_factory=error_factory, timeout=timeout)
    try:
        _require_sqlite_owner_anchor(connection, owner_key, error_factory)
        return connection
    except BaseException as exc:
        _close_failed_stable_read_open(connection, state=None, manual_session_release=False, error_factory=error_factory, primary_error=exc)
        raise

def _close_failed_stable_read_open(connection: StableReadOnlySQLiteConnection | None, *, state: _StableReadSessionState | None, manual_session_release: bool, error_factory: SQLiteReadErrorFactory, primary_error: BaseException) -> None:
    """Best-effort cleanup that never replaces the opening failure."""
    cleanup_failures: list[BaseException] = []
    if connection is not None:
        try:
            connection.close()
        except BaseException as exc:
            cleanup_failures.append(exc)
    if manual_session_release and state is not None:
        try:
            state.release(error_factory)
        except BaseException as exc:
            cleanup_failures.append(exc)
    for cleanup_failure in cleanup_failures:
        primary_error.add_note(f'SQLite stable-read cleanup also failed: {type(cleanup_failure).__name__}: {cleanup_failure}')

def open_validated_sqlite_write(database_path: str | Path, *, expected: SQLiteWriteExpectation, owner_key: str | None=None, expected_existing_sha256: str | None=None, error_factory: SQLiteReadErrorFactory, timeout: float=30.0) -> ValidatedSQLiteWriteConnection:
    """Open a copy-on-write session against one pinned SQLite path.

    ``expected`` makes create versus update authority explicit.  New
    QuestMemory-owned images require an owner key that is embedded before the
    connection is returned.  Existing images require that owner key or an
    exact caller-held SHA-256.  The canonical inode is never passed to SQLite
    for writing.  The source is copied into an in-memory connection, commits
    are validated checkpoints, and ``close()`` publishes the last committed
    generation through a same-parent atomic replace.  All QuestMemory callers
    must close the returned connection and treat a close error as failure.
    """
    _validate_write_expectation(expected, owner_key=owner_key, expected_existing_sha256=expected_existing_sha256, error_factory=error_factory)
    requested = Path(database_path)
    if not requested.is_absolute():
        _fail(error_factory, 'SQLite write target requires an absolute path')
    _validate_write_target_leaf(requested, error_factory)
    path = Path(os.path.abspath(requested))
    mutex = _acquire_write_mutex(path, timeout, error_factory)
    directory_chain: _PinnedDirectoryChain | None = None
    published_lease: _PinnedPublishedImage | None = None
    guard: _ValidatedWriteGuard | None = None
    connection: ValidatedSQLiteWriteConnection | None = None
    try:
        directory_chain = _ensure_and_pin_directory_chain(path.parent, error_factory)
        directory_chain.verify()
        candidate = path.parent / path.name
        _reject_sidecars(candidate, error_factory)
        exists = _path_present(candidate, error_factory)
        if expected == 'new':
            if exists:
                _fail(error_factory, 'SQLite NEW write target already exists')
            source_image = b''
            published = _PublishedWriteImage(False, None)
        else:
            if not exists:
                _fail(error_factory, 'SQLite EXISTING write target is missing')
            _reject_lexical_reparse_chain(candidate, error_factory)
            if os.name != 'nt':
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise error_factory('SQLite write target could not be resolved') from exc
                if resolved != candidate:
                    _fail(error_factory, 'SQLite write target escaped its verified parent')
            published_lease = _pin_published_image(candidate, error_factory)
            source_image, fingerprint = _read_database_image(candidate, error_factory)
            published_lease.verify_exact(error_factory)
            if published_lease.fingerprint != fingerprint:
                _fail(error_factory, 'SQLite pinned existing image does not match measured source')
            _validate_rollback_sqlite_image(source_image, error_factory)
            if expected_existing_sha256 is not None and fingerprint.sha256 != expected_existing_sha256:
                _fail(error_factory, 'SQLite existing write target does not match the expected SHA-256')
            published = _PublishedWriteImage(True, fingerprint)
        connection = sqlite3.connect(':memory:', timeout=timeout, factory=ValidatedSQLiteWriteConnection)
        if source_image:
            connection.deserialize(source_image)
            connection.execute('PRAGMA schema_version').fetchone()
        if expected == 'new':
            assert owner_key is not None
            _initialize_sqlite_owner_anchor(connection, owner_key, error_factory)
        elif owner_key is not None:
            _require_sqlite_owner_anchor(connection, owner_key, error_factory)
        connection.execute('PRAGMA temp_store=MEMORY')
        if connection.execute('PRAGMA temp_store').fetchone() != (2,):
            _fail(error_factory, 'SQLite memory-only temp_store could not be enforced')
        working_image = _serialize_write_database(connection, error_factory)
        guard = _ValidatedWriteGuard(database_path=candidate, directory_chain=directory_chain, mutex=mutex, published=published, published_lease=published_lease, owner_key=owner_key, working_sha256=hashlib.sha256(working_image).hexdigest(), error_factory=error_factory)
        guard._verify_published_image()
        connection.install_write_guard(guard)
        connection.lock_validated_authorizer()
        return connection
    except sqlite3.Error as exc:
        _abort_failed_validated_open(connection, guard, published_lease, directory_chain, mutex)
        raise error_factory('SQLite validated write target could not be opened') from exc
    except BaseException:
        _abort_failed_validated_open(connection, guard, published_lease, directory_chain, mutex)
        raise

def controller_sqlite_owner_key(project_root: str | Path, database_path: str | Path, owner_token: str, *, error_factory: SQLiteReadErrorFactory=ValueError) -> str:
    """Bind an external controller token to one portable controller-local path."""
    root = Path(project_root)
    database = Path(database_path)
    if not root.is_absolute() or not database.is_absolute():
        _fail(error_factory, 'SQLite owner-key paths must be absolute')
    if type(owner_token) is not str or len(owner_token) != 64 or any((character not in '0123456789abcdef' for character in owner_token)):
        _fail(error_factory, 'SQLite external owner token is missing or invalid')
    root_text = os.path.normcase(os.path.abspath(root))
    database_text = os.path.normcase(os.path.abspath(database))
    try:
        relative = os.path.relpath(database_text, root_text)
    except ValueError as exc:
        raise error_factory('SQLite owner-key paths are on different volumes') from exc
    relative_path = Path(relative)
    if relative_path.is_absolute() or '..' in relative_path.parts:
        _fail(error_factory, 'SQLite owner-key database is outside its controller')
    portable_database = relative.replace('\\', '/').casefold()
    binding = f'questmemory-sqlite-owner-v1\x00external-token={owner_token}\x00database={portable_database}'.encode()
    return 'qm-sqlite-owner-v1:' + hashlib.sha256(binding).hexdigest()

def _validate_write_expectation(expected: SQLiteWriteExpectation, *, owner_key: str | None, expected_existing_sha256: str | None, error_factory: SQLiteReadErrorFactory) -> None:
    if expected not in {'new', 'existing'}:
        _fail(error_factory, 'SQLite write expectation must be NEW or EXISTING')
    if owner_key is not None:
        _validate_owner_key(owner_key, error_factory)
    if expected_existing_sha256 is not None and (len(expected_existing_sha256) != 64 or any((character not in '0123456789abcdef' for character in expected_existing_sha256))):
        _fail(error_factory, 'SQLite expected existing SHA-256 is invalid')
    if expected == 'new':
        if owner_key is None or expected_existing_sha256 is not None:
            _fail(error_factory, 'SQLite NEW writes require only an owner key')
        return
    if (owner_key is None) == (expected_existing_sha256 is None):
        _fail(error_factory, 'SQLite EXISTING writes require exactly one provenance anchor')

def _validate_owner_key(owner_key: str, error_factory: SQLiteReadErrorFactory) -> None:
    prefix = 'qm-sqlite-owner-v1:'
    digest = owner_key.removeprefix(prefix) if isinstance(owner_key, str) else ''
    if type(owner_key) is not str or not owner_key.startswith(prefix) or len(digest) != 64 or any((character not in '0123456789abcdef' for character in digest)):
        _fail(error_factory, 'SQLite owner key is invalid')

def _initialize_sqlite_owner_anchor(connection: sqlite3.Connection, owner_key: str, error_factory: SQLiteReadErrorFactory) -> None:
    try:
        connection.execute(_SQLITE_OWNER_SCHEMA)
        connection.execute(f'INSERT INTO {_SQLITE_OWNER_TABLE} (singleton, schema_version, owner_key) VALUES (1, 1, ?)', (owner_key,))
        connection.commit()
    except sqlite3.Error as exc:
        raise error_factory('SQLite owner anchor could not be initialized') from exc

def _require_sqlite_owner_anchor(connection: sqlite3.Connection, owner_key: str, error_factory: SQLiteReadErrorFactory) -> None:
    try:
        schema = [tuple(row) for row in connection.execute('SELECT type, sql FROM sqlite_schema WHERE name = ?', (_SQLITE_OWNER_TABLE,)).fetchall()]
        rows = [tuple(row) for row in connection.execute(f'SELECT singleton, schema_version, owner_key FROM {_SQLITE_OWNER_TABLE} ORDER BY singleton').fetchall()]
    except sqlite3.Error as exc:
        raise error_factory('SQLite owner anchor is missing or unreadable') from exc
    if schema != [('table', _SQLITE_OWNER_SCHEMA)] or rows != [(1, 1, owner_key)]:
        _fail(error_factory, 'SQLite owner anchor does not match this controller')

def _abort_failed_validated_open(connection: ValidatedSQLiteWriteConnection | None, guard: _ValidatedWriteGuard | None, published_lease: _PinnedPublishedImage | None, directory_chain: _PinnedDirectoryChain | None, mutex: _WriteMutex) -> None:
    cleanup_steps: list[Callable[[], None]] = []
    cleanup_failures: list[BaseException] = []
    if connection is not None:
        cleanup_steps.append(connection.abort_without_publish)
    if guard is not None:
        cleanup_steps.append(guard.close)
    if published_lease is not None:
        cleanup_steps.append(published_lease.close)
    if directory_chain is not None:
        cleanup_steps.append(directory_chain.close)
    cleanup_steps.append(mutex.close)
    for cleanup in cleanup_steps:
        try:
            cleanup()
        except BaseException as exc:
            cleanup_failures.append(exc)

def _validate_write_target_leaf(path: Path, error_factory: SQLiteReadErrorFactory) -> None:
    raw = str(path)
    folded = raw.casefold()
    if '\x00' in raw or folded.startswith(('\\\\.\\', '\\\\?\\globalroot', '\\??\\')):
        _fail(error_factory, 'SQLite write target uses a device namespace')
    name = path.name
    if not name or name in {'.', '..'}:
        _fail(error_factory, 'SQLite write target leaf is missing')
    if any((ord(character) < 32 for character in name)):
        _fail(error_factory, 'SQLite write target leaf contains a control character')
    if any((character in '<>:"/\\|?*' for character in name)):
        _fail(error_factory, 'SQLite write target leaf is not Win32-safe')
    if name.endswith((' ', '.')):
        _fail(error_factory, 'SQLite write target leaf has an unsafe suffix')
    reserved = {'con', 'prn', 'aux', 'nul', 'clock$', 'conin$', 'conout$', *(f'com{index}' for index in range(1, 10)), *(f'lpt{index}' for index in range(1, 10))}
    if name.split('.', 1)[0].casefold() in reserved:
        _fail(error_factory, 'SQLite write target leaf is a reserved device name')

def _acquire_write_mutex(path: Path, timeout: float, error_factory: SQLiteReadErrorFactory) -> _WriteMutex:
    key = os.path.normcase(str(path))
    with _WRITE_MUTEXES_GUARD:
        thread_lock = _WRITE_MUTEXES.setdefault(key, threading.Lock())
    if not thread_lock.acquire(timeout=max(0.0, timeout)):
        _fail(error_factory, 'SQLite validated-write lease timed out')
    windows_handle: int | None = None
    try:
        if os.name == 'nt':
            windows_handle = _acquire_windows_mutex(key, timeout, error_factory)
        return _WriteMutex(key=key, thread_lock=thread_lock, windows_handle=windows_handle, error_factory=error_factory)
    except BaseException:
        thread_lock.release()
        raise

def _acquire_windows_mutex(key: str, timeout: float, error_factory: SQLiteReadErrorFactory) -> int:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    wait_for_single = kernel32.WaitForSingleObject
    wait_for_single.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    wait_for_single.restype = ctypes.c_uint32
    name = 'Local\\QuestMemory.SQLiteWrite.' + hashlib.sha256(key.encode('utf-8')).hexdigest()
    handle = create_mutex(None, 0, name)
    if not handle:
        raise error_factory('SQLite Windows write mutex could not be created')
    wait_ms = min(int(max(0.0, timeout) * 1000), 4294967294)
    result = wait_for_single(handle, wait_ms)
    if result not in {0, 128}:
        kernel32.CloseHandle(handle)
        if result == 258:
            _fail(error_factory, 'SQLite validated-write lease timed out')
        _fail(error_factory, 'SQLite Windows write mutex could not be acquired')
    return int(handle)

def _release_windows_mutex(handle: int) -> None:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.ReleaseMutex(ctypes.c_void_p(handle))
    kernel32.CloseHandle(ctypes.c_void_p(handle))

def _ensure_and_pin_directory_chain(parent: Path, error_factory: SQLiteReadErrorFactory) -> _PinnedDirectoryChain:
    if not parent.is_absolute() or not parent.anchor:
        _fail(error_factory, 'SQLite write-target parent must be absolute')
    entries: list[_PinnedDirectory] = []
    current = Path(parent.anchor)
    paths = [current]
    for part in parent.parts[1:]:
        current = current / part
        paths.append(current)
    try:
        for index, directory in enumerate(paths):
            try:
                _path_lstat(directory)
            except FileNotFoundError:
                if index == 0:
                    raise error_factory('SQLite write-target filesystem anchor is missing')
                try:
                    _mkdir_native(directory, parent_handle=entries[-1].handle)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise error_factory('SQLite write-target directory could not be created') from exc
            except OSError as exc:
                raise error_factory('SQLite write-target directory metadata is unavailable') from exc
            _reject_one_directory_component(directory, error_factory)
            before = _directory_identity(directory, error_factory)
            handle = _open_directory_handle(directory, error_factory, parent_handle=entries[-1].handle if entries else None)
            try:
                after = _directory_identity(directory, error_factory)
            except BaseException:
                _close_directory_handle(handle)
                raise
            if after != before:
                _close_directory_handle(handle)
                _fail(error_factory, 'SQLite write-target directory changed while being pinned')
            entries.append(_PinnedDirectory(directory, before, handle))
        guard = _PinnedDirectoryChain(tuple(entries), error_factory)
        guard.verify()
        return guard
    except BaseException:
        for entry in reversed(entries):
            _close_directory_handle(entry.handle)
        raise

def _reject_one_directory_component(path: Path, error_factory: SQLiteReadErrorFactory) -> None:
    try:
        metadata = _path_lstat(path)
    except OSError as exc:
        raise error_factory('SQLite write-target directory component is missing') from exc
    if _path_is_reparse(metadata):
        _fail(error_factory, 'SQLite write-target directory contains a symlink or reparse component')
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(error_factory, 'SQLite write-target path component is not a directory')

def _open_directory_handle(path: Path, error_factory: SQLiteReadErrorFactory, *, parent_handle: int | None=None) -> int:
    if os.name != 'nt':
        flags = os.O_RDONLY
        flags |= getattr(os, 'O_DIRECTORY', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            return os.open(path, flags)
        except OSError as exc:
            raise error_factory('SQLite write-target directory could not be pinned') from exc
    if parent_handle is not None:
        return _open_windows_relative(parent_handle, path.name, directory=True, create=False, error_factory=error_factory)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    create_file.restype = ctypes.c_void_p
    handle = create_file(_windows_extended_path(path), 2147483648, 1 | 2, None, 3, 33554432 | 2097152, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        raise error_factory('SQLite write-target directory could not be pinned')
    return int(handle)

def _close_directory_handle(handle: int) -> None:
    if os.name == 'nt':
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    else:
        os.close(handle)

def _windows_extended_path(path: Path) -> str:
    value = str(path)
    if value.startswith('\\\\?\\'):
        return value
    if value.startswith('\\\\'):
        return '\\\\?\\UNC\\' + value[2:]
    return '\\\\?\\' + value

def _native_filesystem_path(path: Path) -> str:
    if os.name == 'nt':
        return _windows_extended_path(path)
    return os.fspath(path)

def _path_lstat(path: Path) -> os.stat_result:
    return os.lstat(_native_filesystem_path(path))

@dataclass(frozen=True, slots=True)
class _WindowsStatSnapshot:
    """One handle's metadata with explicit NT change/creation semantics."""
    raw: os.stat_result
    st_ctime_ns: int
    windows_creation_time_ns: int

    def __getattr__(self, name: str):
        return getattr(self.raw, name)

def _windows_basic_metadata(handle: int) -> tuple[int, int, int, int]:

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [('CreationTime', ctypes.c_int64), ('LastAccessTime', ctypes.c_int64), ('LastWriteTime', ctypes.c_int64), ('ChangeTime', ctypes.c_int64), ('FileAttributes', ctypes.c_uint32)]
    query = ctypes.WinDLL('kernel32', use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    query.restype = ctypes.c_int
    info = FileBasicInfo()
    if not query(ctypes.c_void_p(handle), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    epoch = 116444736000000000
    return ((info.CreationTime - epoch) * 100, (info.LastWriteTime - epoch) * 100, (info.ChangeTime - epoch) * 100, int(info.FileAttributes))

def _handle_stat(descriptor: int) -> os.stat_result | _WindowsStatSnapshot:
    if os.name != 'nt':
        return os.fstat(descriptor)
    import msvcrt
    native_handle = msvcrt.get_osfhandle(descriptor)
    before_basic = _windows_basic_metadata(native_handle)
    before = os.fstat(descriptor)
    after = os.fstat(descriptor)
    after_basic = _windows_basic_metadata(native_handle)
    if before_basic != after_basic or _stat_fingerprint(before) != _stat_fingerprint(after):
        raise OSError('SQLite native handle metadata changed while being measured')
    creation, modified, changed, attributes = after_basic
    if modified != after.st_mtime_ns or attributes != after.st_file_attributes:
        raise OSError('SQLite native and descriptor metadata disagree')
    birthtime = getattr(after, 'st_birthtime_ns', None)
    if birthtime is not None and birthtime != creation:
        raise OSError('SQLite native and descriptor creation time disagree')
    return _WindowsStatSnapshot(after, changed, creation)

def _path_stat(path: Path) -> os.stat_result | _WindowsStatSnapshot:
    if os.name != 'nt':
        return os.stat(_native_filesystem_path(path), follow_symlinks=False)
    import msvcrt
    create_file = ctypes.WinDLL('kernel32', use_last_error=True).CreateFileW
    create_file.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    create_file.restype = ctypes.c_void_p
    handle = create_file(_native_filesystem_path(path), 128, 1 | 2 | 4, None, 3, 33554432 | 2097152, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _close_windows_handle(int(handle))
        raise
    try:
        return _handle_stat(descriptor)
    finally:
        os.close(descriptor)

def _path_present(path: Path, error_factory: SQLiteReadErrorFactory) -> bool:
    try:
        _path_lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise error_factory('SQLite path presence could not be established') from exc
    return True

def _path_is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, 'st_file_attributes', 0) & _REPARSE_FLAG)

def _open_native_file(path: Path, mode: str):
    return open(_native_filesystem_path(path), mode)

@contextmanager
def _read_file_lease(path: Path):
    """Pin one Windows file against writers/deletion before taking a baseline.

    POSIX retains the original optimistic stable-image protocol. There is no
    wider-sharing retry or metadata-only fallback on Windows.
    """
    if os.name != 'nt':
        yield None
        return
    import msvcrt
    create_file = ctypes.WinDLL('kernel32', use_last_error=True).CreateFileW
    create_file.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    create_file.restype = ctypes.c_void_p
    handle = create_file(_native_filesystem_path(path), 2147483648 | 128, 1, None, 3, 128 | 2097152, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _close_windows_handle(int(handle))
        raise
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    try:
        snapshot = _handle_stat(stream.fileno())
        if not stat.S_ISREG(snapshot.st_mode) or _path_is_reparse(snapshot) or snapshot.st_nlink != 1:
            raise OSError('SQLite read lease requires a single-link non-reparse file')
        yield stream
    finally:
        stream.close()

def _mkdir_native(path: Path, *, parent_handle: int | None=None) -> None:
    if os.name == 'nt' and parent_handle is not None:
        handle = _open_windows_relative(parent_handle, path.name, directory=True, create=True, error_factory=OSError)
        _close_windows_handle(handle)
        return
    os.mkdir(_native_filesystem_path(path))

def _open_windows_relative(parent_handle: int, name: str, *, directory: bool, create: bool, error_factory: SQLiteReadErrorFactory, read_only: bool=False) -> int:
    """Open exactly one child of a pinned directory; never traverse reparses.

    Opt-in read-only file leases exclude write/delete while permitting other
    readers. Default database/publication access and sharing stay unchanged.
    """
    if type(read_only) is not bool or (read_only and (create or directory)):
        _fail(error_factory, 'SQLite relative read-only policy is invalid')
    if not name or name in {'.', '..'} or any((c in name for c in '\\/:')):
        _fail(error_factory, 'SQLite relative child name is invalid')

    class UnicodeString(ctypes.Structure):
        _fields_ = [('Length', ctypes.c_ushort), ('MaximumLength', ctypes.c_ushort), ('Buffer', ctypes.c_void_p)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [('Length', ctypes.c_ulong), ('RootDirectory', ctypes.c_void_p), ('ObjectName', ctypes.POINTER(UnicodeString)), ('Attributes', ctypes.c_ulong), ('SecurityDescriptor', ctypes.c_void_p), ('SecurityQualityOfService', ctypes.c_void_p)]
    encoded = name.encode('utf-16-le')
    buffer = ctypes.create_string_buffer(encoded + b'\x00\x00')
    value = UnicodeString(len(encoded), len(encoded) + 2, ctypes.addressof(buffer))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent_handle, ctypes.pointer(value), 64 | 4096, None, None)
    handle = ctypes.c_void_p()
    io_status = (ctypes.c_size_t * 2)()
    ntdll = ctypes.WinDLL('ntdll')
    native_open = ntdll.NtCreateFile
    native_open.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
    native_open.restype = ctypes.c_long
    access = 1179785 if directory or read_only else 1245599
    status = native_open(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(io_status), None, 128, 3 if directory else 1, 2 if create else 1, 32 | (1 if directory else 64), None, 0)
    if status < 0:
        if status & 4294967295 == 3221225525:
            raise FileExistsError(name)
        _fail(error_factory, f'SQLite relative open failed (NTSTATUS {status & 4294967295:08x})')
    return int(handle.value)

def _pin_published_image(path: Path, error_factory: SQLiteReadErrorFactory) -> _PinnedPublishedImage:
    if os.name != 'nt':
        fingerprint = _measure_database(path, error_factory)
        return _PinnedPublishedImage(path=path, fingerprint=fingerprint, identity=_database_write_identity(path, error_factory), windows_handle=None)
    initial_identity = _database_write_identity(path, error_factory)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    create_file.restype = ctypes.c_void_p
    handle = create_file(_windows_extended_path(path), 2147483648 | 128, 1 | 4, None, 3, 128 | 2097152, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        error_code = ctypes.get_last_error()
        raise error_factory(f'SQLite existing write target could not be leased against writers (Windows error {error_code})')
    lease_handle = int(handle)
    try:
        before = _path_stat(path)
        identity = _database_write_identity(path, error_factory)
        if identity != initial_identity:
            _fail(error_factory, 'SQLite existing write target changed while being leased')
        measured = _measure_database(path, error_factory)
        after = _path_stat(path)
        if _stat_fingerprint(before) != _stat_fingerprint(after) or measured.metadata != _stat_fingerprint(after):
            _fail(error_factory, 'SQLite existing write target changed while being leased')
        fingerprint = measured
        lease = _PinnedPublishedImage(path=path, fingerprint=fingerprint, identity=identity, windows_handle=lease_handle)
        lease.verify_exact(error_factory)
        return lease
    except BaseException:
        _close_windows_handle(lease_handle)
        raise

def _read_database_image(path: Path, error_factory: SQLiteReadErrorFactory) -> tuple[bytes, _DatabaseFingerprint]:
    try:
        with _read_file_lease(path) as lease:
            return _read_leased_database_image(path, error_factory, lease)
    except OSError as exc:
        raise error_factory('SQLite write target could not retain a read lease') from exc

def _read_leased_database_image(path: Path, error_factory: SQLiteReadErrorFactory, read_lease=None) -> tuple[bytes, _DatabaseFingerprint]:
    if os.name == 'nt' and read_lease is None:
        _fail(error_factory, 'SQLite database image requires an actual read lease')
    chunks: list[bytes] = []
    byte_count = 0
    hasher = hashlib.sha256()
    try:
        before_path = _path_stat(path)
        if not stat.S_ISREG(before_path.st_mode) or _path_is_reparse(before_path):
            _fail(error_factory, 'SQLite write target is not a regular file')
        if before_path.st_nlink != 1:
            _fail(error_factory, 'SQLite write target has a hardlink alias')
        if before_path.st_size > MAX_STABLE_SQLITE_BYTES:
            _fail(error_factory, 'SQLite database exceeds the validated-write byte budget')
        with _open_native_file(path, 'rb') as handle:
            before_handle = _handle_stat(handle.fileno())
            while (chunk := handle.read(1024 * 1024)):
                byte_count += len(chunk)
                if byte_count > MAX_STABLE_SQLITE_BYTES:
                    _fail(error_factory, 'SQLite database exceeds the validated-write byte budget')
                chunks.append(chunk)
                hasher.update(chunk)
            after_handle = _handle_stat(handle.fileno())
        after_path = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite write target could not be measured') from exc
    fingerprint = _stat_fingerprint(before_handle)
    if read_lease is not None and _stat_fingerprint(_handle_stat(read_lease.fileno())) != fingerprint or _stat_fingerprint(before_path) != fingerprint or _stat_fingerprint(after_handle) != fingerprint or (_stat_fingerprint(after_path) != fingerprint):
        _fail(error_factory, 'SQLite write target changed while being measured')
    return (b''.join(chunks), _DatabaseFingerprint(metadata=fingerprint, sha256=hasher.hexdigest()))

def _validate_rollback_sqlite_image(image: bytes, error_factory: SQLiteReadErrorFactory) -> None:
    if not image:
        return
    if len(image) < 100 or image[:16] != b'SQLite format 3\x00':
        _fail(error_factory, 'SQLite write source does not contain a valid header')
    if image[18:20] != b'\x01\x01':
        _fail(error_factory, 'SQLite validated write requires a quiescent rollback-journal image')

def _serialize_write_database(connection: sqlite3.Connection, error_factory: SQLiteReadErrorFactory) -> bytes:
    try:
        return connection.serialize()
    except sqlite3.OperationalError as exc:
        try:
            empty = connection.execute('SELECT count(*) FROM sqlite_schema').fetchone() == (0,)
        except sqlite3.Error:
            empty = False
        if empty:
            return b''
        raise error_factory('SQLite in-memory write image could not be serialized') from exc
    except sqlite3.Error as exc:
        raise error_factory('SQLite in-memory write image could not be serialized') from exc

def _write_staged_image(database_path: Path, image: bytes, image_sha256: str, error_factory: SQLiteReadErrorFactory) -> _PinnedStage:
    if os.name == 'nt':
        return _write_windows_staged_image(database_path, image, image_sha256, error_factory)
    stage: Path | None = None
    descriptor: int | None = None
    try:
        for _ in range(32):
            candidate = database_path.parent / f'.{database_path.name}.{secrets.token_hex(16)}.qm-write-stage'
            try:
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 384)
                stage = candidate
                break
            except FileExistsError:
                continue
        if stage is None or descriptor is None:
            _fail(error_factory, 'SQLite staging path could not be reserved')
        view = memoryview(image)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                _fail(error_factory, 'SQLite staging write made no progress')
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        measured = _measure_database(stage, error_factory)
        if measured.sha256 != image_sha256:
            _fail(error_factory, 'SQLite staged image hash does not match memory')
        return _PinnedStage(path=stage, fingerprint=measured, identity=_database_write_identity(stage, error_factory), windows_handle=None)
    except OSError as exc:
        raise error_factory('SQLite write image could not be staged') from exc
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if stage is not None:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
        raise

def _write_windows_staged_image(database_path: Path, image: bytes, image_sha256: str, error_factory: SQLiteReadErrorFactory) -> _PinnedStage:
    for _ in range(32):
        stage = database_path.parent / f'.{database_path.name}.{secrets.token_hex(16)}.qm-write-stage'
        handle = _create_windows_stage_handle(stage, error_factory)
        if handle is None:
            continue
        try:
            _write_windows_file_handle(handle, image, error_factory)
            fingerprint = _fingerprint_windows_file_handle(stage, handle, error_factory)
            if fingerprint.sha256 != image_sha256:
                _fail(error_factory, 'SQLite staged image hash does not match memory')
            return _PinnedStage(path=stage, fingerprint=fingerprint, identity=_database_write_identity(stage, error_factory), windows_handle=handle)
        except BaseException:
            _delete_windows_file_by_handle(handle)
            _close_windows_handle(handle)
            raise
    _fail(error_factory, 'SQLite staging path could not be reserved')

def _create_windows_stage_handle(path: Path, error_factory: SQLiteReadErrorFactory) -> int | None:
    guard = _ensure_and_pin_directory_chain(path.parent, error_factory)
    try:
        return _open_windows_relative(guard.entries[-1].handle, path.name, directory=False, create=True, error_factory=error_factory)
    except FileExistsError:
        return None
    finally:
        guard.close()

def _write_windows_file_handle(handle: int, image: bytes, error_factory: SQLiteReadErrorFactory) -> None:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    set_pointer = kernel32.SetFilePointerEx
    set_pointer.argtypes = (ctypes.c_void_p, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint32)
    set_pointer.restype = ctypes.c_int
    write_file = kernel32.WriteFile
    write_file.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p)
    write_file.restype = ctypes.c_int
    if not set_pointer(ctypes.c_void_p(handle), 0, None, 0):
        _fail(error_factory, 'SQLite staging handle could not be rewound')
    offset = 0
    while offset < len(image):
        chunk = image[offset:offset + 1024 * 1024]
        buffer = ctypes.create_string_buffer(chunk)
        written = ctypes.c_uint32()
        if not write_file(ctypes.c_void_p(handle), buffer, len(chunk), ctypes.byref(written), None):
            _fail(error_factory, 'SQLite staging handle write failed')
        if written.value < 1:
            _fail(error_factory, 'SQLite staging handle write made no progress')
        offset += written.value
    if not kernel32.SetEndOfFile(ctypes.c_void_p(handle)):
        _fail(error_factory, 'SQLite staging handle could not be truncated')
    _flush_windows_handle(handle, error_factory)

def _fingerprint_windows_file_handle(path: Path, handle: int, error_factory: SQLiteReadErrorFactory) -> _DatabaseFingerprint:
    try:
        before = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite pinned staging metadata is unavailable') from exc
    if not stat.S_ISREG(before.st_mode) or _path_is_reparse(before) or before.st_nlink != 1:
        _fail(error_factory, 'SQLite pinned staging file is not single-link regular')
    digest = _hash_windows_file_handle(handle, error_factory)
    try:
        after = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite pinned staging metadata is unavailable') from exc
    if _stat_fingerprint(before) != _stat_fingerprint(after):
        _fail(error_factory, 'SQLite pinned staging file changed during hashing')
    return _DatabaseFingerprint(_stat_fingerprint(after), digest)

def _hash_windows_file_handle(handle: int, error_factory: SQLiteReadErrorFactory) -> str:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    set_pointer = kernel32.SetFilePointerEx
    set_pointer.argtypes = (ctypes.c_void_p, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint32)
    set_pointer.restype = ctypes.c_int
    read_file = kernel32.ReadFile
    read_file.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p)
    read_file.restype = ctypes.c_int
    if not set_pointer(ctypes.c_void_p(handle), 0, None, 0):
        _fail(error_factory, 'SQLite pinned staging handle could not be rewound')
    hasher = hashlib.sha256()
    byte_count = 0
    buffer = ctypes.create_string_buffer(1024 * 1024)
    while True:
        read = ctypes.c_uint32()
        if not read_file(ctypes.c_void_p(handle), buffer, len(buffer), ctypes.byref(read), None):
            _fail(error_factory, 'SQLite pinned staging handle could not be read')
        if read.value == 0:
            break
        byte_count += read.value
        if byte_count > MAX_STABLE_SQLITE_BYTES:
            _fail(error_factory, 'SQLite database exceeds the validated-write byte budget')
        hasher.update(buffer.raw[:read.value])
    return hasher.hexdigest()

def _publish_pinned_stage(stage: _PinnedStage, database_path: Path, *, replace: bool, parent_handle: int, error_factory: SQLiteReadErrorFactory) -> None:
    stage.verify_exact(error_factory)
    original_stage_path = stage.path
    stage.path = database_path
    stage.moved = True
    try:
        if stage.windows_handle is not None:
            _rename_windows_file_handle(stage.windows_handle, database_path, replace, error_factory, parent_handle=parent_handle)
        elif replace:
            os.replace(original_stage_path, database_path)
        else:
            os.link(original_stage_path, database_path, follow_symlinks=False)
            os.unlink(original_stage_path)
    except BaseException:
        target_is_stage = _path_matches_write_identity(database_path, stage.identity)
        source_is_stage = _path_matches_write_identity(original_stage_path, stage.identity)
        if source_is_stage and (not target_is_stage):
            stage.path = original_stage_path
            stage.moved = False
        raise

def _path_matches_write_identity(path: Path, expected: tuple[int, int, int, int]) -> bool:
    try:
        value = _path_stat(path)
    except OSError:
        return False
    current = (value.st_dev, value.st_ino, value.st_nlink, value.st_mode)
    return current == expected

def _rename_windows_file_handle(handle: int, target_path: Path, replace: bool, error_factory: SQLiteReadErrorFactory, *, parent_handle: int) -> None:

    class RenameInformation(ctypes.Structure):
        _fields_ = [('Flags', ctypes.c_uint32), ('RootDirectory', ctypes.c_void_p), ('FileNameLength', ctypes.c_uint32), ('FileName', ctypes.c_wchar * 1)]
    encoded = target_path.name.encode('utf-16-le')
    offset = RenameInformation.FileName.offset
    buffer = ctypes.create_string_buffer(max(ctypes.sizeof(RenameInformation), offset + len(encoded)))
    info = RenameInformation.from_buffer(buffer)
    info.Flags = 3 if replace else 0
    info.RootDirectory = parent_handle
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    io_status = (ctypes.c_size_t * 2)()
    ntdll = ctypes.WinDLL('ntdll')
    rename = ntdll.NtSetInformationFile
    rename.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
    rename.restype = ctypes.c_long
    status = rename(ctypes.c_void_p(handle), ctypes.byref(io_status), buffer, offset + len(encoded), 65)
    if status < 0:
        raise error_factory(f'SQLite pinned relative publication failed (NTSTATUS {status & 4294967295:08x})')

def _flush_windows_handle(handle: int, error_factory: SQLiteReadErrorFactory) -> None:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    if not kernel32.FlushFileBuffers(ctypes.c_void_p(handle)):
        _fail(error_factory, 'SQLite pinned staging handle could not be synchronized')

def _delete_windows_file_by_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    delete = ctypes.c_int(1)
    kernel32.SetFileInformationByHandle(ctypes.c_void_p(handle), 4, ctypes.byref(delete), ctypes.sizeof(delete))

def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CloseHandle(ctypes.c_void_p(handle))

def _sync_file(path: Path, error_factory: SQLiteReadErrorFactory) -> None:
    try:
        with _open_native_file(path, 'r+b') as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise error_factory('SQLite published image could not be synchronized') from exc

def _resolve_database_path(path: Path, error_factory: SQLiteReadErrorFactory) -> Path:
    if not path.is_absolute():
        _fail(error_factory, 'SQLite stable read requires an absolute database path')
    _reject_lexical_reparse_chain(path, error_factory)
    if os.name == 'nt':
        resolved = Path(os.path.abspath(path))
    else:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise error_factory('SQLite stable-read database is missing') from exc
    _reject_lexical_reparse_chain(resolved, error_factory)
    _database_metadata_fingerprint(resolved, error_factory)
    return resolved

def _build_guard(resolved: Path, error_factory: SQLiteReadErrorFactory) -> _StableImageGuard:
    parent_fingerprint = _metadata_fingerprint(resolved.parent, require_directory=True, error_factory=error_factory)
    _reject_sidecars(resolved, error_factory)
    database_fingerprint = _measure_database(resolved, error_factory)
    _reject_sidecars(resolved, error_factory)
    return _StableImageGuard(database_path=resolved, database_fingerprint=database_fingerprint, parent_fingerprint=parent_fingerprint, error_factory=error_factory)

def _session_key(path: Path) -> str:
    return os.path.normcase(str(path))

def _stable_read_authorizer(action_code: int, argument_one: str | None, argument_two: str | None, _database_name: str | None, _trigger_or_view: str | None) -> int:
    if action_code in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
        return sqlite3.SQLITE_DENY
    if action_code == sqlite3.SQLITE_PRAGMA and argument_two is not None:
        if (argument_one or '').casefold() in {'foreign_key_list', 'index_info', 'index_xinfo', 'table_info', 'table_xinfo'}:
            return sqlite3.SQLITE_OK
        if (argument_one or '').casefold() == 'foreign_keys' and argument_two.casefold() in {'1', 'on'}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def _validated_write_authorizer(action_code: int, argument_one: str | None, argument_two: str | None, _database_name: str | None, _trigger_or_view: str | None) -> int:
    if action_code == sqlite3.SQLITE_PRAGMA and (argument_one or '').casefold() in {'foreign_key_list', 'index_info', 'index_xinfo', 'table_info', 'table_xinfo'}:
        return sqlite3.SQLITE_OK
    owner_table = _SQLITE_OWNER_TABLE.casefold()
    if action_code != sqlite3.SQLITE_READ and owner_table in {(argument_one or '').casefold(), (argument_two or '').casefold()}:
        return sqlite3.SQLITE_DENY
    if action_code in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
        return sqlite3.SQLITE_DENY
    if action_code == sqlite3.SQLITE_PRAGMA and argument_two is not None:
        if (argument_one or '').casefold() in {'foreign_key_list', 'index_info', 'index_xinfo', 'table_info', 'table_xinfo'}:
            return sqlite3.SQLITE_OK
        if (argument_one or '').casefold() == 'foreign_keys' and argument_two.casefold() in {'1', 'on'}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def _reject_lexical_reparse_chain(path: Path, error_factory: SQLiteReadErrorFactory) -> None:
    if not path.is_absolute() or not path.anchor:
        _fail(error_factory, 'SQLite database path must be explicit and absolute')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = _path_lstat(current)
        except OSError as exc:
            raise error_factory('SQLite database path contains a missing component') from exc
        if _path_is_reparse(metadata):
            _fail(error_factory, 'SQLite database path contains a symlink or reparse component')

def _reject_sidecars(path: Path, error_factory: SQLiteReadErrorFactory) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = Path(f'{path}{suffix}')
        if _path_present(sidecar, error_factory):
            _fail(error_factory, f'SQLite stable read requires a quiescent image without {suffix} sidecar')

def _measure_database(path: Path, error_factory: SQLiteReadErrorFactory) -> _DatabaseFingerprint:
    try:
        with _read_file_lease(path) as lease:
            return _measure_leased_database(path, error_factory, lease)
    except OSError as exc:
        raise error_factory('SQLite stable-read database could not retain a read lease') from exc

def _measure_leased_database(path: Path, error_factory: SQLiteReadErrorFactory, read_lease=None) -> _DatabaseFingerprint:
    if os.name == 'nt' and read_lease is None:
        _fail(error_factory, 'SQLite database measurement requires an actual read lease')
    hasher = hashlib.sha256()
    byte_count = 0
    try:
        before_path = _path_stat(path)
        if not stat.S_ISREG(before_path.st_mode) or _path_is_reparse(before_path):
            _fail(error_factory, 'SQLite stable-read database is not a regular file')
        if before_path.st_nlink != 1:
            _fail(error_factory, 'SQLite stable-read database has a hardlink alias')
        if before_path.st_size > MAX_STABLE_SQLITE_BYTES:
            _fail(error_factory, 'SQLite database exceeds the stable-read byte budget')
        with _open_native_file(path, 'rb') as handle:
            before_handle = _handle_stat(handle.fileno())
            while (chunk := handle.read(1024 * 1024)):
                byte_count += len(chunk)
                if byte_count > MAX_STABLE_SQLITE_BYTES:
                    _fail(error_factory, 'SQLite database exceeds the stable-read byte budget')
                hasher.update(chunk)
            after_handle = _handle_stat(handle.fileno())
        after_path = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite stable-read database could not be measured') from exc
    fingerprint = _stat_fingerprint(before_handle)
    if read_lease is not None and _stat_fingerprint(_handle_stat(read_lease.fileno())) != fingerprint or _stat_fingerprint(before_path) != fingerprint or _stat_fingerprint(after_handle) != fingerprint or (_stat_fingerprint(after_path) != fingerprint):
        _fail(error_factory, 'SQLite database changed while being measured')
    return _DatabaseFingerprint(metadata=fingerprint, sha256=hasher.hexdigest())

def _metadata_fingerprint(path: Path, *, require_directory: bool, error_factory: SQLiteReadErrorFactory) -> tuple[int, ...]:
    try:
        value = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite stable-read path metadata is unavailable') from exc
    if require_directory and (not stat.S_ISDIR(value.st_mode)):
        _fail(error_factory, 'SQLite database parent is not a directory')
    return _stat_fingerprint(value)

def _database_metadata_fingerprint(path: Path, error_factory: SQLiteReadErrorFactory) -> tuple[int, ...]:
    try:
        value = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite stable-read database metadata is unavailable') from exc
    if not stat.S_ISREG(value.st_mode) or _path_is_reparse(value):
        _fail(error_factory, 'SQLite stable-read database is not a regular file')
    if value.st_nlink != 1:
        _fail(error_factory, 'SQLite stable-read database has a hardlink alias')
    if value.st_size > MAX_STABLE_SQLITE_BYTES:
        _fail(error_factory, 'SQLite database exceeds the stable-read byte budget')
    return _stat_fingerprint(value)

def _directory_identity(path: Path, error_factory: SQLiteReadErrorFactory) -> tuple[int, int, int, int]:
    try:
        value = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite directory identity is unavailable') from exc
    if not stat.S_ISDIR(value.st_mode):
        _fail(error_factory, 'SQLite verified parent is not a directory')
    return (value.st_dev, value.st_ino, value.st_mode, int(getattr(value, 'st_file_attributes', 0)))

def _database_write_identity(path: Path, error_factory: SQLiteReadErrorFactory) -> tuple[int, int, int, int]:
    try:
        value = _path_stat(path)
    except OSError as exc:
        raise error_factory('SQLite write-target metadata is unavailable') from exc
    if not stat.S_ISREG(value.st_mode) or _path_is_reparse(value):
        _fail(error_factory, 'SQLite write target is not a regular file')
    if value.st_nlink != 1:
        _fail(error_factory, 'SQLite write target has a hardlink alias')
    return (value.st_dev, value.st_ino, value.st_nlink, value.st_mode)

def _stat_fingerprint(value: os.stat_result | _WindowsStatSnapshot) -> tuple[int, ...]:
    fingerprint = (value.st_dev, value.st_ino, value.st_nlink, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns, int(getattr(value, 'st_file_attributes', 0)))
    if isinstance(value, _WindowsStatSnapshot):
        return (*fingerprint, value.windows_creation_time_ns)
    return fingerprint

def _fail(error_factory: SQLiteReadErrorFactory, message: str) -> None:
    raise error_factory(message)
