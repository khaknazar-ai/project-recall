# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from projectrecall.controller_transition_lock_v1 import ControllerTransitionLeaseV1
from projectrecall.controller_transition_lock_v1 import ControllerTransitionLockErrorV1
from projectrecall.durable_io import DURABLE_WRITE_LOCK
from projectrecall.operational_truth_findings import validate_operational_truth_root
from projectrecall.project_bootstrap_v1 import FutureProjectBootstrapResultV1
from projectrecall.storage_policy import is_approved_windows_project_path
SCHEMA_VERSION = 1
MARKER_KIND = 'future_project_controller_marker_v1'
MARKER_FILE = '.questmemory-controller-v1.json'
MAX_MARKER_BYTES = 64 * 1024

class FutureProjectControllerMarkerError(RuntimeError):
    """The durable future-controller selector witness is absent or invalid."""

@dataclass(frozen=True, slots=True)
class FutureProjectControllerMarkerV1:
    path: Path
    sha256: str
    bootstrap_id: str
    bootstrap_spec_sha256: str
    bootstrap_receipt_sha256: str
    managed_target_root: Path

    @property
    def binding(self) -> tuple[str, str, str, str]:
        return (self.bootstrap_id, self.bootstrap_spec_sha256, self.bootstrap_receipt_sha256, str(self.managed_target_root))

def _ensure_future_project_controller_marker_under_transition_lock_v1(bootstrap: FutureProjectBootstrapResultV1, *, transition_lease: ControllerTransitionLeaseV1) -> FutureProjectControllerMarkerV1:
    """Ensure the marker while the caller owns the controller transition."""
    root = validate_operational_truth_root(bootstrap.controller_root)
    _require_marker_transition_lease_v1(root, transition_lease)
    if root != bootstrap.controller_root:
        raise FutureProjectControllerMarkerError('future-controller marker root differs from bootstrap root')
    payload = {'schema_version': SCHEMA_VERSION, 'marker_kind': MARKER_KIND, 'bootstrap_id': bootstrap.bootstrap_id, 'bootstrap_spec_sha256': bootstrap.spec_sha256, 'bootstrap_receipt_sha256': bootstrap.receipt_sha256, 'managed_target_root': str(bootstrap.managed_target_root), 'authority_granted': False}
    expected = _pretty_json(payload)
    path = root / MARKER_FILE
    transition_lease.verify()
    with DURABLE_WRITE_LOCK:
        try:
            existing = _read_marker_bytes(path, allow_absent=True)
            if existing is None:
                _publish_create_only(path, expected)
                existing = _read_marker_bytes(path, allow_absent=False)
            if existing != expected:
                raise FutureProjectControllerMarkerError('future-controller marker already exists with different bytes')
            marker = _verify_future_project_controller_marker_under_transition_lock_v1(root, transition_lease=transition_lease)
        except FutureProjectControllerMarkerError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise FutureProjectControllerMarkerError('future-controller marker could not be published safely') from exc
    transition_lease.verify()
    if marker.binding != (bootstrap.bootstrap_id, bootstrap.spec_sha256, bootstrap.receipt_sha256, str(bootstrap.managed_target_root)):
        raise FutureProjectControllerMarkerError('future-controller marker differs from bootstrap identity')
    return marker

def _publish_create_only(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.future-controller-marker-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    except OSError as exc:
        raise FutureProjectControllerMarkerError('future-controller marker create-only publication failed') from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

def _verify_future_project_controller_marker_under_transition_lock_v1(controller_root: str | Path, *, transition_lease: ControllerTransitionLeaseV1) -> FutureProjectControllerMarkerV1:
    """Verify the marker without reacquiring an already-held transition."""
    root = validate_operational_truth_root(controller_root)
    _require_marker_transition_lease_v1(root, transition_lease)
    path = root / MARKER_FILE
    raw = _read_marker_bytes(path, allow_absent=False)
    assert raw is not None
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FutureProjectControllerMarkerError('future-controller marker is not valid UTF-8 JSON') from exc
    if not isinstance(value, dict) or set(value) != {'schema_version', 'marker_kind', 'bootstrap_id', 'bootstrap_spec_sha256', 'bootstrap_receipt_sha256', 'managed_target_root', 'authority_granted'}:
        raise FutureProjectControllerMarkerError('future-controller marker has an invalid schema')
    if _pretty_json(value) != raw:
        raise FutureProjectControllerMarkerError('future-controller marker bytes are not canonical')
    if value['schema_version'] != SCHEMA_VERSION or value['marker_kind'] != MARKER_KIND or value['authority_granted'] is not False:
        raise FutureProjectControllerMarkerError('future-controller marker kind or authority is invalid')
    bootstrap_id = _identifier(value['bootstrap_id'], 'bootstrap_id')
    if not bootstrap_id.startswith('future-project-bootstrap-v1-'):
        raise FutureProjectControllerMarkerError('future-controller marker bootstrap identity is invalid')
    spec_sha256 = _digest(value['bootstrap_spec_sha256'], 'spec SHA-256')
    receipt_sha256 = _digest(value['bootstrap_receipt_sha256'], 'receipt SHA-256')
    managed_target = _absolute_d_root(value['managed_target_root'], 'managed_target_root')
    marker = FutureProjectControllerMarkerV1(path=path, sha256=hashlib.sha256(raw).hexdigest(), bootstrap_id=bootstrap_id, bootstrap_spec_sha256=spec_sha256, bootstrap_receipt_sha256=receipt_sha256, managed_target_root=managed_target)
    transition_lease.verify()
    return marker

def _require_marker_transition_lease_v1(controller_root: Path, transition_lease: ControllerTransitionLeaseV1) -> None:
    transition_lease.verify()
    if transition_lease.controller_root != controller_root:
        raise ControllerTransitionLockErrorV1('future-controller marker lease is bound to a different root')

def _read_marker_bytes(path: Path, *, allow_absent: bool) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        if allow_absent:
            return None
        raise FutureProjectControllerMarkerError('future-controller marker is missing')
    except OSError as exc:
        raise FutureProjectControllerMarkerError('future-controller marker cannot be inspected') from exc
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)
    if stat.S_ISLNK(before.st_mode) or bool(getattr(before, 'st_file_attributes', 0) & reparse_flag) or (not stat.S_ISREG(before.st_mode)) or (before.st_nlink != 1) or (before.st_size > MAX_MARKER_BYTES):
        raise FutureProjectControllerMarkerError('future-controller marker is not a bounded distinct regular file')
    identity = _identity(before)
    try:
        with path.open('rb') as stream:
            if _identity(os.fstat(stream.fileno())) != identity:
                raise FutureProjectControllerMarkerError('future-controller marker identity changed')
            raw = stream.read(MAX_MARKER_BYTES + 1)
            if _identity(os.fstat(stream.fileno())) != identity:
                raise FutureProjectControllerMarkerError('future-controller marker changed while read')
        after = path.lstat()
    except FutureProjectControllerMarkerError:
        raise
    except OSError as exc:
        raise FutureProjectControllerMarkerError('future-controller marker cannot be read') from exc
    if len(raw) != before.st_size or _identity(after) != identity:
        raise FutureProjectControllerMarkerError('future-controller marker bytes or identity changed')
    return raw

def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_nlink, int(getattr(metadata, 'st_file_attributes', 0)))

def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or (len(value) > 160) or any((ord(character) < 32 or ord(character) == 127 for character in value)):
        raise FutureProjectControllerMarkerError(f'future-controller marker {label} is invalid')
    return value

def _digest(value: Any, label: str) -> str:
    result = _identifier(value, label)
    if len(result) != 64 or any((character not in '0123456789abcdef' for character in result)):
        raise FutureProjectControllerMarkerError(f'future-controller marker {label} is invalid')
    return result

def _absolute_d_root(value: Any, label: str) -> Path:
    text = _identifier(value, label)
    path = Path(text)
    if not path.is_absolute() or '..' in path.parts:
        raise FutureProjectControllerMarkerError(f'future-controller marker {label} is not absolute')
    if os.name == 'nt' and (not is_approved_windows_project_path(path)):
        raise FutureProjectControllerMarkerError(f'future-controller marker {label} is outside approved project storage')
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FutureProjectControllerMarkerError(f'future-controller marker {label} cannot be resolved') from exc
    if os.name == 'nt' and (not is_approved_windows_project_path(resolved)):
        raise FutureProjectControllerMarkerError(f'future-controller marker {label} resolves outside approved project storage')
    return resolved

def _pretty_json(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise FutureProjectControllerMarkerError('future-controller marker cannot be encoded canonically') from exc
