# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import ctypes
import hashlib
import math
import os
import stat
import threading
import unicodedata
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import TracebackType
from typing import Literal
from typing import TypeAlias
from projectrecall.storage_policy import is_approved_windows_project_path
CONTROLLER_TRANSITION_MUTEX_PREFIX_V1 = 'Global\\QuestMemory.ControllerTransitionV1.'
DEFAULT_CONTROLLER_TRANSITION_TIMEOUT_SECONDS_V1 = 5.0
MAX_CONTROLLER_TRANSITION_TIMEOUT_SECONDS_V1 = 300.0
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED_0 = 128
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 4294967295
_REPARSE_FLAG = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)
ControllerRecoveryJournalKindV1: TypeAlias = Literal['snapshot_restore', 'contribution_materialization']
_KNOWN_RECOVERY_JOURNAL_KINDS_V1 = frozenset({'snapshot_restore', 'contribution_materialization'})

class ControllerTransitionLockErrorV1(RuntimeError):
    """Base class for fail-closed controller-transition lock errors."""
    reason_code = 'controller_transition_lock_failed'

class ControllerTransitionLockPlatformErrorV1(ControllerTransitionLockErrorV1):
    """The current host cannot provide the required Windows lock semantics."""
    reason_code = 'controller_transition_lock_platform_unsupported'

class ControllerTransitionLockPathErrorV1(ControllerTransitionLockErrorV1):
    """The controller root is not a provable, local, non-reparse D: root."""
    reason_code = 'controller_transition_lock_root_invalid'

class ControllerTransitionLockTimeoutErrorV1(ControllerTransitionLockErrorV1):
    """The bounded mutex wait elapsed without ownership."""
    reason_code = 'controller_transition_lock_timeout'

class ControllerTransitionLockIntegrityErrorV1(ControllerTransitionLockErrorV1):
    """The controller identity changed or the lease could not be maintained."""
    reason_code = 'controller_transition_lock_integrity_failed'

class ControllerTransitionRecoveryPendingErrorV1(ControllerTransitionLockErrorV1):
    """An unresolved durable transition journal blocks ordinary mutations."""
    reason_code = 'controller_transition_recovery_pending'

@dataclass(frozen=True, slots=True)
class _ControllerRootObservationV1:
    resolved_root: Path
    normalized_root: str
    identity: tuple[int, int, int]

@dataclass(slots=True)
class ControllerTransitionLeaseV1:
    """One acquired controller-transition mutex lease.

    The lease must be released by the same thread that acquired it.  Use it as
    a context manager.  ``verify()`` is intentionally public so callers can
    recheck the root immediately before each irreversible publication boundary.
    """
    controller_root: Path
    normalized_controller_root: str
    mutex_name: str
    root_identity: tuple[int, int, int]
    abandoned: bool
    _mutex_handle: int = field(repr=False)
    _owner_thread_id: int = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    @property
    def is_released(self) -> bool:
        return self._released

    def verify(self) -> None:
        """Fail closed unless the lease owner and exact root identity persist."""
        if self._released:
            raise ControllerTransitionLockIntegrityErrorV1('controller transition lease is already released')
        if threading.get_ident() != self._owner_thread_id:
            raise ControllerTransitionLockIntegrityErrorV1('controller transition lease must be verified by its owner thread')
        current = _observe_controller_root_v1(self.controller_root)
        _require_same_observation_v1(expected_root=self.controller_root, expected_normalized=self.normalized_controller_root, expected_identity=self.root_identity, current=current, phase='lease verification')

    def release(self) -> None:
        """Release one recursive ownership level and close this lease handle."""
        if self._released:
            return
        if threading.get_ident() != self._owner_thread_id:
            raise ControllerTransitionLockIntegrityErrorV1('controller transition lease must be released by its owner thread')
        try:
            _release_windows_mutex_v1(self._mutex_handle)
        finally:
            self._released = True

    def __enter__(self) -> ControllerTransitionLeaseV1:
        if self._released:
            raise ControllerTransitionLockIntegrityErrorV1('released controller transition lease cannot be re-entered')
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> bool:
        del exc_type, exc_value, traceback
        verification_error: BaseException | None = None
        try:
            self.verify()
        except BaseException as exc:
            verification_error = exc
        try:
            self.release()
        except BaseException:
            if verification_error is not None:
                raise ControllerTransitionLockIntegrityErrorV1('controller transition lease verification and release both failed') from verification_error
            raise
        if verification_error is not None:
            raise verification_error
        return False

def acquire_controller_transition_lock_v1(controller_root: str | os.PathLike[str], *, timeout_seconds: float=DEFAULT_CONTROLLER_TRANSITION_TIMEOUT_SECONDS_V1, recovery_journal_kind: ControllerRecoveryJournalKindV1 | None=None) -> ControllerTransitionLeaseV1:
    """Acquire the exact controller's global mutex within a bounded timeout.

    Root policy and identity are checked before waiting and again immediately
    after ownership is obtained.  If the second observation differs, ownership
    is released before the integrity error is raised.
    """
    timeout = _validate_timeout_v1(timeout_seconds)
    recovery_kind = _validate_recovery_journal_kind_v1(recovery_journal_kind)
    before = _observe_controller_root_v1(controller_root)
    mutex_name = _mutex_name_from_normalized_root_v1(before.normalized_root)
    handle = _create_windows_mutex_handle_v1(mutex_name)
    owns_mutex = False
    try:
        abandoned = _wait_for_windows_mutex_v1(handle, timeout)
        owns_mutex = True
        after = _observe_controller_root_v1(before.resolved_root)
        _require_same_observation_v1(expected_root=before.resolved_root, expected_normalized=before.normalized_root, expected_identity=before.identity, current=after, phase='post-acquire verification')
        _require_recovery_journal_authority_v1(before.resolved_root, recovery_journal_kind=recovery_kind)
        return ControllerTransitionLeaseV1(controller_root=before.resolved_root, normalized_controller_root=before.normalized_root, mutex_name=mutex_name, root_identity=before.identity, abandoned=abandoned, _mutex_handle=handle, _owner_thread_id=threading.get_ident())
    except BaseException as original:
        try:
            if owns_mutex:
                _release_windows_mutex_v1(handle)
            else:
                _close_windows_handle_v1(handle)
        except BaseException as cleanup_error:
            raise ControllerTransitionLockIntegrityErrorV1('controller transition mutex cleanup failed after acquisition error') from cleanup_error
        raise original

def _validate_recovery_journal_kind_v1(value: ControllerRecoveryJournalKindV1 | None) -> ControllerRecoveryJournalKindV1 | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _KNOWN_RECOVERY_JOURNAL_KINDS_V1:
        raise ControllerTransitionLockErrorV1('controller transition recovery journal kind is unsupported')
    return value

def _require_recovery_journal_authority_v1(controller_root: Path, *, recovery_journal_kind: ControllerRecoveryJournalKindV1 | None) -> None:
    pending = _pending_recovery_journal_kinds_v1(controller_root)
    if not pending:
        return
    if recovery_journal_kind is None:
        raise ControllerTransitionRecoveryPendingErrorV1('controller transition is blocked by unresolved recovery journal(s): ' + ', '.join(sorted(pending)))
    unexpected = pending - {recovery_journal_kind}
    if unexpected:
        raise ControllerTransitionRecoveryPendingErrorV1('controller recovery entrypoint does not own pending journal kind(s): ' + ', '.join(sorted(unexpected)))

def _pending_recovery_journal_kinds_v1(controller_root: Path) -> set[str]:
    pending: set[str] = set()
    state_root = _optional_plain_child_directory_v1(controller_root, '.questmemory', label='controller state root')
    if state_root is None:
        return pending
    recovery_root = _optional_plain_child_directory_v1(state_root, 'RECOVERY', label='controller recovery root')
    if recovery_root is not None:
        snapshot_recovery = _optional_plain_child_directory_v1(recovery_root, 'SNAPSHOT_RESTORE_V1', label='snapshot restore recovery namespace')
        if snapshot_recovery is not None:
            active = snapshot_recovery / 'ACTIVE'
            try:
                active_metadata = active.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ControllerTransitionLockIntegrityErrorV1('snapshot restore recovery journal cannot be inspected') from exc
            else:
                if not stat.S_ISDIR(active_metadata.st_mode) or _metadata_is_link_or_reparse_v1(active_metadata) or active.resolve(strict=True).parent != snapshot_recovery.resolve(strict=True):
                    raise ControllerTransitionLockIntegrityErrorV1('snapshot restore recovery journal path is unsafe')
                pending.add('snapshot_restore')
    transitions = _optional_plain_child_directory_v1(state_root, 'TRANSITIONS', label='controller transition journal namespace')
    if transitions is not None:
        try:
            children = tuple(transitions.iterdir())
        except OSError as exc:
            raise ControllerTransitionLockIntegrityErrorV1('controller transition journal namespace cannot be listed') from exc
        for child in children:
            folded = child.name.casefold()
            if not (folded.startswith('contribution-materialization-') and folded.endswith('.json')):
                continue
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise ControllerTransitionLockIntegrityErrorV1('contribution materialization journal cannot be inspected') from exc
            if not stat.S_ISREG(metadata.st_mode) or _metadata_is_link_or_reparse_v1(metadata) or int(metadata.st_nlink) != 1 or (child.resolve(strict=True).parent != transitions.resolve(strict=True)):
                raise ControllerTransitionLockIntegrityErrorV1('contribution materialization journal must be a distinct regular file')
            pending.add('contribution_materialization')
    return pending

def _optional_plain_child_directory_v1(parent: Path, name: str, *, label: str) -> Path | None:
    candidate = parent / name
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControllerTransitionLockIntegrityErrorV1(f'{label} cannot be inspected') from exc
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_link_or_reparse_v1(metadata):
        raise ControllerTransitionLockIntegrityErrorV1(f'{label} must be a plain directory')
    try:
        direct_parent = candidate.resolve(strict=True).parent
        expected_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ControllerTransitionLockIntegrityErrorV1(f'{label} identity cannot be resolved') from exc
    if direct_parent != expected_parent:
        raise ControllerTransitionLockIntegrityErrorV1(f'{label} escaped its exact parent')
    return candidate

def _validate_timeout_v1(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ControllerTransitionLockErrorV1('controller transition timeout must be a finite number')
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0.0 or timeout > MAX_CONTROLLER_TRANSITION_TIMEOUT_SECONDS_V1:
        raise ControllerTransitionLockErrorV1('controller transition timeout is outside the bounded policy')
    return timeout

def _observe_controller_root_v1(value: str | os.PathLike[str] | Path) -> _ControllerRootObservationV1:
    if os.name != 'nt':
        raise ControllerTransitionLockPlatformErrorV1('Controller Transition Lock V1 requires Windows kernel mutex semantics')
    try:
        candidate = Path(value)
    except (TypeError, ValueError) as exc:
        raise ControllerTransitionLockPathErrorV1('controller root must be an explicit local path') from exc
    raw = str(candidate)
    if not candidate.is_absolute() or not candidate.anchor or raw.startswith(('\\\\', '//')) or (not is_approved_windows_project_path(candidate)):
        raise ControllerTransitionLockPathErrorV1('controller root must be an approved absolute local storage path')
    if '..' in candidate.parts:
        raise ControllerTransitionLockPathErrorV1('controller root must not contain parent traversal')
    _require_non_reparse_directory_chain_v1(candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ControllerTransitionLockPathErrorV1('controller root must already exist and resolve locally') from exc
    if not resolved.is_absolute() or not is_approved_windows_project_path(resolved) or str(resolved).startswith(('\\\\', '//')):
        raise ControllerTransitionLockPathErrorV1('controller root resolved outside the approved local storage policy')
    _require_non_reparse_directory_chain_v1(candidate)
    _require_non_reparse_directory_chain_v1(resolved)
    try:
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ControllerTransitionLockPathErrorV1('controller root identity could not be observed') from exc
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_link_or_reparse_v1(metadata):
        raise ControllerTransitionLockPathErrorV1('controller root must be a regular non-reparse directory')
    if int(metadata.st_ino) <= 0:
        raise ControllerTransitionLockPathErrorV1('controller root has no stable filesystem identity')
    normalized = unicodedata.normalize('NFC', os.path.normcase(os.path.normpath(str(resolved))))
    identity = (int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mode))
    return _ControllerRootObservationV1(resolved_root=resolved, normalized_root=normalized, identity=identity)

def _require_non_reparse_directory_chain_v1(path: Path) -> None:
    current = Path(path.anchor)
    paths = [current]
    for part in path.parts[1:]:
        current = current / part
        paths.append(current)
    for component in paths:
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise ControllerTransitionLockPathErrorV1('controller root contains a missing or unreadable component') from exc
        if _metadata_is_link_or_reparse_v1(metadata):
            raise ControllerTransitionLockPathErrorV1('controller root contains a link or reparse component')
        if not stat.S_ISDIR(metadata.st_mode):
            raise ControllerTransitionLockPathErrorV1('controller root path component is not a directory')

def _metadata_is_link_or_reparse_v1(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, 'st_file_attributes', 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_FLAG)

def _require_same_observation_v1(*, expected_root: Path, expected_normalized: str, expected_identity: tuple[int, int, int], current: _ControllerRootObservationV1, phase: str) -> None:
    if current.resolved_root != expected_root or current.normalized_root != expected_normalized or current.identity != expected_identity:
        raise ControllerTransitionLockIntegrityErrorV1(f'controller root identity changed during {phase}')

def _mutex_name_from_normalized_root_v1(normalized_root: str) -> str:
    digest = hashlib.sha256(normalized_root.encode('utf-8')).hexdigest()
    return CONTROLLER_TRANSITION_MUTEX_PREFIX_V1 + digest

def _create_windows_mutex_handle_v1(name: str) -> int:
    kernel32 = _windows_kernel32_v1()
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(None, 0, name)
    if not handle:
        error = ctypes.get_last_error()
        raise ControllerTransitionLockErrorV1(f'Windows controller transition mutex could not be created ({error})')
    return int(handle)

def _wait_for_windows_mutex_v1(handle: int, timeout_seconds: float) -> bool:
    kernel32 = _windows_kernel32_v1()
    wait_for_single = kernel32.WaitForSingleObject
    wait_for_single.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    wait_for_single.restype = ctypes.c_uint32
    wait_ms = min(int(math.ceil(timeout_seconds * 1000.0)), int(MAX_CONTROLLER_TRANSITION_TIMEOUT_SECONDS_V1 * 1000.0))
    result = int(wait_for_single(ctypes.c_void_p(handle), wait_ms))
    if result == _WAIT_OBJECT_0:
        return False
    if result == _WAIT_ABANDONED_0:
        return True
    if result == _WAIT_TIMEOUT:
        raise ControllerTransitionLockTimeoutErrorV1('controller transition mutex acquisition timed out')
    if result == _WAIT_FAILED:
        error = ctypes.get_last_error()
        raise ControllerTransitionLockErrorV1(f'Windows controller transition mutex wait failed ({error})')
    raise ControllerTransitionLockErrorV1('Windows controller transition mutex returned an unexpected wait state')

def _release_windows_mutex_v1(handle: int) -> None:
    kernel32 = _windows_kernel32_v1()
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (ctypes.c_void_p,)
    release_mutex.restype = ctypes.c_int
    release_error: int | None = None
    try:
        if not release_mutex(ctypes.c_void_p(handle)):
            release_error = ctypes.get_last_error()
    finally:
        _close_windows_handle_v1(handle)
    if release_error is not None:
        raise ControllerTransitionLockIntegrityErrorV1(f'Windows controller transition mutex release failed ({release_error})')

def _close_windows_handle_v1(handle: int) -> None:
    kernel32 = _windows_kernel32_v1()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    if not close_handle(ctypes.c_void_p(handle)):
        error = ctypes.get_last_error()
        raise ControllerTransitionLockIntegrityErrorV1(f'Windows controller transition mutex handle close failed ({error})')

def _windows_kernel32_v1() -> object:
    if os.name != 'nt':
        raise ControllerTransitionLockPlatformErrorV1('Controller Transition Lock V1 requires Windows')
    return ctypes.WinDLL('kernel32', use_last_error=True)
