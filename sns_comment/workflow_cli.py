"""Reproject saved collection evidence without recollecting or calling AI."""

import argparse
import json
import os
from pathlib import Path

from .api import PilotError
from .google_access import access_token
from .profiles import read_settings, select
from . import workflow


def build(root, settings, stages=None):
    accounts=[]
    for name in settings['cloud']['scheduled_accounts']:
        config=select(settings,name)
        folder=Path(root)/name
        source=json.loads((folder/'source.json').read_text())
        saved=folder/'daily-analysis.json'
        annotations=json.loads(saved.read_text()).get('annotations',{}) if saved.exists() else {}
        accounts.append(workflow.make_account(config,source,annotations,stages))
    return workflow.merge(accounts)


def main(argv=None):
    parser=argparse.ArgumentParser(description='저장된 동일 원본으로 v2 마스터 시트 재반영. 외부 AI 호출 없음.')
    parser.add_argument('action',choices=['export','sync'])
    inputs=parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--source-root',type=Path)
    inputs.add_argument('--projection',type=Path,help='원격 runs/날짜/workflow.json을 내려받아 시트 단계만 재처리')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    settings=read_settings(args.config)
    source=json.loads(args.projection.read_text()) if args.projection else build(args.source_root,settings,
        {'sheets':'저장 자료 기준 초기 반영','connection':'자동 갱신 배포·인증 검증 전'})
    workflow.validate_projection(source)
    headers=source['headers']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(source,ensure_ascii=False,indent=2))
    if args.action=='sync':
        target=settings['workflow']['google_sheets']
        client=workflow.Client(access_token(target),target['spreadsheet_id'],headers=headers)
        checkpoint=args.output.with_suffix('.sync.json')
        result=workflow.sync(source,client,lambda value:checkpoint.write_text(json.dumps(value,ensure_ascii=False,indent=2)),args.dry_run)
    else:result={'status':'exported','rows':{name:len(rows)-1 for name,rows in source['tabs'].items()}}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    os.umask(0o077)
    try:main()
    except (PilotError,OSError,ValueError,KeyError) as error:
        raise SystemExit(str(error)) from None
