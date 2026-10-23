# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from projectrecall.config import FUTURE_PROJECT_CONTROLLER_KIND
from projectrecall.config import load_config
from projectrecall.controller_transition_lock_v1 import acquire_controller_transition_lock_v1
from projectrecall.core_memory_v1 import _value
from projectrecall.core_memory_v1 import approve_core_memory_v1
from projectrecall.core_memory_v1 import prepare_core_memory_v1
from projectrecall.durable_io import atomic_write_texts
from projectrecall.model_mesh.schemas import sha256_json
from projectrecall.model_mesh.schemas import strict_json_loads
from projectrecall.file_safety import _capture_root_chain
from projectrecall.file_safety import _read_regular_bytes
from projectrecall.file_safety import _verify_directory_bindings
from projectrecall.operational_text import _unfenced_lines
DIRECTORY = '.questmemory/MEMORY_PATCHES_V1'
PROTOCOL = 'local-report-memory-patch-v1'
_ID = re.compile('^memory-patch-[0-9a-f]{24}$')

def _require(condition, message):
    if not condition:
        raise ValueError('Memory patch: ' + message)

def _root(project_root):
    root = Path(project_root).absolute()
    _capture_root_chain(root)
    config = load_config(root)
    _require(config.project_root == root, 'controller binding differs')
    return (root, config)

def _read(root, relative, maximum=65536):
    path = root / relative
    _require(path.is_relative_to(root), 'source outside controller')
    bindings = _capture_root_chain(path.parent)
    raw, _ = _read_regular_bytes(path, label='memory patch source', maximum=maximum)
    _verify_directory_bindings(bindings)
    return raw

def _report_reference(root, report):
    path = Path(report)
    path = path if path.is_absolute() else root / path
    _require(path.is_relative_to(root), 'report outside controller')
    relative = path.relative_to(root).as_posix()
    _require(relative.startswith('.questmemory/SESSION_REPORTS/') and path.suffix == '.md', 'select a Markdown SESSION_REPORTS file')
    _require(re.fullmatch('[A-Za-z0-9_./-]+', relative) is not None and all((part not in {'', '.', '..'} and (not part.endswith('.')) for part in relative.split('/'))), 'ambiguous report path')
    _require(not any((re.fullmatch('(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\\..*)?', part) for part in relative.split('/'))), 'reserved report path')
    return relative

def _extract(raw):
    text = raw.decode('utf-8')
    kind, values, seen = (None, [], set())
    for _, line in _unfenced_lines(text):
        heading = re.match('^\\s{0,3}#{1,6}\\s+(.+?)\\s*$', line)
        if heading:
            kind = {'decisions proposed': 'decision', 'constraints proposed': 'constraint'}.get(heading[1].lower())
            continue
        if kind is None or not line.strip():
            continue
        match = re.fullmatch('\\s*[-*]\\s+([a-z0-9][a-z0-9._:-]{0,95}):\\s+(.+?)\\s*', line)
        _require(match is not None, 'proposal must be a bullet: key: statement')
        value = _value(kind, match[1], match[2], [], True, 'required')
        identity = (kind, value['key'])
        _require(identity not in seen, 'duplicate memory key in report')
        seen.add(identity)
        values.append(value)
        _require(len(values) <= 16, 'report proposal limit exceeded')
    return values

def ingest_memory_patches_v1(project_root, report):
    """Explicit draft-only ingestion, with no legacy operational or Core writes."""
    root, config = _root(project_root)
    with acquire_controller_transition_lock_v1(root) as lease:
        reference = _report_reference(root, report)
        raw = _read(root, reference)
        drafts, writes, bindings = _prepare_drafts(root, config, reference, raw)
        _require(_read(root, reference) == raw, 'report changed before draft publication')
        lease.verify()
        _verify_directory_bindings(bindings)
        if writes:
            atomic_write_texts(writes)
        return [_row(root, draft) for draft in drafts]

def _prepare_drafts(root, config, reference, raw):
    """Prepare the same drafts for one composite legacy ingestion publication."""
    values = _extract(raw)
    if values and config.controller_kind == FUTURE_PROJECT_CONTROLLER_KIND:
        _, _, current = _core_observation(root, include_memory=True)
        values = [value for value in values if not any((all((tip.get(key) == field for key, field in value.items())) for tip in current))]
    source_sha = hashlib.sha256(raw).hexdigest()
    drafts = []
    for value in values:
        proposal = prepare_core_memory_v1(root, **value) if config.controller_kind == FUTURE_PROJECT_CONTROLLER_KIND else None
        body = {'protocol_id': PROTOCOL, 'schema_version': 1, 'controller_root': root.as_posix(), 'source_reference': reference, 'source_sha256': source_sha, 'memory': value, 'core_proposal': proposal, 'authority_granted': False}
        digest = sha256_json(body)
        drafts.append({**body, 'patch_id': 'memory-patch-' + digest[:24], 'patch_sha256': digest})
    if not drafts:
        return ([], {}, ())
    directory = root / DIRECTORY
    _capture_root_chain(directory.parent)
    directory.mkdir(exist_ok=True)
    bindings = _capture_root_chain(directory)
    writes = {}
    for draft in drafts:
        path = directory / (draft['patch_id'] + '.json')
        text = json.dumps(draft, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        if path.exists() or path.is_symlink():
            _require(_read(root, path.relative_to(root)) == text.encode('utf-8'), 'existing draft differs')
        else:
            writes[path] = text
    return (drafts, writes, bindings)

def _load(root, patch_id):
    _require(isinstance(patch_id, str) and _ID.fullmatch(patch_id), 'invalid patch identity')
    raw = _read(root, DIRECTORY + '/' + patch_id + '.json')
    draft = strict_json_loads(raw.decode('utf-8'))
    fields = {'protocol_id', 'schema_version', 'controller_root', 'source_reference', 'source_sha256', 'memory', 'core_proposal', 'authority_granted', 'patch_id', 'patch_sha256'}
    _require(isinstance(draft, dict) and set(draft) == fields, 'invalid draft schema')
    body = {k: v for k, v in draft.items() if k not in {'patch_id', 'patch_sha256'}}
    digest = sha256_json(body)
    _require(draft['protocol_id'] == PROTOCOL and draft['schema_version'] == 1 and (draft['authority_granted'] is False) and (draft['controller_root'] == root.as_posix()) and (draft['patch_id'] == patch_id == 'memory-patch-' + digest[:24]) and (draft['patch_sha256'] == digest), 'draft content binding differs')
    _require(_value(**draft['memory']) == draft['memory'], 'invalid memory content')
    _require(_report_reference(root, draft['source_reference']) == draft['source_reference'], 'source reference differs')
    _require(re.fullmatch('[0-9a-f]{64}', draft['source_sha256']) is not None, 'invalid source digest')
    proposal = draft['core_proposal']
    if proposal is not None:
        _require(isinstance(proposal, dict) and proposal.get('memory') == draft['memory'] and (proposal.get('proposal_sha256') == sha256_json({k: v for k, v in proposal.items() if k != 'proposal_sha256'})), 'Core proposal differs')
    return draft

def _core_observation(root, *, include_memory=False):
    from projectrecall import core_memory_v1 as memory
    from projectrecall import core_state_v2 as core
    from projectrecall.sqlite_stable_read import open_owned_stable_read_only_sqlite
    _, config, receipt = memory._binding(root)
    projection = memory.clean._build_bootstrap_projection(config, receipt.project_id)
    connection = open_owned_stable_read_only_sqlite(config.sqlite_path.resolve(strict=True), owner_key=memory.clean._owner_key(config), error_factory=ValueError)
    try:
        import sqlite3
        connection.row_factory = sqlite3.Row
        memory.clean._validate_connection(connection, config=config, project_id=receipt.project_id, projection=projection)
        applied = {json.loads(row[0])['proposal']['proposal_sha256'] for row in connection.execute(f'SELECT payload_json FROM {core.EVENTS_TABLE} WHERE event_type=?', (memory.EVENT,))}
        tip = connection.execute(f'SELECT event_digest FROM {core.EVENTS_TABLE} ORDER BY ordinal DESC LIMIT 1').fetchone()[0]
        if include_memory:
            return (applied, tip, memory.memory_tips_v1(connection))
        return (applied, tip)
    finally:
        connection.close()

def _rejection(root, draft):
    reference = DIRECTORY + '/' + draft['patch_id'] + '.rejected.json'
    path = root / reference
    if not path.exists() and (not path.is_symlink()):
        return None
    value = strict_json_loads(_read(root, reference).decode('utf-8'))
    _require(isinstance(value, dict) and set(value) == {'patch_id', 'patch_sha256', 'disposition', 'reason'} and (value['patch_id'] == draft['patch_id']) and (value['patch_sha256'] == draft['patch_sha256']) and (value['disposition'] == 'rejected'), 'rejection binding differs')
    _value('decision', 'rejection', value['reason'], [], True, 'required')
    return value

def _row(root, draft, observation=None):
    proposal = draft['core_proposal']
    rejected = _rejection(root, draft)
    if proposal is None:
        status = 'rejected' if rejected else 'needs_clean_core'
    else:
        applied, tip = observation if observation is not None else _core_observation(root)
        status = 'applied' if proposal['proposal_sha256'] in applied else 'rejected' if rejected else 'stale' if proposal['base_event_digest'] != tip else 'pending'
    try:
        source_current = hashlib.sha256(_read(root, draft['source_reference'])).hexdigest() == draft['source_sha256']
    except (OSError, ValueError, RuntimeError):
        source_current = False
    if status == 'pending' and (not source_current):
        status = 'stale'
    return {'patch_id': draft['patch_id'], 'patch_sha256': draft['patch_sha256'], 'proposal_sha256': proposal['proposal_sha256'] if proposal else None, 'kind': draft['memory']['kind'], 'key': draft['memory']['key'], 'statement': draft['memory']['statement'], 'status': status, 'source_reference': draft['source_reference'], 'source_current': source_current}

def list_memory_patches_v1(project_root):
    root, config = _root(project_root)
    with acquire_controller_transition_lock_v1(root):
        directory = root / DIRECTORY
        if not directory.exists() and (not directory.is_symlink()):
            return []
        _capture_root_chain(directory)
        paths = sorted((path for path in directory.glob('*.json') if not path.name.endswith('.rejected.json')))
        _require(len(paths) <= 256, 'draft display limit exceeded')
        observation = _core_observation(root) if config.controller_kind == FUTURE_PROJECT_CONTROLLER_KIND else None
        return [_row(root, _load(root, path.stem), observation) for path in paths]

def inspect_memory_patch_v1(project_root, patch_id):
    root, _ = _root(project_root)
    with acquire_controller_transition_lock_v1(root):
        draft = _load(root, patch_id)
        return {**draft, 'review': _row(root, draft)}

def approve_memory_patch_v1(project_root, patch_id, *, patch_sha256, approval_id, approval_statement, approval_text_sha256, confirm_human_approval=False):
    _require(confirm_human_approval is True, 'explicit human confirmation required')
    root, _ = _root(project_root)
    with acquire_controller_transition_lock_v1(root) as lease:
        draft = _load(root, patch_id)
        _require(draft['patch_sha256'] == patch_sha256 and patch_sha256 in approval_statement, 'exact patch approval required')
        _require(draft['core_proposal'] is not None, 'requires an existing clean Core; no implicit legacy migration')
        _require(_rejection(root, draft) is None, 'patch was rejected')
        raw = _read(root, draft['source_reference'])
        _require(hashlib.sha256(raw).hexdigest() == draft['source_sha256'], 'source report changed')
        _require(draft['memory'] in _extract(raw), 'memory does not correspond to the selected report')
        lease.verify()
        proposal = draft['core_proposal']
        return approve_core_memory_v1(root, proposal, proposal_sha256=proposal['proposal_sha256'], approval_id=approval_id, approval_statement=approval_statement, approval_text_sha256=approval_text_sha256, confirm_human_approval=True)
