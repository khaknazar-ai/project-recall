# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence
from projectrecall.controller_transition_lock_v1 import ControllerTransitionLeaseV1
from projectrecall.controller_transition_lock_v1 import ControllerTransitionLockErrorV1
from projectrecall.controller_transition_lock_v1 import acquire_controller_transition_lock_v1
from projectrecall.runtime_command import questmemory_powershell_command
from projectrecall.storage_policy import is_approved_windows_project_path
SCHEMA_VERSION = 1
BOOTSTRAP_KIND = 'future_project_bootstrap_v1'
PENDING_APPROVAL = 'pending_human_approval'
QUESTMEMORY_DIRECTORY = '.questmemory'
RECEIPTS_DIRECTORY = 'BOOTSTRAP_RECEIPTS'
RECEIPT_FILE = 'receipt.json'
RECEIPT_INTEGRITY_STORAGE = 'validation recomputes the exact receipt SHA-256 from stable BOOTSTRAP_RECEIPTS/receipt.json bytes'
OPERATIONAL_SOURCE_PATHS = ('PROJECT_CHARTER.md', 'CURRENT_TASK.md', 'TASKS.md', 'PLAN/quest_tree.json', 'PROJECT_STATE_CAPSULE.md', 'STATUS.md', 'DECISIONS.md', 'CONSTRAINTS.md', 'SIDE_QUESTS.md', 'COMPLETED_TASKS.md', 'WORKING_COMMANDS.md', 'EVIDENCE_LOG.md')
_SAFE_ID_RE = re.compile('^[a-z0-9][a-z0-9._:-]{0,127}$')
_STAGING_PREFIX = '.qmb1-'

class FutureProjectBootstrapError(RuntimeError):
    """Base error for the fail-closed Future Project Bootstrap V1 boundary."""

class FutureProjectBootstrapSpecError(FutureProjectBootstrapError):
    """The proposed project identity or initial task is invalid."""

class FutureProjectBootstrapPathPolicyError(FutureProjectBootstrapError):
    """A controller or target root violates the current local host policy."""

class FutureProjectBootstrapConflictError(FutureProjectBootstrapError):
    """Existing project state prevents a new bootstrap from being materialized."""

class FutureProjectBootstrapIntegrityError(FutureProjectBootstrapError):
    """A prior bootstrap cannot be replayed because its exact bytes drifted."""

@dataclass(frozen=True, slots=True)
class FutureProjectInitialTaskV1:
    task_id: str
    title: str
    expected_output: str
    done_criteria: tuple[str, ...]
    do_not_do: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class FutureProjectBootstrapSpecV1:
    project_id: str
    name: str
    objective: str
    controller_root: str | Path
    managed_target_root: str | Path
    initial_task: FutureProjectInitialTaskV1

@dataclass(frozen=True, slots=True)
class BootstrapFileDigestV1:
    relative_path: str
    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, str | int]:
        return {'path': self.relative_path, 'sha256': self.sha256, 'size_bytes': self.size_bytes}

@dataclass(frozen=True, slots=True)
class FutureProjectBootstrapResultV1:
    bootstrap_id: str
    spec_sha256: str
    controller_root: Path
    managed_target_root: Path
    receipt_path: Path
    receipt_sha256: str
    files: tuple[BootstrapFileDigestV1, ...]
    replayed: bool
    approval_status: str = PENDING_APPROVAL
    authority_granted: bool = False

    @property
    def operational_files(self) -> tuple[BootstrapFileDigestV1, ...]:
        return tuple((item for item in self.files if not item.relative_path.startswith(f'{QUESTMEMORY_DIRECTORY}/{RECEIPTS_DIRECTORY}/')))

@dataclass(frozen=True, slots=True)
class VerifiedBootstrapReceiptV1:
    """Exact immutable bootstrap receipt, independent of later source state."""
    bootstrap_id: str
    spec_sha256: str
    project_id: str
    name: str
    objective: str
    controller_root: Path
    managed_target_root: Path
    receipt_path: Path
    receipt_sha256: str
    initial_task: FutureProjectInitialTaskV1
    operational_files: tuple[BootstrapFileDigestV1, ...]

@dataclass(frozen=True, slots=True)
class _ValidatedBootstrapSpecV1:
    project_id: str
    name: str
    objective: str
    controller_root: Path
    managed_target_root: Path
    initial_task: FutureProjectInitialTaskV1

    def canonical_payload(self) -> dict[str, Any]:
        return {'schema_version': SCHEMA_VERSION, 'project_id': self.project_id, 'name': self.name, 'objective': self.objective, 'controller_root': str(self.controller_root), 'managed_target_root': str(self.managed_target_root), 'initial_task': {'task_id': self.initial_task.task_id, 'title': self.initial_task.title, 'expected_output': self.initial_task.expected_output, 'done_criteria': list(self.initial_task.done_criteria), 'do_not_do': list(self.initial_task.do_not_do)}}

def prepare_future_project_bootstrap_v1(spec: FutureProjectBootstrapSpecV1) -> FutureProjectBootstrapResultV1:
    """Prepare a project-neutral, non-authoritative controller state atomically.

    The managed target is read only.  All controller operational sources are
    staged below the controller root and become visible together when the
    staged directory is renamed to ``.questmemory``.  A byte-identical replay
    returns the existing receipt without touching any file; any other existing
    state fails closed.
    """
    validated = _validate_spec(spec)
    spec_bytes = _canonical_json_bytes(validated.canonical_payload())
    spec_sha256 = _sha256(spec_bytes)
    bootstrap_id = f'future-project-bootstrap-v1-{spec_sha256[:24]}'
    operational_bytes = _render_operational_sources(validated, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256)
    operational_digests = tuple((_digest_for(relative_path, operational_bytes[relative_path]) for relative_path in OPERATIONAL_SOURCE_PATHS))
    receipt_payload = _receipt_payload(validated, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, operational_digests=operational_digests)
    receipt_bytes = _pretty_json_bytes(receipt_payload)
    receipt_sha256 = _sha256(receipt_bytes)
    receipt_relative_path = f'{RECEIPTS_DIRECTORY}/{RECEIPT_FILE}'
    expected_files: dict[str, bytes] = {**operational_bytes, receipt_relative_path: receipt_bytes}
    try:
        transition_lock = acquire_controller_transition_lock_v1(validated.controller_root)
    except ControllerTransitionLockErrorV1 as exc:
        raise FutureProjectBootstrapConflictError(f'controller transition serialization failed ({exc})') from exc
    with transition_lock as transition_lease:
        _revalidate_roots(validated)
        transition_lease.verify()
        controller_identity = _directory_identity(validated.controller_root)
        target_identity = _directory_identity(validated.managed_target_root)
        _assert_managed_target_has_no_operational_sources(validated)
        questmemory_dir = validated.controller_root / QUESTMEMORY_DIRECTORY
        if _lexists(questmemory_dir):
            result = _validate_exact_replay(validated, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, receipt_sha256=receipt_sha256, receipt_relative_path=receipt_relative_path, expected_files=expected_files, operational_digests=operational_digests)
            _require_directory_identity(validated.controller_root, controller_identity)
            _require_directory_identity(validated.managed_target_root, target_identity)
            _assert_managed_target_has_no_operational_sources(validated)
            transition_lease.verify()
            return result
        _assert_no_abandoned_staging(validated.controller_root)
        staging_dir = Path(tempfile.mkdtemp(prefix=f'{_STAGING_PREFIX}{spec_sha256[:12]}-', dir=validated.controller_root))
        try:
            for relative_path, content in expected_files.items():
                _write_staged_file(staging_dir, relative_path, content)
            _verify_exact_tree(staging_dir, expected_files)
            _revalidate_roots(validated)
            _require_directory_identity(validated.controller_root, controller_identity)
            _require_directory_identity(validated.managed_target_root, target_identity)
            _assert_managed_target_has_no_operational_sources(validated)
            try:
                _commit_staged_directory(staging_dir, questmemory_dir)
            except OSError:
                if not _lexists(questmemory_dir):
                    raise
                replay = _validate_exact_replay(validated, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, receipt_sha256=receipt_sha256, receipt_relative_path=receipt_relative_path, expected_files=expected_files, operational_digests=operational_digests)
                _require_directory_identity(validated.controller_root, controller_identity)
                _require_directory_identity(validated.managed_target_root, target_identity)
                _assert_managed_target_has_no_operational_sources(validated)
                return replay
            try:
                _revalidate_roots(validated)
                _require_directory_identity(validated.controller_root, controller_identity)
                _require_directory_identity(validated.managed_target_root, target_identity)
                _assert_managed_target_has_no_operational_sources(validated)
                _verify_exact_tree(questmemory_dir, expected_files)
            except Exception:
                _rollback_owned_publication(questmemory_dir, expected_files)
                raise
        finally:
            if _lexists(staging_dir):
                _remove_owned_staging(staging_dir, validated.controller_root)
        transition_lease.verify()
        return _result(validated, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, receipt_sha256=receipt_sha256, receipt_relative_path=receipt_relative_path, operational_digests=operational_digests, replayed=False)

def verify_bootstrap_receipt_v1(controller_root: str | Path) -> VerifiedBootstrapReceiptV1:
    """Verify the receipt under the root transition mutex."""
    controller = _validate_d_local_root(controller_root, 'controller_root')
    try:
        with acquire_controller_transition_lock_v1(controller) as transition_lease:
            return _verify_bootstrap_receipt_under_transition_lock_v1(controller, transition_lease=transition_lease)
    except ControllerTransitionLockErrorV1 as exc:
        raise FutureProjectBootstrapIntegrityError(f'bootstrap receipt transition serialization failed ({exc})') from exc

def _verify_bootstrap_receipt_under_transition_lock_v1(controller_root: str | Path, *, transition_lease: ControllerTransitionLeaseV1) -> VerifiedBootstrapReceiptV1:
    """Verify the immutable pending receipt without requiring pending post-state.

    Activation is an authorized successor to the pending operational files, so
    the original receipt must remain independently verifiable after those files
    advance.  This function reconstructs the receipt and its pre-state digests
    from the exact embedded specification; it deliberately grants no lifecycle
    authority by itself.
    """
    controller = _validate_d_local_root(controller_root, 'controller_root')
    _require_bootstrap_transition_lease_v1(controller, transition_lease)
    result = _verify_immutable_bootstrap_receipt_stable_v1(controller)
    transition_lease.verify()
    return result

def _verify_immutable_bootstrap_receipt_stable_v1(controller_root):
    """Internal read-only reconstruction for child→parent citations.

    No transition authority: immutable receipt bytes/identities are checked
    twice without taking a parent mutex while a child lease is held.
    Ordinary bootstrap/activation operations still require their root lease.
    """
    controller = _validate_d_local_root(controller_root, 'controller_root')
    controller_identity = _directory_identity(controller)
    questmemory_dir = controller / QUESTMEMORY_DIRECTORY
    if not questmemory_dir.is_dir() or _is_link_or_reparse(questmemory_dir):
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt requires a regular local .questmemory directory')
    questmemory_identity = _directory_identity(questmemory_dir)
    receipts_dir = questmemory_dir / RECEIPTS_DIRECTORY
    if not receipts_dir.is_dir() or _is_link_or_reparse(receipts_dir):
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt requires the stable receipt directory')
    receipts_identity = _directory_identity(receipts_dir)
    receipt_path = _require_exact_receipt_namespace(receipts_dir)
    receipt_bytes = _read_regular_file(receipt_path)
    receipt_sha256 = _sha256(receipt_bytes)
    try:
        receipt_payload = json.loads(receipt_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt is not valid UTF-8 JSON') from exc
    if not isinstance(receipt_payload, dict):
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt must be a JSON object')
    spec = _spec_from_receipt_payload(receipt_payload)
    validated = _validate_spec(spec)
    if validated.controller_root != controller:
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt is bound to a different controller root')
    target_identity = _directory_identity(validated.managed_target_root)
    _assert_managed_target_has_no_operational_sources(validated)
    spec_sha256 = _sha256(_canonical_json_bytes(validated.canonical_payload()))
    bootstrap_id = f'future-project-bootstrap-v1-{spec_sha256[:24]}'
    operational_bytes = _render_operational_sources(validated, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256)
    operational_digests = tuple((_digest_for(relative_path, operational_bytes[relative_path]) for relative_path in OPERATIONAL_SOURCE_PATHS))
    expected_receipt = _pretty_json_bytes(_receipt_payload(validated, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, operational_digests=operational_digests))
    if receipt_bytes != expected_receipt:
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt differs from the exact deterministic reconstruction')
    if receipt_sha256 != _sha256(expected_receipt):
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt SHA-256 differs from its exact bytes')
    _require_exact_receipt_namespace(receipts_dir)
    _revalidate_roots(validated)
    _require_directory_identity(controller, controller_identity)
    _require_directory_identity(questmemory_dir, questmemory_identity)
    _require_directory_identity(receipts_dir, receipts_identity)
    _require_directory_identity(validated.managed_target_root, target_identity)
    _assert_managed_target_has_no_operational_sources(validated)
    result = VerifiedBootstrapReceiptV1(bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, project_id=validated.project_id, name=validated.name, objective=validated.objective, controller_root=controller, managed_target_root=validated.managed_target_root, receipt_path=receipt_path, receipt_sha256=receipt_sha256, initial_task=validated.initial_task, operational_files=operational_digests)
    if _read_regular_file(receipt_path) != receipt_bytes:
        raise FutureProjectBootstrapIntegrityError('immutable bootstrap receipt changed during read')
    return result

def _require_bootstrap_transition_lease_v1(controller_root: Path, transition_lease: ControllerTransitionLeaseV1) -> None:
    transition_lease.verify()
    if transition_lease.controller_root != controller_root:
        raise ControllerTransitionLockErrorV1('bootstrap verifier lease is bound to a different controller root')

def _spec_from_receipt_payload(receipt_payload: Mapping[str, Any]) -> FutureProjectBootstrapSpecV1:
    spec_payload = receipt_payload.get('spec')
    if not isinstance(spec_payload, dict):
        raise FutureProjectBootstrapIntegrityError('pending bootstrap receipt has no exact spec object')
    task_payload = spec_payload.get('initial_task')
    if not isinstance(task_payload, dict):
        raise FutureProjectBootstrapIntegrityError('pending bootstrap receipt has no exact initial task object')
    done_criteria = task_payload.get('done_criteria')
    do_not_do = task_payload.get('do_not_do')
    if not isinstance(done_criteria, list) or not isinstance(do_not_do, list):
        raise FutureProjectBootstrapIntegrityError('pending bootstrap receipt task lists are malformed')
    return FutureProjectBootstrapSpecV1(project_id=spec_payload.get('project_id'), name=spec_payload.get('name'), objective=spec_payload.get('objective'), controller_root=spec_payload.get('controller_root'), managed_target_root=spec_payload.get('managed_target_root'), initial_task=FutureProjectInitialTaskV1(task_id=task_payload.get('task_id'), title=task_payload.get('title'), expected_output=task_payload.get('expected_output'), done_criteria=tuple(done_criteria), do_not_do=tuple(do_not_do)))

def _validate_spec(spec: FutureProjectBootstrapSpecV1) -> _ValidatedBootstrapSpecV1:
    if not isinstance(spec, FutureProjectBootstrapSpecV1):
        raise FutureProjectBootstrapSpecError('spec must be FutureProjectBootstrapSpecV1')
    normalized = _normalize_spec(spec, controller_root=spec.controller_root, managed_target_root=spec.managed_target_root)
    controller_root = _validate_d_local_root(spec.controller_root, 'controller_root')
    managed_target_root = _validate_d_local_root(spec.managed_target_root, 'managed_target_root')
    if _paths_overlap(controller_root, managed_target_root):
        raise FutureProjectBootstrapPathPolicyError('controller_root and managed_target_root must be separate, non-overlapping directories')
    result = replace(normalized, controller_root=controller_root, managed_target_root=managed_target_root)
    _required_drive_for_spec(result)
    return result

def _normalize_spec(spec, *, controller_root, managed_target_root):
    """Pure text normalization; live path admission remains in _validate_spec."""
    if not isinstance(spec, FutureProjectBootstrapSpecV1):
        raise FutureProjectBootstrapSpecError('spec must be FutureProjectBootstrapSpecV1')
    project_id = _clean_id(spec.project_id, 'project_id')
    name = _clean_line(spec.name, 'name', maximum=200)
    objective = _clean_markdown_paragraph(spec.objective, 'objective', maximum=4000)
    if not isinstance(spec.initial_task, FutureProjectInitialTaskV1):
        raise FutureProjectBootstrapSpecError('initial_task must be FutureProjectInitialTaskV1')
    task_id = _clean_id(spec.initial_task.task_id, 'initial_task.task_id')
    if task_id == 'main-quest':
        raise FutureProjectBootstrapSpecError('initial_task.task_id is reserved: main-quest')
    task = FutureProjectInitialTaskV1(task_id=task_id, title=_clean_line(spec.initial_task.title, 'initial_task.title', maximum=300), expected_output=_clean_line(spec.initial_task.expected_output, 'initial_task.expected_output', maximum=4000), done_criteria=_clean_lines(spec.initial_task.done_criteria, 'initial_task.done_criteria', maximum_items=32, item_maximum=1000, require_nonempty=True), do_not_do=_clean_lines(spec.initial_task.do_not_do, 'initial_task.do_not_do', maximum_items=32, item_maximum=1000, require_nonempty=False))
    return _ValidatedBootstrapSpecV1(project_id=project_id, name=name, objective=objective, controller_root=controller_root, managed_target_root=managed_target_root, initial_task=task)

def _validate_d_local_root(value: str | Path, label: str) -> Path:
    try:
        candidate = Path(value)
    except TypeError as exc:
        raise FutureProjectBootstrapPathPolicyError(f'{label} must be a local absolute path') from exc
    if not candidate.is_absolute():
        raise FutureProjectBootstrapPathPolicyError(f'{label} must be an absolute local path')
    if os.name != 'nt':
        raise FutureProjectBootstrapPathPolicyError('Future Project Bootstrap V1 requires the current Windows local host policy')
    if not is_approved_windows_project_path(candidate):
        raise FutureProjectBootstrapPathPolicyError(f'{label} must be inside approved project storage')
    if str(candidate).startswith(('\\\\', '//')):
        raise FutureProjectBootstrapPathPolicyError(f'{label} must be local; UNC paths are not allowed')
    _assert_no_link_or_reparse_components(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FutureProjectBootstrapPathPolicyError(f'{label} must already exist and resolve locally') from exc
    if not resolved.is_dir():
        raise FutureProjectBootstrapPathPolicyError(f'{label} must be an existing directory')
    if not is_approved_windows_project_path(resolved):
        raise FutureProjectBootstrapPathPolicyError(f'{label} resolved outside approved project storage')
    _assert_no_link_or_reparse_components(resolved, label)
    return resolved

def _revalidate_roots(spec: _ValidatedBootstrapSpecV1) -> None:
    _required_drive_for_spec(spec)
    for label, expected in (('controller_root', spec.controller_root), ('managed_target_root', spec.managed_target_root)):
        current = _validate_d_local_root(expected, label)
        if current != expected:
            raise FutureProjectBootstrapPathPolicyError(f'{label} changed while bootstrap was being prepared')
    if _paths_overlap(spec.controller_root, spec.managed_target_root):
        raise FutureProjectBootstrapPathPolicyError('controller_root and managed_target_root now overlap')

def _assert_managed_target_has_no_operational_sources(spec: _ValidatedBootstrapSpecV1) -> None:
    target_state = spec.managed_target_root / QUESTMEMORY_DIRECTORY
    if _lexists(target_state):
        raise FutureProjectBootstrapConflictError('managed_target_root already contains .questmemory; a central controller cannot create a second operational truth source')

def _render_operational_sources(spec: _ValidatedBootstrapSpecV1, *, bootstrap_id: str, spec_sha256: str) -> dict[str, bytes]:
    task = spec.initial_task
    authority_lines = _authority_markdown_lines()
    do_not_do = task.do_not_do or ('Do not execute the task before exact human approval.',)
    charter = '\n'.join(['# Project Charter', '', f'Schema: {BOOTSTRAP_KIND}', f'Bootstrap ID: `{bootstrap_id}`', f'Bootstrap Spec SHA-256: `{spec_sha256}`', f'Project ID: `{spec.project_id}`', f'Project: {spec.name}', f'Controller Root: `{spec.controller_root}`', f'Managed Target Root: `{spec.managed_target_root}`', f'Charter Status: {PENDING_APPROVAL}', 'Fact Type: working_assumption', '', '## Main Quest', '', spec.objective, '', '## Approval Boundary', '', 'This charter is a project-neutral proposal assembled from the bootstrap input. It is not an owner-approved Product Contract or Blueprint.', '', '## Initial Task', '', f'Task: {task.title}', f'Task ID: `{task.task_id}`', 'Status: planned', f'Approval Status: {PENDING_APPROVAL}', 'Execution Authorized: false', '', '## Authority', '', *authority_lines, ''])
    current_task = '\n'.join(['# Current Task', '', f'Bootstrap ID: `{bootstrap_id}`', f'Bootstrap Spec SHA-256: `{spec_sha256}`', f'Project ID: `{spec.project_id}`', f'Task: {task.title}', f'Task ID: `{task.task_id}`', f'Parent Quest: {spec.objective}', 'Parent Node ID: `main-quest`', 'Status: planned', f'Approval Status: {PENDING_APPROVAL}', 'Execution Authorized: false', '', '## Expected Output', '', f'- {task.expected_output}', '', '## Done Criteria', '', *[f'- {item}' for item in task.done_criteria], '', '## Do Not Do', '', *[f'- {item}' for item in do_not_do], '- Do not write to the managed target from this bootstrap.', '- Do not call a provider, hand off to Planner, or infer approval.', ''])
    tasks = '\n'.join([f'# {spec.name} Tasks', '', f'Bootstrap ID: `{bootstrap_id}`', f'Bootstrap Spec SHA-256: `{spec_sha256}`', f'Project ID: `{spec.project_id}`', f'Bootstrap Status: {PENDING_APPROVAL}', '', '## Active', '', 'No task is active. Human approval is pending.', '', '## Completed', '', 'No task is completed. Completion requires evidence.', '', '## Planned', '', f'* id: {task.task_id}', f'  title: {task.title}', '  status: planned', '  parent: main-quest', '  depends_on: none', '  source: .questmemory/CURRENT_TASK.md', '  evidence: pending', f'  approval_status: {PENDING_APPROVAL}', '  execution_authorized: false', ''])
    quest_tree = _pretty_json_bytes({'schema_version': 1, 'project': {'id': spec.project_id, 'name': spec.name, 'main_quest': spec.objective, 'controller_root': str(spec.controller_root), 'managed_target_root': str(spec.managed_target_root), 'approval_status': PENDING_APPROVAL}, 'root_id': 'main-quest', 'nodes': {'main-quest': _plan_node(node_id='main-quest', node_type='main_quest', title=spec.objective, parent_id=None, children=[task.task_id], description='Proposed Main Quest from unapproved bootstrap input.', next_action='Await exact human approval.'), task.task_id: _plan_node(node_id=task.task_id, node_type='task', title=task.title, parent_id='main-quest', acceptance_criteria=list(task.done_criteria), evidence_required=['Exact human approval record for this bootstrap.', 'Task completion evidence before any completed status.'], description=task.expected_output, next_action='Await exact human approval before execution.')}, 'bootstrap': {'kind': BOOTSTRAP_KIND, 'bootstrap_id': bootstrap_id, 'spec_sha256': spec_sha256, 'initial_task_id': task.task_id, 'approval_status': PENDING_APPROVAL, 'human_approval_recorded': False, 'execution_authorized': False, 'planner_handoff_authorized': False, 'managed_target_write_authorized': False}})
    capsule = '\n'.join(['# Project State Capsule', '', f'Bootstrap ID: `{bootstrap_id}`', f'Bootstrap Spec SHA-256: `{spec_sha256}`', f'Project ID: `{spec.project_id}`', f'Project: {spec.name}', f'Bootstrap Status: {PENDING_APPROVAL}', 'Human Approval Recorded: false', 'Authority Granted: false', '', '## Main Quest', '', spec.objective, '', '## Initial Task', '', f'Task: {task.title}', f'Task ID: `{task.task_id}`', 'Status: planned', 'Execution Authorized: false', '', '## Topology', '', f'- Controller: `{spec.controller_root}`', f'- Read-only managed target: `{spec.managed_target_root}`', '- Controller and target are separate and non-overlapping.', '', '## Next Action', '', 'Review the exact bootstrap receipt and record a separate human approval before Planner handoff or task execution.', ''])
    status = '\n'.join(['# Status', '', f'Bootstrap ID: `{bootstrap_id}`', f'Bootstrap Spec SHA-256: `{spec_sha256}`', f'Project ID: `{spec.project_id}`', f'Project Status: {PENDING_APPROVAL}', 'Current Status: Project-neutral operational sources prepared atomically; exact human approval is still required.', f'Initial Task ID: `{task.task_id}`', 'Initial Task Status: planned', 'Human Approval Recorded: false', 'Authority Granted: false', 'Managed Target Written: false', 'Provider Called: false', 'Planner Handoff Authorized: false', '', '## Next Action', '', 'Inspect the receipt and approve or reject this proposed bootstrap through a separate owner-controlled boundary.', ''])
    source_header = [f'Bootstrap ID: `{bootstrap_id}`', f'Bootstrap Spec SHA-256: `{spec_sha256}`', f'Project ID: `{spec.project_id}`', '']
    decisions = '\n'.join(['# Decisions', '', *source_header, '## Locked Decisions', '', 'No owner-approved locked decision was recorded by bootstrap.', 'Bootstrap inputs remain working assumptions pending exact human approval.', ''])
    constraints = '\n'.join(['# Constraints', '', *source_header, f'- Keep controller state under `{spec.controller_root}`.', f'- Treat `{spec.managed_target_root}` as read-only during bootstrap and discovery.', '- Keep the controller and managed target separate and non-overlapping.', '- Do not execute the planned task before exact human approval.', '- Do not mark work complete without byte-bound evidence.', '- Do not infer owner decisions, provider authority, release authority, push authority, or protected-write authority.', ''])
    side_quests = '\n'.join(['# Side Quests', '', *source_header, 'No Side Quest or Nested Side Quest was created by bootstrap.', ''])
    completed_tasks = '\n'.join(['# Completed Tasks', '', *source_header, 'No Task is completed. Completion requires evidence and the normal guarded transition.', ''])
    working_commands = '\n'.join(['# Working Commands', '', *source_header, 'Inspection only while approval is pending:', '', '```powershell', questmemory_powershell_command('status', '--project-root', str(spec.controller_root)), '```', ''])
    evidence_log = '\n'.join(['# Evidence Log', '', *source_header, '- Bootstrap operational sources are bound by the exact-SHA-256 receipt under `.questmemory/BOOTSTRAP_RECEIPTS/`.', '- Evidence status: pending; bootstrap grants no completion authority.', ''])
    return {'PROJECT_CHARTER.md': charter.encode('utf-8'), 'CURRENT_TASK.md': current_task.encode('utf-8'), 'TASKS.md': tasks.encode('utf-8'), 'PLAN/quest_tree.json': quest_tree, 'PROJECT_STATE_CAPSULE.md': capsule.encode('utf-8'), 'STATUS.md': status.encode('utf-8'), 'DECISIONS.md': decisions.encode('utf-8'), 'CONSTRAINTS.md': constraints.encode('utf-8'), 'SIDE_QUESTS.md': side_quests.encode('utf-8'), 'COMPLETED_TASKS.md': completed_tasks.encode('utf-8'), 'WORKING_COMMANDS.md': working_commands.encode('utf-8'), 'EVIDENCE_LOG.md': evidence_log.encode('utf-8')}

def _plan_node(*, node_id: str, node_type: str, title: str, parent_id: str | None, children: list[str] | None=None, acceptance_criteria: list[str] | None=None, evidence_required: list[str] | None=None, description: str='', next_action: str='') -> dict[str, Any]:
    return {'id': node_id, 'type': node_type, 'title': title, 'status': 'planned', 'approval_status': PENDING_APPROVAL, 'execution_authorized': False, 'parent_id': parent_id, 'children': children or [], 'depends_on': [], 'severity': None, 'why': '', 'description': description, 'acceptance_criteria': acceptance_criteria or [], 'evidence_required': evidence_required or [], 'evidence_refs': [], 'result_references': [], 'mitigation': '', 'decision': '', 'rationale': '', 'promoted_to': None, 'created_from': BOOTSTRAP_KIND, 'next_action': next_action}

def _authority_markdown_lines() -> list[str]:
    return ['- Human approval recorded: false', '- Task execution authorized: false', '- Planner handoff authorized: false', '- Provider call authorized: false', '- Managed-target write authorized: false', '- Protected write authorized: false', '- Release, push, or pull-request authority: false']

def _required_drive_for_spec(spec: _ValidatedBootstrapSpecV1) -> str:
    drive = spec.controller_root.drive.upper()
    if drive != spec.managed_target_root.drive.upper():
        raise FutureProjectBootstrapPathPolicyError('controller and target must use the same approved local drive')
    return drive

def _receipt_payload(spec: _ValidatedBootstrapSpecV1, *, bootstrap_id: str, spec_sha256: str, operational_digests: Sequence[BootstrapFileDigestV1]) -> dict[str, Any]:
    digest_payload = [item.as_dict() for item in operational_digests]
    return {'schema_version': SCHEMA_VERSION, 'receipt_kind': BOOTSTRAP_KIND, 'bootstrap_id': bootstrap_id, 'spec_sha256': spec_sha256, 'status': PENDING_APPROVAL, 'deterministic': True, 'spec': spec.canonical_payload(), 'topology': {'mode': 'central_controller_separate_managed_target', 'required_drive': _required_drive_for_spec(spec), 'overlap_allowed': False, 'managed_target_written': False}, 'approval': {'status': PENDING_APPROVAL, 'human_approval_recorded': False, 'approval_record_id': None}, 'authority': {'authority_granted': False, 'task_execution_authorized': False, 'planner_handoff_authorized': False, 'provider_call_authorized': False, 'canonical_target_write_authorized': False, 'protected_write_authorized': False, 'release_authorized': False, 'push_authorized': False, 'pull_request_authorized': False}, 'operational_files': digest_payload, 'operational_file_count': len(digest_payload), 'operational_file_set_sha256': _sha256(_canonical_json_bytes(digest_payload)), 'receipt_integrity': {'algorithm': 'sha256', 'storage': RECEIPT_INTEGRITY_STORAGE}}

def _validate_exact_replay(spec: _ValidatedBootstrapSpecV1, *, bootstrap_id: str, spec_sha256: str, receipt_sha256: str, receipt_relative_path: str, expected_files: Mapping[str, bytes], operational_digests: tuple[BootstrapFileDigestV1, ...]) -> FutureProjectBootstrapResultV1:
    questmemory_dir = spec.controller_root / QUESTMEMORY_DIRECTORY
    if not questmemory_dir.is_dir() or _is_link_or_reparse(questmemory_dir):
        raise FutureProjectBootstrapConflictError('controller .questmemory exists but is not a regular local directory')
    receipts_dir = questmemory_dir / RECEIPTS_DIRECTORY
    if not receipts_dir.is_dir() or _is_link_or_reparse(receipts_dir):
        raise FutureProjectBootstrapConflictError('existing operational sources have no valid bootstrap receipt')
    receipt_path = _require_exact_receipt_namespace(receipts_dir)
    receipt_content = _read_regular_file(receipt_path)
    actual_receipt_sha256 = _sha256(receipt_content)
    try:
        persisted_receipt = json.loads(receipt_content.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt is not valid UTF-8 JSON') from exc
    persisted_spec_sha256 = persisted_receipt.get('spec_sha256')
    if persisted_spec_sha256 != spec_sha256:
        raise FutureProjectBootstrapConflictError('existing operational sources belong to a different bootstrap spec')
    if receipt_path.name != Path(receipt_relative_path).name:
        raise FutureProjectBootstrapIntegrityError('existing bootstrap receipt differs from the exact expected receipt')
    _verify_expected_files(questmemory_dir, expected_files)
    if actual_receipt_sha256 != receipt_sha256:
        raise FutureProjectBootstrapIntegrityError('existing bootstrap receipt hash differs from the exact replay')
    return _result(spec, bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, receipt_sha256=receipt_sha256, receipt_relative_path=receipt_relative_path, operational_digests=operational_digests, replayed=True)

def _result(spec: _ValidatedBootstrapSpecV1, *, bootstrap_id: str, spec_sha256: str, receipt_sha256: str, receipt_relative_path: str, operational_digests: tuple[BootstrapFileDigestV1, ...], replayed: bool) -> FutureProjectBootstrapResultV1:
    receipt_controller_relative = f'{QUESTMEMORY_DIRECTORY}/{receipt_relative_path}'
    receipt_path = spec.controller_root / receipt_controller_relative
    receipt_digest = BootstrapFileDigestV1(relative_path=receipt_controller_relative, sha256=receipt_sha256, size_bytes=receipt_path.stat().st_size)
    controller_digests = tuple((BootstrapFileDigestV1(relative_path=f'{QUESTMEMORY_DIRECTORY}/{item.relative_path}', sha256=item.sha256, size_bytes=item.size_bytes) for item in operational_digests))
    return FutureProjectBootstrapResultV1(bootstrap_id=bootstrap_id, spec_sha256=spec_sha256, controller_root=spec.controller_root, managed_target_root=spec.managed_target_root, receipt_path=receipt_path, receipt_sha256=receipt_sha256, files=(*controller_digests, receipt_digest), replayed=replayed)

def _directory_identity(path: Path) -> tuple[int, int, int]:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise FutureProjectBootstrapPathPolicyError('bootstrap root identity could not be observed') from exc
    if not stat.S_ISDIR(observed.st_mode) or _is_link_or_reparse(path):
        raise FutureProjectBootstrapPathPolicyError('bootstrap root identity is not a regular local directory')
    return (observed.st_dev, observed.st_ino, observed.st_mode)

def _require_directory_identity(path: Path, expected: tuple[int, int, int]) -> None:
    if _directory_identity(path) != expected:
        raise FutureProjectBootstrapPathPolicyError('bootstrap root identity changed during initialization')

def _rollback_owned_publication(questmemory_dir: Path, expected_files: Mapping[str, bytes]) -> None:
    try:
        _verify_exact_tree(questmemory_dir, expected_files)
    except FutureProjectBootstrapError:
        return
    shutil.rmtree(questmemory_dir)

def _write_staged_file(root: Path, relative_path: str, content: bytes) -> None:
    relative = Path(relative_path)
    destination = root / relative
    if relative.is_absolute() or '..' in relative.parts:
        raise FutureProjectBootstrapIntegrityError(f'unsafe staged path: {relative_path}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())

def _commit_staged_directory(staging_dir: Path, destination: Path) -> None:
    if _lexists(destination):
        raise FileExistsError(str(destination))
    os.replace(staging_dir, destination)

def _verify_exact_tree(root: Path, expected_files: Mapping[str, bytes]) -> None:
    actual_files, actual_directories = _tree_inventory(root)
    expected_file_names = set(expected_files)
    expected_directories: set[str] = set()
    for relative_path in expected_file_names:
        parent = Path(relative_path).parent
        while parent != Path('.'):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_files != expected_file_names or actual_directories != expected_directories:
        raise FutureProjectBootstrapIntegrityError('bootstrap operational tree is not the exact expected file set')
    for relative_path, expected_content in expected_files.items():
        actual_content = _read_regular_file(root / Path(relative_path))
        if actual_content != expected_content:
            raise FutureProjectBootstrapIntegrityError(f'bootstrap file bytes drifted: {relative_path}')

def _verify_expected_files(root: Path, expected_files: Mapping[str, bytes]) -> None:
    if not root.is_dir() or _is_link_or_reparse(root):
        raise FutureProjectBootstrapIntegrityError('bootstrap operational root must be a regular local directory')
    for relative_path, expected_content in expected_files.items():
        candidate = root / Path(relative_path)
        current = root
        for part in Path(relative_path).parts:
            current = current / part
            if _lexists(current) and _is_link_or_reparse(current):
                raise FutureProjectBootstrapIntegrityError(f'bootstrap path contains a link or reparse point: {relative_path}')
        actual_content = _read_regular_file(candidate)
        if actual_content != expected_content:
            raise FutureProjectBootstrapIntegrityError(f'bootstrap file bytes drifted: {relative_path}')

def _require_exact_receipt_namespace(receipts_dir: Path) -> Path:
    try:
        entries = list(receipts_dir.iterdir())
    except OSError as exc:
        raise FutureProjectBootstrapIntegrityError('pending bootstrap receipt namespace cannot be inspected') from exc
    if len(entries) != 1 or entries[0].name != RECEIPT_FILE:
        raise FutureProjectBootstrapIntegrityError('bootstrap receipt namespace requires exactly one exact-SHA-verified stable receipt.json')
    receipt_path = entries[0]
    if not receipt_path.is_file() or _is_link_or_reparse(receipt_path):
        raise FutureProjectBootstrapIntegrityError('pending bootstrap receipt is not a regular local file')
    return receipt_path

def _tree_inventory(root: Path) -> tuple[set[str], set[str]]:
    if not root.is_dir() or _is_link_or_reparse(root):
        raise FutureProjectBootstrapIntegrityError('bootstrap operational root must be a regular local directory')
    files: set[str] = set()
    directories: set[str] = set()
    for directory, child_directories, child_files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for child_name in child_directories:
            child = directory_path / child_name
            if _is_link_or_reparse(child):
                raise FutureProjectBootstrapIntegrityError('bootstrap tree contains a link or reparse directory')
            directories.add(child.relative_to(root).as_posix())
        for child_name in child_files:
            child = directory_path / child_name
            if _is_link_or_reparse(child) or not child.is_file():
                raise FutureProjectBootstrapIntegrityError('bootstrap tree contains a non-regular file')
            files.add(child.relative_to(root).as_posix())
    return (files, directories)

def _read_regular_file(path: Path) -> bytes:
    if not path.is_file() or _is_link_or_reparse(path):
        raise FutureProjectBootstrapIntegrityError(f'bootstrap artifact is not a regular file: {path.name}')
    return path.read_bytes()

def _assert_no_abandoned_staging(controller_root: Path) -> None:
    if any(controller_root.glob(f'{_STAGING_PREFIX}*')):
        raise FutureProjectBootstrapConflictError('an abandoned Future Project Bootstrap V1 staging directory exists')

def _remove_owned_staging(staging_dir: Path, controller_root: Path) -> None:
    if staging_dir.parent != controller_root or not staging_dir.name.startswith(_STAGING_PREFIX):
        raise FutureProjectBootstrapIntegrityError('refusing to remove a staging path outside the controller boundary')
    if staging_dir.is_dir() and (not _is_link_or_reparse(staging_dir)):
        shutil.rmtree(staging_dir)
    elif _lexists(staging_dir):
        raise FutureProjectBootstrapIntegrityError('staging path changed type during bootstrap')

def _assert_no_link_or_reparse_components(path: Path, label: str) -> None:
    anchor = Path(path.anchor)
    current = anchor
    for part in path.parts[1:]:
        current = current / part
        if _lexists(current) and _is_link_or_reparse(current):
            raise FutureProjectBootstrapPathPolicyError(f'{label} contains a symlink or reparse component')

def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        if os.path.samefile(left, right):
            return True
    except OSError:
        pass
    return left == right or left in right.parents or right in left.parents

def _clean_id(value: object, label: str) -> str:
    clean = _clean_line(value, label, maximum=128)
    if not _SAFE_ID_RE.fullmatch(clean):
        raise FutureProjectBootstrapSpecError(f'{label} must match {_SAFE_ID_RE.pattern}')
    return clean

def _clean_line(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise FutureProjectBootstrapSpecError(f'{label} must be a string')
    clean = value.strip()
    if not clean:
        raise FutureProjectBootstrapSpecError(f'{label} cannot be empty')
    if len(clean) > maximum:
        raise FutureProjectBootstrapSpecError(f'{label} exceeds the {maximum}-character limit')
    if any((ord(character) < 32 or ord(character) == 127 for character in clean)):
        raise FutureProjectBootstrapSpecError(f'{label} must be a single control-free line')
    return clean

def _clean_markdown_paragraph(value: object, label: str, *, maximum: int) -> str:
    clean = _clean_line(value, label, maximum=maximum)
    if re.match('^(?:#{1,6}(?:\\s|$)|>|```|~~~|[-+*]\\s|\\d+[.)]\\s)', clean):
        raise FutureProjectBootstrapSpecError(f'{label} must be plain paragraph text, not Markdown structure')
    return clean

def _clean_lines(value: object, label: str, *, maximum_items: int, item_maximum: int, require_nonempty: bool) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise FutureProjectBootstrapSpecError(f'{label} must be a bounded sequence of strings')
    if len(value) > maximum_items:
        raise FutureProjectBootstrapSpecError(f'{label} exceeds the {maximum_items}-item limit')
    cleaned = tuple((_clean_line(item, f'{label}[{index}]', maximum=item_maximum) for index, item in enumerate(value)))
    if require_nonempty and (not cleaned):
        raise FutureProjectBootstrapSpecError(f'{label} cannot be empty')
    if len(set(cleaned)) != len(cleaned):
        raise FutureProjectBootstrapSpecError(f'{label} contains duplicates')
    return cleaned

def _digest_for(relative_path: str, content: bytes) -> BootstrapFileDigestV1:
    return BootstrapFileDigestV1(relative_path=relative_path, sha256=_sha256(content), size_bytes=len(content))

def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')

def _lexists(path: Path) -> bool:
    return os.path.lexists(path)
