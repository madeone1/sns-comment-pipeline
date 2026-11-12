"""Validate a token's account snapshot before changing collection or reporting."""

import base64
import copy
import datetime as dt
import hashlib
import hmac
import json
import re

from .api import GraphClient, GraphError, PilotError, next_cursor, page_data
from .collector import Collector
from .connection_registry import REGISTRY_OBJECT, SECRET_VERSION, checked_account, initial_registry

STATUS_OBJECT='connections/status.json'
VERSION='2026-11-11-token-snapshot-v1'


class VerifiedPlan(dict):
    """An in-memory proof binds the checked bearer; JSON never includes it."""
    def bind(self,token):
        self._token_hash=hashlib.sha256(token.encode()).digest()
        return self
    def matches(self,token):
        return hmac.compare_digest(self._token_hash,hashlib.sha256(token.encode()).digest())


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def active_hash(registry):
    return digest({'accounts':sorted([{k:a.get(k) for k in ('id','username','label','active','source_prefix','group_id')}
        for a in registry['accounts'].values() if a['active']],key=lambda a:a['id']),
        'credential':registry['groups'].get('primary',{}).get('secret_version')})


def source_secret(settings):
    value=settings.get('connections',{}).get('token_secret') or 'projects/'+settings['cloud']['project_id']+'/secrets/sns-meta-token'
    if not re.fullmatch(r'projects/[a-z0-9-]+/secrets/sns-meta-token',value):
        raise PilotError('운영 Meta 토큰 저장소 경로를 확인해주세요.')
    return value


def read_token(transport,reference,validate=True):
    if not (SECRET_VERSION.fullmatch(reference) or re.fullmatch(r'projects/[a-z0-9-]+/secrets/sns-meta-token/versions/latest',reference)):
        raise PilotError('검증된 Meta 토큰 버전이 필요합니다.')
    value=transport.json('GET','https://secretmanager.googleapis.com/v1/'+reference+':access')
    name=value.get('name','')
    if not SECRET_VERSION.fullmatch(name):raise PilotError('Meta 토큰의 고정 버전을 확인하지 못했습니다.')
    try:token=base64.b64decode(value['payload']['data'],validate=True).decode().strip()
    except (KeyError,ValueError,UnicodeError):
        if validate:raise PilotError('Meta 토큰 저장 형식이 올바르지 않습니다.') from None
        token=''
    if validate and (not token or any(c.isspace() for c in token)):raise PilotError('등록된 Meta 토큰 형식을 확인해주세요.')
    return token,name


def list_accounts(client,previous=(),max_pages=20,max_accounts=25):
    found={};routes=[]
    paths=[('me/assigned_instagram_accounts','id,username'),
           ('me/accounts','id,instagram_business_account{id,username}')]
    for path,fields in paths:
        cursor=None;seen=set();completed=False
        for page in range(max_pages):
            if cursor in seen:raise PilotError('계정 목록 커서가 반복되어 기존 대상을 유지합니다.')
            seen.add(cursor)
            try:payload=client.get(path,fields=fields,limit=50,**({'after':cursor} if cursor else {}))
            except GraphError as error:
                if page==0 and error.code in (10,100,200) and error.status in (400,403):
                    routes.append({'path':path,'status':'unavailable','code':error.code});break
                raise
            for item in page_data(payload):
                account=item if path.endswith('assigned_instagram_accounts') else item.get('instagram_business_account')
                if account is None:continue
                account=checked_account(account)
                old=found.get(account['id'])
                if old and old['username']!=account['username']:raise PilotError('같은 계정 ID의 조회 결과가 달라 기존 대상을 유지합니다.')
                found[account['id']]=account
                if len(found)>max_accounts:raise PilotError('계정 목록이 검증 상한을 넘어 기존 대상을 유지합니다.')
            cursor=next_cursor(payload)
            if cursor is None:
                routes.append({'path':path,'status':'complete','pages':page+1});completed=True;break
        else:raise PilotError('계정 목록을 끝까지 읽지 못해 기존 대상을 유지합니다.')
    if not any(r['status']=='complete' for r in routes):raise PilotError('계정 목록 조회 권한을 확인하지 못해 기존 대상을 유지합니다.')
    # Some token types expose only one listing route. A known accessible asset
    # omitted by that route must not disappear merely because discovery missed it.
    for old in previous:
        if not old.get('active') or old['id'] in found:continue
        try:account=checked_account(client.get(old['id'],fields='id,username'))
        except GraphError as error:
            if error.code in (10,100,200) and error.status in (400,403):continue
            raise
        if account['id']!=old['id']:raise PilotError('이전 계정의 ID 대조가 실패했습니다.')
        found[account['id']]=account
        routes.append({'path':'previous_account','id':account['id'],'status':'verified'})
    if not found:raise PilotError('새 토큰에서 접근 계정을 확인하지 못해 기존 대상을 유지합니다.')
    if len({a['username'] for a in found.values()})!=len(found):raise PilotError('조회된 계정 이름이 중복되었습니다.')
    return sorted(found.values(),key=lambda a:a['id']),routes


def include_direct_accounts(client,accounts,routes,labels,excluded):
    """Read an explicit account ID when page discovery does not list it.

    The operator named the ID. A failed direct read stops before any secret
    or registry write. A successful read then uses the same account, media
    and comment checks as a listed account.
    """
    found={a['id']:a for a in accounts}
    for identifier in labels:
        if identifier in found or identifier in excluded:continue
        if not isinstance(identifier,str) or not identifier.isdigit():
            raise PilotError('직접 조회할 계정 ID는 숫자여야 합니다. 기존 연결은 유지합니다.')
        try:account=checked_account(client.get(identifier,fields='id,username'))
        except GraphError as error:
            if error.code in (10,100,200) and error.status in (400,403):
                raise PilotError('지정한 계정 ID '+identifier+'를 이 토큰으로 직접 조회하지 못했습니다. 기존 연결과 비밀 저장소는 유지합니다.') from None
            raise
        if account['id']!=identifier:raise PilotError('직접 조회한 계정 ID가 입력과 다릅니다. 기존 연결은 유지합니다.')
        if any(other['username']==account['username'] and other['id']!=account['id'] for other in found.values()):
            raise PilotError('직접 조회한 @아이디가 다른 계정 ID와 겹칩니다. 기존 연결을 유지합니다.')
        found[account['id']]=account
        routes.append({'path':'direct_account','id':account['id'],'status':'verified'})
    if len(found)>25:raise PilotError('계정 목록이 검증 상한을 넘어 기존 대상을 유지합니다.')
    return sorted(found.values(),key=lambda a:a['id']),routes


def inspect_token(settings,registry,token,labels=None,exclusions=None,graph=GraphClient):
    client=graph(token,settings['api_version'],max_requests=150)
    labels=labels or {};excluded=set(exclusions or ())
    accounts,routes=list_accounts(client,registry['accounts'].values())
    accounts,routes=include_direct_accounts(client,accounts,routes,labels,excluded)
    known={key:a['label'] for key,a in registry['accounts'].items()}
    selected=[];unknown=[];checks=[]
    for value in accounts:
        identifier=value['id']
        if identifier in excluded:continue
        label=labels.get(identifier,known.get(identifier))
        if not label:
            unknown.append(value);continue
        account=checked_account({**value,'label':label})
        if any(a['username']==account['username'] and a['id']!=identifier for a in registry['accounts'].values()):
            raise PilotError('기존 @아이디와 새 계정 ID가 충돌하여 기존 연결을 유지합니다.')
        Collector(client,None,account['username']).account(identifier)
        media=page_data(client.get(identifier+'/media',fields='id',limit=1))
        if media:
            key=media[0].get('id')
            if not isinstance(key,str) or not key.isdigit():raise PilotError('게시물 ID 형식을 확인하지 못했습니다.')
            page_data(client.get(key+'/comments',fields='id',limit=1))
        checks.append({'id':identifier,'account':True,'media':True,'comments':True if media else 'no_media_to_query'})
        selected.append(account)
    old_active={a['id'] for a in registry['accounts'].values() if a['active']}
    chosen={a['id'] for a in selected}
    return VerifiedPlan({'schema':1,'version':VERSION,'registry_hash':digest(registry),'available':accounts,'accounts':selected,
        'unknown':unknown,'excluded':sorted(excluded),'routes':routes,'checks':checks,
        'changes':{'kept':sorted(chosen&old_active),'added':sorted(chosen-old_active),'paused':sorted(old_active-chosen)},
        'status':'mapping_required' if unknown else 'verified' if selected else 'empty_selection',
        'requests':client.request_count}).bind(token)


def snapshot(registry,plan,reference,actor,instant,secret):
    if plan.get('status')!='verified' or not plan['accounts']:raise PilotError('계정·지역 검증을 완료한 뒤 토큰을 적용해주세요.')
    if digest(registry)!=plan['registry_hash']:raise PilotError('검증 후 연결 설정이 바뀌었습니다. 토큰을 다시 검증해주세요.')
    if not SECRET_VERSION.fullmatch(reference):raise PilotError('고정된 Meta 토큰 버전이 필요합니다.')
    result=copy.deepcopy(registry)
    previous=result.get('token_control',{}).get('revision')
    for account in result['accounts'].values():account['active']=False
    for account in plan['accounts']:
        old=result['accounts'].get(account['id'],{})
        result['accounts'][account['id']]={**old,**account,'group_id':'primary','active':True,
            'source_prefix':old.get('source_prefix','accounts/ig_'+account['id']),'verified_at':instant}
    result['groups']['primary']={'id':'primary','label':'운영 액세스 토큰','secret_version':reference,
        'legacy':False,'verified_at':instant}
    revision=digest({'reference':reference,'accounts':plan['accounts'],'excluded':plan['excluded']})
    result['token_control']={'mode':'local_token','source_secret':secret,'source_version':reference,'revision':revision,
        'verified_at':instant,'excluded':plan['excluded'],'version':VERSION,'active_hash':active_hash(result)}
    if previous!=revision:
        entry={'at':instant,'by':actor,'action':'verified_token_snapshot','group_id':'primary',
            'secret_version':reference,'account_ids':[a['id'] for a in plan['accounts']],
            'revision':revision,'changes':plan['changes']}
        result.setdefault('connection_history',[]).append(entry)
        result['audit']=[*result.get('audit',[]),entry][-100:]
    return result


def reconcile(settings,state,graph=GraphClient,apply=True):
    """Called under the writer lease. Reject a bad latest version before collection."""
    registry,generation=state.json_get(REGISTRY_OBJECT,initial_registry(settings))
    secret=source_secret(settings)
    token,reference=read_token(state.transport,secret+'/versions/latest')
    control=registry.get('token_control',{})
    if control.get('source_version')==reference and control.get('version')==VERSION:
        if control.get('active_hash')!=active_hash(registry):
            raise PilotError('검증 후 수집 대상이나 토큰 참조가 다른 경로에서 바뀌었습니다. 로컬 토큰 파일에서 다시 검증해주세요.')
        return registry,{'status':'unchanged','revision':control['revision'],'source_version':reference}
    exclusions=control.get('excluded',[])
    plan=inspect_token(settings,registry,token,exclusions=exclusions,graph=graph)
    # Migration retains the explicitly selected production regions. New assets
    # need the operator's region label; a token is never a guessed region name.
    if not control and plan['unknown']:
        exclusions=sorted(set(exclusions)|{a['id'] for a in plan['unknown']})
        plan=inspect_token(settings,registry,token,exclusions=exclusions,graph=graph)
    instant=dt.datetime.now(dt.timezone.utc).isoformat()
    if plan['status']!='verified':
        state.json_put(STATUS_OBJECT,{'status':plan['status'],'candidate_version':reference,'checked_at':instant,'plan':plan,
            'message':'새 지역 매핑을 로컬 토큰 실행 파일에서 확인해주세요. 이전 검증 연결은 보존했습니다.'})
        raise PilotError('새 토큰의 계정·지역 매핑이 완료되지 않아 기존 보고와 시트를 보존합니다.')
    updated=snapshot(registry,plan,reference,'scheduled_token_check',instant,secret)
    summary={'status':'applied' if apply else 'verified','source_version':reference,
        'revision':updated['token_control']['revision'],'plan':plan,'checked_at':instant}
    if apply:
        path='connections/history/'+updated['token_control']['revision']+'/registry-before-'+str(generation)+'.json'
        before,_=state.json_get(path)
        if before is None:state.json_put(path,registry,generation=0)
        state.json_put(REGISTRY_OBJECT,updated,generation)
        state.json_put(STATUS_OBJECT,summary)
    return updated,summary


def verify_delivery(shared,configs,day,collection_status,results=None,notion_client=None):
    """Read real checkpoints; a token-save response never means data is synced."""
    from .cloud_state import CloudState
    revisions={c.get('_connection_revision') for c in configs}
    if len(revisions)!=1 or None in revisions:raise PilotError('출력 대조의 연결 버전이 일치하지 않습니다.')
    revision=next(iter(revisions));active={c['account_id'] for c in configs};delivery=[]
    failed={r['account'] for r in (results or []) if r.get('status')=='failed'}
    live_pages={}
    for config in configs:
        state=CloudState(shared.bucket,shared.transport,prefix=config['_cloud_prefix'])
        source,_=state.json_get('runs/'+day+'/source.json',{})
        sheets,_=state.json_get('runs/'+day+'/sheets.json',{})
        projection,_=state.json_get('runs/'+day+'/workflow.json',{})
        documents,_=state.json_get('state/reports.json',{})
        document=documents.get(day,{})
        replay,_=state.json_get('runs/'+day+'/output-connection.json',{})
        source_ok=((source.get('connection_revision')==revision or
            (replay.get('revision')==revision and replay.get('source_hash')==digest(source)))
            and config['expected_username'] not in failed)
        sheet_ok=(source_ok and sheets.get('status')=='complete' and sheets.get('source_hash')==projection.get('source_hash')
            and active<=set(sheets.get('account_tabs',{})))
        notion_ok=(source_ok and document.get('status')=='published' and document.get('connection_revision')==revision
            and document.get('source_hash')==digest(source)
            and (document.get('result',{}).get('readback_verified') or document.get('result',{}).get('unchanged')))
        if notion_client is not None:
            page_id=document.get('result',{}).get('page_id')
            if page_id and page_id not in live_pages:
                try:live_pages[page_id]=read_report_state(notion_client,page_id,day,configs,revision)
                except PilotError:live_pages[page_id]={}
            actual=live_pages.get(page_id,{}).get(config['expected_username'])
            expected=next((v['counts']['comments_and_replies'] for v in source.get('views',[]) if v['period']['key']=='daily'),None)
            notion_ok=notion_ok and actual is not None and actual.get('count')==expected and actual.get('revision')==revision
        delivery.append({'id':config['account_id'],'username':config['expected_username'],'label':config.get('region_label',config['expected_username']),
            'sheets':'반영 확인' if sheet_ok else '확인 대기','notion':'반영 확인' if notion_ok else '확인 대기',
            'counts':next((v['counts'] for v in source.get('views',[]) if v['period']['key']=='daily'),None),
            'sheet_tab':sheets.get('account_tabs',{}).get(config['account_id']),
            'notion_page':document.get('result',{}).get('page_url')})
    complete=all(r['sheets']=='반영 확인' and r['notion']=='반영 확인' for r in delivery)
    value={'status':'synced' if complete else 'sync_pending','stage':'시트·노션 반영 대조 완료' if complete else '시트·노션 확인 대기',
        'revision':revision,'day':day,'delivery':delivery,'collection_status':collection_status,
        'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    shared.json_put(STATUS_OBJECT,value)
    shared.json_put('connections/delivery/'+revision+'/'+day+'.json',value)
    return value


def read_report_state(client,page_id,day,configs,revision):
    """Re-read current regions, excluding preserved inactive history."""
    from .notion_mcp import body,normalized_markdown
    from .notion_sections import locate
    from .regional_notion import containers,ARCHIVE
    page=client.call('fetch',{'id':page_id});text=body(page);area=locate(text)
    if area['kind']!='heading':raise PilotError('SNS Comments 일반 제목 대조가 실패했습니다.')
    wanted='인스타그램 댓글 자동 보고 · '+day+' 08시'
    owned=[inner for _,_,title,inner in containers(text[area['body_start']:area['body_end']],'callout') if normalized_markdown(title)==wanted]
    if len(owned)!=1:raise PilotError('현재 연결 보고의 경계를 확인하지 못했습니다.')
    expected={c['expected_username'] for c in configs};actual={}
    for _,_,title,inner in containers(owned[0],'toggle'):
        if title in ('공통 수집 기록',ARCHIVE):continue
        match=re.search(r'수집 계정: @([a-z0-9_.]+)',normalized_markdown(inner))
        if not match or match.group(1) not in expected or match.group(1) in actual:
            raise PilotError('현재 노션 보고의 계정 목록이 검증된 토큰과 다릅니다.')
        count=re.search(r' · 새 댓글 (\d+)건$',title)
        actual[match.group(1)]={'count':int(count.group(1)) if count else None,
            'revision':revision if '연결 검증 ID: '+revision in normalized_markdown(inner) else None}
    if set(actual)!=expected:raise PilotError('노션 보고에 일부 수집 대상이 누락되었습니다.')
    return actual
