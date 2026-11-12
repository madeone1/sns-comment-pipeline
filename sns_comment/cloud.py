"""Cloud Run job: collect, preserve raw evidence, analyze and write dated Notion sections."""

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from .account import collect_continuously, collection_window, previous_month_start
from .api import GraphClient, PilotError
from .cloud_state import CloudState
from .notion import block
from .notion_mcp import NoDailyPage, NotionMcpClient
from .notion_oauth import cloud_credentials
from .mcp import McpClient
from .profiles import check_database, environment_token, load, read_settings, require_enabled, select
from .reporting import Analyzer, INTENT_POLICY_VERSION, REPORT_TEMPLATE_VERSION, KST, digest, render, snapshot, utc, windows, written_periods
from .storage import Store
from .daily_merge import cloud_preflight as merge_daily_pages
from . import guidance, screening, workflow, token_connections
from .google_access import access_token as google_access_token


ROOT = Path(__file__).resolve().parent.parent


def refresh_document(config, state, document, target, analyzer, store, classification_rules,
                     load_report_rules, checkpoint):
    """Regenerate stale pending output from its own saved evidence, not new data."""
    latest = load_report_rules()
    proofs = document.setdefault('guidance', {})
    publication=document.get('publication_review',{})
    repair=(publication.get('compliant') is False and publication.get('rules_identity')==guidance.identity(latest))
    for key in list(document['sections']):
        proof = proofs.get(key, {})
        if (proof.get('rules_identity') == guidance.identity(latest)
                and proof.get('template_version') == REPORT_TEMPLATE_VERSION
                and proof.get('rules',{}).get('account_context')==latest.get('account_context') and not repair):
            continue
        origin = proof.get('source_run', target)
        source, _ = state.json_get(f'runs/{origin}/source.json')
        if not source:
            raise PilotError('지침이 변경된 대기 보고의 저장 원본이 없어 기존 보고를 보존합니다.')
        view = next((v for v in source['views'] if v['period']['target_day']==target
            and ('monthly' if v['period']['key']=='monthly_final' else v['period']['key'])==key), None)
        if view is None:
            raise PilotError('대기 보고의 날짜·원본 기간이 달라 갱신을 보류합니다.')
        context = source['recent_seven_days']
        rows = {r['id']:r for r in context['comments']+view['comments']}
        annotations = analyzer.classify(store.db, list(rows.values()), classification_rules, checkpoint)
        def load_for_draft():
            value=load_report_rules()
            return {**value,'_guideline_feedback':guidance.feedback(publication)} if repair else value
        report, latest = guidance.generate_current(analyzer, view, context, annotations, load_for_draft)
        latest.pop('_guideline_feedback',None)
        label = proof.get('collection_label', '저장 원본 재검토 · 수집 '+source['collection']['status'])
        sections = render(view, context, report, annotations, latest, label)
        if source.get('basis')=='comment_written_at':
            floor=source.get('media_since')
            scope=(f"게시물 {dt.datetime.fromisoformat(floor).astimezone(KST):%Y-%m-%d} 이후" if floor
                   else '게시물 작성일 제한 없음')
            scope+=' · 댓글 작성일 기준 일일 목록·7일 참고'
        else:
            scope='저장 당시 최근 게시물 범위의 댓글·답글'
        sections.insert(0, block('paragraph', '수집 계정: @'+config['expected_username']+' · '+scope+' · 저장된 동일 원본의 최신 지침 재검토'))
        if view.get('coverage_gaps') or not view['period'].get('coverage_complete', True):
            note=view['period'].get('coverage_note') or '저장 당시 미확인 범위가 남아 있음'
            sections.append(block('paragraph', '수집 범위 확인 대기: '+note+' · 현재 건수는 잠정입니다.'))
        document['sections'][key] = sections
        proofs[key] = {'rules_identity':guidance.identity(latest), 'rules':latest,
            'source_run':origin,'collection_label':label,'review':report['guideline_review'],
            'template_version':REPORT_TEMPLATE_VERSION}
    current=load_report_rules()
    if any(p.get('rules_identity')!=guidance.identity(current) for p in proofs.values()):
        raise PilotError('기간별 보고를 준비하는 중 작성 지침이 변경되어 갱신을 보류합니다.')
    return latest


def log(event, **values):
    print(json.dumps({"event":event, **values},ensure_ascii=False),flush=True)


def retry(operation, checkpoint=lambda: None, sleep=time.sleep):
    for attempt in range(4):
        try:
            return operation()
        except PilotError as error:
            checkpoint()
            transient = bool(re.search(r"HTTP (?:429|5\d\d|0)\b|통신 실패",str(error)))
            if not transient or attempt == 3:
                raise
            log("retry_pending", attempt=attempt+1, wait_seconds=300)
            sleep(300)


def coverage(connection, account_id, period):
    if period.get('basis') == 'comment_written_at':
        if period.get('coverage_note') and not period.get('coverage_complete'):
            return [period['coverage_note']]
        return [] if period.get('coverage_complete') else [('선택한 게시물 기간의 추가 수집 확인 대기' if period.get('media_since') else
            '게시물 작성일 제한 없는 초기 대조·추가 수집 확인 대기')]
    start = dt.datetime.fromisoformat(period["start"]).astimezone(KST)
    end = dt.datetime.fromisoformat(period["end"]).astimezone(KST)
    if not period["key"].startswith("monthly"):
        full=connection.execute("""SELECT 1 FROM collection_scopes WHERE account_id=? AND status='complete'
            AND media_policy='recent_posts' AND window_start<=? AND window_end>=? LIMIT 1""",
            (account_id,utc(period["media_start"]),utc(period["end"]))).fetchone()
        if full:
            return []
    completed = {dt.datetime.fromisoformat(r[0]).astimezone(KST).date().isoformat() for r in connection.execute(
        "SELECT window_end FROM collection_scopes WHERE account_id=? AND status='complete' AND media_policy='recent_posts'",(account_id,))}
    expected, day = [], start.date()
    if start.hour == 8:
        day += dt.timedelta(days=1)
    # Monthly midnight-to-midnight finalization requires next-day 08h collection.
    last = end.date()
    while day <= last:
        expected.append(day.isoformat())
        day += dt.timedelta(days=1)
    return [day for day in expected if day not in completed]


def execute(config, state, notion, analyzer, instant=None, lease_held=False):
    written_mode = config.get('cloud', {}).get('comment_written_windows', False)
    periods = written_periods(instant) if written_mode else windows(instant)
    day = periods[0]["target_day"]
    start, end = collection_window(instant=instant)
    bounded = written_mode and config.get('cloud',{}).get('post_history')=='previous_month'
    frontier = written_mode and config.get('cloud',{}).get('comment_history')=='previous_month'
    media_since = previous_month_start(end) if bounded else None
    if media_since:
        periods = [{**p, 'media_since':media_since} for p in periods]
    with (contextlib.nullcontext() if lease_held else state.lease()), tempfile.TemporaryDirectory(prefix="sns-cloud-") as temp:
        path = Path(temp)/"comments.sqlite3"
        generation = state.restore_database(path)
        check_database(config, path)
        store = Store(path)
        current_published = False

        def checkpoint():
            nonlocal generation
            generation = state.save_database(store,generation)

        try:
            # A live read each run; proposal/example child pages are excluded.
            active=config.get('workflow',{}).get('enabled',False)
            projection_active=active or config.get('workflow',{}).get('google_sheets',{}).get('enabled',False)
            rules = retry(lambda:notion.workflow_rules(config['notion']['rules_page_id'],config['workflow']['instructions_page_id'])
                          if active else notion.rules(config["notion"]["rules_page_id"]))
            instructions = config['notion'].get('report_instructions_page_id')
            report_rules = retry(lambda:notion.workflow_rules(config['notion']['rules_page_id'],instructions)) if instructions and not active else rules
            verify_guidelines=config['notion'].get('verify_report_guidelines',bool(instructions))
            account_context={'account_id':config['account_id'],'username':config['expected_username'],
                'region':config.get('region_label',config['expected_username'])}
            if verify_guidelines and not instructions:
                raise PilotError('보고 준수 검증에는 명시된 최신 작성 지침 문서가 필요합니다.')
            def latest_report_rules():
                latest=retry(lambda:notion.workflow_rules(config['notion']['rules_page_id'],instructions))
                parent=latest.get('parent')
                if parent and parent!={k:rules.get(k) for k in ('page_id','version','last_edited_at')}:
                    raise PilotError('분류 기준 문서가 실행 중 변경되어 저장 원본으로 재분석해야 합니다.')
                return {**latest,'enforce_guidelines':True,'account_context':account_context}
            if verify_guidelines:
                report_rules={**report_rules,'enforce_guidelines':True,'account_context':account_context}
            credential=config.get('_credential_secret_version') or config.get('_web_secret_version')
            if credential:
                token,_=token_connections.read_token(state.transport,credential)
            else:
                token = environment_token(config)
            collection_credential,_=state.json_get('state/collection-credential.json',{}) if credential else ({},0)
            credential_changed=bool(credential and collection_credential.get('secret_version')!=credential)
            client = GraphClient(token,config["api_version"],max_requests=500,
                                 api_host=config.get("_meta_api_host", "graph.facebook.com"))
            state.json_put(f"runs/{day}/rules.json",rules)
            if instructions:
                state.json_put(f"runs/{day}/report-rules.json",report_rules)
            ticks = 0

            def progress(value):
                nonlocal ticks
                ticks += 1
                if ticks % 25 == 0:
                    checkpoint()

            def collect(policy):
                scope_start = previous_month_start(end) if policy=='recent_comments' else start
                budget = 5000-client.request_count
                if budget < 2:
                    raise PilotError("계정 수집의 전체 요청 예산에 도달했습니다.")
                prior=store.db.execute("""SELECT status FROM collection_scopes WHERE account_id=? AND owner=?
                    AND window_start=? AND window_end=? AND media_policy=? ORDER BY started_at DESC,id DESC LIMIT 1""",
                    (config["account_id"],config["expected_username"].lstrip("@"),scope_start,end,policy)).fetchone()
                return collect_continuously(client,store,config["expected_username"],scope_start,end,
                    account_id=config["account_id"],max_requests=min(500,budget),max_total_requests=budget,
                    restart=credential_changed or bool(policy!='recent_comments' and (policy=="recent_posts" or written_mode) and prior and prior["status"]=="complete"),
                    media_policy=policy,discovery_pages=config["collection"].get("discovery_pages",1),
                    workers=config["collection"].get("workers",3),progress=progress)

            primary = retry(lambda:collect('recent_comments' if frontier else 'previous_month_posts' if bounded else 'recent_posts'),checkpoint)
            checkpoint()
            if credential_changed:
                state.json_put('state/collection-credential.json',{'secret_version':credential,
                    'connection_revision':config.get('_connection_revision'),'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()})
            if primary["status"] != "complete" and not (bounded or frontier):
                raise PilotError("최근 게시물 수집이 미완료입니다. 정상 0건으로 보고하지 않습니다.")
            log("primary_collected",date=day,account=config["expected_username"],
                posts=primary["media_discovered"],api_requests=primary["api_requests"])
            tracking = None
            webhook_result = None
            webhook_enabled = config.get('cloud', {}).get('webhook_enabled', False)
            historical = None
            if written_mode:
                from .comment_windows import baseline, aggregate
                if bounded or frontier:
                    tracking = {'status':'complete','scope':'included_in_recent_comment_pages' if frontier else 'included_in_previous_month_posts','api_requests':0}
                else:
                    try:
                        tracking = retry(lambda:collect('tracked_activity'), checkpoint)
                    except PilotError as error:
                        tracking = {'status': 'pending', 'error': str(error)}
                checkpoint()
                if webhook_enabled:
                    from .webhook_worker import process as process_webhooks
                    try:
                        webhook_result = process_webhooks(config, state, store, client, start, end, day, checkpoint)
                    except PilotError as error:
                        checkpoint()
                        webhook_result = {'status': 'pending', 'error': str(error)}
                        state.json_put(f'runs/{day}/webhook.json', webhook_result)
                historical = ({'status':'pending','reason':'historical_parent_replies_before_webhook_not_exhaustive',
                    'top_level_status':primary['status'],'comment_since':previous_month_start(end)} if frontier else
                    {'status':'not_required','reason':'user_selected_previous_month_posts'} if bounded else
                    baseline(config, store, client, start, end, checkpoint))
                primary = aggregate(primary, tracking, webhook_result, historical, store, config['account_id'], start, end, media_since)
                periods = [{**p, 'coverage_complete': primary['status'] == 'complete'} for p in periods]
                if frontier:
                    periods=[{**p,'coverage_note':'최신 댓글 우선 확인. 웹훅 연결 전 과거 부모 댓글의 새 답글은 누락 가능'} for p in periods]
            state.json_put(f"runs/{day}/collection.json",{"primary":primary,"tracking_separate":tracking,
                **({'historical_baseline': historical, 'webhook': webhook_result} if written_mode else {})})
            context_period = {"key":"recent_seven_days","label":"최근 7일 참고","start":start,"end":end,
                              "media_start":start,"target_day":day}
            if written_mode:
                context_period.update(basis='comment_written_at', coverage_complete=primary['status']=='complete')
                if media_since:
                    context_period['media_since']=media_since
            context = snapshot(store.db,config["account_id"],context_period)
            views = [snapshot(store.db,config["account_id"],period) for period in periods]
            all_rows = {r["id"]:r for view in [context,*views] for r in view["comments"]}
            # Raw source is committed remotely before any AI call or report write.
            source={"views":views,"recent_seven_days":context,"collection":primary}
            if config.get('_connection_revision'):source['connection_revision']=config['_connection_revision']
            if written_mode:
                source.update(basis='comment_written_at', media_age_restricted=bool(media_since), media_since=media_since,
                    collection_coverage=primary['coverage'], generated_at=dt.datetime.now(dt.timezone.utc).isoformat())
            state.json_put(f"runs/{day}/source.json",source)
            stages={'collection':'complete','analysis':'pending','sheets':'미반영','drive':'미연결','notion':'pending'}
            if projection_active:
                selected=screening.persist(store.db,config['account_id'],list(all_rows.values()))
                checkpoint()
                state.json_put(f'runs/{day}/screening.json',selected)
                state.json_put(f'runs/{day}/stages.json',stages)
            annotations = retry(lambda:analyzer.classify(store.db,list(all_rows.values()),rules,checkpoint),checkpoint)
            checkpoint()
            stages['analysis']='분류 대조 완료'

            archived=False
            def export_workflow():
                nonlocal archived
                if not projection_active:return
                projection=workflow.make_account(config,source,annotations,stages)
                state.json_put(f'runs/{day}/workflow.json',projection)
                drive=config['workflow'].get('google_drive',{})
                if drive.get('enabled') and not archived:
                    from .drive_archive import Client as DriveClient, bundle
                    import sqlite3
                    try:
                        backup=Path(temp)/'archive.sqlite3'
                        with sqlite3.connect(backup) as dest:store.db.backup(dest)
                        raw=bundle({'source.json':source,'annotations.json':annotations,'rules.json':rules,
                                    'screening.json':selected,'workflow.json':projection,'comments.sqlite3':backup.read_bytes()})
                        result=DriveClient(google_access_token(drive,state.transport),drive.get('folder_id')).archive(config['expected_username']+'/'+day,raw)
                        state.json_put(f'runs/{day}/drive.json',result)
                        stages['drive']='보관 대조 완료';archived=True
                    except PilotError:
                        stages['drive']='보관 대기 · 인증·실행 기록 확인'
                        state.json_put(f'runs/{day}/drive.json',{'status':'pending','raw_preserved':True})
                target=config['workflow'].get('google_sheets',{})
                if target.get('enabled'):
                    try:
                        sheet_client=workflow.Client(google_access_token(target,state.transport),target['spreadsheet_id'],headers=projection['headers'])
                        result=workflow.sync(projection,sheet_client,
                            lambda value:state.json_put(f'runs/{day}/sheets.json',value),
                            account_roster=config.get('_sheet_roster'))
                        stages['sheets']='반영 완료' if result['status']=='complete' else '반영 대기'
                    except PilotError:
                        stages['sheets']='반영 대기 · 인증·실행 기록 확인'
                        state.json_put(f'runs/{day}/sheets.json',{'status':'pending','source_hash':projection['source_hash'],
                            'retry_source':f'runs/{day}/workflow.json','raw_preserved':True})
                state.json_put(f'runs/{day}/stages.json',stages)

            export_workflow()
            documents, document_generation = state.json_get("state/reports.json",{})
            completed=primary.get("last_scope_completed_at")
            completed=dt.datetime.fromisoformat(completed).astimezone(KST).isoformat() if completed else "미확인"
            post_label = (f"게시물 {dt.datetime.fromisoformat(media_since).astimezone(KST):%Y-%m-%d} 이후" if media_since else '게시물 작성일 제한 없음')
            collection_label = (f"댓글 작성일 기준 수집 {primary['status']} · {post_label} · "+
                ('지난달 이후 댓글 확인 · 웹훅 연결 전의 과거 부모 댓글 새 답글은 누락 가능' if frontier else
                 '선택한 게시물 기간 대조' if bounded else f"과거 게시물 대조 {historical['status']}")
                if written_mode else f"선택 범위 수집 {primary['status']} · 실제 수집 완료 {completed} "
                f"· 게시물 발견 {config['collection'].get('discovery_pages',1)}페이지, 전체 게시물 누락 부재 검증 전")
            analysis_statuses=[]
            for view in views:
                view["coverage_gaps"] = coverage(store.db,config["account_id"],view["period"])
                if view["period"]["key"].startswith("monthly") and view["coverage_gaps"]:
                    view["period"]["label"] += "·수집 누락 가능·잠정"
                if verify_guidelines:
                    report,report_rules=guidance.generate_current(analyzer,view,context,annotations,latest_report_rules)
                    state.json_put(f'runs/{day}/report-rules.json',report_rules)
                else:
                    report = retry(lambda:analyzer.report(view,context,annotations,report_rules))
                analysis_statuses.append(report.get('narrative_status','draft_validated'))
                sections = render(view,context,report,annotations,report_rules,collection_label)
                sections.insert(0,block("paragraph",f"수집 계정: @{config['expected_username']} · "+
                    (f'댓글 작성일 기준: 매일 08시 직전 24시간 일일 목록·직전 7일 참고. {post_label}.' if written_mode else
                     '기본 범위: 최근 7일 작성 게시물의 댓글·답글. 이전 게시물 별도 추적은 본 집계에서 제외.')))
                if not projection_active and not config.get('cloud',{}).get('sheets_sync_enabled',False):
                    sections[-1]['toggle']['children'].append(block('paragraph',
                        '운영 범위: 원본은 비공개 Cloud Storage에 보존. 규정집의 Google Sheets 누적·Drive 백업은 미연결, 개인 알림 전달·사람 검수·실제 댓글 응답은 검증되지 않음.'))
                if projection_active:
                    sections[-1]['toggle']['children'].append(block('paragraph',
                        f"시트: {stages['sheets']} · Drive: {stages['drive']} · 사람 검수·실제 SNS 답변은 별도 확인."))
                if active:
                    # The account remains owned by publish()'s stable callout;
                    # compact regional content is folded inside that boundary.
                    label=workflow.LABELS.get(config['expected_username'],config['expected_username'])
                    details=sections[:3]+[sections[-1]]
                    summary=[block('bulleted_list_item',line) for line in report['lines']]
                    sections=[block('toggle',f"{label} · 새 댓글 {view['counts']['comments_and_replies']}건",children=summary+details)]
                if view["coverage_gaps"]:
                    sections.append(block("paragraph",('수집 범위 확인 대기: ' if written_mode else '수집 확인이 없는 기준일: ')+", ".join(view["coverage_gaps"])+". 전체 기간 보고 확정 전."))
                target = view["period"]["target_day"]
                document = documents.setdefault(target,{"sections":{}})
                key = "monthly" if view["period"]["key"] == "monthly_final" else view["period"]["key"]
                document["sections"][key] = sections
                if verify_guidelines:
                    document.setdefault('guidance',{})[key]={'rules_identity':guidance.identity(report_rules),
                        'rules':report_rules,'source_run':day,'collection_label':collection_label,
                        'review':report['guideline_review'],'template_version':REPORT_TEMPLATE_VERSION}
                document["status"] = "pending"
                if config.get('_connection_revision'):
                    document['connection_revision']=config['_connection_revision']
                    document['account_roster']=config.get('_report_roster')
                    document['source_hash']=token_connections.digest(source)
                state.json_put(f"runs/{day}/{view['period']['key']}-analysis.json",{
                    "report":report,"view":view,"annotations":annotations,"intent_policy_version":INTENT_POLICY_VERSION,
                    "report_template_version":REPORT_TEMPLATE_VERSION,"rule_hash":digest(report_rules)})
            document_generation = state.json_put("state/reports.json",documents,document_generation)
            # Pending dates are replayed against their own event date, never the
            # latest title or today's page. Monthly completion preserves daily
            # and weekly parts on the prior month-end page.
            failures, waiting = [], []
            for target, document in sorted(documents.items()):
                if document.get("status") != "pending":
                    continue
                sections = []
                sections.extend(document["sections"].get("daily",[]))
                supplementary=[]
                for key in ("weekly","monthly"):
                    supplementary.extend(document["sections"].get(key,[]))
                if supplementary:
                    sections.append(block("toggle","주간·월간 보고",children=supplementary))
                try:
                    notion.account_roster=document.get('account_roster',config.get('_report_roster'))
                    notion.connection_revision=document.get('connection_revision',config.get('_connection_revision'))
                    if verify_guidelines:
                        report_rules=refresh_document(config,state,document,target,analyzer,store,rules,latest_report_rules,checkpoint)
                        sections=list(document['sections'].get('daily',[]))
                        supplements=[b for k in ('weekly','monthly') for b in document['sections'].get(k,[])]
                        if supplements:sections.append(block('toggle','주간·월간 보고',children=supplements))
                        document_generation=state.json_put('state/reports.json',documents,document_generation)
                        def publication_checkpoint(value):
                            document['publication_review']=value
                            state.json_put(f'runs/{day}/publication-guidance-{target}.json',value)
                        validator=guidance.publication_validator(analyzer,report_rules,latest_report_rules,publication_checkpoint)
                        result=retry(lambda:notion.publish(target,sections,through_day=day,guideline_validator=validator))
                    else:
                        result = retry(lambda:notion.publish(target,sections,through_day=day))
                    document.update(status="published",result=result)
                    document.pop("last_error",None)
                    if target == day:
                        current_published = True
                    log("notion_published",date=target,account=config["expected_username"],
                        page_id=result["page_id"],page_day=result.get("page_day",target))
                except NoDailyPage as error:
                    document.update(status="pending",last_error=str(error))
                    waiting.append(target)
                    log("notion_waiting_for_page",date=target,account=config["expected_username"])
                except PilotError as error:
                    document.update(status="pending",last_error=str(error))
                    failures.append(target)
                document_generation = state.json_put("state/reports.json",documents,document_generation)
            # Secondary old-post tracking never delays the primary daily report.
            # A completed same-period tracking scope is reused on retries.
            if config.get("cloud",{}).get("refresh_known_tracking",True) and not webhook_enabled and not written_mode:
                try:
                    tracking = retry(lambda:collect("tracked_activity"),checkpoint)
                except PilotError as error:
                    tracking = {"status":"failed","error":str(error)}
                checkpoint()
                state.json_put(f"runs/{day}/collection.json",{"primary":primary,"tracking_separate":tracking})
                log("secondary_tracking_finished",date=day,status=tracking["status"])
            if webhook_enabled and not written_mode:
                from .webhook_worker import process as process_webhooks
                try:
                    webhook_result = process_webhooks(config, state, store, client, start, end, day, checkpoint)
                except PilotError as error:
                    checkpoint()
                    webhook_result = {'status': 'pending', 'error': str(error), 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat()}
                    state.json_put(f'runs/{day}/webhook.json', webhook_result)
                state.json_put(f'runs/{day}/collection.json', {'primary': primary, 'tracking_separate': tracking,
                    'webhook': webhook_result})
                log('webhook_batch_finished', date=day, status=webhook_result['status'])
            if failures:
                raise PilotError("Notion 기록 보류: "+", ".join(failures))
            report_status = "pending" if waiting else "published"
            stages['notion']=report_status
            export_workflow()
            analysis_status='counts_only_review_required' if 'validation_failed_counts_only' in analysis_statuses else 'draft_validated'
            workflow_pending=projection_active and (report_status!='published' or
                (config['workflow'].get('google_sheets',{}).get('enabled') and stages['sheets']!='반영 완료') or
                (config['workflow'].get('google_drive',{}).get('enabled') and stages['drive']!='보관 대조 완료'))
            log("daily_complete",date=day,account=config["expected_username"],
                report_status=report_status,analysis_status=analysis_status,comments=context["counts"]["comments_and_replies"],api_requests=client.request_count)
            return {"status":"pending" if workflow_pending or (webhook_result and webhook_result['status'] != 'complete') or (written_mode and primary['status']!='complete') else "complete","day":day,"primary":primary,"tracking_separate":tracking,
                    "report_status":report_status,"analysis_status":analysis_status,"pending_report_dates":waiting,
                    **({'webhook': webhook_result} if webhook_enabled else {}),
                    **({'workflow_stages':stages} if projection_active else {})}
        except PilotError as error:
            checkpoint()
            state.json_put(f"runs/{day}/failure.json",{"status":"failed","error":str(error),"at":dt.datetime.now(dt.timezone.utc).isoformat()})
            log("daily_failed",severity="ERROR",date=day,error=str(error))
            # A failure marker must not overwrite an existing or just-written
            # report. Failure status remains explicit in the run state/log.
            log('report_preserved_on_failure',date=day,error=str(error))
            raise
        finally:
            store.close()


def scheduled_configs(settings):
    names = settings.get("cloud",{}).get("scheduled_accounts")
    if not isinstance(names,list) or not names or any(not isinstance(name,str) for name in names):
        raise PilotError("정기 수집 계정 목록을 cloud.scheduled_accounts에 명시해주세요.")
    configs = [select(settings,name) for name in names]
    if len({config["expected_username"] for config in configs}) != len(configs):
        raise PilotError("정기 수집 계정 목록이 중복되었습니다.")
    for config in configs:
        require_enabled(config)
    roster = []
    active_names = {c['expected_username'] for c in configs}
    for name in [settings['expected_username'], *settings.get('accounts', {})]:
        account = select(settings, name)
        if account.get('account_id'):
            roster.append({'id': account['account_id'], 'username': account['expected_username'],
                'label': workflow.LABELS.get(account['expected_username'], account['expected_username']),
                'active': account['expected_username'] in active_names})
    for config in configs:
        config['_sheet_roster'] = roster
    return configs


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Cloud Run용 SNS 일일 수집·분석·Notion 기록")
    parser.add_argument("--config",type=Path,default=ROOT/"config.json")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--account", help="실행할 Instagram @아이디")
    selection.add_argument("--all-accounts", action="store_true", help="명시된 정기 수집 계정을 차례로 실행")
    selection.add_argument("--merge-only", action="store_true", help="일일 업무 중복 병합만 실행")
    args = parser.parse_args(argv)
    try:
        settings = read_settings(args.config)
        configs = None if args.all_accounts else [load(args.config,args.account)]
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        shared = CloudState(os.environ["SNS_STATE_BUCKET"])
        # All accounts share the existing OAuth identity. The global lease must
        # cover refresh AND MCP calls, so refresh-token rotation cannot race.
        with shared.lease():
            instant = dt.datetime.now(dt.timezone.utc)
            if args.all_accounts:
                from .connection_registry import REGISTRY_OBJECT, runtime_configs
                registry, _ = shared.json_get(REGISTRY_OBJECT)
                if settings.get('connections',{}).get('enabled'):
                    try:
                        registry,connection=token_connections.reconcile(settings,shared)
                    except PilotError as error:
                        shared.json_put(token_connections.STATUS_OBJECT,{'status':'validation_failed','stage':'token_validation',
                            'error':str(error),'checked_at':instant.isoformat(),'previous_connection_preserved':True})
                        raise
                    log('token_connection_checked',status=connection['status'],revision=connection['revision'])
                if registry is not None:
                    configs = runtime_configs(settings, registry)
                    log('account_snapshot_loaded', accounts=len(configs))
                    if not configs:
                        log('collection_paused', reason='앱에서 수집 중인 계정 없음')
                        return 0
                else:
                    configs = scheduled_configs(settings)
            for config in configs:
                require_enabled(config)
                if config.get("notion", {}).get("auth_mode", "mcp_oauth") != "mcp_oauth":
                    raise PilotError("자동 운영은 기존 Notion MCP OAuth 연결만 사용합니다.")
            try:
                merge_result = merge_daily_pages(configs[0]["notion"],shared,instant)
            except (PilotError,OSError,ValueError,KeyError,TypeError):
                merge_result = {"status":"held","reason":"daily_merge_verification_or_configuration_required"}
            log("daily_merge_finished",**merge_result)
            if args.merge_only:
                summary = {"mode":"merge_only", "checked_at":instant.isoformat(), "daily_merge":merge_result,
                           "status":"held" if merge_result.get("status")=="held" else "complete"}
                shared.json_put("daily-merge/"+instant.astimezone(dt.timezone(dt.timedelta(hours=9))).date().isoformat()+"/latest.json",summary)
                log("daily_merge_job_finished",**summary)
                return int(summary["status"]=="held")
            if "SNS_NOTION_MCP_TOKEN" in os.environ:
                log("notion_mcp_diagnostic_token_ignored")
            auth = cloud_credentials(shared.transport,os.environ["SNS_NOTION_OAUTH_SECRET"])
            auth.refresh()
            log("notion_oauth_refreshed")
            client = McpClient(auth)
            results = []
            for config in configs:
                name = config["expected_username"]
                state = shared if not config["_cloud_prefix"] else CloudState(
                    os.environ["SNS_STATE_BUCKET"],shared.transport,prefix=config["_cloud_prefix"])
                log("account_started",account=name)
                try:
                    notion = NotionMcpClient(client,config["notion"]["daily_data_source_id"],username=name,
                        regional=config['notion'].get('regional_report',False),region_label=config.get('region_label'),
                        account_roster=config.get('_report_roster'),connection_revision=config.get('_connection_revision'))
                    analyzer = Analyzer(state.transport,project,config.get("cloud",{}).get("ai_model","gemini-3.8-flash"))
                    result = execute(config,state,notion,analyzer,instant=instant,lease_held=True)
                    primary = result.get("primary",{})
                    results.append({"account":name,"status":result["status"],
                        "report_status":result.get("report_status","published"),
                        "analysis_status":result.get('analysis_status','unknown'),
                        **({'workflow_stages':result['workflow_stages']} if 'workflow_stages' in result else {}),
                        "pending_report_dates":result.get("pending_report_dates",[]),
                        "posts":primary.get("media_discovered"),"counts":primary.get("stored_in_window")})
                    log("account_complete",**results[-1])
                    if config.get('_connection_revision'):
                        shared.json_put(token_connections.STATUS_OBJECT,{'status':'sync_pending','stage':'계정 갱신 '+str(len(results))+'/'+str(len(configs)),
                            'revision':config['_connection_revision'],'accounts_completed':[r['account'] for r in results]})
                except (PilotError,OSError,ValueError,KeyError,TypeError) as error:
                    message = str(error) if isinstance(error,PilotError) else "계정 설정·응답 형식 확인 필요. 비밀정보는 출력하지 않음."
                    results.append({"account":name,"status":"failed","error":message})
                    log("account_failed",severity="ERROR",**results[-1])
            summary = {"day":windows(instant)[0]["target_day"],"accounts":results,"daily_merge":merge_result,
                       "status":"failed" if any(r["status"]=="failed" for r in results) else
                                "pending" if any(r['status']=='pending' for r in results) else "complete"}
            shared.json_put(f"pipeline/{summary['day']}/status.json",summary)
            if args.all_accounts and settings.get('connections',{}).get('enabled'):
                delivered=token_connections.verify_delivery(shared,configs,summary['day'],summary['status'],results,client)
                if delivered['status']!='synced' and summary['status']=='complete':
                    summary['status']='pending';shared.json_put(f"pipeline/{summary['day']}/status.json",summary)
            log("pipeline_finished",**summary)
        return 0 if summary['status']=='complete' else 1
    except PilotError as error:
        log("job_failed",severity="ERROR",error=str(error))
        return 1
    except (OSError,ValueError,KeyError,TypeError):
        log("job_failed",severity="ERROR",error="클라우드 설정·응답 형식 확인 필요. 비밀정보는 출력하지 않음.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
