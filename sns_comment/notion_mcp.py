"""Notion MCP adapter; narrowly edits the dated SNS Comments section."""

import html
import datetime as dt
import json
import re
import time
import urllib.parse

from .api import PilotError
from .profiles import username as account_username


class NoDailyPage(PilotError):
    """No eligible daily page exists yet; keep the dated report for replay."""


def page_id(value):
    if isinstance(value,str) and re.fullmatch(r"[0-9a-fA-F-]{32,36}",value):
        compact = value.replace("-","").lower()
        if re.fullmatch(r"[0-9a-f]{32}",compact):
            return compact
    parsed = urllib.parse.urlsplit(str(value))
    matches = re.findall(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])",parsed.path.replace("-","").lower())
    if parsed.scheme!="https" or parsed.hostname not in {"app.notion.com","www.notion.so","notion.so"} or len(matches)!=1:
        raise PilotError("일일 업무 페이지 ID를 확인하지 못했습니다.")
    return matches[0]


def literal(text):
    text = html.escape(str(text),quote=False)
    return re.sub(r"([\\`*_\[\]#|~])",r"\\\1",text)


def markdown_rich(items):
    result=[]
    for item in items:
        value=item.get("text",{})
        text=literal(value.get("content",item.get("plain_text","")))
        url=(value.get("link") or {}).get("url")
        if url:
            parsed=urllib.parse.urlsplit(url)
            if parsed.scheme!="https" or not parsed.hostname:
                raise PilotError("보고서에 허용되지 않은 근거 링크가 있습니다.")
            text=f"[{text}]({url.replace(')', '%29')})"
        result.append(text)
    return "".join(result)


def markdown_blocks(blocks):
    output=[]
    for item in blocks:
        kind=item["type"]
        value=item[kind]
        text=markdown_rich(value.get("rich_text",[]))
        if kind=="paragraph":output.append(text)
        elif kind=="bulleted_list_item":output.append("- "+text.replace("\n"," "))
        elif kind=="quote":output.append("<quote>\n"+text+"\n</quote>")
        elif kind=="toggle":output.append("<toggle>\n"+text+"\n\n"+markdown_blocks(value.get("children",[]))+"\n</toggle>")
        else:raise PilotError("지원하지 않는 자동 보고 블록입니다.")
    return "\n\n".join(output)


def body(page):
    text=page.get("text","")
    start=text.find("<content>\n")
    end=text.rfind("\n</content>")
    return text[start+len("<content>\n"):end] if start>=0 and end>start else text


def normalized_markdown(text):
    # Notion returns quotes as either XML or Markdown and paragraph line breaks
    # as <br />. Ignore these equivalent encodings, retaining text and links.
    text=re.sub(r'</?quote\b[^>]*>', '', text)
    text=re.sub(r'(?m)^>[ \t]?', '', text)
    text=re.sub(r'<br\s*/?>', '\n', text)
    text=re.sub(r"\\([\\`*_\[\]#|~])", r"\1", text)
    def autolink(match):
        label,url=match.groups()
        parsed=urllib.parse.urlsplit(url)
        # Notion turns plain character names such as D.va into domain links.
        # Only that equivalent label/host pair is ignored; evidence URLs and
        # changed destinations remain part of the readback comparison.
        return label if parsed.netloc.casefold()==label.casefold() and parsed.path in ('','/') and not parsed.query and not parsed.fragment else match.group()
    text=re.sub(r'\[([^\]\s]+)\]\((https?://[^)\s]+)\)',autolink,text)
    return re.sub(r"\s+", " ", text).strip()


def properties(page):
    match=re.search(r"<properties>\s*(\{.*?\})\s*</properties>",page.get("text",""),re.S)
    if not match:
        raise PilotError("일일 업무 페이지 속성을 확인하지 못했습니다.")
    return json.loads(match.group(1))


KST = dt.timezone(dt.timedelta(hours=9))
DAILY_PAGE_LIMIT = 10
EXACT_DATE_TITLE = re.compile(r'^<date start="(\d{4}-\d{2}-\d{2})"\s*/>$')


def _log(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def _created_kst(props, page):
    raw = None
    for key in ("date:생성 일시:start", "생성 일시", "created_time", "createdTime"):
        if isinstance(props, dict) and props.get(key):
            raw = props[key]
            break
    if raw is None and isinstance(page, dict):
        raw = page.get("created_time") or page.get("createdTime")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(KST)


def _exact_date_title(props, event_day):
    raw = props.get("이름") if isinstance(props, dict) else None
    if not isinstance(raw, str):
        raw = props.get("title") if isinstance(props, dict) and isinstance(props.get("title"), str) else ""
    match = EXACT_DATE_TITLE.match(raw.strip())
    return bool(match and match.group(1) == event_day)


def auto_created_daily_page(page, event_day):
    """The page Notion makes for that day: exact date title, created that day or the day before (KST)."""
    props = page.get("_props") if isinstance(page, dict) else None
    created = _created_kst(props, page)
    if created is None or not _exact_date_title(props, event_day):
        return False
    event = dt.date.fromisoformat(event_day)
    start = dt.datetime.combine(event - dt.timedelta(days=1), dt.time(0, 0), tzinfo=KST)
    end = dt.datetime.combine(event + dt.timedelta(days=1), dt.time(0, 0), tzinfo=KST)
    return start <= created < end


def choose_daily_page(candidates):
    """Earliest event day. A same-day duplicate is kept only when one page is the auto-created page."""
    ordered = sorted(candidates, key=lambda page: (page["_daily_day"], page.get("id") or ""))
    earliest = ordered[0]["_daily_day"]
    same = [page for page in ordered if page["_daily_day"] == earliest]
    if len(same) == 1:
        return same[0]
    picked = [page for page in same if auto_created_daily_page(page, earliest)]
    if len(picked) != 1:
        raise PilotError(f"{earliest} 일일 업무 페이지가 여러 개입니다. 임의 선택하지 않습니다.")
    _log("notion_daily_page_duplicate_resolved", severity="WARNING", day=earliest,
         chosen=picked[0].get("id"), pages=[page.get("id") for page in same])
    return picked[0]


def root_matches(pattern, content):
    """Ignore headings in archived merge appendices and other native containers."""
    found, stack, offset, fence = [], [], 0, None
    tags = re.compile(r"<(\/?)(toggle|callout|quote|columns|column|ul|ol|li|table|tr|td|th|synced-block)(?:\s[^>]*)?>")
    for line in content.splitlines(keepends=True):
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
        elif fence is None:
            if not stack:
                match = pattern.match(content, offset)
                if match:
                    found.append(match)
            for match in tags.finditer(line):
                closing, name = match.group(1), match.group(2)
                if match.group().endswith("/>"):
                    continue
                if closing:
                    if not stack or stack.pop() != name:
                        raise PilotError("Notion 본문 컨테이너 구조가 불완전해 갱신을 보류합니다.")
                else:
                    stack.append(name)
        offset += len(line)
    if stack or fence:
        raise PilotError("Notion 본문 컨테이너 구조가 불완전해 갱신을 보류합니다.")
    return found


class NotionMcpClient:
    def __init__(self,client,source_id,username="waterbomb_official",regional=False,region_label=None,
                 account_roster=None,connection_revision=None):
        self.client,self.source_id=client,source_id
        self.username = account_username(username)
        self.regional = regional
        self.region_label = region_label
        self.account_roster=account_roster
        self.connection_revision=connection_revision
        self.schema_loaded=False
        self.markdown_loaded=False
        access=client.call("get-tool-access",{})
        for key in ("fetch","query_data_sources","update_page"):
            entry=access.get("current_tool_access",{}).get(key,{})
            if entry.get("status") not in {"available","available_with_limit"}:
                raise PilotError("Notion MCP 연결의 필수 도구 접근 권한을 확인해야 합니다: "+key)

    def fetch(self,identifier):
        result=self.client.call("fetch",{"id":identifier})
        if not isinstance(result,dict) or not result.get("text"):
            raise PilotError("Notion 문서 응답을 확인하지 못했습니다.")
        return result

    def rules(self,page_id):
        page=self.fetch(page_id)
        if page.get("truncated") or page.get("unknown_block_count",0):
            raise PilotError("최신 규정집 전체를 읽지 못해 분석을 중단합니다.")
        text=body(page)
        # Reference child pages are not silently treated as adopted rules.
        text=re.sub(r"<page\b[^>]*>.*?</page>","",text,flags=re.S)
        version=re.search(r"\bv\d+(?:\.\d+)+",page.get("title","")+"\n"+text)
        if not version or not page.get("page_last_edited_at"):
            raise PilotError("규정 버전·최종 수정 시각을 확인하지 못했습니다.")
        return {"page_id":page_id,"version":version.group(),"last_edited_at":page["page_last_edited_at"],"text":text}

    def workflow_rules(self, parent_id, instructions_id):
        """Read only an explicitly adopted child, not linked examples/history."""
        parent=self.rules(parent_id)
        child=self.rules(instructions_id)
        text=child['text']
        adopted=re.search(r'통합본 (v\d+(?:\.\d+)+) /',text)
        if not adopted:raise PilotError('통합 지침서의 현재 버전을 확인하지 못했습니다.')
        child['version']=adopted.group(1)
        start=text.find('<toggle')
        if start < 0 or '사람이 수정하는 항목별 작성 지침' not in text[:start]:
            raise PilotError('통합 지침서의 사람 작성 영역을 확인하지 못했습니다.')
        # Nested detail toggles belong to the operating section; find the first
        # following named top-level section instead of trusting arbitrary links.
        match=re.search(r'<toggle[^>]*>\s*(?:상세 작성법|문서 관리|실제 수집|관련 문서)',text[start:],re.S)
        operating=text[start:start+match.start()] if match else text[start:]
        if '운영 기준·파이프라인 기본 지침' not in operating or '## 7.' not in operating:
            raise PilotError('통합 지침서의 선별·보관 기준을 확인하지 못했습니다.')
        human=text[:start].strip()
        def item(label):
            found=re.search(r'### '+re.escape(label)+r'\s*\n(.*?)(?=\n### |\Z)',human,re.S)
            if not found:raise PilotError('사람 작성 지침이 누락되었습니다: '+label)
            return found.group(1).strip()
        # Style edits do not invalidate every per-comment classification.
        classification=item('데이터 선별·축적')+'\n'+operating
        return {**child,'parent':{k:v for k,v in parent.items() if k!='text'},
                'workflow_enabled':True,'text':human+'\n'+operating,
                'classification_text':classification,'report_text':human+'\n'+operating}

    def daily_page(self,day,through_day=None):
        try:
            start = dt.date.fromisoformat(day)
            stop = dt.date.fromisoformat(through_day or day)
            if stop < start:
                raise ValueError
        except (TypeError,ValueError):
            raise PilotError("보고 대상 날짜 범위를 확인해주세요.") from None
        source="collection://"+self.source_id
        if not self.schema_loaded:
            schema=self.fetch(source)
            if any(column not in schema["text"] for column in ['"date:이벤트 시간:start"','"유형"','url TEXT']):
                raise PilotError("일일 업무 데이터베이스의 날짜·유형 속성이 변경되어 확인이 필요합니다.")
            self.schema_loaded=True
        query={"data":{"mode":"sql","data_source_urls":[source],
            "query":f'SELECT url,"유형","date:이벤트 시간:start" FROM "{source}" WHERE "유형"=? '
                    'AND substr("date:이벤트 시간:start",1,10)>=? AND substr("date:이벤트 시간:start",1,10)<=? '
                    'ORDER BY substr("date:이벤트 시간:start",1,10),url LIMIT ' + str(DAILY_PAGE_LIMIT),
            "params":["오늘 업무",start.isoformat(),stop.isoformat()]}}
        try:
            result=self.client.call("query-data-sources",query)
        except PilotError:
            # Re-read the owning schema before a bounded read-only retry. A
            # missing-column failure must never trigger guessed SQL mutations.
            self.schema_loaded=False
            fresh=self.fetch(source)
            if any(column not in fresh["text"] for column in ['"date:이벤트 시간:start"','"유형"','url TEXT']):
                raise PilotError("일일 업무 데이터베이스의 속성이 변경되어 확인이 필요합니다.")
            self.schema_loaded=True
            time.sleep(1)
            result=self.client.call("query-data-sources",query)
        if not isinstance(result,dict) or not isinstance(result.get("results"),list):
            raise PilotError("Notion 일일 업무 조회 응답 형식이 변경되었습니다. 페이지 부재로 처리하지 않습니다.")
        rows=result["results"]
        if not rows:
            # Structured date filtering is independent of SQL timestamp text
            # formatting and gives an empty SQL lookup one bounded recheck.
            result=self.client.call("query-data-sources",{"data":{"mode":"rows","data_source_url":source,
                "filter":{"type":"group","operator":"and","filters":[
                    {"type":"property","property":"유형","propertyType":"select","operator":"enum_is",
                     "value":{"type":"exact","value":"오늘 업무"}},
                    {"type":"property","property":"이벤트 시간","propertyType":"date","operator":"date_is_on_or_after",
                     "value":{"type":"exact","value":start.isoformat()}},
                    {"type":"property","property":"이벤트 시간","propertyType":"date","operator":"date_is_on_or_before",
                     "value":{"type":"exact","value":stop.isoformat()}}]},
                "sort":[{"property":"이벤트 시간","direction":"ascending"}],"limit":DAILY_PAGE_LIMIT}})
            if not isinstance(result,dict) or not isinstance(result.get("results"),list):
                raise PilotError("Notion 일일 업무 재조회 응답 형식을 확인하지 못했습니다.")
            rows=result["results"]
        if not rows:
            raise NoDailyPage(f"{day} 보고를 받을 일일 업무 페이지가 아직 없습니다. 다음 일일 업무에 날짜별로 기록합니다.")
        if any(not isinstance(row,dict) or not row.get("url") for row in rows):
            raise PilotError("일일 업무 조회 결과의 페이지 URL을 확인하지 못했습니다.")
        # Fetch property values even for SQL/rows results; only the earliest
        # eligible date is accepted, and duplicate pages remain an error.
        candidates=[]
        for row in rows:
            page=self.fetch(row["url"])
            props=properties(page)
            event_day=str(props.get("date:이벤트 시간:start",""))[:10]
            if props.get("유형")!="오늘 업무" or not start.isoformat()<=event_day<=stop.isoformat():
                raise PilotError("일일 업무 날짜 또는 유형이 보고 대상과 다릅니다.")
            expected=page_id(row["url"])
            if page.get("id") and page_id(page["id"])!=expected:
                raise PilotError("일일 업무 페이지 응답이 조회한 대상과 다릅니다.")
            page.update(id=expected,_daily_day=event_day,_props=props)
            candidates.append(page)
        if result.get("has_more"):
            raise PilotError("일일 업무 조회 결과가 불완전해 기록을 보류합니다.")
        return choose_daily_page(candidates)

    def publish(self,day,sections,through_day=None,guideline_validator=None):
        if not self.markdown_loaded:
            self.fetch("notion://docs/notion-flavored-markdown-spec")
            self.markdown_loaded=True
        page=self.daily_page(day,through_day)
        content=body(page)
        from .notion_sections import locate, replace_body
        area=locate(content)
        section=content[area['body_start']:area['body_end']]
        if re.search(r"<unknown\b|Content omitted|Truncated",section):
            raise PilotError("SNS Comments 영역을 완전하게 읽지 못했습니다.")
        label=f"인스타그램 댓글 자동 보고 · {day} 08시 · @{self.username}"
        carried = (f'해당 날짜 일일 업무가 없어 <date start="{page["_daily_day"]}" /> 일일 업무에 기록한 날짜별 보고입니다.\n\n'
                   if page["_daily_day"]!=day else "")
        report='<callout icon="📊" color="blue_bg">\n'+literal(label)+"\n\n"+carried+markdown_blocks(sections)+"\n</callout>"
        def validate_report():
            if guideline_validator:
                heading = ('### SNS Comments\n' if area['kind']=='toggle'
                           else content[area['start']:area['body_start']]+'\n')
                guideline_validator(heading+report, {'account':self.username,'day':day,
                    'section_kind':'heading','original_section_kind':area['kind'],
                    'account_roster':self.account_roster,'connection_revision':self.connection_revision})
        owned=[]
        for match in re.finditer(r"<callout\b[^>]*>\n",section):
            title=section[match.end():].split("\n",1)[0].replace("\\_","_")
            end=section.find("</callout>",match.end())
            if title==label and end>=0:
                owned.append(section[match.start():end+len("</callout>")])
        if len(owned)>1:
            raise PilotError("SNS Comments 자동 보고가 중복되어 확인이 필요합니다.")
        if self.regional:
            from .regional_notion import update
            markdown=carried+markdown_blocks(sections)
            if self.connection_revision:markdown+='\n\n연결 검증 ID: '+literal(self.connection_revision)
            updated, report = update(section,day,self.username,markdown,self.region_label,self.account_roster,self.connection_revision)
            if guideline_validator and hasattr(guideline_validator,'prepare'):
                prepared=guideline_validator.prepare(report,{'account':self.username,'day':day,
                    'account_roster':self.account_roster,'connection_revision':self.connection_revision})
                if updated.count(report)!=1:
                    raise PilotError('전체 요약 적용 영역이 중복입니다.')
                updated=updated.replace(report,prepared,1)
                report=prepared
            validate_report()
            if normalized_markdown(updated)==normalized_markdown(section) and area['kind']!='toggle':
                return {"page_id":page["id"],"page_url":page.get("url"),"day":day,"page_day":page["_daily_day"],"unchanged":True}
            old,new=replace_body(content,area,updated)
        elif owned:
            validate_report()
            old=owned[0]
            if re.search(r"<(?:page|database|folder)\b",old):
                raise PilotError("자동 보고 안의 하위 콘텐츠를 보존하기 위해 갱신을 보류합니다.")
            if old==report and area['kind']!='toggle':
                return {"page_id":page["id"],"page_url":page.get("url"),"day":day,"page_day":page["_daily_day"],"unchanged":True}
            new=report
        else:
            validate_report()
            placeholders=[x for x in ("- <b>SNS Comments:</b>","- **SNS Comments:**") if section.count(x)==1]
            if placeholders:
                old=placeholders[0]
                new=report
            else:
                old,new=replace_body(content,area,'\n\n'+report+section)
        if content.count(old)!=1:
            raise PilotError("SNS Comments 갱신 지점을 유일하게 확인하지 못했습니다.")
        if area['kind']=='toggle' and old!=content[area['start']:area['end']]:
            old,new=replace_body(content,area,section.replace(old,new,1))
        result=self.client.call("update-page",{"page_id":page["id"],"command":"update_content",
            "content_updates":[{"old_str":old,"new_str":new}],"allow_async":False})
        task=result.get("async_task") or (result if result.get("type")=="async_task" else None)
        if task:
            task_id=task.get("task_id")
            if not task_id:
                raise PilotError("Notion 비동기 작업 식별자를 확인하지 못했습니다.")
            for _ in range(60):
                time.sleep(2)
                polled=self.client.call("get-async-task",{"task_id":task_id})
                if polled.get("status")=="succeeded":
                    result=polled.get("result",{})
                    break
                if polled.get("status")=="failed":
                    raise PilotError("Notion 비동기 보고 반영이 실패했습니다.")
            else:
                raise PilotError("Notion 비동기 보고 반영이 대기 중입니다. 결과 확인 후 재실행이 필요합니다.")
        if result.get("warnings"):
            raise PilotError("Notion 부분 갱신 경고가 있어 완료를 보류합니다.")
        # A successful update response alone does not prove that all sections
        # landed. Leave the document pending on a missing or partial readback.
        verified=body(self.fetch(page['id']))
        if normalized_markdown(report) not in normalized_markdown(verified):
            raise PilotError("Notion 보고를 다시 읽은 결과가 작성 내용과 달라 완료를 보류합니다.")
        expected=content.replace(old,new,1)
        expected_area,verified_area=locate(expected),locate(verified)
        if normalized_markdown(expected[expected_area['start']:expected_area['end']])!=normalized_markdown(verified[verified_area['start']:verified_area['end']]):
            raise PilotError('SNS Comments 안에 동시 변경이 있어 다시 확인해야 합니다.')
        # update_content targets only the SNS area. Other workers and people may
        # legitimately edit other work while the report is reviewed; retain it.
        concurrent=normalized_markdown(expected)!=normalized_markdown(verified)
        return {"page_id":page["id"],"page_url":page.get("url"),"day":day,"page_day":page["_daily_day"],
            "readback_verified":True,"other_work_changed_concurrently":concurrent}
