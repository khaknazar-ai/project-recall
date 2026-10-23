# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from projectrecall import core_state_v2 as v2
from projectrecall.config import FUTURE_PROJECT_CONTROLLER_KIND
from projectrecall.config import QuestMemoryConfig
from projectrecall.canonical_json import canonical_json_bytes
from projectrecall.project_bootstrap_v1 import VerifiedBootstrapReceiptV1
from projectrecall.project_bootstrap_v1 import verify_bootstrap_receipt_v1
from projectrecall.sqlite_stable_read import controller_sqlite_owner_key
from projectrecall.sqlite_stable_read import open_owned_stable_read_only_sqlite
from projectrecall.sqlite_stable_read import open_validated_sqlite_write
CLEAN_PROFILE_ID = 'projectrecall-memory-only-core-v1'
INSTALLATION_MODE = 'clean_core_schema_v2_new_project'
OPERATION_KIND = 'clean_core_schema_v2_install'
EVENT_TYPE = 'clean_core_schema_v2_installed'
PROJECTION_EVENT_TYPE = 'new_project_bootstrap_truth_projected'
PROJECTION_OPERATION_KIND = 'new_project_bootstrap_truth_projection_v1'
_DIGEST_RE = re.compile('^[0-9a-f]{64}$')

class CleanCoreStateV2Error(RuntimeError):
    """The new-project clean V2 profile is missing, partial, or unsafe."""

@dataclass(frozen=True, slots=True)
class CleanCoreStateV2Validation:
    database_path: str
    project_id: str
    schema_fingerprint: str
    semantic_snapshot_sha256: str
    domain_row_count: int
    source_record_count: int
    migration_receipt_count: int
    reconciliation_manifest_count: int
    event_count: int
    bootstrap_truth_projection_sha256: str

@dataclass(frozen=True, slots=True)
class CleanCoreStateV2InstallResult:
    database_path: str
    database_sha256: str
    schema_fingerprint: str
    semantic_snapshot_sha256: str
    replay_noop: bool
    automatic_legacy_import_performed: bool = False

@dataclass(frozen=True, slots=True)
class _BootstrapProjectionV1:
    receipt: VerifiedBootstrapReceiptV1
    source_bundle_sha256: str
    projection_sha256: str
    rows: tuple[tuple[str, tuple[dict[str, Any], ...]], ...]

    @property
    def rows_by_table(self) -> dict[str, tuple[dict[str, Any], ...]]:
        return dict(self.rows)

    @property
    def domain_row_count(self) -> int:
        return sum((len(rows) for _, rows in self.rows))

def install_clean_core_state_v2(config: QuestMemoryConfig, *, project_id: str, phase_hook: Callable[[str], None] | None=None) -> CleanCoreStateV2InstallResult:
    """Install schema plus exact new-project truth through controller COW."""
    _validate_binding(config, project_id)
    projection = _build_bootstrap_projection(config, project_id)
    database = config.sqlite_path.resolve(strict=True)
    schema_state = _installed_schema_state(config, database)
    if schema_state == 'exact':
        try:
            validation = validate_clean_core_state_v2(config, project_id=project_id)
        except CleanCoreStateV2Error:
            _validate_upgradeable_base_profile(config, project_id=project_id)
        else:
            return CleanCoreStateV2InstallResult(database_path=validation.database_path, database_sha256=_sha256_file(database), schema_fingerprint=validation.schema_fingerprint, semantic_snapshot_sha256=validation.semantic_snapshot_sha256, replay_noop=True)
    if schema_state != 'absent':
        schema_state = 'exact_base_profile'
    owner_key = _owner_key(config)
    connection = open_validated_sqlite_write(database, expected='existing', owner_key=owner_key, error_factory=CleanCoreStateV2Error)
    published = False
    try:
        connection.row_factory = sqlite3.Row
        v2._register_write_functions(connection)
        connection.execute('PRAGMA foreign_keys=ON')
        legacy_before = v2._non_core_legacy_snapshot_connection(connection)
        writer_schema_state = v2._schema_state(connection)
        if schema_state == 'absent' and writer_schema_state != 'absent':
            raise CleanCoreStateV2Error('Core V2 namespace changed before install')
        if schema_state == 'exact_base_profile':
            if writer_schema_state != 'exact':
                raise CleanCoreStateV2Error('base clean profile changed before upgrade')
            _validate_base_profile_connection(connection, config=config, project_id=project_id, require_projection_absent=True)
        _call_phase(phase_hook, 'clean_v2_prestate')
        connection.execute('BEGIN IMMEDIATE')
        try:
            if schema_state == 'absent':
                _install_base_schema_and_zero_import_genesis(connection, config=config, project_id=project_id, phase_hook=phase_hook)
            _insert_bootstrap_projection(connection, config=config, project_id=project_id, projection=projection)
            _call_phase(phase_hook, 'clean_v2_bootstrap_truth')
            _insert_projection_event(connection, config=config, project_id=project_id, projection=projection)
            _call_phase(phase_hook, 'clean_v2_projection_receipt')
            validation = _validate_connection(connection, config=config, project_id=project_id, projection=projection)
            if v2._non_core_legacy_snapshot_connection(connection) != legacy_before:
                raise CleanCoreStateV2Error('clean Core V2 installation changed pre-existing operational truth')
            _call_phase(phase_hook, 'clean_v2_postvalidate_precommit')
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        connection.close()
        published = True
    except sqlite3.Error as exc:
        raise CleanCoreStateV2Error(f'clean Core V2 transaction failed: {exc}') from exc
    finally:
        if not published:
            connection.abort_without_publish()
    _call_phase(phase_hook, 'clean_v2_postcommit_durable')
    durable = validate_clean_core_state_v2(config, project_id=project_id)
    return CleanCoreStateV2InstallResult(database_path=durable.database_path, database_sha256=_sha256_file(database), schema_fingerprint=durable.schema_fingerprint, semantic_snapshot_sha256=durable.semantic_snapshot_sha256, replay_noop=False)

def validate_clean_core_state_v2(config: QuestMemoryConfig, *, project_id: str) -> CleanCoreStateV2Validation:
    """Validate the exact clean profile through the owned stable-read path."""
    _validate_binding(config, project_id)
    projection = _build_bootstrap_projection(config, project_id)
    database = config.sqlite_path.resolve(strict=True)
    connection = open_owned_stable_read_only_sqlite(database, owner_key=_owner_key(config), error_factory=CleanCoreStateV2Error)
    try:
        connection.row_factory = sqlite3.Row
        validation = _validate_connection(connection, config=config, project_id=project_id, projection=projection)
    finally:
        connection.close()
    return CleanCoreStateV2Validation(database_path=str(database), project_id=validation.project_id, schema_fingerprint=validation.schema_fingerprint, semantic_snapshot_sha256=validation.semantic_snapshot_sha256, domain_row_count=validation.domain_row_count, source_record_count=validation.source_record_count, migration_receipt_count=validation.migration_receipt_count, reconciliation_manifest_count=validation.reconciliation_manifest_count, event_count=validation.event_count, bootstrap_truth_projection_sha256=validation.bootstrap_truth_projection_sha256)

def _validate_connection(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str, projection: _BootstrapProjectionV1) -> CleanCoreStateV2Validation:
    if v2._schema_state(connection) != 'exact':
        raise CleanCoreStateV2Error('Core V2 schema is absent, partial, future, or invalid')
    expected_fingerprint = v2.expected_schema_fingerprint_v2()
    if v2._metadata(connection) != _expected_metadata(config, project_id, projection):
        raise CleanCoreStateV2Error('clean Core V2 metadata mismatch')
    integrity = str(connection.execute('PRAGMA integrity_check').fetchone()[0])
    foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
    if integrity != 'ok' or foreign_keys:
        raise CleanCoreStateV2Error('clean Core V2 integrity or foreign keys failed')
    if int(connection.execute('PRAGMA user_version').fetchone()[0]) != 0:
        raise CleanCoreStateV2Error('clean Core V2 must not use PRAGMA user_version')
    table_counts = {table: int(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]) for table in v2._ALL_TABLES}
    for table in (v2.MIGRATION_RECEIPTS_TABLE, v2.RECONCILIATION_MANIFESTS_TABLE, v2.SOURCE_RECORDS_TABLE, v2.SOURCE_MAPPINGS_TABLE):
        if table_counts[table] != 0:
            raise CleanCoreStateV2Error(f'clean Core V2 import ledger is not empty: {table}')
    event_count = table_counts[v2.EVENTS_TABLE]
    if event_count < 2 or event_count > 1026 or table_counts[v2.IDEMPOTENCY_TABLE] != event_count:
        raise CleanCoreStateV2Error('clean Core V2 receipt chain has an unsupported length')
    if v2._validate_event_chain(connection) != event_count:
        raise CleanCoreStateV2Error('clean Core V2 event chain is invalid')
    base_event_digest = _require_base_event_binding(connection, config=config, project_id=project_id)
    _require_projection_event_binding(connection, config=config, project_id=project_id, projection=projection, previous_event_digest=base_event_digest)
    if event_count == 2:
        expected_rows = projection.rows_by_table
        for table in v2._DOMAIN_TABLES:
            _require_exact_rows(connection, table, expected_rows.get(table, ()))
    else:
        from projectrecall.core_memory_v1 import EVENT as memory_event, validate_memory_events_v1
        if any((str(row[0]) != memory_event for row in connection.execute(f'SELECT event_type FROM {v2.EVENTS_TABLE} WHERE ordinal>=3'))):
            raise CleanCoreStateV2Error('ProjectRecall accepts bootstrap and approved memory events only')
        memory_rows = validate_memory_events_v1(connection, project_id=project_id, projection=projection)
        expected_rows = {table: [dict(item) for item in rows] for table, rows in projection.rows_by_table.items()}
        for table, rows in memory_rows.items():
            expected_rows.setdefault(table, []).extend(rows)
        for table in v2._DOMAIN_TABLES:
            _require_exact_rows(connection, table, expected_rows.get(table, ()))
    snapshot = v2._semantic_snapshot_connection(connection)
    return CleanCoreStateV2Validation(database_path='', project_id=project_id, schema_fingerprint=expected_fingerprint, semantic_snapshot_sha256=str(snapshot['snapshot_sha256']), domain_row_count=sum((table_counts[table] for table in v2._DOMAIN_TABLES)), source_record_count=table_counts[v2.SOURCE_RECORDS_TABLE], migration_receipt_count=table_counts[v2.MIGRATION_RECEIPTS_TABLE], reconciliation_manifest_count=table_counts[v2.RECONCILIATION_MANIFESTS_TABLE], event_count=event_count, bootstrap_truth_projection_sha256=projection.projection_sha256)

def _base_metadata(config: QuestMemoryConfig, project_id: str) -> dict[str, str]:
    return {'automatic_legacy_task_truth_import': 'false', 'bootstrap_id': str(config.bootstrap_id), 'bootstrap_receipt_sha256': str(config.bootstrap_receipt_sha256), 'bootstrap_spec_sha256': str(config.bootstrap_spec_sha256), 'clean_profile_id': CLEAN_PROFILE_ID, 'installation_mode': INSTALLATION_MODE, 'project_id': project_id, 'schema_fingerprint': v2.expected_schema_fingerprint_v2(), 'schema_version': str(v2.SCHEMA_VERSION), 'selective_import_requires_separate_exact_approval': 'true'}

def _projection_metadata(projection: _BootstrapProjectionV1) -> dict[str, str]:
    return {'bootstrap_domain_row_count': str(projection.domain_row_count), 'bootstrap_truth_projection_schema': 'new_project_bootstrap_truth_v1', 'bootstrap_truth_projection_sha256': projection.projection_sha256, 'bootstrap_truth_source_bundle_sha256': projection.source_bundle_sha256}

def _expected_metadata(config: QuestMemoryConfig, project_id: str, projection: _BootstrapProjectionV1) -> dict[str, str]:
    return {**_base_metadata(config, project_id), **_projection_metadata(projection)}

def _base_request_body(config: QuestMemoryConfig, project_id: str) -> dict[str, object]:
    return {'bootstrap_id': config.bootstrap_id, 'bootstrap_receipt_sha256': config.bootstrap_receipt_sha256, 'bootstrap_spec_sha256': config.bootstrap_spec_sha256, 'clean_profile_id': CLEAN_PROFILE_ID, 'project_id': project_id, 'schema_fingerprint': v2.expected_schema_fingerprint_v2(), 'selective_import_requires_separate_exact_approval': True}

def _build_bootstrap_projection(config: QuestMemoryConfig, project_id: str, *, verified_receipt=None) -> _BootstrapProjectionV1:
    try:
        receipt = verified_receipt or verify_bootstrap_receipt_v1(config.project_root)
    except Exception as exc:
        if isinstance(exc, CleanCoreStateV2Error):
            raise
        raise CleanCoreStateV2Error('clean Core V2 cannot verify the exact bootstrap receipt') from exc
    if receipt.project_id != project_id or receipt.bootstrap_id != config.bootstrap_id or receipt.spec_sha256 != config.bootstrap_spec_sha256 or (receipt.receipt_sha256 != config.bootstrap_receipt_sha256):
        raise CleanCoreStateV2Error('bootstrap receipt differs from the clean Core V2 controller binding')
    files = {item.relative_path: item for item in receipt.operational_files}
    try:
        charter_digest = files['PROJECT_CHARTER.md'].sha256
        task_digest = files['CURRENT_TASK.md'].sha256
    except KeyError as exc:
        raise CleanCoreStateV2Error('bootstrap receipt lacks required project/task source digests') from exc
    source_bundle_sha = _sha256_json({'bootstrap_receipt_sha256': receipt.receipt_sha256, 'operational_files': [item.as_dict() for item in sorted(receipt.operational_files, key=lambda value: value.relative_path)]})
    main_quest_id = 'main-quest'
    project_row = {'completion_evidence_id': None, 'human_approval_evidence_id': None, 'human_approval_required': 1, 'main_quest_id': main_quest_id, 'project_id': receipt.project_id, 'quest_completion_level': 0, 'source_digest': charter_digest, 'status': 'planned', 'title': receipt.name}
    main_quest_row = {'completion_evidence_id': None, 'completion_level': 0, 'human_approval_evidence_id': None, 'human_approval_required': 1, 'main_quest_id': main_quest_id, 'project_id': receipt.project_id, 'source_digest': charter_digest, 'status': 'planned', 'title': receipt.objective}
    task = receipt.initial_task
    task_row = {'human_approval_required': 1, 'main_quest_id': main_quest_id, 'parent_epic_id': None, 'parent_main_quest_id': main_quest_id, 'parent_milestone_id': None, 'parent_side_quest_id': None, 'parent_task_id': None, 'parent_type': 'main_quest', 'project_id': receipt.project_id, 'source_digest': task_digest, 'status': 'planned', 'task_id': task.task_id, 'title': task.title}
    criteria = tuple(({'criterion_id': 'qm-bootstrap-criterion-' + _sha256_json({'bootstrap_receipt_sha256': receipt.receipt_sha256, 'description': description, 'index': index, 'task_id': task.task_id})[:24], 'description': description, 'main_quest_id': main_quest_id, 'project_id': receipt.project_id, 'required': 1, 'source_digest': task_digest, 'task_id': task.task_id} for index, description in enumerate(task.done_criteria)))
    rows = tuple(sorted(((v2.PROJECTS_TABLE, (project_row,)), (v2.MAIN_QUESTS_TABLE, (main_quest_row,)), (v2.TASKS_TABLE, (task_row,)), (v2.COMPLETION_CRITERIA_TABLE, criteria)), key=lambda item: item[0]))
    projection_body = {'bootstrap_id': receipt.bootstrap_id, 'bootstrap_receipt_sha256': receipt.receipt_sha256, 'project_id': receipt.project_id, 'protocol_id': 'questmemory-new-project-bootstrap-truth-projection-v1', 'rows': {table: list(table_rows) for table, table_rows in rows}, 'source_bundle_sha256': source_bundle_sha}
    return _BootstrapProjectionV1(receipt=receipt, source_bundle_sha256=source_bundle_sha, projection_sha256=_sha256_json(projection_body), rows=rows)

def _install_base_schema_and_zero_import_genesis(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str, phase_hook: Callable[[str], None] | None) -> None:
    connection.execute(v2._METADATA_DDL)
    for statement in v2._TABLE_DDL:
        connection.execute(statement)
    for statement in v2._INDEX_DDL:
        connection.execute(statement)
    for statement in v2._TRIGGER_DDL:
        connection.execute(statement)
    connection.executemany(f'INSERT INTO {v2.METADATA_TABLE}(key, value) VALUES (?, ?)', tuple(sorted(_base_metadata(config, project_id).items())))
    _call_phase(phase_hook, 'clean_v2_schema')
    contract = _base_event_contract(config, project_id)
    connection.execute(f'INSERT INTO {v2.EVENTS_TABLE}(event_id, project_id, ordinal, event_type, payload_json, previous_event_digest, event_digest) VALUES (?, NULL, 1, ?, ?, NULL, ?)', (contract['event_id'], EVENT_TYPE, contract['payload_json'], contract['event_digest']))
    connection.execute(f'INSERT INTO {v2.IDEMPOTENCY_TABLE}(operation_kind, idempotency_key, request_sha256, result_sha256) VALUES (?, ?, ?, ?)', (OPERATION_KIND, contract['idempotency_key'], contract['request_sha256'], contract['result_sha256']))
    _call_phase(phase_hook, 'clean_v2_genesis')

def _insert_bootstrap_projection(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str, projection: _BootstrapProjectionV1) -> None:
    if v2._metadata(connection) != _base_metadata(config, project_id):
        raise CleanCoreStateV2Error('base clean metadata changed before projection')
    if any((int(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]) for table in v2._DOMAIN_TABLES)):
        raise CleanCoreStateV2Error('clean domain is not empty before projection')
    connection.executemany(f'INSERT INTO {v2.METADATA_TABLE}(key, value) VALUES (?, ?)', tuple(sorted(_projection_metadata(projection).items())))
    for table, rows in projection.rows:
        for row in rows:
            _insert_exact_row(connection, table, row)

def _insert_exact_row(connection: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    columns = tuple(sorted(row))
    quoted = ', '.join((v2._quote_identifier(column) for column in columns))
    placeholders = ', '.join(('?' for _ in columns))
    connection.execute(f'INSERT INTO {v2._quote_identifier(table)}({quoted}) VALUES ({placeholders})', tuple((row[column] for column in columns)))

def _insert_projection_event(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str, projection: _BootstrapProjectionV1) -> None:
    previous = _require_base_event_binding(connection, config=config, project_id=project_id)
    contract = _projection_event_contract(connection, config=config, project_id=project_id, projection=projection, previous_event_digest=previous)
    connection.execute(f'INSERT INTO {v2.EVENTS_TABLE}(event_id, project_id, ordinal, event_type, payload_json, previous_event_digest, event_digest) VALUES (?, ?, 2, ?, ?, ?, ?)', (contract['event_id'], project_id, PROJECTION_EVENT_TYPE, contract['payload_json'], previous, contract['event_digest']))
    connection.execute(f'INSERT INTO {v2.IDEMPOTENCY_TABLE}(operation_kind, idempotency_key, request_sha256, result_sha256) VALUES (?, ?, ?, ?)', (PROJECTION_OPERATION_KIND, contract['idempotency_key'], contract['request_sha256'], contract['result_sha256']))

def _base_event_contract(config: QuestMemoryConfig, project_id: str) -> dict[str, str]:
    request = _base_request_body(config, project_id)
    request_sha = _sha256_json(request)
    event_id = f'qm-event-clean-v2-{request_sha[:24]}'
    payload_json = canonical_json_bytes({**request, 'automatic_legacy_import_performed': False, 'domain_row_count': 0, 'source_record_count': 0}).decode('utf-8')
    event_digest = v2._event_digest_v2(event_id, 1, EVENT_TYPE, payload_json, None)
    return {'event_digest': event_digest, 'event_id': event_id, 'idempotency_key': f'clean-v2:{config.bootstrap_id}', 'payload_json': payload_json, 'request_sha256': request_sha, 'result_sha256': _sha256_json({'event_digest': event_digest, 'schema_fingerprint': v2.expected_schema_fingerprint_v2(), 'zero_import': True})}

def _projection_event_contract(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str, projection: _BootstrapProjectionV1, previous_event_digest: str, semantic_domain_sha256: str | None=None) -> dict[str, str]:
    domain_sha = semantic_domain_sha256 or str(v2._domain_snapshot_connection(connection)['snapshot_sha256'])
    request = {'bootstrap_receipt_sha256': config.bootstrap_receipt_sha256, 'bootstrap_truth_projection_sha256': projection.projection_sha256, 'bootstrap_truth_source_bundle_sha256': projection.source_bundle_sha256, 'legacy_task_truth_imported': False, 'project_id': project_id, 'semantic_domain_sha256': domain_sha}
    request_sha = _sha256_json(request)
    event_id = f'qm-event-bootstrap-truth-v1-{request_sha[:24]}'
    payload_json = canonical_json_bytes({**request, 'domain_row_count': projection.domain_row_count, 'source_mapping_count': 0}).decode('utf-8')
    event_digest = v2._event_digest_v2(event_id, 2, PROJECTION_EVENT_TYPE, payload_json, previous_event_digest)
    return {'event_digest': event_digest, 'event_id': event_id, 'idempotency_key': f'bootstrap-truth:{config.bootstrap_id}', 'payload_json': payload_json, 'request_sha256': request_sha, 'result_sha256': _sha256_json({'event_digest': event_digest, 'projection_sha256': projection.projection_sha256, 'semantic_domain_sha256': domain_sha})}

def _require_base_event_binding(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str) -> str:
    contract = _base_event_contract(config, project_id)
    event = connection.execute(f'SELECT event_id, project_id, event_type, payload_json, previous_event_digest, event_digest FROM {v2.EVENTS_TABLE} WHERE ordinal=1').fetchone()
    if event is None or tuple(event) != (contract['event_id'], None, EVENT_TYPE, contract['payload_json'], None, contract['event_digest']):
        raise CleanCoreStateV2Error('clean Core V2 genesis event binding mismatch')
    idempotency = connection.execute(f'SELECT request_sha256, result_sha256 FROM {v2.IDEMPOTENCY_TABLE} WHERE operation_kind=? AND idempotency_key=?', (OPERATION_KIND, contract['idempotency_key'])).fetchone()
    if idempotency is None or tuple(idempotency) != (contract['request_sha256'], contract['result_sha256']):
        raise CleanCoreStateV2Error('clean Core V2 genesis idempotency mismatch')
    return contract['event_digest']

def _require_projection_event_binding(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str, projection: _BootstrapProjectionV1, previous_event_digest: str) -> None:
    stored = connection.execute(f'SELECT payload_json FROM {v2.EVENTS_TABLE} WHERE ordinal=2').fetchone()
    if stored is None:
        raise CleanCoreStateV2Error('bootstrap truth projection event is missing')
    try:
        stored_payload = json.loads(str(stored[0]))
        stored_domain_sha = stored_payload['semantic_domain_sha256']
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CleanCoreStateV2Error('bootstrap truth projection payload is invalid') from exc
    if not isinstance(stored_domain_sha, str) or _DIGEST_RE.fullmatch(stored_domain_sha) is None:
        raise CleanCoreStateV2Error('bootstrap truth projection domain binding is invalid')
    contract = _projection_event_contract(connection, config=config, project_id=project_id, projection=projection, previous_event_digest=previous_event_digest, semantic_domain_sha256=stored_domain_sha)
    event = connection.execute(f'SELECT event_id, project_id, event_type, payload_json, previous_event_digest, event_digest FROM {v2.EVENTS_TABLE} WHERE ordinal=2').fetchone()
    if event is None or tuple(event) != (contract['event_id'], project_id, PROJECTION_EVENT_TYPE, contract['payload_json'], previous_event_digest, contract['event_digest']):
        raise CleanCoreStateV2Error('bootstrap truth projection event mismatch')
    idempotency = connection.execute(f'SELECT request_sha256, result_sha256 FROM {v2.IDEMPOTENCY_TABLE} WHERE operation_kind=? AND idempotency_key=?', (PROJECTION_OPERATION_KIND, contract['idempotency_key'])).fetchone()
    if idempotency is None or tuple(idempotency) != (contract['request_sha256'], contract['result_sha256']):
        raise CleanCoreStateV2Error('bootstrap truth projection idempotency mismatch')

def _validate_upgradeable_base_profile(config: QuestMemoryConfig, *, project_id: str) -> None:
    connection = open_owned_stable_read_only_sqlite(config.sqlite_path.resolve(strict=True), owner_key=_owner_key(config), error_factory=CleanCoreStateV2Error)
    try:
        connection.row_factory = sqlite3.Row
        _validate_base_profile_connection(connection, config=config, project_id=project_id, require_projection_absent=True)
    finally:
        connection.close()

def _validate_base_profile_connection(connection: sqlite3.Connection, *, config: QuestMemoryConfig, project_id: str, require_projection_absent: bool) -> None:
    if v2._schema_state(connection) != 'exact':
        raise CleanCoreStateV2Error('base clean Core V2 schema is not exact')
    metadata = v2._metadata(connection)
    base = _base_metadata(config, project_id)
    if any((metadata.get(key) != value for key, value in base.items())):
        raise CleanCoreStateV2Error('base clean Core V2 metadata mismatch')
    if require_projection_absent and metadata != base:
        raise CleanCoreStateV2Error('partial bootstrap projection metadata exists')
    if require_projection_absent:
        if any((int(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]) for table in v2._DOMAIN_TABLES)):
            raise CleanCoreStateV2Error('base clean Core V2 domain is not empty')
        event_count = int(connection.execute(f'SELECT count(*) FROM {v2.EVENTS_TABLE}').fetchone()[0])
        idempotency_count = int(connection.execute(f'SELECT count(*) FROM {v2.IDEMPOTENCY_TABLE}').fetchone()[0])
        if event_count != 1 or idempotency_count != 1:
            raise CleanCoreStateV2Error('base clean Core V2 receipt counts changed')
        if v2._validate_event_chain(connection) != 1:
            raise CleanCoreStateV2Error('base clean Core V2 event chain changed')
    _require_base_event_binding(connection, config=config, project_id=project_id)

def _require_exact_rows(connection: sqlite3.Connection, table: str, expected: Sequence[Mapping[str, Any]]) -> None:
    actual = [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]
    actual_bytes = sorted((canonical_json_bytes(row) for row in actual))
    expected_bytes = sorted((canonical_json_bytes(dict(row)) for row in expected))
    if actual_bytes != expected_bytes:
        raise CleanCoreStateV2Error(f'clean Core V2 bootstrap truth differs from its receipt: {table}')

def _validate_binding(config: QuestMemoryConfig, project_id: str) -> None:
    config.validate_paths()
    if config.controller_kind != FUTURE_PROJECT_CONTROLLER_KIND:
        raise CleanCoreStateV2Error('clean Core V2 installation is limited to future-project controllers')
    if not isinstance(project_id, str) or not project_id.strip():
        raise CleanCoreStateV2Error('project_id is required')
    for label, value in (('bootstrap_spec_sha256', config.bootstrap_spec_sha256), ('bootstrap_receipt_sha256', config.bootstrap_receipt_sha256)):
        if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
            raise CleanCoreStateV2Error(f'{label} is missing or invalid')
    if not isinstance(config.bootstrap_id, str) or not config.bootstrap_id.startswith('future-project-bootstrap-v1-'):
        raise CleanCoreStateV2Error('bootstrap_id is missing or invalid')
    if config.sqlite_owner_token is None:
        raise CleanCoreStateV2Error('SQLite owner token is missing')

def _owner_key(config: QuestMemoryConfig) -> str:
    assert config.sqlite_owner_token is not None
    return controller_sqlite_owner_key(config.project_root.resolve(), config.sqlite_path.resolve(), config.sqlite_owner_token, error_factory=CleanCoreStateV2Error)

def _installed_schema_state(config: QuestMemoryConfig, database: Path) -> str:
    connection = open_owned_stable_read_only_sqlite(database, owner_key=_owner_key(config), error_factory=CleanCoreStateV2Error)
    try:
        connection.row_factory = sqlite3.Row
        return v2._schema_state(connection)
    finally:
        connection.close()

def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError as exc:
        raise CleanCoreStateV2Error('cannot hash controller SQLite') from exc
    return digest.hexdigest()

def _call_phase(hook: Callable[[str], None] | None, phase: str) -> None:
    if hook is not None:
        hook(phase)
