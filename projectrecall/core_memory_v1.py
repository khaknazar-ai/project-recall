# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from projectrecall import core_state_clean_v2 as clean
from projectrecall import core_state_v2 as core
from projectrecall.config import load_config
from projectrecall.controller_transition_lock_v1 import acquire_controller_transition_lock_v1
from projectrecall.model_mesh.schemas import canonical_json
from projectrecall.model_mesh.schemas import sha256_json
from projectrecall.sqlite_stable_read import open_owned_stable_read_only_sqlite
EVENT = 'clean_core_memory_approved_v1'
OPERATION = 'approve_clean_core_memory_v1'
PROTOCOL = 'clean-core-memory-v1'
_ID = re.compile('^[a-z0-9][a-z0-9._:-]{0,95}$')

def _require(condition, message):
    if not condition:
        raise ValueError('Core memory: ' + message)

def _binding(root, *, stable_read=False):
    from projectrecall.config import FUTURE_PROJECT_CONTROLLER_KIND
    from projectrecall.project_bootstrap_v1 import _verify_immutable_bootstrap_receipt_stable_v1, verify_bootstrap_receipt_v1
    root = Path(root).absolute()
    config = load_config(root)
    _require(config.controller_kind == FUTURE_PROJECT_CONTROLLER_KIND, 'clean controller required')
    receipt = _verify_immutable_bootstrap_receipt_stable_v1(root) if stable_read else verify_bootstrap_receipt_v1(root)
    clean._validate_binding(config, receipt.project_id)
    return (root, config, receipt)

def _value(kind, key, statement, task_ids, locked, severity):
    _require(kind in ('decision', 'constraint'), 'unknown memory kind')
    _require(isinstance(key, str) and _ID.fullmatch(key), 'invalid memory key')
    _require(type(locked) is bool and severity in ('advisory', 'required', 'blocking'), 'invalid memory flags')
    _require(isinstance(task_ids, list) and len(task_ids) <= 16 and all((isinstance(t, str) and _ID.fullmatch(t) for t in task_ids)) and (len(set(task_ids)) == len(task_ids)), 'invalid task scope')
    from projectrecall.content_validation import _context_fact
    _context_fact({'fact_id': 'memory-check', 'statement': statement, 'source_reference': 'data/questmemory.sqlite', 'source_sha256': 'a' * 64, 'status': 'current', 'audience': 'shared'})
    _require(len(statement.encode('utf-8')) <= 400, 'memory statement budget exceeded')
    return {'kind': kind, 'key': key, 'statement': statement, 'task_ids': sorted(task_ids), 'locked': locked, 'severity': severity}

def _proposal(value, *, project_id, bootstrap_id, prior_digest, previous):
    body = {'protocol_id': PROTOCOL, 'schema_version': 1, 'project_id': project_id, 'bootstrap_id': bootstrap_id, 'base_event_digest': prior_digest, 'memory': value, 'version': previous['version'] + 1 if previous else 1, 'previous_record_id': previous['record_id'] if previous else None, 'previous_record_digest': previous['record_digest'] if previous else None}
    return {**body, 'proposal_sha256': sha256_json(body)}

def prepare_core_memory_v1(controller_root, *, kind, key, statement, task_ids=None, locked=True, severity='required'):
    """Prepare a zero-authority proposal; no file/database writes."""
    _root, config, receipt = _binding(controller_root)
    value = _value(kind, key, statement, [] if task_ids is None else task_ids, locked, severity)
    projection = clean._build_bootstrap_projection(config, receipt.project_id)
    connection = open_owned_stable_read_only_sqlite(config.sqlite_path.resolve(strict=True), owner_key=clean._owner_key(config), error_factory=ValueError)
    try:
        connection.row_factory = sqlite3.Row
        clean._validate_connection(connection, config=config, project_id=receipt.project_id, projection=projection)
        status = connection.execute(f'SELECT status FROM {core.PROJECTS_TABLE} WHERE project_id=?', (receipt.project_id,)).fetchone()[0]
        _require(status != 'completed', 'project is terminal')
        tasks = {r[0] for r in connection.execute(f'SELECT task_id FROM {core.TASKS_TABLE} WHERE project_id=?', (receipt.project_id,))}
        _require(set(value['task_ids']) <= tasks, 'task scope is not in this project')
        tips = memory_tips_v1(connection)
        previous = next((t for t in tips if t['kind'] == kind and t['key'] == key), None)
        prior = connection.execute(f'SELECT event_digest FROM {core.EVENTS_TABLE} ORDER BY ordinal DESC LIMIT 1').fetchone()[0]
        return _proposal(value, project_id=receipt.project_id, bootstrap_id=receipt.bootstrap_id, prior_digest=prior, previous=previous)
    finally:
        connection.close()

def _materialize(proposal, approval):
    """Deterministic immutable row projection. No policy is inferred from text."""
    memory = proposal['memory']
    kind, project = (memory['kind'], proposal['project_id'])
    seed = sha256_json({'proposal': proposal, 'approval': approval})
    evidence_id = 'memory-approval-' + sha256_json([project, approval['approval_id']])[:24]
    record_id = 'memory-' + kind + '-' + seed[:24]
    record_digest = sha256_json({'memory': memory, 'version': proposal['version'], 'previous': proposal['previous_record_digest'], 'approval': seed})
    value = canonical_json({k: memory[k] for k in ('statement', 'task_ids', 'severity')})
    rows = {core.EVIDENCE_TABLE: [{'evidence_id': evidence_id, 'project_id': project, 'evidence_kind': 'human_approval', 'verification_status': 'accepted', 'content_sha256': approval['statement_sha256'], 'source_uri': 'core-memory-approval:' + approval['approval_id'], 'summary': 'Explicit approval of memory proposal ' + proposal['proposal_sha256']}]}
    if kind == 'constraint':
        rows[core.CONSTRAINTS_TABLE] = [{'constraint_id': record_id, 'project_id': project, 'constraint_key': memory['key'], 'version': proposal['version'], 'scope': 'task' if memory['task_ids'] else 'project', 'severity': memory['severity'], 'locked': int(memory['locked']), 'value_json': value, 'constraint_digest': record_digest, 'supersedes_constraint_id': proposal['previous_record_id'], 'previous_constraint_digest': proposal['previous_record_digest'], 'approval_evidence_id': evidence_id}]
    else:
        needs_patch = proposal['version'] > 1 and proposal.get('previous_locked', False)
        patch_id, review_id = ('memory-patch-' + seed[:24], 'memory-review-' + seed[:24])
        if needs_patch:
            rows[core.MEMORY_PATCHES_TABLE] = [{'memory_patch_id': patch_id, 'project_id': project, 'patch_type': 'propose_decision_change', 'status': 'proposed', 'target_decision_id': proposal['previous_record_id'], 'expected_target_digest': proposal['previous_record_digest'], 'patch_digest': sha256_json(proposal), 'payload_json': canonical_json(proposal)}]
            rows[core.MEMORY_PATCH_REVIEWS_TABLE] = [{'review_id': review_id, 'memory_patch_id': patch_id, 'project_id': project, 'disposition': 'accepted', 'approval_evidence_id': evidence_id, 'review_digest': sha256_json(approval), 'reviewed_payload_json': None}]
        rows[core.DECISIONS_TABLE] = [{'decision_id': record_id, 'project_id': project, 'decision_key': memory['key'], 'version': proposal['version'], 'value_json': value, 'reason': 'Explicit owner-approved memory event', 'locked': int(memory['locked']), 'decision_digest': record_digest, 'supersedes_decision_id': proposal['previous_record_id'], 'previous_decision_digest': proposal['previous_record_digest'], 'approval_evidence_id': evidence_id if needs_patch else None, 'approval_memory_patch_id': patch_id if needs_patch else None, 'approval_memory_patch_review_id': review_id if needs_patch else None}]
        if needs_patch:
            rows[core.MEMORY_PATCH_APPLICATIONS_TABLE] = [{'application_id': 'memory-application-' + seed[:24], 'memory_patch_id': patch_id, 'review_id': review_id, 'decision_id': record_id, 'application_digest': sha256_json([seed, record_digest])}]
    tip = {**memory, 'version': proposal['version'], 'record_id': record_id, 'record_digest': record_digest, 'approval_evidence_id': evidence_id}
    return (rows, tip)

def _event(payload, ordinal, previous_digest):
    request = payload['proposal']['proposal_sha256']
    event_id = 'qm-memory-event-' + sha256_json(payload)[:24]
    encoded = canonical_json(payload)
    digest = core._event_digest_v2(event_id, ordinal, EVENT, encoded, previous_digest)
    return ({'event_id': event_id, 'project_id': payload['proposal']['project_id'], 'ordinal': ordinal, 'event_type': EVENT, 'payload_json': encoded, 'previous_event_digest': previous_digest, 'event_digest': digest}, {'operation_kind': OPERATION, 'idempotency_key': request, 'request_sha256': request, 'result_sha256': sha256_json({'event_digest': digest})})

def _check_approval(approval, proposal):
    _require(isinstance(approval, dict) and set(approval) == {'approval_id', 'statement', 'statement_sha256'}, 'invalid approval fields')
    _require(isinstance(approval['approval_id'], str) and _ID.fullmatch(approval['approval_id']), 'invalid approval identity')
    statement = approval['statement']
    _require(isinstance(statement, str) and 1 <= len(statement.encode('utf-8')) <= 800 and (proposal['proposal_sha256'] in statement), 'exact proposal approval statement required')
    from projectrecall.content_validation import _context_fact
    _context_fact({'fact_id': 'approval-check', 'statement': statement, 'source_reference': 'data/questmemory.sqlite', 'source_sha256': 'a' * 64, 'status': 'current', 'audience': 'shared'})
    _require(hashlib.sha256(statement.encode('utf-8')).hexdigest() == approval['statement_sha256'], 'approval statement hash differs')

def validate_memory_events_v1(connection, *, project_id, projection):
    """Replay approved memory in event order; used by the strict Core validator."""
    rows, tips, approvals = ({}, {}, set())
    tasks = {projection.receipt.initial_task.task_id}
    terminal = False
    for raw in connection.execute(f'SELECT * FROM {core.EVENTS_TABLE} WHERE ordinal>=3 ORDER BY ordinal'):
        event = dict(raw)
        _require(event['event_type'] == EVENT, 'unsupported memory-only event')
        payload = json.loads(event['payload_json'])
        _require(isinstance(payload, dict) and set(payload) == {'proposal', 'approval'}, 'invalid event fields')
        proposal, approval = (payload['proposal'], payload['approval'])
        memory = proposal['memory']
        _require(set(memory) == {'kind', 'key', 'statement', 'task_ids', 'locked', 'severity'}, 'invalid memory fields')
        value = _value(**memory)
        _require(set(value['task_ids']) <= tasks, 'memory task did not exist at approval')
        previous = tips.get((memory['kind'], memory['key']))
        expected = _proposal(value, project_id=project_id, bootstrap_id=projection.receipt.bootstrap_id, prior_digest=event['previous_event_digest'], previous=previous)
        _require(canonical_json(proposal) == canonical_json(expected), 'proposal prestate/content binding differs')
        _check_approval(approval, proposal)
        _require(approval['approval_id'] not in approvals, 'duplicate approval identity')
        approvals.add(approval['approval_id'])
        expected_event, idempotency = _event(payload, event['ordinal'], event['previous_event_digest'])
        _require(event == expected_event, 'memory event binding differs')
        actual = connection.execute(f'SELECT * FROM {core.IDEMPOTENCY_TABLE} WHERE operation_kind=? AND idempotency_key=?', (OPERATION, idempotency['idempotency_key'])).fetchone()
        _require(actual is not None and dict(actual) == idempotency, 'memory idempotency binding differs')
        derived, tip = _materialize({**proposal, 'previous_locked': previous['locked'] if previous else False}, approval)
        tips[memory['kind'], memory['key']] = tip
        for table, entries in derived.items():
            rows.setdefault(table, []).extend(entries)
    return rows

def memory_tips_v1(connection, *, task_id=None):
    """Call only after full clean validation in the same stable connection."""
    tips = {}
    for kind, table in (('decision', core.DECISIONS_TABLE), ('constraint', core.CONSTRAINTS_TABLE)):
        for raw in connection.execute(f'SELECT * FROM {table} ORDER BY version'):
            row = dict(raw)
            value = json.loads(row['value_json'])
            tips[kind, row[kind + '_key']] = {'kind': kind, 'key': row[kind + '_key'], **value, 'version': row['version'], 'locked': bool(row['locked']), 'record_id': row[kind + '_id'], 'record_digest': row[kind + '_digest']}
    return [item for _, item in sorted(tips.items()) if task_id is None or not item['task_ids'] or task_id in item['task_ids']]

def approve_core_memory_v1(controller_root, proposal, *, proposal_sha256, approval_id, approval_statement, approval_text_sha256, confirm_human_approval=False, expected_source_sha256=None):
    """Explicit exact approval/application. Never called by resume or extraction."""
    _require(confirm_human_approval is True, 'explicit human confirmation required')
    _require(isinstance(proposal, dict) and proposal.get('proposal_sha256') == proposal_sha256 and (sha256_json({k: v for k, v in proposal.items() if k != 'proposal_sha256'}) == proposal_sha256), 'proposal digest differs')
    approval = {'approval_id': approval_id, 'statement': approval_statement, 'statement_sha256': approval_text_sha256}
    _check_approval(approval, proposal)
    root, config, receipt = _binding(controller_root)
    projection = clean._build_bootstrap_projection(config, receipt.project_id)
    payload = {'proposal': proposal, 'approval': approval}
    from projectrecall.sqlite_stable_read import open_validated_sqlite_write
    with acquire_controller_transition_lock_v1(root) as lease:
        lease.verify()
        clean.validate_clean_core_state_v2(config, project_id=receipt.project_id)
        reader = open_owned_stable_read_only_sqlite(config.sqlite_path.resolve(strict=True), owner_key=clean._owner_key(config), error_factory=ValueError)
        try:
            old = reader.execute(f'SELECT payload_json FROM {core.EVENTS_TABLE} WHERE event_type=?', (EVENT,)).fetchall()
            if any((json.loads(row[0]) == payload for row in old)):
                return {'replay_noop': True, 'execution_authority_granted': False, 'proposal_sha256': proposal_sha256}
        finally:
            reader.close()
        expected = prepare_core_memory_v1(root, **proposal['memory'])
        _require(expected == proposal, 'stale proposal prestate')
        connection = open_validated_sqlite_write(config.sqlite_path.resolve(strict=True), expected='existing', owner_key=clean._owner_key(config) if expected_source_sha256 is None else None, expected_existing_sha256=expected_source_sha256, error_factory=ValueError)
        published = False
        try:
            connection.row_factory = sqlite3.Row
            core._register_write_functions(connection)
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            clean._validate_connection(connection, config=config, project_id=receipt.project_id, projection=projection)
            tip = connection.execute(f'SELECT ordinal,event_digest FROM {core.EVENTS_TABLE} ORDER BY ordinal DESC LIMIT 1').fetchone()
            _require(tip[1] == proposal['base_event_digest'], 'stale event prestate')
            previous = next((t for t in memory_tips_v1(connection) if t['kind'] == proposal['memory']['kind'] and t['key'] == proposal['memory']['key']), None)
            rows, _ = _materialize({**proposal, 'previous_locked': previous['locked'] if previous else False}, approval)
            for table, entries in rows.items():
                for row in entries:
                    clean._insert_exact_row(connection, table, row)
            event, idempotency = _event(payload, tip[0] + 1, tip[1])
            clean._insert_exact_row(connection, core.EVENTS_TABLE, event)
            clean._insert_exact_row(connection, core.IDEMPOTENCY_TABLE, idempotency)
            clean._validate_connection(connection, config=config, project_id=receipt.project_id, projection=projection)
            lease.verify()
            connection.commit()
            connection.close()
            published = True
        finally:
            if not published:
                if connection.in_transaction:
                    connection.rollback()
                connection.abort_without_publish()
        return {'replay_noop': False, 'execution_authority_granted': False, 'proposal_sha256': proposal_sha256}
