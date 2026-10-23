# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import json
import math
from typing import Any

class StrictJsonError(json.JSONDecodeError):
    """Value-free JSON error: never include a supplied key, value or document."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category, '', 0)

def strict_json_loads(raw: str | bytes, *, reject_duplicates: bool=True) -> Any:
    """Decode unambiguous finite JSON; valid historical JSON remains unchanged."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if reject_duplicates and key in result:
                raise StrictJsonError('duplicate_json_key')
            result[key] = value
        return result

    def constant(_value: str) -> Any:
        raise StrictJsonError('nonfinite_json_number')

    def number(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise StrictJsonError('nonfinite_json_number')
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except StrictJsonError:
        raise
    except (ValueError, RecursionError) as exc:
        raise StrictJsonError('invalid_json_syntax_or_capacity') from exc

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()
