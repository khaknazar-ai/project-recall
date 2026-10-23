"""ProjectRecall public command line. No providers, shell or execution commands."""
import argparse
import json
import sqlite3
from . import service
from . import memory_patches_cli_v1 as patches

def main(argv=None):
    parser=argparse.ArgumentParser(prog='project-recall',description='Propose, approve and recover local project memory.')
    parser.add_argument('--version',action='version',version='ProjectRecall 0.1.0')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('create','propose','list','show','approve','memory','resume'):
        cmd=commands.add_parser(name,aliases=['status'] if name=='resume' else [])
        cmd.add_argument('--project-root',required=True)
        if name=='create':
            cmd.add_argument('--target-root',required=True)
            cmd.add_argument('--id',required=True)
            cmd.add_argument('--objective',required=True)
            cmd.add_argument('--task-id',default='review-memory')
            cmd.add_argument('--task-title',default='Review project memory')
        if name=='propose':cmd.add_argument('report')
        if name in ('show','approve'):cmd.add_argument('patch_id')
        if name in ('list','memory'):cmd.add_argument('--json',action='store_true')
        if name=='approve':
            for flag in ('sha256','approval-id','approval-statement','approval-text-sha256'):
                cmd.add_argument('--'+flag,required=True)
            cmd.add_argument('--confirm-human-approval',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='create':
            value=service.create(args.project_root,args.target_root,args.id,args.objective,args.task_id,args.task_title)
        else:
            root,_,_=service.owned_root(args.project_root)
            if args.command=='propose':value=patches.ingest_memory_patches_v1(root,args.report)
            elif args.command=='list':value=patches.list_memory_patches_v1(root)
            elif args.command=='show':value=patches.inspect_memory_patch_v1(root,args.patch_id)
            elif args.command=='approve':
                value=patches.approve_memory_patch_v1(root,args.patch_id,patch_sha256=args.sha256,
                    approval_id=args.approval_id,approval_statement=args.approval_statement,
                    approval_text_sha256=args.approval_text_sha256,
                    confirm_human_approval=args.confirm_human_approval)
            else:value=service.context(root)
        if args.command=='memory' and not args.json:
            print('\n'.join(row['key']+': '+row['statement'] for row in value['memory']) or 'No approved memory.')
        elif args.command=='list' and not args.json:
            visible=[row for row in value if row['status'] not in ('applied','rejected')]
            print('\n'.join(row['patch_id']+'  '+row['status']+'  '+row['key'] for row in visible) or 'No pending proposals.')
        else:print(json.dumps(value,ensure_ascii=False,sort_keys=True))
        return 0
    except (ValueError,RuntimeError,OSError,TypeError,KeyError,sqlite3.Error):
        print('ProjectRecall: project, source or exact human approval could not be verified.',file=__import__('sys').stderr)
        return 1