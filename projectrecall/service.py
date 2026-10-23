"""Narrow production adapters over the extracted bootstrap/owned-memory protocols."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path
from . import core_memory_v1 as memory
from . import core_state_clean_v2 as clean
from . import core_state_v2 as core
from .config import FUTURE_PROJECT_CONTROLLER_KIND, _ensure_config_under_transition_lock
from .controller_transition_lock_v1 import acquire_controller_transition_lock_v1
from .durable_io import atomic_write_texts
from .file_safety import _capture_root_chain, _read_regular_bytes, _verify_directory_bindings
from .future_project_controller_marker_v1 import _ensure_future_project_controller_marker_under_transition_lock_v1
from .project_bootstrap_v1 import FutureProjectBootstrapSpecV1, FutureProjectInitialTaskV1, prepare_future_project_bootstrap_v1
from .sqlite_stable_read import open_validated_sqlite_write, open_owned_stable_read_only_sqlite
from .storage_policy import is_approved_windows_project_path
APP_MARKER='projectrecall.json'


def plain_root(value):
    root=Path(value)
    if not root.is_absolute() or not is_approved_windows_project_path(root):
        raise ValueError('An approved absolute local project path is required')
    _capture_root_chain(root)
    if root.resolve(strict=True)!=root:
        raise ValueError('Path aliases are not accepted')
    return root


def read_marker(root):
    bindings=_capture_root_chain(root)
    raw,_=_read_regular_bytes(root/APP_MARKER,label='ProjectRecall marker',maximum=4096)
    _verify_directory_bindings(bindings)
    from .model_mesh.schemas import strict_json_loads
    value=strict_json_loads(raw.decode('utf-8'))
    expected={'application','version','controller_root','project_id','bootstrap_id','receipt_sha256','authority_granted'}
    if (not isinstance(value,dict) or set(value)!=expected or value['application']!='ProjectRecall'
        or type(value['version']) is not int or value['version']!=1
        or value['controller_root']!=root.as_posix() or value['authority_granted'] is not False):
        raise ValueError('Foreign or invalid ProjectRecall marker')
    return value


def owned_root(value):
    root=plain_root(value)
    marker=read_marker(root)
    _,config,receipt=memory._binding(root)
    if (marker['project_id']!=receipt.project_id or marker['bootstrap_id']!=receipt.bootstrap_id
        or marker['receipt_sha256']!=receipt.receipt_sha256):
        raise ValueError('Application marker differs from bootstrap')
    clean.validate_clean_core_state_v2(config,project_id=receipt.project_id)
    return root,config,receipt


def create(value,target_value,project_id,objective,task_id,title):
    root,target=plain_root(value),plain_root(target_value)
    if root==target or root.is_relative_to(target) or target.is_relative_to(root):
        raise ValueError('Controller and target paths must not overlap')
    # Existing foreign state is never adopted, rewritten or upgraded.
    entries=list(root.iterdir())
    if entries:
        owned_root(root)
    with acquire_controller_transition_lock_v1(root) as lease:
        spec=FutureProjectBootstrapSpecV1(project_id=project_id,name=project_id,objective=objective,
            controller_root=root,managed_target_root=target,
            initial_task=FutureProjectInitialTaskV1(task_id=task_id,title=title,
                expected_output='Approved project memory recoverable in another process.',
                done_criteria=('The approved decision is present in restored task context.',),
                do_not_do=('Do not execute project code or call providers.',)))
        result=prepare_future_project_bootstrap_v1(spec)
        marker=_ensure_future_project_controller_marker_under_transition_lock_v1(result,transition_lease=lease)
        config,_=_ensure_config_under_transition_lock(root,transition_lease=lease,
            controller_kind=FUTURE_PROJECT_CONTROLLER_KIND,bootstrap_id=result.bootstrap_id,
            bootstrap_spec_sha256=result.spec_sha256,bootstrap_receipt_sha256=result.receipt_sha256,
            controller_marker_sha256=marker.sha256)
        config.data_dir.mkdir(exist_ok=True)
        _capture_root_chain(config.data_dir)
        if not config.sqlite_path.exists():
            writer=open_validated_sqlite_write(config.sqlite_path,expected='new',
                owner_key=clean._owner_key(config),error_factory=ValueError)
            try:
                writer.commit()
                writer.close()
            except BaseException:
                writer.abort_without_publish()
                raise
        installed=clean.install_clean_core_state_v2(config,project_id=project_id)
        app={'application':'ProjectRecall','version':1,'controller_root':root.as_posix(),
             'project_id':project_id,'bootstrap_id':result.bootstrap_id,
             'receipt_sha256':result.receipt_sha256,'authority_granted':False}
        if not entries:
            lease.verify()
            atomic_write_texts({root/APP_MARKER:json.dumps(app,sort_keys=True,indent=2)+'\n'})
        elif read_marker(root)!=app:
            raise ValueError('ProjectRecall identity differs')
        lease.verify()
        owned_root(root)
        return {'application':'ProjectRecall','project_id':project_id,'task_id':task_id,
                'controller_root':root.as_posix(),'replayed':installed.replay_noop,
                'authority_granted':False}


def context(value):
    root,config,receipt=owned_root(value)
    projection=clean._build_bootstrap_projection(config,receipt.project_id)
    reader=open_owned_stable_read_only_sqlite(config.sqlite_path,owner_key=clean._owner_key(config),error_factory=ValueError)
    try:
        reader.row_factory=sqlite3.Row
        clean._validate_connection(reader,config=config,project_id=receipt.project_id,projection=projection)
        row=reader.execute(f'SELECT * FROM {core.TASKS_TABLE} WHERE task_id=? AND project_id=?',
            (receipt.initial_task.task_id,receipt.project_id)).fetchone()
        if row is None:raise ValueError('Task is missing')
        approved=memory.memory_tips_v1(reader,task_id=receipt.initial_task.task_id)
        source=hashlib.sha256(config.sqlite_path.read_bytes()).hexdigest()
        return {'project_id':receipt.project_id,'task':{'id':row['task_id'],'title':row['title'],'status':row['status']},
                'memory':approved,'source_sha256':source,'authority_granted':False,'replay_allowed':False}
    finally:
        reader.close()