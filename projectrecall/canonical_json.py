# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import json
from typing import Any

class ReconciliationManifestError(RuntimeError):
    """The pending reconciliation manifest is unsafe, stale, or ambiguous."""

def canonical_json_bytes(value: Any) -> bytes:
    """Return the only accepted canonical JSON encoding.

    V1 accepts ordinary JSON values but rejects floats entirely.  Rejecting
    floats avoids cross-runtime number rendering differences and also rejects
    NaN and infinity before serialization.  Object keys must be strings.  The
    returned bytes are UTF-8, compact, sorted by key, and have no trailing LF.
    """
    _validate_json_value(value, path='$', seen=set())
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        raw = rendered.encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReconciliationManifestError('value cannot be encoded as canonical UTF-8 JSON') from exc
    if raw.endswith(b'\n') or raw.startswith(b'\xef\xbb\xbf'):
        raise ReconciliationManifestError('canonical JSON byte policy changed')
    return raw

def canonical_json_sha256(value: Any) -> str:
    """Return SHA-256 over :func:`canonical_json_bytes`."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

def _validate_json_value(value: Any, *, path: str, seen: set[int]) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, float):
        raise ReconciliationManifestError(f'floating-point JSON is forbidden at {path}')
    if isinstance(value, int):
        return
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            raise ReconciliationManifestError(f'cyclic JSON list at {path}')
        seen.add(identity)
        try:
            for index, item in enumerate(value):
                _validate_json_value(item, path=f'{path}[{index}]', seen=seen)
        finally:
            seen.remove(identity)
        return
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            raise ReconciliationManifestError(f'cyclic JSON object at {path}')
        seen.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ReconciliationManifestError(f'non-string JSON object key at {path}')
                _validate_json_value(item, path=f'{path}.{key}', seen=seen)
        finally:
            seen.remove(identity)
        return
    raise ReconciliationManifestError(f'unsupported JSON value type at {path}: {type(value).__name__}')
