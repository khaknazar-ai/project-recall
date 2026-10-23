# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import os
import stat
from pathlib import Path
from projectrecall.paths import PROJECT_ROOT_ENV

class OperationalTruthError(RuntimeError):
    """Source identity, freshness, or report integrity cannot be established."""

def validate_operational_truth_root(project_root: str | Path | None=None) -> Path:
    """Validate explicit, environment, or current root before resolving aliases.

    This is a path-safety check, not an operational truth or authority decision.
    Call it before config loaders that would otherwise erase lexical evidence.
    """
    selected = project_root
    if selected is None:
        selected = os.environ.get(PROJECT_ROOT_ENV) or Path.cwd()
    return _safe_root(selected)

def _safe_root(project_root: str | Path) -> Path:
    raw = str(project_root)
    if raw.startswith(('\\\\', '//')):
        raise OperationalTruthError('network/device project-root paths are not permitted')
    supplied = Path(project_root)
    if '..' in supplied.parts:
        raise OperationalTruthError('project-root parent traversal is not permitted')
    absolute = supplied.absolute()
    try:
        for component in reversed((absolute, *absolute.parents)):
            metadata = component.lstat()
            reparse = getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
            if stat.S_ISLNK(metadata.st_mode) or reparse:
                raise OperationalTruthError('project-root path contains a link/reparse component')
            if not stat.S_ISDIR(metadata.st_mode):
                raise OperationalTruthError('project-root component is not a directory')
        resolved = absolute.resolve(strict=True)
        if resolved != absolute:
            raise OperationalTruthError('project-root identity changed during resolution')
        return resolved
    except OperationalTruthError:
        raise
    except OSError as exc:
        raise OperationalTruthError('project root cannot be resolved safely') from exc
