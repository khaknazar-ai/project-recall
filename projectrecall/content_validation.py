# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import re
from pathlib import PurePosixPath
from typing import Any
from projectrecall.future_project_live_authority_v2 import _digest

def _context_fact(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {'fact_id', 'statement', 'source_reference', 'source_sha256', 'status', 'audience'} or (not all((isinstance(item, str) for item in value.values()))):
        _fail('invalid context fact fields')
    _id(value['fact_id'], 'context fact ID')
    if value['status'] not in {'current', 'superseded'} or value['audience'] not in {'brain', 'shared', 'launcher'}:
        _fail('invalid context fact lifecycle or audience')
    statement, reference = (value['statement'], value['source_reference'])
    if not statement.strip() or len(statement.encode('utf-8')) > 800 or '\x00' in statement:
        _fail('invalid context statement size')
    if not reference or len(reference) > 240 or '\\' in reference or (':' in reference) or PurePosixPath(reference).is_absolute() or any((part in {'', '.', '..'} for part in reference.split('/'))) or any((ord(char) < 32 for char in reference)):
        _fail('invalid context source reference')
    _digest(value['source_sha256'])
    if re.search('(?i)(?:api[_-]?key|access[_-]?token|password|secret|credential|aws[_-]?(?:secret[_-]?access[_-]?key|session[_-]?token|access[_-]?key[_-]?id))(?:\\\\*[\'\\"])?\\s*[:=]|\\bbearer\\s+\\S+|\\b(?:sk-|ghp_|github_pat_|xox[baprs]-|glpat-)[a-z0-9_-]{8,}|\\b(?:AKIA|ASIA)[A-Z0-9]{16}\\b|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|\\beyJ[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\b', '\n'.join(value.values())):
        _fail('context fact contains credential-like material')
    return dict(value)

def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', value):
        _fail(f'invalid {label}')
    return value

def _fail(message):
    raise ValueError(message)
