# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import re
from typing import Any

def _fail(message: str) -> None:
    from projectrecall.future_project_agent_execution_v1 import FutureProjectAgentExecutionErrorV1
    raise FutureProjectAgentExecutionErrorV1(f'Future authority reference: {message}')

def _digest(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        _fail('invalid SHA-256')
    return value
