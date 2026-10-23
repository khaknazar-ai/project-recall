# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import re
from collections.abc import Iterator
_FENCE_RE = re.compile('^ {0,3}(`{3,}|~{3,})(.*)$')

def _unfenced_lines(text: str) -> Iterator[tuple[int, str]]:
    fence_character: str | None = None
    fence_length = 0
    for index, line in enumerate(text.splitlines()):
        match = _FENCE_RE.match(line)
        if match:
            run, tail = match.groups()
            if fence_character is None:
                if run[0] == '`' and '`' in tail:
                    yield (index, line)
                    continue
                fence_character, fence_length = (run[0], len(run))
            elif run[0] == fence_character and len(run) >= fence_length and (not tail.strip()):
                fence_character = None
            continue
        if fence_character is None:
            yield (index, line)
