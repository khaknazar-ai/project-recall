# Derived from QuestMemory; see NOTICE.md.
from __future__ import annotations
import hashlib
import sqlite3
from functools import lru_cache
from typing import Any
from projectrecall import core_state_v1 as v1
from projectrecall.canonical_json import canonical_json_bytes
PROPOSAL_ID = 'CP-2026-08-31-001'
SCHEMA_VERSION = 2
CORE_PREFIX = 'questmemory_core_v2_'
APPROVAL_TEXT = 'Одобряю CP-2026-08-31-001, вариант 1: сохранить Epic как отдельный additive entity в новой Core Schema V2 и продолжить только provider-free owner-resolution tooling и semantic-import тесты на disposable SQLite. Не разрешаю owner decisions от моего имени, запись в data/questmemory.sqlite, protected writes, providers, release или push; canonical application и approval точного owner-resolved manifest остаются отдельными подтверждениями.'
APPROVAL_TEXT_UTF8_LENGTH = 576
APPROVAL_TEXT_SHA256 = '9f88a5e70c8fa3f9f775d58c59c48213ea5af43c4eeaff1edbc627cfe8722dda'
APPROVAL_SCOPE = 'cp-2026-08-31-001-option-1-disposable-tooling-only'
METADATA_TABLE = f'{CORE_PREFIX}metadata'
MIGRATION_RECEIPTS_TABLE = f'{CORE_PREFIX}migration_receipts'
RECONCILIATION_MANIFESTS_TABLE = f'{CORE_PREFIX}reconciliation_manifests'
SOURCE_RECORDS_TABLE = f'{CORE_PREFIX}source_records'
SOURCE_MAPPINGS_TABLE = f'{CORE_PREFIX}source_mappings'
PROJECTS_TABLE = f'{CORE_PREFIX}projects'
MAIN_QUESTS_TABLE = f'{CORE_PREFIX}main_quests'
MILESTONES_TABLE = f'{CORE_PREFIX}milestones'
EPICS_TABLE = f'{CORE_PREFIX}epics'
SIDE_QUESTS_TABLE = f'{CORE_PREFIX}side_quests'
TASKS_TABLE = f'{CORE_PREFIX}tasks'
TASK_DEPENDENCIES_TABLE = f'{CORE_PREFIX}task_dependencies'
COMPLETION_CRITERIA_TABLE = f'{CORE_PREFIX}completion_criteria'
EVIDENCE_TABLE = f'{CORE_PREFIX}evidence'
EVIDENCE_LINKS_TABLE = f'{CORE_PREFIX}evidence_links'
DECISIONS_TABLE = f'{CORE_PREFIX}decisions'
CONSTRAINTS_TABLE = f'{CORE_PREFIX}constraints'
MEMORY_PATCHES_TABLE = f'{CORE_PREFIX}memory_patches'
MEMORY_PATCH_REVIEWS_TABLE = f'{CORE_PREFIX}memory_patch_reviews'
MEMORY_PATCH_APPLICATIONS_TABLE = f'{CORE_PREFIX}memory_patch_applications'
EVENTS_TABLE = f'{CORE_PREFIX}events'
IDEMPOTENCY_TABLE = f'{CORE_PREFIX}idempotency'

class CoreStateV2Error(RuntimeError):
    """Base exception for the V2 disposable boundary."""

class CoreStateV2SchemaError(CoreStateV2Error):
    """V2 schema or semantic state is partial, future, or invalid."""

def _v2_sql(statement: str) -> str:
    return statement.replace(v1.CORE_PREFIX, CORE_PREFIX).replace('questmemory_event_digest_v1', 'questmemory_event_digest_v2')
_METADATA_DDL = _v2_sql(v1._METADATA_DDL)
_MIGRATION_RECEIPTS_DDL = f"\nCREATE TABLE {MIGRATION_RECEIPTS_TABLE} (\n    receipt_id TEXT PRIMARY KEY,\n    proposal_id TEXT NOT NULL CHECK(proposal_id = '{PROPOSAL_ID}'),\n    selected_option INTEGER NOT NULL CHECK(selected_option = 1),\n    approval_scope TEXT NOT NULL CHECK(\n        approval_scope IN\n        ('{APPROVAL_SCOPE}','synthetic-test-only-no-owner-authority')),\n    approval_text_sha256 TEXT NOT NULL CHECK(length(approval_text_sha256) = 64),\n    approval_text_utf8_length INTEGER NOT NULL CHECK(approval_text_utf8_length = 576),\n    schema_version INTEGER NOT NULL CHECK(schema_version = 2),\n    schema_fingerprint TEXT NOT NULL CHECK(length(schema_fingerprint) = 64),\n    v1_schema_fingerprint TEXT NOT NULL CHECK(length(v1_schema_fingerprint) = 64),\n    v1_semantic_snapshot_sha256 TEXT NOT NULL\n        CHECK(length(v1_semantic_snapshot_sha256) = 64),\n    manifest_file_sha256 TEXT NOT NULL CHECK(length(manifest_file_sha256) = 64),\n    manifest_content_sha256 TEXT NOT NULL CHECK(length(manifest_content_sha256) = 64),\n    source_sqlite_sha256 TEXT NOT NULL CHECK(length(source_sqlite_sha256) = 64),\n    legacy_snapshot_sha256 TEXT NOT NULL CHECK(length(legacy_snapshot_sha256) = 64),\n    v1_legacy_snapshot_sha256 TEXT NOT NULL\n        CHECK(length(v1_legacy_snapshot_sha256) = 64),\n    mode TEXT NOT NULL CHECK(mode IN\n        ('schema_v2_disposable','semantic_import_disposable_synthetic',\n         'semantic_import_disposable_owner_resolved')),\n    synthetic_fixture_sha256 TEXT,\n    synthetic_fixture_id TEXT,\n    synthetic_fixture_json TEXT CHECK(\n        synthetic_fixture_json IS NULL OR json_valid(synthetic_fixture_json)),\n    semantic_domain_sha256 TEXT,\n    owner_response_file_sha256 TEXT,\n    owner_resolved_candidate_file_sha256 TEXT,\n    disposable_import_approval_file_sha256 TEXT,\n    canonical_application_approval_sha256 TEXT,\n    owner_decisions_authorized INTEGER NOT NULL CHECK(owner_decisions_authorized = 0),\n    canonical_application_authorized INTEGER NOT NULL\n        CHECK(canonical_application_authorized = 0),\n    provider_or_network_authorized INTEGER NOT NULL\n        CHECK(provider_or_network_authorized = 0),\n    request_sha256 TEXT NOT NULL UNIQUE CHECK(length(request_sha256) = 64),\n    CHECK(canonical_application_approval_sha256 IS NULL),\n    CHECK(\n        (mode = 'schema_v2_disposable'\n         AND synthetic_fixture_sha256 IS NULL\n         AND synthetic_fixture_id IS NULL\n         AND synthetic_fixture_json IS NULL\n         AND semantic_domain_sha256 IS NULL\n         AND owner_response_file_sha256 IS NULL\n         AND owner_resolved_candidate_file_sha256 IS NULL\n         AND disposable_import_approval_file_sha256 IS NULL)\n        OR\n        (mode = 'semantic_import_disposable_synthetic'\n         AND synthetic_fixture_sha256 IS NOT NULL\n         AND synthetic_fixture_id IS NOT NULL\n         AND synthetic_fixture_json IS NOT NULL\n         AND semantic_domain_sha256 IS NOT NULL\n         AND owner_response_file_sha256 IS NULL\n         AND owner_resolved_candidate_file_sha256 IS NULL\n         AND disposable_import_approval_file_sha256 IS NULL)\n        OR\n        (mode = 'semantic_import_disposable_owner_resolved'\n         AND synthetic_fixture_sha256 IS NULL\n         AND synthetic_fixture_id IS NULL\n         AND synthetic_fixture_json IS NULL\n         AND semantic_domain_sha256 IS NOT NULL\n         AND owner_response_file_sha256 IS NOT NULL\n         AND owner_resolved_candidate_file_sha256 IS NOT NULL\n         AND disposable_import_approval_file_sha256 IS NOT NULL)\n    )\n) WITHOUT ROWID\n".strip()
_EPICS_DDL = f"\nCREATE TABLE {EPICS_TABLE} (\n    epic_id TEXT PRIMARY KEY,\n    project_id TEXT NOT NULL,\n    main_quest_id TEXT NOT NULL,\n    parent_type TEXT NOT NULL CHECK(parent_type IN ('main_quest','milestone')),\n    parent_main_quest_id TEXT,\n    parent_milestone_id TEXT,\n    title TEXT NOT NULL,\n    status TEXT NOT NULL CHECK(status IN\n        ('not_started','planned','in_progress','active','blocked',\n         'completed','archived','superseded')),\n    completion_level INTEGER NOT NULL DEFAULT 0\n        CHECK(completion_level BETWEEN 0 AND 100),\n    human_approval_required INTEGER NOT NULL DEFAULT 0\n        CHECK(human_approval_required IN (0,1)),\n    completion_evidence_id TEXT,\n    human_approval_evidence_id TEXT,\n    source_digest TEXT NOT NULL CHECK(length(source_digest) = 64),\n    UNIQUE(project_id, main_quest_id, epic_id),\n    CHECK((parent_main_quest_id IS NOT NULL) +\n          (parent_milestone_id IS NOT NULL) = 1),\n    CHECK((parent_type = 'main_quest'\n           AND parent_main_quest_id = main_quest_id\n           AND parent_milestone_id IS NULL)\n       OR (parent_type = 'milestone'\n           AND parent_main_quest_id IS NULL\n           AND parent_milestone_id IS NOT NULL)),\n    CHECK(status <> 'completed' OR\n          (completion_level = 100 AND completion_evidence_id IS NOT NULL\n           AND (human_approval_required = 0\n                OR human_approval_evidence_id IS NOT NULL))),\n    FOREIGN KEY(project_id, main_quest_id)\n        REFERENCES {MAIN_QUESTS_TABLE}(project_id, main_quest_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,\n    FOREIGN KEY(project_id, main_quest_id, parent_milestone_id)\n        REFERENCES {MILESTONES_TABLE}(project_id, main_quest_id, milestone_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,\n    FOREIGN KEY(project_id, completion_evidence_id)\n        REFERENCES {EVIDENCE_TABLE}(project_id, evidence_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,\n    FOREIGN KEY(project_id, human_approval_evidence_id)\n        REFERENCES {EVIDENCE_TABLE}(project_id, evidence_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED\n) WITHOUT ROWID\n".strip()
_TASKS_DDL = f"\nCREATE TABLE {TASKS_TABLE} (\n    task_id TEXT PRIMARY KEY,\n    project_id TEXT NOT NULL,\n    main_quest_id TEXT NOT NULL,\n    parent_type TEXT NOT NULL CHECK(parent_type IN\n        ('main_quest','milestone','epic','side_quest','task')),\n    parent_main_quest_id TEXT,\n    parent_milestone_id TEXT,\n    parent_epic_id TEXT,\n    parent_side_quest_id TEXT,\n    parent_task_id TEXT,\n    title TEXT NOT NULL,\n    status TEXT NOT NULL CHECK(status IN\n        ('not_started','planned','in_progress','active','blocked',\n         'completed','archived','superseded')),\n    human_approval_required INTEGER NOT NULL DEFAULT 0\n        CHECK(human_approval_required IN (0,1)),\n    source_digest TEXT NOT NULL CHECK(length(source_digest) = 64),\n    UNIQUE(project_id, main_quest_id, task_id),\n    CHECK((parent_main_quest_id IS NOT NULL) +\n          (parent_milestone_id IS NOT NULL) +\n          (parent_epic_id IS NOT NULL) +\n          (parent_side_quest_id IS NOT NULL) +\n          (parent_task_id IS NOT NULL) = 1),\n    CHECK((parent_type = 'main_quest' AND parent_main_quest_id = main_quest_id\n           AND parent_milestone_id IS NULL AND parent_epic_id IS NULL\n           AND parent_side_quest_id IS NULL AND parent_task_id IS NULL)\n       OR (parent_type = 'milestone' AND parent_main_quest_id IS NULL\n           AND parent_milestone_id IS NOT NULL AND parent_epic_id IS NULL\n           AND parent_side_quest_id IS NULL AND parent_task_id IS NULL)\n       OR (parent_type = 'epic' AND parent_main_quest_id IS NULL\n           AND parent_milestone_id IS NULL AND parent_epic_id IS NOT NULL\n           AND parent_side_quest_id IS NULL AND parent_task_id IS NULL)\n       OR (parent_type = 'side_quest' AND parent_main_quest_id IS NULL\n           AND parent_milestone_id IS NULL AND parent_epic_id IS NULL\n           AND parent_side_quest_id IS NOT NULL AND parent_task_id IS NULL)\n       OR (parent_type = 'task' AND parent_main_quest_id IS NULL\n           AND parent_milestone_id IS NULL AND parent_epic_id IS NULL\n           AND parent_side_quest_id IS NULL AND parent_task_id IS NOT NULL)),\n    FOREIGN KEY(project_id, main_quest_id)\n        REFERENCES {MAIN_QUESTS_TABLE}(project_id, main_quest_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,\n    FOREIGN KEY(project_id, main_quest_id, parent_milestone_id)\n        REFERENCES {MILESTONES_TABLE}(project_id, main_quest_id, milestone_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,\n    FOREIGN KEY(project_id, main_quest_id, parent_epic_id)\n        REFERENCES {EPICS_TABLE}(project_id, main_quest_id, epic_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,\n    FOREIGN KEY(project_id, main_quest_id, parent_side_quest_id)\n        REFERENCES {SIDE_QUESTS_TABLE}(project_id, main_quest_id, side_quest_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,\n    FOREIGN KEY(project_id, main_quest_id, parent_task_id)\n        REFERENCES {TASKS_TABLE}(project_id, main_quest_id, task_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED\n) WITHOUT ROWID\n".strip()

def _build_table_ddl() -> tuple[str, ...]:
    statements: list[str] = []
    for statement in v1._TABLE_DDL:
        if f'CREATE TABLE {v1.MIGRATION_RECEIPTS_TABLE}' in statement:
            statements.append(_MIGRATION_RECEIPTS_DDL)
            continue
        if f'CREATE TABLE {v1.TASKS_TABLE}' in statement:
            statements.extend((_EPICS_DDL, _TASKS_DDL))
            continue
        statements.append(_v2_sql(statement))
    return tuple(statements)
_TABLE_DDL = _build_table_ddl()
_INDEX_DDL = tuple((_v2_sql(statement) for statement in v1._INDEX_DDL)) + (f'CREATE INDEX {CORE_PREFIX}epics_parent_idx ON {EPICS_TABLE}(project_id, main_quest_id, parent_milestone_id)', f'CREATE INDEX {CORE_PREFIX}tasks_epic_parent_idx ON {TASKS_TABLE}(project_id, main_quest_id, parent_epic_id)')

def _extra_trigger_ddl() -> tuple[str, ...]:
    epic_completion = tuple((_v2_sql(statement) for statement in v1._quest_completion_triggers(EPICS_TABLE, 'epic')))
    epic_digest = tuple((_v2_sql(statement) for statement in v1._digest_format_triggers(EPICS_TABLE, 'epics', ('source_digest',))))
    receipt_digest = tuple((_v2_sql(statement) for statement in v1._digest_format_triggers(MIGRATION_RECEIPTS_TABLE, 'migration_receipts_v2_authority', ('approval_text_sha256', 'v1_schema_fingerprint', 'v1_semantic_snapshot_sha256', 'v1_legacy_snapshot_sha256', 'synthetic_fixture_sha256', 'semantic_domain_sha256', 'owner_response_file_sha256', 'owner_resolved_candidate_file_sha256', 'disposable_import_approval_file_sha256'))))
    hierarchy = (f"\nCREATE TRIGGER {CORE_PREFIX}main_quest_parent_terminal_insert_guard\nBEFORE INSERT ON {MAIN_QUESTS_TABLE}\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {PROJECTS_TABLE}\n        WHERE project_id = NEW.project_id AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:project') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}main_quest_parent_terminal_update_guard\nBEFORE UPDATE OF project_id ON {MAIN_QUESTS_TABLE}\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {PROJECTS_TABLE}\n        WHERE project_id = NEW.project_id AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:project') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}milestone_parent_terminal_insert_guard\nBEFORE INSERT ON {MILESTONES_TABLE}\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}milestone_parent_terminal_update_guard\nBEFORE UPDATE OF project_id, main_quest_id ON {MILESTONES_TABLE}\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}side_quest_parent_terminal_insert_guard\nBEFORE INSERT ON {SIDE_QUESTS_TABLE}\nBEGIN\n    SELECT CASE WHEN NEW.parent_main_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.parent_main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\n    SELECT CASE WHEN NEW.parent_milestone_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MILESTONES_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND milestone_id = NEW.parent_milestone_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:milestone') END;\n    SELECT CASE WHEN NEW.parent_side_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {SIDE_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND side_quest_id = NEW.parent_side_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:side_quest') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}side_quest_parent_terminal_update_guard\nBEFORE UPDATE OF parent_type, parent_main_quest_id, parent_milestone_id,\n                 parent_side_quest_id\nON {SIDE_QUESTS_TABLE}\nBEGIN\n    SELECT CASE WHEN NEW.parent_main_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.parent_main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\n    SELECT CASE WHEN NEW.parent_milestone_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MILESTONES_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND milestone_id = NEW.parent_milestone_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:milestone') END;\n    SELECT CASE WHEN NEW.parent_side_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {SIDE_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND side_quest_id = NEW.parent_side_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:side_quest') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}side_quest_drift_approval_insert_guard\nBEFORE INSERT ON {SIDE_QUESTS_TABLE}\nWHEN NEW.relevance_status = 'drift_approved'\nBEGIN\n    SELECT CASE WHEN NOT EXISTS (\n        SELECT 1 FROM {EVIDENCE_TABLE}\n        WHERE project_id = NEW.project_id\n          AND evidence_id = NEW.plan_drift_approval_evidence_id\n          AND evidence_kind = 'human_approval'\n          AND verification_status = 'accepted'\n    ) THEN RAISE(ABORT, 'side_quest_plan_drift_approval_missing') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}side_quest_drift_approval_update_guard\nBEFORE UPDATE OF relevance_status, plan_drift_approval_evidence_id\nON {SIDE_QUESTS_TABLE}\nWHEN NEW.relevance_status = 'drift_approved'\nBEGIN\n    SELECT CASE WHEN NOT EXISTS (\n        SELECT 1 FROM {EVIDENCE_TABLE}\n        WHERE project_id = NEW.project_id\n          AND evidence_id = NEW.plan_drift_approval_evidence_id\n          AND evidence_kind = 'human_approval'\n          AND verification_status = 'accepted'\n    ) THEN RAISE(ABORT, 'side_quest_plan_drift_approval_missing') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}project_epic_completion_descendant_gate\nBEFORE UPDATE OF status ON {PROJECTS_TABLE}\nWHEN NEW.status = 'completed' AND OLD.status <> 'completed'\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {EPICS_TABLE}\n        WHERE project_id = OLD.project_id AND status <> 'completed'\n    ) THEN RAISE(ABORT, 'project_completion_epic_incomplete') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}main_quest_epic_completion_descendant_gate\nBEFORE UPDATE OF status ON {MAIN_QUESTS_TABLE}\nWHEN NEW.status = 'completed' AND OLD.status <> 'completed'\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {EPICS_TABLE}\n        WHERE project_id = OLD.project_id\n          AND main_quest_id = OLD.main_quest_id\n          AND status <> 'completed'\n    ) THEN RAISE(ABORT, 'main_quest_completion_epic_incomplete') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}milestone_epic_completion_descendant_gate\nBEFORE UPDATE OF status ON {MILESTONES_TABLE}\nWHEN NEW.status = 'completed' AND OLD.status <> 'completed'\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {EPICS_TABLE}\n        WHERE parent_milestone_id = OLD.milestone_id AND status <> 'completed'\n    ) THEN RAISE(ABORT, 'milestone_completion_epic_incomplete') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}epic_completion_descendant_gate\nBEFORE UPDATE OF status ON {EPICS_TABLE}\nWHEN NEW.status = 'completed' AND OLD.status <> 'completed'\nBEGIN\n    SELECT CASE WHEN EXISTS (\n        SELECT 1 FROM {TASKS_TABLE}\n        WHERE parent_epic_id = OLD.epic_id AND status <> 'completed'\n    ) THEN RAISE(ABORT, 'epic_completion_task_incomplete') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}epic_parent_terminal_insert_guard\nBEFORE INSERT ON {EPICS_TABLE}\nBEGIN\n    SELECT CASE WHEN NEW.parent_main_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.parent_main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\n    SELECT CASE WHEN NEW.parent_milestone_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MILESTONES_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND milestone_id = NEW.parent_milestone_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:milestone') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}epic_parent_terminal_update_guard\nBEFORE UPDATE OF parent_type, parent_main_quest_id, parent_milestone_id\nON {EPICS_TABLE}\nBEGIN\n    SELECT CASE WHEN NEW.parent_main_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.parent_main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\n    SELECT CASE WHEN NEW.parent_milestone_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MILESTONES_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND milestone_id = NEW.parent_milestone_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:milestone') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}task_parent_terminal_insert_guard\nBEFORE INSERT ON {TASKS_TABLE}\nBEGIN\n    SELECT CASE WHEN NEW.parent_main_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.parent_main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\n    SELECT CASE WHEN NEW.parent_milestone_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MILESTONES_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND milestone_id = NEW.parent_milestone_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:milestone') END;\n    SELECT CASE WHEN NEW.parent_epic_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {EPICS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND epic_id = NEW.parent_epic_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:epic') END;\n    SELECT CASE WHEN NEW.parent_side_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {SIDE_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND side_quest_id = NEW.parent_side_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:side_quest') END;\nEND\n".strip(), f"\nCREATE TRIGGER {CORE_PREFIX}task_parent_terminal_update_guard\nBEFORE UPDATE OF parent_type, parent_main_quest_id, parent_milestone_id,\n                 parent_epic_id, parent_side_quest_id, parent_task_id\nON {TASKS_TABLE}\nBEGIN\n    SELECT CASE WHEN NEW.parent_main_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MAIN_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.parent_main_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:main_quest') END;\n    SELECT CASE WHEN NEW.parent_milestone_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {MILESTONES_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND milestone_id = NEW.parent_milestone_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:milestone') END;\n    SELECT CASE WHEN NEW.parent_epic_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {EPICS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND epic_id = NEW.parent_epic_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:epic') END;\n    SELECT CASE WHEN NEW.parent_side_quest_id IS NOT NULL AND EXISTS (\n        SELECT 1 FROM {SIDE_QUESTS_TABLE}\n        WHERE project_id = NEW.project_id\n          AND main_quest_id = NEW.main_quest_id\n          AND side_quest_id = NEW.parent_side_quest_id\n          AND status = 'completed'\n    ) THEN RAISE(ABORT, 'completed_parent_guard:side_quest') END;\nEND\n".strip())
    return epic_completion + epic_digest + receipt_digest + hierarchy
_TRIGGER_DDL = tuple((_v2_sql(statement) for statement in v1._TRIGGER_DDL)) + _extra_trigger_ddl()
_ALL_TABLES = (METADATA_TABLE, MIGRATION_RECEIPTS_TABLE, RECONCILIATION_MANIFESTS_TABLE, SOURCE_RECORDS_TABLE, SOURCE_MAPPINGS_TABLE, PROJECTS_TABLE, MAIN_QUESTS_TABLE, MILESTONES_TABLE, EPICS_TABLE, SIDE_QUESTS_TABLE, TASKS_TABLE, TASK_DEPENDENCIES_TABLE, COMPLETION_CRITERIA_TABLE, EVIDENCE_TABLE, EVIDENCE_LINKS_TABLE, DECISIONS_TABLE, CONSTRAINTS_TABLE, MEMORY_PATCHES_TABLE, MEMORY_PATCH_REVIEWS_TABLE, MEMORY_PATCH_APPLICATIONS_TABLE, EVENTS_TABLE, IDEMPOTENCY_TABLE)
_DOMAIN_TABLES = (SOURCE_RECORDS_TABLE, SOURCE_MAPPINGS_TABLE, PROJECTS_TABLE, MAIN_QUESTS_TABLE, MILESTONES_TABLE, EPICS_TABLE, SIDE_QUESTS_TABLE, TASKS_TABLE, TASK_DEPENDENCIES_TABLE, COMPLETION_CRITERIA_TABLE, EVIDENCE_TABLE, EVIDENCE_LINKS_TABLE, DECISIONS_TABLE, CONSTRAINTS_TABLE, MEMORY_PATCHES_TABLE, MEMORY_PATCH_REVIEWS_TABLE, MEMORY_PATCH_APPLICATIONS_TABLE)

def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

def _event_digest_v2(event_id: str, ordinal: int, event_type: str, payload_json: str, previous_event_digest: str | None) -> str:
    return _sha256_bytes(canonical_json_bytes({'event_id': event_id, 'event_type': event_type, 'ordinal': int(ordinal), 'payload_json': payload_json, 'previous_event_digest': previous_event_digest}))

def _register_write_functions(connection: sqlite3.Connection) -> None:
    connection.create_function('questmemory_event_digest_v2', 5, _event_digest_v2, deterministic=True)

def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'

def _schema_rows(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = connection.execute('SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE sql IS NOT NULL AND (name LIKE ? OR tbl_name LIKE ?) ORDER BY type, name, tbl_name, sql', (f'{CORE_PREFIX}%', f'{CORE_PREFIX}%')).fetchall()
    return [{'name': str(row['name']), 'sql': str(row['sql']), 'table_name': str(row['tbl_name']), 'type': str(row['type'])} for row in rows]

def _schema_fingerprint_connection(connection: sqlite3.Connection) -> str:
    return _sha256_bytes(canonical_json_bytes(_schema_rows(connection)))

@lru_cache(maxsize=1)
def expected_schema_fingerprint_v2() -> str:
    if len(APPROVAL_TEXT.encode('utf-8')) != APPROVAL_TEXT_UTF8_LENGTH:
        raise CoreStateV2SchemaError('embedded approval UTF-8 length drifted')
    if _sha256_bytes(APPROVAL_TEXT.encode('utf-8')) != APPROVAL_TEXT_SHA256:
        raise CoreStateV2SchemaError('embedded approval SHA-256 drifted')
    connection = sqlite3.connect(':memory:', isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        _register_write_functions(connection)
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute(_METADATA_DDL)
        for statement in _TABLE_DDL:
            connection.execute(statement)
        for statement in _INDEX_DDL:
            connection.execute(statement)
        for statement in _TRIGGER_DDL:
            connection.execute(statement)
        return _schema_fingerprint_connection(connection)
    except sqlite3.Error as exc:
        raise CoreStateV2SchemaError(f'V2 schema definition is not executable: {exc}') from exc
    finally:
        connection.close()

def _schema_state(connection: sqlite3.Connection) -> str:
    rows = _schema_rows(connection)
    if not rows:
        return 'absent'
    if _schema_fingerprint_connection(connection) != expected_schema_fingerprint_v2():
        return 'invalid'
    return 'exact'

def _typed_cell(value: Any) -> list[Any]:
    if value is None:
        return ['null', None]
    if isinstance(value, bytes):
        return ['blob', value.hex()]
    if isinstance(value, str):
        return ['text', value]
    if isinstance(value, int):
        return ['integer', str(value)]
    if isinstance(value, float):
        return ['real', value.hex()]
    raise CoreStateV2SchemaError(f'unsupported SQLite value: {type(value)!r}')

def _typed_table_payload(connection: sqlite3.Connection, table_name: str) -> dict[str, Any]:
    info = connection.execute(f'PRAGMA table_info({_quote_identifier(table_name)})').fetchall()
    columns = [str(row[1]) for row in info]
    if not columns:
        raise CoreStateV2SchemaError(f'table metadata missing: {table_name}')
    primary = [name for _rank, name in sorted(((int(row[5]), str(row[1])) for row in info if int(row[5]) > 0))]
    order = primary or columns
    select_columns = ', '.join((_quote_identifier(column) for column in columns))
    order_columns = ', '.join((_quote_identifier(column) for column in order))
    rows = connection.execute(f'SELECT {select_columns} FROM {_quote_identifier(table_name)} ORDER BY {order_columns}').fetchall()
    payload_rows = [[_typed_cell(value) for value in row] for row in rows]
    payload = {'columns': columns, 'row_count': len(rows), 'rows': payload_rows}
    return {'columns': columns, 'row_count': len(rows), 'rows_sha256': _sha256_bytes(canonical_json_bytes(payload))}

def _semantic_snapshot_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = {table: _typed_table_payload(connection, table) for table in _ALL_TABLES}
    body = {'protocol_id': 'questmemory-core-state-v2-semantic-snapshot', 'schema_fingerprint': _schema_fingerprint_connection(connection), 'schema_version': SCHEMA_VERSION, 'tables': tables}
    return {**body, 'snapshot_sha256': _sha256_bytes(canonical_json_bytes(body))}

def _domain_snapshot_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = {table: _typed_table_payload(connection, table) for table in _DOMAIN_TABLES}
    body = {'protocol_id': 'questmemory-core-state-v2-domain-snapshot', 'schema_fingerprint': _schema_fingerprint_connection(connection), 'tables': tables}
    return {**body, 'snapshot_sha256': _sha256_bytes(canonical_json_bytes(body))}

def _non_core_legacy_snapshot_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    schema_rows = connection.execute('SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name NOT LIKE ? AND tbl_name NOT LIKE ? AND name NOT LIKE ? AND tbl_name NOT LIKE ? ORDER BY type, name, tbl_name, sql', (f'{v1.CORE_PREFIX}%', f'{v1.CORE_PREFIX}%', f'{CORE_PREFIX}%', f'{CORE_PREFIX}%')).fetchall()
    schemas = [{'name': str(row[1]), 'sql': None if row[3] is None else str(row[3]), 'table_name': str(row[2]), 'type': str(row[0])} for row in schema_rows]
    table_names = [str(row[0]) for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE ? AND name NOT LIKE ? ORDER BY name", (f'{v1.CORE_PREFIX}%', f'{CORE_PREFIX}%')).fetchall()]
    tables = {table: _typed_table_payload(connection, table) for table in table_names}
    body = {'schema': schemas, 'tables': tables, 'user_version': int(connection.execute('PRAGMA user_version').fetchone()[0])}
    return {**body, 'snapshot_sha256': _sha256_bytes(canonical_json_bytes(body))}

def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(row[0]): str(row[1]) for row in connection.execute(f'SELECT key, value FROM {METADATA_TABLE} ORDER BY key').fetchall()}

def _validate_event_chain(connection: sqlite3.Connection) -> int:
    rows = connection.execute(f'SELECT event_id, ordinal, event_type, payload_json, previous_event_digest, event_digest FROM {EVENTS_TABLE} ORDER BY ordinal').fetchall()
    previous: str | None = None
    for expected_ordinal, row in enumerate(rows, start=1):
        ordinal = int(row[1])
        if ordinal != expected_ordinal or row[4] != previous:
            raise CoreStateV2SchemaError('V2 event chain is discontinuous')
        expected = _event_digest_v2(str(row[0]), ordinal, str(row[2]), str(row[3]), previous)
        if str(row[5]) != expected:
            raise CoreStateV2SchemaError('V2 event digest mismatch')
        previous = expected
    return len(rows)
