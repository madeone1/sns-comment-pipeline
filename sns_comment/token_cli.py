"""Local hidden-input token replacement; no hosted management app required."""

import argparse
import base64
import datetime as dt
import hmac
import json
import sys
import time

from .api import PilotError
from .cloud_state import CloudState,GoogleTransport
from .connection_registry import REGISTRY_OBJECT,initial_registry
from .credentials import prompt_meta_token
from .gcloud_account import announce,permission_help,resolve_operator,run_gcloud
from .profiles import read_settings
from .token_connections import STATUS_OBJECT,VerifiedPlan,digest,inspect_token,read_token,snapshot,source_secret


class LocalTransport(GoogleTransport):
    def __init__(self,project,account,explicit=False):
        super().__init__();self.project=project;self.account=account;self.explicit=explicit
    def gcloud(self,args):
        # Pass --account only for an explicit override. Otherwise gcloud uses
        # the active authorized account announced at startup.
        result=run_gcloud(args,project=self.project,account=self.account if self.explicit else None)
        if result.returncode:
            raise PilotError(permission_help(self.account,self.project))
        return result.stdout
    def token(self):
        if self._expires<time.time()+60:
            self._token=self.gcloud(['auth','print-access-token']).strip();self._expires=time.time()+3000
        return self._token


def display(plan):
    print('\n이번 토큰의 수집 대상',flush=True)
    for account in plan['accounts']:print('  '+account['label']+' · @'+account['username']+' · '+account['id'])
    print('유지 '+str(len(plan['changes']['kept']))+'개 / 추가 '+str(len(plan['changes']['added']))+
          '개 / 수집 대상에서 제외 '+str(len(plan['changes']['paused']))+'개',flush=True)


def apply_token(settings,state,token,plan,actor):
    """Caller holds the same global lease as the scheduled writer."""
    if not isinstance(plan,VerifiedPlan) or not plan.matches(token):raise PilotError('검증한 토큰과 입력 토큰이 다릅니다. 다시 검증해주세요.')
    registry,generation=state.json_get(REGISTRY_OBJECT,initial_registry(settings))
    if digest(registry)!=plan['registry_hash']:raise PilotError('검증 중 연결 설정이 바뀌었습니다. 다시 실행해주세요.')
    if plan['status']!='verified':raise PilotError('계정·지역 검증이 완료되지 않았습니다. 기존 토큰은 유지했습니다.')
    secret=source_secret(settings)
    current,reference=read_token(state.transport,secret+'/versions/latest',validate=False)
    if plan.get('source_version_before') and plan['source_version_before']!=reference:
        raise PilotError('검증 중 저장소의 토큰이 다른 경로에서 바뀌었습니다. 다시 실행해주세요.')
    if not hmac.compare_digest(current,token):
        value=state.transport.json('POST','https://secretmanager.googleapis.com/v1/'+secret+':addVersion',
            {'payload':{'data':base64.b64encode(token.encode()).decode()}})
        reference=value['name']
    instant=dt.datetime.now(dt.timezone.utc).isoformat()
    updated=snapshot(registry,plan,reference,actor,instant,secret)
    revision=updated['token_control']['revision']
    path='connections/history/'+revision+'/registry-before-'+str(generation)+'.json'
    before,_=state.json_get(path)
    if before is None:state.json_put(path,registry,generation=0)
    state.json_put(REGISTRY_OBJECT,updated,generation)
    status={'status':'sync_pending','stage':'token_verified','revision':revision,'source_version':reference,
        'accounts':plan['accounts'],'changes':plan['changes'],'checked_at':instant}
    state.json_put(STATUS_OBJECT,status)
    return status


def wait_for_delivery(transport,state,execution,region,revision,timeout=3600):
    end=time.monotonic()+timeout;previous=None
    while time.monotonic()<end:
        value,_=state.json_get(STATUS_OBJECT,{})
        if value.get('revision')!=revision:raise PilotError('그사이 토큰 연결이 바뀌었습니다. 현재 연결 상태를 확인해주세요.')
        if value.get('stage')!=previous:
            print('진행: '+value.get('stage','갱신 작업 시작 대기'),flush=True);previous=value.get('stage')
        status=json.loads(transport.gcloud(['run','jobs','executions','describe',execution,'--region='+region,'--format=json']))['status']
        if status.get('completionTime'):
            if value.get('status')=='synced':
                print('계정·지역과 시트·노션 보고의 연결을 재조회해 확인했습니다.',flush=True)
                for row in value.get('delivery',[]):
                    print('  '+row['label']+' · 시트 '+row['sheets']+' · 노션 '+row['notion'],flush=True)
                if value.get('collection_status')!='complete':print('수집 미확인 범위는 잠정 상태로 표시했습니다. 전체 수집 완료와는 구분합니다.',flush=True)
                return value
            raise PilotError('갱신 작업에 확인 대기 단계가 있습니다. 검증된 연결과 기존 원본은 보존했습니다. 같은 파일의 --status로 확인해주세요.')
        time.sleep(10)
    raise PilotError('작업이 계속 진행 중입니다. --status로 확인해주세요. 연결과 원본은 보존됩니다.')


def main(argv=None):
    parser=argparse.ArgumentParser(description='액세스 토큰 검증 → 계정·지역 연결 → 시트·노션 갱신')
    parser.add_argument('--config');parser.add_argument('--check',action='store_true',help='현재 저장 토큰을 검증, 저장·수집 실행 없음')
    parser.add_argument('--account',help='Google Cloud 계정(이메일). 없으면 SNS_GCLOUD_ACCOUNT, 그다음 cloud.account, 없으면 현재 활성 계정')
    parser.add_argument('--status',action='store_true',help='현재 연결과 시트·노션 반영 상태 조회')
    parser.add_argument('--no-wait',action='store_true',help='갱신 작업을 시작한 뒤 종료')
    parser.add_argument('--adopt-current',action='store_true',help='기존 토큰의 검증된 계정 연결만 초기 등록')
    parser.add_argument('--region',action='append',default=[],metavar='ID=지역',help='새 계정의 지역 표시명 지정')
    parser.add_argument('--exclude',action='append',default=[],metavar='ID',help='이번 토큰의 수집 대상에서 제외')
    parser.add_argument('--json',action='store_true',help='상태를 JSON으로 출력')
    args=parser.parse_args(argv)
    try:
        settings=read_settings(args.config);project=settings['cloud']['project_id']
        account,explicit=resolve_operator(args.account,settings.get('cloud',{}).get('account'),project)
        announce(account,project,explicit,sys.stderr if args.json else sys.stdout)
        transport=LocalTransport(project,account,explicit);state=CloudState(project+'-state',transport)
        if args.status:
            status,_=state.json_get(STATUS_OBJECT,{})
            if args.json:print(json.dumps(status,ensure_ascii=False,indent=2))
            else:
                print(status.get('stage',status.get('status','아직 연결 확인 전')))
                if status.get('error'):print(status['error'])
                for row in status.get('delivery',status.get('accounts',[])):
                    print(row.get('label',row.get('username',''))+' · 시트 '+row.get('sheets','확인 대기')+' · 노션 '+row.get('notion','확인 대기'))
            return 0
        registry,_=state.json_get(REGISTRY_OBJECT,initial_registry(settings))
        stored,base_reference=read_token(transport,source_secret(settings)+'/versions/latest',validate=args.check or args.adopt_current)
        if args.check or args.adopt_current:token=stored
        else:token=prompt_meta_token('운영 연결')
        del stored
        exclusions=list(set(registry.get('token_control',{}).get('excluded',[]))|set(args.exclude))
        labels={}
        for item in args.region:
            identifier,separator,label=item.partition('=')
            if not separator or not identifier.isdigit() or not label.strip():raise PilotError('--region은 계정ID=지역이름 형식입니다.')
            labels[identifier]=label.strip()
        exclusions=[key for key in exclusions if key not in labels]
        plan=inspect_token(settings,registry,token,labels=labels,exclusions=exclusions)
        if set(labels)-{a['id'] for a in plan['available']}:raise PilotError('입력한 지역 매핑의 계정 ID를 새 토큰에서 찾지 못했습니다.')
        if not args.check and not args.adopt_current and sys.stdin.isatty():
            for item in plan['available']:
                if item['id'] not in exclusions:continue
                label=input('이전 제외 계정 @'+item['username']+'의 지역명 (빈칸: 제외 유지): ').strip()
                if label:labels[item['id']]=label;exclusions.remove(item['id'])
            plan=inspect_token(settings,registry,token,labels=labels,exclusions=exclusions)
        if plan['unknown'] and not args.check:
            for item in plan['unknown']:
                if not sys.stdin.isatty():raise PilotError('새 계정의 지역 이름을 로컬 실행 파일에서 입력해주세요.')
                label=input('@'+item['username']+'의 지역 표시명 (빈칸: 이번 수집에서 제외): ').strip()
                if label:labels[item['id']]=label
                else:exclusions=[*exclusions,item['id']]
            plan=inspect_token(settings,registry,token,labels=labels,exclusions=exclusions)
        plan['source_version_before']=base_reference
        display(plan)
        if args.check:
            if plan['status']!='verified':print('새 계정의 지역 매핑 확인이 필요합니다.');return 1
            print('토큰·계정·게시물·댓글 접근 검증 완료. 저장하거나 수집을 실행하지 않았습니다.');return 0
        with state.lease():status=apply_token(settings,state,token,plan,account)
        del token
        if args.adopt_current:
            print('기존 토큰과 계정 이력을 유지한 채 검증된 연결을 등록했습니다.');return 0
        print('토큰 검증과 계정·지역 연결을 저장했습니다. 시트·노션 갱신을 시작합니다.',flush=True)
        region=settings['cloud'].get('region','asia-northeast3')
        execution=transport.gcloud(['run','jobs','execute','sns-comments-daily','--region='+region,'--async','--format=value(metadata.name)']).strip()
        print('갱신 실행: '+execution,flush=True)
        if not args.no_wait:wait_for_delivery(transport,state,execution,region,status['revision'])
        return 0
    except KeyboardInterrupt:
        print('\n종료했습니다. 이미 시작한 클라우드 작업은 계속됩니다.',flush=True);return 130
    except (PilotError,ValueError,KeyError,OSError) as error:
        print(str(error) if isinstance(error,PilotError) else '연결 또는 응답 형식을 확인해주세요. 토큰값은 출력하지 않습니다.',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
