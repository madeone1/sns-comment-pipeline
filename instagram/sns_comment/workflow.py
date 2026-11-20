"""One versioned source for DB screening, Sheets projections and backups.

All public workbook columns are mapped by name. Existing human fields are
preserved; retries reconcile immutable keys and never append blindly.
"""

import collections
import urllib.parse

from .api import PilotError
from . import screening
from .account_tabs import account_metadata, reconcile
from .reporting import repeats
from .sheets import HEADERS as LEGACY_HEADERS, SheetsClient, cell, column_name, request_batches, local_time


VERSION = "sns-workflow-v3-regional"
LABELS = {"waterbomb_official": "대표(공식)", "waterbomb_busan_official": "부산",
          "waterbomb_sokcho_official": "속초", "waterbomb_toronto_official": "토론토"}
REVIEW_FRONT = ["연결 키", "지역", "원문 발췌", "선정 사유", "자동 주제", "자동 의도", "자동 감정", "담당", "처리 상태", "답변 내용", "검토 여부", "게시물 링크", "목록 상태"]
HEADERS = {
    "한눈에 보기": ["연결 키", "지역", "새 댓글", "단순 반응", "내용 분석 대상", "판단 보류", "확인된 작성자", "검토 대상", "수집 상태", "시트 반영", "원본 기준"],
    "분류와 처리": REVIEW_FRONT + [x for x in LEGACY_HEADERS["분류와 처리"] if x not in REVIEW_FRONT],
    "게시물 반응": ["연결 키", "지역", "게시물 링크", "게시물 내용", "새 댓글", "단순 반응", "내용 분석 대상", "판단 보류", "확인된 작성자", "문의", "불만", "행동 요청", "반복 후보 댓글", "반복 후보 작성자", "기준일"],
    "기간별 요약": ["보고 키", "지역", "보고 종류", "저장된 댓글·답글", "단순 반응", "내용 분석 대상", "판단 보류", "확인된 관객 작성자 수", "문의", "불만", "행동 요청"] +
                 [x for x in LEGACY_HEADERS["기간별 요약"] if x not in {"보고 키", "지역", "보고 종류", "저장된 댓글·답글", "확인된 관객 작성자 수"}],
    "댓글 원본": ["연결 키", "지역", "원문", "게시물 링크", "조회 상태"] + [x for x in LEGACY_HEADERS["댓글 원본"] if x not in {"연결 키", "원문", "게시물 링크"}],
    "수집 로그": LEGACY_HEADERS["수집 로그"] + ["지역", "원본 지문", "분류 상태", "시트 반영", "Drive 보관", "노션 보고"],
    "설정": LEGACY_HEADERS["설정"] + ["요청값", "반영 상태"],
}
HUMAN = {
    "분류와 처리": set(LEGACY_HEADERS["분류와 처리"]) - {"연결 키", "계정 ID", "댓글 ID", "현재 원본 버전", "게시물 링크", "계정 링크"},
    "기간별 요약": {"반복 문의", "미해결 건수", "근거", "실행안", "담당"},
    "수집 로그": {"복구 결과"}, "설정": {"요청값"},
}
REGIONAL_FRONT = ["연결 키", "원문", "작성 계정", "작성 시각 (KST)", "댓글·답글", "검토 여부", "담당",
                  "처리 상태", "답변 내용", "자동 주제", "자동 의도", "자동 감정", "게시물 링크", "조회 상태",
                  "계정 ID", "댓글 ID", "상위 댓글 ID", "게시물 ID", "작성자 ID", "본문 제공 여부",
                  "공식 계정 여부", "원본 버전", "선정 사유"]
REGIONAL_HEADERS = REGIONAL_FRONT + [h for h in LEGACY_HEADERS["분류와 처리"]
                                     if h in HUMAN["분류와 처리"] and h not in REGIONAL_FRONT]


def region_tab(account_name, label=None):
    if label and label != LABELS.get(account_name):
        return label + ' · @' + account_name
    return LABELS.get(account_name, "계정 · " + account_name)


def regional_rows(config, comments, annotations, current_ids=None):
    """Every supplied original, including simple reactions and unavailable text."""
    rows = [REGIONAL_HEADERS[:]]
    seen = set()
    for comment in sorted(comments, key=lambda r: (r['written_at'], r['id']), reverse=True):
        identifier = str(comment['id'])
        if identifier in seen or (comment.get('account_id') and comment['account_id'] != config['account_id']):
            raise PilotError('지역별 원본의 중복 또는 다른 계정 자료를 확인해주세요.')
        seen.add(identifier)
        annotation = annotations.get(identifier, {})
        record = {
            '연결 키': config['account_id'] + ':' + identifier, '원문': comment.get('text') or '',
            '작성 계정': comment.get('username') or '', '작성 시각 (KST)': local_time(comment['written_at']),
            '댓글·답글': '답글' if comment.get('parent_id') else '댓글',
            '검토 여부': '검토 전', '처리 상태': '미확인', '답변 상태': '미확인',
            '자동 주제': annotation.get('category', ''), '자동 의도': annotation.get('intent', ''),
            '자동 감정': annotation.get('sentiment', ''), '게시물 링크': comment.get('permalink', ''),
            '조회 상태': '현재 조회' if current_ids is None or identifier in current_ids else '이전 자료',
            '계정 ID': config['account_id'], '댓글 ID': identifier, '상위 댓글 ID': comment.get('parent_id') or '',
            '게시물 ID': comment.get('media_id', ''), '작성자 ID': comment.get('author_id') or '',
            '본문 제공 여부': '미제공' if comment.get('text') is None else '제공',
            '공식 계정 여부': {0:'관객', 1:'공식 계정', None:'미확인'}[comment.get('is_owner')],
            '원본 버전': comment.get('payload_hash', ''), '분류 상태': '자동 분류' if annotation else '미분류',
            '분석 집계 제외 여부': '미판정',
        }
        rows.append([record.get(h, '') for h in REGIONAL_HEADERS])
    return rows


def values(name, record):
    record=dict(record)
    for key,value in record.items():
        if value and ('시각 (KST' in key or key in {'원본 기준','기준일'}):record[key]=local_time(value)
    return [record.get(key, "") for key in HEADERS[name]]


def selection(rows):
    result = screening.summarize(rows)
    if sum(result["counts"].values()) != len(rows):
        raise PilotError("선별 처리군과 원본 건수가 일치하지 않습니다.")
    return result


def make_account(config, source, annotations, stages=None):
    """Build from the same source.json committed before analysis, never Sheets."""
    account = config["account_id"]
    name = config["expected_username"].lstrip("@")
    region = config.get('region_label') or LABELS.get(name, name)
    if source['collection'].get('account_id', account) != account:
        raise PilotError('수집 원본과 시트 대상 계정 ID가 다릅니다.')
    views = source["views"]
    daily = next(v for v in views if v["period"]["key"] == "daily")
    context = source["recent_seven_days"]
    for view in [context, *views]:
        if any(r.get('account_id', account) != account for r in view['comments']):
            raise PilotError('수집 원본에 다른 계정 자료가 있습니다.')
    index = {r["id"]: r for r in context["comments"]}
    for view in views:
        index.update({r["id"]: r for r in view["comments"]})
    stages = stages or {}
    tabs = {name: [header[:]] for name, header in HEADERS.items()}
    picks = selection(list(index.values()))
    daily_selection = selection(daily["comments"])
    repeat_ids=set(repeats(list(index.values()))['comment_ids'])
    review = []
    for row in index.values():
        decision = screening.classify(row)
        annotation = annotations.get(row["id"], {})
        intent = annotation.get("intent", "")
        reasons = []
        if decision["group"] == "hold": reasons.append(decision["reason"])
        if intent in {"문의", "불만", "구체적 행동 요청", "판단 보류"} and decision["group"] != "simple": reasons.append(intent)
        if decision["group"] == "analysis" and not annotation: reasons.append("분류 대기")
        if row['id'] in repeat_ids and decision['group']=='analysis':reasons.append('동일 작성자·문구 반복 후보')
        if decision["group"] == "official": continue
        # Review queues are explicit, not every reaction and never inferred tickets.
        if not reasons: continue
        key = account + ":" + row["id"]
        review.append(row)
        tabs["분류와 처리"].append(values("분류와 처리", {
            "연결 키": key, "지역": region, "계정 ID": account, "댓글 ID": row["id"],
            "원문 발췌": row.get("text") or "본문 확인 불가", "선정 사유": " / ".join(reasons),
            "자동 주제": annotation.get("category", ""), "자동 의도": intent,
            "자동 감정": annotation.get("sentiment", ""), "게시물 링크": row.get("permalink", ""),
            "현재 원본 버전": row.get("payload_hash", ""), "목록 상태": "검토 대상",
            "분류 상태": "자동 분류" if annotation else "미분류", "검토 여부": "검토 전",
            "답변 상태": "미확인", "처리 상태": "미확인", "분석 집계 제외 여부": "미판정",
        }))
    # Sampling is deterministic; it supplements real review reasons without
    # expanding emoji floods into one task per comment.
    sample_limit = config.get("workflow", {}).get("review_sample", 20)
    if not isinstance(sample_limit, int) or not 0 <= sample_limit <= 50:
        raise PilotError("검수 표본 수는 0~50 사이 정수여야 합니다.")
    review_ids = {r["id"] for r in review}
    sample = sorted((r for r in index.values() if r["id"] not in review_ids and r.get("is_owner") != 1),
                    key=lambda r: screening.fingerprint([account, r["id"]]))[:sample_limit]
    for row in sample:
        annotation = annotations.get(row["id"], {})
        tabs["분류와 처리"].append(values("분류와 처리", {"연결 키": account+":"+row["id"], "지역": region,
            "계정 ID": account, "댓글 ID": row["id"], "원문 발췌": row.get("text") or "본문 확인 불가",
            "선정 사유": "선별·분류 검수 표본", "자동 주제": annotation.get("category", ""),
            "자동 의도": annotation.get("intent", ""), "자동 감정": annotation.get("sentiment", ""),
            "현재 원본 버전": row.get("payload_hash", ""), "게시물 링크": row.get("permalink", ""),
            "목록 상태": "검토 표본", "검토 여부": "검토 전", "답변 상태": "미확인", "처리 상태": "미확인"}))
    selected_ids = {r[0].split(":", 1)[1] for r in tabs["분류와 처리"][1:]}
    selected_ids.update(r["id"] for r in index.values() if screening.classify(r)["group"] == "analysis")
    for row in sorted(index.values(), key=lambda r: (r["written_at"], r["id"])):
        if row["id"] not in selected_ids: continue
        tabs["댓글 원본"].append(values("댓글 원본", {"연결 키": account+":"+row["id"], "지역": region,
            "계정 ID": account, "댓글 ID": row["id"],
            "상위 댓글 ID": row.get("parent_id") or "", "게시물 ID": row.get("media_id", ""),
            "원문": row.get("text") or "", "본문 제공 여부": "미제공" if row.get("text") is None else "제공",
            "게시물 링크": row.get("permalink", ""), "작성자 ID": row.get("author_id") or "",
            "공식 계정 여부": "공식 계정" if row.get("is_owner") == 1 else "관객" if row.get("is_owner") == 0 else "미확인",
            "작성 시각 (KST)": row["written_at"], "원본 버전": row.get("payload_hash", ""), "조회 상태": "현재 조회"}))
    for view in [*views,context]:
        period = view["period"]; rows = view["comments"]; screen = selection(rows); metric = view["counts"]
        intents = collections.Counter(annotations.get(r["id"], {}).get("intent") for r in rows if screening.classify(r)["group"] == "analysis")
        tabs["기간별 요약"].append(values("기간별 요약", {"보고 키": f"{account}:{period['key']}:{period['start']}:{period['end']}",
            "지역": region, "계정 ID": account, "보고 종류": period.get("label",period["key"]),
            "저장된 댓글·답글": len(rows), "단순 반응": screen["counts"]["simple"],
            "내용 분석 대상": screen["counts"]["analysis"], "판단 보류": screen["counts"]["hold"],
            "확인된 관객 작성자 수": metric["unique_known_authors"], "문의": intents["문의"], "불만": intents["불만"],
            "행동 요청": intents["구체적 행동 요청"], "본문 제공": len(rows)-metric["text_unavailable"],
            "본문 미제공": metric["text_unavailable"], "공식 계정 작성": screen["counts"]["official"],
            "작성자 미확인": metric["unknown_author_comments"], "데이터 상태": "수집 완료" if source["collection"]["status"]=="complete" else "잠정",
            "시작 시각 (KST, 포함)": period["start"], "종료 시각 (KST, 미포함)": period["end"],
            "미해결 건수": "미확인", "적용 규정": config.get("workflow",{}).get("policy_version",screening.VERSION)}))
    for media in context["media"]:
        rows = [r for r in daily["comments"] if r["media_id"] == media["id"]]
        screen = selection(rows); intents=collections.Counter(annotations.get(r["id"],{}).get("intent") for r in rows if screening.classify(r)["group"]=="analysis")
        repeated=repeats(rows)
        tabs["게시물 반응"].append(values("게시물 반응", {"연결 키": f"{account}:{media['id']}:{daily['period']['end']}",
            "지역":region,"게시물 링크":media.get("permalink",""),"게시물 내용":next(iter((media.get("caption") or "").splitlines()),'')[:120],
            "새 댓글":len(rows),"단순 반응":screen["counts"]["simple"],"내용 분석 대상":screen["counts"]["analysis"],
            "판단 보류":screen["counts"]["hold"],"확인된 작성자":len({r['author_id'] for r in rows if r.get('author_id') and r.get('is_owner')!=1}),
            "문의":intents["문의"],"불만":intents["불만"],"행동 요청":intents["구체적 행동 요청"],
            "반복 후보 댓글":len(repeated['comment_ids']),"반복 후보 작성자":repeated['accounts'],"기준일":daily['period']['target_day']}))
    source_hash = screening.fingerprint({"account_id": account, "account_name": name, "region": region,
        "comments": sorted((r['id'], r.get('payload_hash', screening.fingerprint(r))) for r in index.values()),
        "periods": [v['period'] for v in views], "annotations": annotations, "policy": screening.VERSION})
    tabs["한눈에 보기"].append(values("한눈에 보기", {"연결 키":account,"지역":region,"새 댓글":len(daily['comments']),
        "단순 반응":daily_selection['counts']['simple'],"내용 분석 대상":daily_selection['counts']['analysis'],
        "판단 보류":daily_selection['counts']['hold'],"확인된 작성자":daily['counts']['unique_known_authors'],
        "검토 대상":len(review),"수집 상태":source['collection']['status'],"시트 반영":stages.get('sheets','미반영'),
        "원본 기준":daily['period']['end']}))
    tabs["수집 로그"].append(values("수집 로그", {"실행 ID":source['collection'].get('run_id',source_hash),"지역":region,
        "계정 ID":account,"수집 상태":source['collection']['status'],"처리 단계":"선별·분류·집계",
        "종료 시각 (KST)":source['collection'].get('last_scope_completed_at',''),"원본 지문":source_hash,
        "분류 상태":stages.get('analysis','기존 분류 대조'),"시트 반영":stages.get('sheets','미반영'),
        "Drive 보관":stages.get('drive','미연결'),"노션 보고":stages.get('notion','미반영')}))
    settings={f"{region}/수집 상태":source['collection']['status'],f"{region}/원본 기준":daily['period']['end'],
        f"{region}/선별 버전":screening.VERSION,f"{region}/시트 원본 표시":"선별 대상·검수 표본. 전체 원본은 DB 보존",
        f"{region}/상세 보존 기간":"미설정 · 자동 삭제 없음",f"{region}/Drive 보관":stages.get('drive','미연결'),
        "공통/시트 형식":VERSION,"공통/운영 연결":stages.get('connection','운영 배포·Google 인증 검증 전'),
        "공통/작성 지침":"https://www.notion.so/4130761a512d81509362ffc4b4a8de68",
        "공통/입력 위치":"담당·처리 상태·답변 내용·검토 여부는 사람이 수정. 자동 분류는 별도 열.",
        "공통/설정 요청":"요청값에 변경 희망을 기록. 실제 반영 상태 확인 전에는 실행 설정이 바뀌지 않음."}
    for key,value in settings.items(): tabs['설정'].append(values('설정',{'항목':key,'값':value,'반영 상태':'문서·코드 전환 준비'}))
    regional = region_tab(name, config.get('region_label'))
    tabs[regional] = regional_rows(config, list(index.values()), annotations)
    return {"schema":VERSION,"account_id":account,"account_name":name,"region":region,"day":daily['period']['target_day'],
            "source_hash":source_hash,"selection":picks,"tabs":tabs,"headers":{**HEADERS, regional:REGIONAL_HEADERS},
            "regional_tabs":{regional:account}}


def merge(accounts):
    if not accounts: raise PilotError("통합할 계정 자료가 없습니다.")
    if len({a['account_id'] for a in accounts}) != len(accounts): raise PilotError("통합 계정이 중복됩니다.")
    tabs={name:[header[:]] for name,header in HEADERS.items()}
    headers=dict(HEADERS); regional_tabs={}
    for account in accounts:
        for name, identifier in account.get('regional_tabs', {}).items():
            if name in regional_tabs: raise PilotError('지역별 탭 이름이 중복됩니다: '+name)
            regional_tabs[name]=identifier
        headers.update(account.get('headers', {}))
        for name,rows in account['tabs'].items():
            tabs.setdefault(name, [headers[name][:]])
            existing={r[0] for r in tabs[name][1:]}
            tabs[name].extend(row for row in rows[1:] if row[0] not in existing)
    return {'schema':VERSION,'accounts':[a['account_id'] for a in accounts], 'tabs':tabs,
            'headers':headers,'regional_tabs':regional_tabs,
            'source_hash':screening.fingerprint([a['source_hash'] for a in accounts])}


def plan(source, metadata, snapshot):
    props={s['properties']['title']:s['properties'] for s in metadata.get('sheets',[])}
    requests=[];changed={}
    for name,rows in source['tabs'].items():
        regional=name in source.get('regional_tabs', {})
        header=REGIONAL_HEADERS if regional else HEADERS[name]
        if name not in props or name not in snapshot or snapshot[name][:1]!=[header]:
            raise PilotError("시트 v2 전환·헤더 확인 필요: "+name)
        existing={}
        for i,row in enumerate(snapshot[name][1:],1):
            row = ['' if value is None else value for value in row]
            if not any(v not in ('',None) for v in row):continue
            if not row or not isinstance(row[0],str) or not row[0] or row[0] in existing:
                raise PilotError("시트 연결 키가 비어 있거나 중복됩니다: "+name)
            existing[row[0]]=(i,row+['']*max(0,len(header)-len(row)))
        if regional:
            account=source['regional_tabs'][name]
            if any(not isinstance(r[0],str) or not r[0].startswith(account+':') for r in rows[1:]):
                raise PilotError('지역별 탭에 다른 계정 자료가 있습니다: '+name)
            if any(not key.startswith(account+':') for key in existing):
                raise PilotError('지역별 탭에 다른 계정 자료가 있습니다: '+name)
        changes=[];next_row=len(snapshot[name]);owned=set(header)-HUMAN.get('분류와 처리' if regional else name,set())
        # Age out only this account's viewing window. History and human work stay.
        incoming={r[0] for r in rows[1:]}
        accounts=set(source.get('accounts', [source.get('account_id')]))
        state_column='조회 상태' if regional or name=='댓글 원본' else '목록 상태' if name=='분류와 처리' else None
        if state_column:
            j=header.index(state_column)
            for key,(i,old) in existing.items():
                if key.split(':',1)[0] in accounts and key not in incoming and old[j]!='이전 자료':
                    changes.append((i,j,['이전 자료']))
        for row in rows[1:]:
            # Sheets represents cleared cells as "" even when the saved source
            # explicitly carries JSON null (e.g. an unfinished collection time).
            row = ['' if value is None else value for value in row]
            if row[0] not in existing:
                changes.append((next_row,0,row));next_row+=1;continue
            i,old=existing[row[0]]
            for j,key in enumerate(header):
                # Older saved sources did not carry usernames. A replay must
                # not erase an author already present in the regional list.
                if regional and key=='작성 계정' and not row[j]:continue
                if key in owned and old[j]!=row[j]:changes.append((i,j,[row[j]]))
        if next_row>props[name]['gridProperties']['rowCount']:
            requests.append({'appendDimension':{'sheetId':props[name]['sheetId'],'dimension':'ROWS','length':next_row-props[name]['gridProperties']['rowCount']}})
        for i,j,row in changes:
            if j==0 and i>=len(snapshot[name]) and len(snapshot[name])>1:
                exemplar={'sheetId':props[name]['sheetId'],'startRowIndex':1,'endRowIndex':2,'startColumnIndex':0,'endColumnIndex':len(header)}
                dest={**exemplar,'startRowIndex':i,'endRowIndex':i+1}
                for kind in ('PASTE_FORMAT','PASTE_DATA_VALIDATION'):
                    requests.append({'copyPaste':{'source':exemplar,'destination':dest,'pasteType':kind,'pasteOrientation':'NORMAL'}})
            requests.append({'updateCells':{'range':{'sheetId':props[name]['sheetId'],'startRowIndex':i,'endRowIndex':i+1,'startColumnIndex':j,'endColumnIndex':j+len(row)},
                                          'rows':[{'values':[cell(v) for v in row]}],'fields':'userEnteredValue'}})
        for sheet in metadata.get('sheets',[]):
            if sheet['properties']['title'] != name: continue
            basic=sheet.get('basicFilter')
            if basic and next_row>basic['range'].get('endRowIndex',next_row):
                requests.append({'setBasicFilter':{'filter':{**basic,'range':{**basic['range'],'endRowIndex':next_row}}}})
            for table in sheet.get('tables',[]):
                end=table.get('range',{}).get('endRowIndex',0)
                if next_row > end:
                    requests.append({'updateTable':{'table':{'tableId':table['tableId'],'range':{**table['range'],'endRowIndex':next_row}},'fields':'range'}})
        changed[name]=len({x[0] for x in changes})
    return {'requests':requests,'changed_rows':changed,'source_hash':source['source_hash']}


def validate_projection(source):
    headers = {**HEADERS, **{name: REGIONAL_HEADERS for name in source.get('regional_tabs', {})}}
    if source.get('schema') != VERSION or set(source.get('tabs', {})) != set(headers):
        raise PilotError('재처리 자료의 시트 형식을 확인해주세요.')
    if set(source.get('regional_tabs', {})) & set(HEADERS):
        raise PilotError('계정 탭과 공통 탭 이름이 겹칩니다.')
    if source.get('headers') != headers:
        raise PilotError('재처리 자료의 헤더 정의가 다릅니다.')
    for name, rows in source['tabs'].items():
        if (not rows or rows[0] != headers[name]
                or any(not isinstance(row, list) or len(row) != len(rows[0]) for row in rows)):
            raise PilotError('재처리 자료의 헤더·열 수가 다릅니다: ' + name)
        keys = [r[0] for r in rows[1:]]
        if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
            raise PilotError('재처리 자료의 연결 키가 비어 있거나 중복됩니다: ' + name)


class Client(SheetsClient):
    def __init__(self, *args, headers=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = headers or {**HEADERS, **{name:REGIONAL_HEADERS for name in LABELS.values()}}

    def snapshot(self):
        metadata=self.request(query={'fields':'spreadsheetId,sheets(properties,tables,basicFilter,developerMetadata)'})
        result={}
        for sheet in metadata.get('sheets',[]):
            prop=sheet['properties'];name=prop['title']
            header = self.headers.get(name)
            bound = account_metadata(sheet)
            if header is None:
                # Find renamed legacy account tabs without reading unrelated
                # common, archive or instructions sheets as comment data.
                a1="'"+name.replace("'","''")+"'!1:1"
                first=self.request('/values/'+urllib.parse.quote(a1,safe=''),query={'valueRenderOption':'FORMULA'}).get('values', [])
                if first != [REGIONAL_HEADERS] and not bound:continue
                header = REGIONAL_HEADERS
            count=prop['gridProperties']['rowCount']
            if count>50_000:raise PilotError('시트 조회 상한 초과. 이관·조회 범위 조정 필요.')
            rows=[]
            for start in range(1,count+1,500):
                end=min(start+499,count)
                a1="'"+name.replace("'","''")+f"'!A{start}:{column_name(len(header))}{end}"
                data=self.request('/values/'+urllib.parse.quote(a1,safe=''),query={'valueRenderOption':'FORMULA'})
                values_=data.get('values',[])
                if not isinstance(values_,list) or any(not isinstance(r,list) for r in values_):raise PilotError('시트 행 응답 오류')
                rows.extend(values_+[[] for _ in range(end-start+1-len(values_))])
            while rows and not rows[-1]:rows.pop()
            result[name]=rows
        return metadata,result


def sync(source, client, checkpoint=lambda _:None, dry_run=False, max_batches=20, account_roster=None):
    validate_projection(source)
    def prepare(metadata, snapshot):
        routed, structure, bindings = reconcile(source, metadata, snapshot, REGIONAL_HEADERS, account_roster)
        if account_roster is not None:
            routed=roster_overview(routed,snapshot,account_roster)
        if structure:
            # Check common headers/keys before creating or hiding any tabs.
            common = {**source, 'tabs': {n:r for n,r in source['tabs'].items() if n in HEADERS}}
            plan(common, metadata, snapshot)
            return {'requests': structure, 'changed_rows': {}, 'source_hash': source['source_hash'],
                    'account_tabs': bindings, 'phase': 'account_tabs'}
        return {**plan(routed, metadata, snapshot), 'account_tabs': bindings, 'phase': 'rows'}
    metadata,snapshot=client.snapshot();pending=prepare(metadata,snapshot)
    batches=0
    status={'status':'pending','source_hash':source['source_hash'],'changed_rows':pending['changed_rows'],
            'account_tabs':pending['account_tabs'],'phase':pending['phase']}
    if dry_run:return {**status,'status':'dry_run','batches':len(list(request_batches(pending['requests'])))}
    checkpoint(status)
    while pending['requests'] and batches<max_batches:
        fresh_meta,fresh=client.snapshot()
        if fresh_meta!=metadata or fresh!=snapshot:raise PilotError('시트 조회 중 변경 감지. 다시 대조 후 재실행 필요.')
        client.request(':batchUpdate',body={'requests':next(request_batches(pending['requests']))})
        metadata,snapshot=client.snapshot();new=prepare(metadata,snapshot)
        if new['requests']==pending['requests']:raise PilotError('시트 반영 대조 실패. 기존 자료와 원본 보존.')
        if status['phase'] == 'account_tabs' and new['phase'] == 'rows':
            status.update(changed_rows=new['changed_rows'], phase='rows')
        pending=new;batches+=1;checkpoint({**status,'batches':batches,'remaining_rows':pending['changed_rows'],'phase':pending['phase']})
    status.update(status='complete' if not pending['requests'] else 'pending',batches=batches,
                  remaining_rows=pending['changed_rows'],phase=pending['phase'],account_tabs=pending['account_tabs'])
    checkpoint(status);return status


def roster_overview(source,snapshot,roster):
    """Label inactive history and prepare new regions without fabricating zeros."""
    import copy
    result=copy.deepcopy(source);name='한눈에 보기';header=HEADERS[name]
    incoming={r[0] for r in result['tabs'][name][1:]}
    existing={r[0]:r for r in snapshot.get(name,[])[1:] if r and r[0]}
    for account in roster:
        identifier=account['id']
        if identifier in incoming:continue
        old=existing.get(identifier)
        previous_status=old[header.index('수집 상태')] if old and len(old)>header.index('수집 상태') else ''
        if account['active'] and old and '대상 제외' not in str(previous_status):continue
        if old is None and not account['active']:continue
        row=list(old)+['']*max(0,len(header)-len(old)) if old else ['']*len(header)
        row[0]=identifier;row[header.index('지역')]=account['label']
        row[header.index('수집 상태')]='수집 확인 전' if account['active'] else '대상 제외 · 이력 보존'
        row[header.index('시트 반영')]='반영 확인 대기' if account['active'] else '이전 자료'
        result['tabs'][name].append(row)
    return result
