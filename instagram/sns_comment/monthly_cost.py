"""Record the previous month's Instagram moderation cost in Notion.

Reads the Cloud Billing standard usage export with the Cloud Run service
account (BigQuery REST, no extra packages) and writes one row to the monthly
cost database. A month marked 수동 is left untouched. Missing or empty billing
data writes nothing.

    python -m sns_comment.monthly_cost
    python -m sns_comment.monthly_cost 2026-10
"""

import argparse
import datetime as dt
import json
import os
import re
import time
import urllib.parse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .api import PilotError
from .cloud_state import CloudError, GoogleTransport


PROJECT = "waterbomb-sns-pipeline"
DATASET = "billing_export"
LOCATION = "asia-northeast3"
BILLING_ACCOUNT = "01152F-3EE377-FB410D"
TABLE = "gcp_billing_export_v1_" + BILLING_ACCOUNT.replace("-", "_")
SEOUL = dt.timezone(dt.timedelta(hours=9))
# Standard usage export was turned on this afternoon. November has no earlier rows.
PARTIAL_MONTH = "2026-11"
PARTIAL_START = dt.datetime(2026, 11, 20, tzinfo=SEOUL)
DATA_SOURCE_URL = "collection://41e0761a-512d-814b-ac2f-000b1ff36608"
DATA_SOURCE_ID = "41e0761a-512d-814b-ac2f-000b1ff36608"
VERTEX_SERVICE_ID = "C7E2-9256-1C43"
SOURCE_LABEL = "BigQuery billing_export"
PROJECT_NOTE = "프로젝트 전체, 유튜브 등 포함"
INSTAGRAM_NOTE = "인스타그램 리소스만 집계"
PARTIAL_NOTE = "11/20 이후만 집계"
INSTAGRAM_MARKERS = (
    "sns-comments-moderate",
    "sns-comments-notion-summary",
    "sns-comments-past-sweep",
    "sns-comments-webhook",
    "sns-comments-monthly-cost",
    "sns-comments-caption-policy",
    "sns-comments",
)
OTHER_MARKERS = (
    "youtube-comments",
    "youtube-notion-report",
    "notion-daily-merge",
)
MONTH = re.compile(r"\d{4}-\d{2}")
QUERY_COLUMNS = (
    "service", "service_id", "sku", "sku_id", "currency", "currency_conversion_rate",
    "labels_json", "system_labels_json", "resource_name", "resource_global_name", "cost", "credits",
)


class CostError(PilotError):
    """A billing or Notion result that must not be written as a number."""


def log(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def parse_month(value):
    if not isinstance(value, str) or not MONTH.fullmatch(value):
        raise CostError("월은 YYYY-MM 형식이어야 합니다.")
    month = int(value[5:])
    if month < 1 or month > 12:
        raise CostError("월은 YYYY-MM 형식이어야 합니다.")
    return value


def month_window(month):
    """Inclusive start and exclusive end of a calendar month in Asia/Seoul."""
    year, mon = int(month[:4]), int(month[5:])
    start = dt.datetime(year, mon, 1, tzinfo=SEOUL)
    end = dt.datetime(year + 1, 1, 1, tzinfo=SEOUL) if mon == 12 else dt.datetime(year, mon + 1, 1, tzinfo=SEOUL)
    if month == PARTIAL_MONTH and start < PARTIAL_START:
        start = PARTIAL_START
    return start, end


def resolve_month(month, now):
    """Default to the previous calendar month in Asia/Seoul. An argument overrides it."""
    if month:
        chosen = parse_month(month.strip())
        start, end = month_window(chosen)
        return chosen, start, end
    if now.tzinfo is None:
        raise CostError("기준 시각에 시간대가 없습니다.")
    seoul = now.astimezone(SEOUL)
    first = seoul.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    chosen = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    start, end = month_window(chosen)
    return chosen, start, end


def _decimal(value, label):
    if isinstance(value, bool) or value is None or value == "":
        raise CostError(label + " 값이 없어 기록하지 않습니다.")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise CostError(label + " 값을 읽지 못했습니다. 기록하지 않습니다.") from None


def _rate_text(rate, factor):
    if isinstance(rate, str) and rate.strip():
        return rate.strip()
    return format(factor, "f")


def to_krw(net, currency, rate):
    """Return won for one grouped row.

    ``cost`` is already in ``currency``. ``currency_conversion_rate`` is US dollars
    to that currency, so a KRW row is not multiplied again. A USD row is converted
    only when the export includes a rate greater than 1 (won per dollar). Any other
    currency is left unwritten instead of guessing a rate.
    """
    code = (currency or "").strip().upper()
    if code == "KRW":
        return net, ""
    if code != "USD":
        raise CostError("결제 통화가 " + (code or "비어 있음") + "라 원화로 기록하지 않습니다.")
    factor = _decimal(rate, "환율")
    if factor <= 1:
        raise CostError("USD 비용을 원화로 바꿀 환율이 없어 기록하지 않습니다.")
    return net * factor, "원본 통화 USD, 환율 " + _rate_text(rate, factor)


def json_number(amount):
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral_value():
        return int(quantized)
    return float(format(quantized, "f"))


def label_text(raw):
    if raw is None or raw == "":
        return ""
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
    if isinstance(parsed, list):
        parts = []
        for item in parsed:
            if isinstance(item, dict):
                parts.append(str(item.get("key", "")) + "=" + str(item.get("value", "")))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if parsed is None else str(parsed)


def _haystack(row):
    parts = (
        row.get("service"), row.get("service_id"), row.get("sku"), row.get("sku_id"),
        row.get("resource_name"), row.get("resource_global_name"),
        label_text(row.get("labels_json") if "labels_json" in row else row.get("labels")),
        label_text(row.get("system_labels_json") if "system_labels_json" in row else row.get("system_labels")),
    )
    return "\n".join("" if part is None else str(part) for part in parts).casefold()


def workload(row):
    """instagram, other, or shared. Shared means the row does not name a workload."""
    text = _haystack(row)
    instagram = any(marker in text for marker in INSTAGRAM_MARKERS)
    other = any(marker in text for marker in OTHER_MARKERS)
    if instagram and not other:
        return "instagram"
    if other and not instagram:
        return "other"
    return "shared"


def is_vertex(row):
    service = (row.get("service") or "")
    service_id = (row.get("service_id") or "").strip().upper()
    if service_id == VERTEX_SERVICE_ID:
        return True
    return "vertex ai" in service.casefold()


def _scope(prepared):
    material = [row for row in prepared if row["krw"] != 0]
    considered = material or prepared
    if not considered:
        return "project", []
    if any(row["bucket"] == "shared" for row in considered):
        return "project", prepared
    if any(row["bucket"] == "instagram" for row in considered) or all(row["bucket"] == "other" for row in considered):
        return "instagram", [row for row in prepared if row["bucket"] == "instagram"]
    return "project", prepared


def build_memo(month, scope, notes):
    parts = []
    if month == PARTIAL_MONTH:
        parts.append(PARTIAL_NOTE)
    parts.append(PROJECT_NOTE if scope == "project" else INSTAGRAM_NOTE)
    for note in notes:
        if note and note not in parts:
            parts.append(note)
    return ". ".join(parts)


def summarize(rows, month):
    """Split won into Vertex AI and everything else. Fall back to the project total."""
    if not isinstance(rows, list):
        raise CostError("결제 조회 결과 형식을 확인하지 못했습니다.")
    prepared = []
    notes = []
    for row in rows:
        if not isinstance(row, dict):
            raise CostError("결제 조회 결과 형식을 확인하지 못했습니다.")
        net = _decimal(row.get("cost"), "비용") + _decimal(row.get("credits"), "크레딧")
        krw, note = to_krw(net, row.get("currency"), row.get("currency_conversion_rate"))
        if note and note not in notes:
            notes.append(note)
        prepared.append({"krw": krw, "bucket": workload(row), "vertex": is_vertex(row)})
    scope, chosen = _scope(prepared)
    gcp = sum((row["krw"] for row in chosen if not row["vertex"]), Decimal(0))
    ai = sum((row["krw"] for row in chosen if row["vertex"]), Decimal(0))
    return {
        "month": month,
        "scope": scope,
        "gcp": json_number(gcp),
        "ai": json_number(ai),
        "memo": build_memo(month, scope, notes),
        "source": SOURCE_LABEL,
    }


def properties_of(summary):
    """Notion properties. 합계 is a formula and is never sent."""
    return {
        "월": summary["month"],
        "구글 클라우드": summary["gcp"],
        "AI 비용": summary["ai"],
        "출처": summary["source"],
        "메모": summary["memo"],
        "기록 방식": "자동",
    }


def schema_has_resource(table):
    fields = (table or {}).get("schema", {}).get("fields") or []
    for field in fields:
        if not isinstance(field, dict) or field.get("name") != "resource":
            continue
        names = {child.get("name") for child in field.get("fields") or [] if isinstance(child, dict)}
        return "name" in names and "global_name" in names
    return False


def billing_sql(include_resource):
    resource = (
        "IFNULL(resource.name, '') AS resource_name, IFNULL(resource.global_name, '') AS resource_global_name"
        if include_resource else
        "'' AS resource_name, '' AS resource_global_name"
    )
    table = "`" + PROJECT + "." + DATASET + "." + TABLE + "`"
    return (
        "SELECT IFNULL(service.description, '') AS service, IFNULL(service.id, '') AS service_id, "
        "IFNULL(sku.description, '') AS sku, IFNULL(sku.id, '') AS sku_id, IFNULL(currency, '') AS currency, "
        "currency_conversion_rate AS currency_conversion_rate, TO_JSON_STRING(labels) AS labels_json, "
        "TO_JSON_STRING(system_labels) AS system_labels_json, " + resource + ", "
        "SUM(cost) AS cost, SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits "
        "FROM " + table + " "
        "WHERE project.id = @project AND usage_start_time >= @start AND usage_start_time < @end "
        "GROUP BY service, service_id, sku, sku_id, currency, currency_conversion_rate, "
        "labels_json, system_labels_json, resource_name, resource_global_name"
    )


def _timestamp_param(name, moment):
    return {
        "name": name,
        "parameterType": {"type": "TIMESTAMP"},
        "parameterValue": {"value": moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")},
    }


def _string_param(name, value):
    return {"name": name, "parameterType": {"type": "STRING"}, "parameterValue": {"value": value}}


def _table_url():
    return ("https://bigquery.googleapis.com/bigquery/v2/projects/" + PROJECT +
            "/datasets/" + DATASET + "/tables/" + TABLE)


def _query_url():
    return "https://bigquery.googleapis.com/bigquery/v2/projects/" + PROJECT + "/queries"


def _results_url(response, page_token):
    job = response.get("jobReference") if isinstance(response, dict) else None
    if not isinstance(job, dict) or not job.get("jobId"):
        raise CostError("결제 조회 작업을 확인하지 못했습니다. 기록하지 않습니다.")
    project = job.get("projectId") or PROJECT
    query = {"location": job.get("location") or LOCATION, "timeoutMs": 60000, "maxResults": 1000, "pageToken": page_token}
    return ("https://bigquery.googleapis.com/bigquery/v2/projects/" + urllib.parse.quote(project, safe="") +
            "/queries/" + urllib.parse.quote(job["jobId"], safe="") + "?" + urllib.parse.urlencode(query))


def _missing(error):
    return isinstance(error, CloudError) and error.status == 404


def load_table(transport):
    """Return the table metadata, or None when the export table is not there yet."""
    try:
        return transport.json("GET", _table_url())
    except CloudError as error:
        if _missing(error):
            return None
        raise


def _field_names(schema):
    fields = (schema or {}).get("fields") or []
    names = []
    for field in fields:
        name = field.get("name") if isinstance(field, dict) else None
        if not isinstance(name, str) or not name:
            raise CostError("결제 조회 결과 형식을 확인하지 못했습니다.")
        names.append(name)
    if names != list(QUERY_COLUMNS):
        raise CostError("결제 조회 결과 열을 확인하지 못했습니다. 기록하지 않습니다.")
    return names


def _parse_rows(rows, schema):
    names = _field_names(schema)
    parsed = []
    for row in rows or []:
        cells = row.get("f") if isinstance(row, dict) else None
        if not isinstance(cells, list) or len(cells) != len(names):
            raise CostError("결제 조회 결과 형식을 확인하지 못했습니다.")
        values = []
        for cell in cells:
            if not isinstance(cell, dict) or "v" not in cell:
                raise CostError("결제 조회 결과 형식을 확인하지 못했습니다.")
            values.append(cell.get("v"))
        parsed.append(dict(zip(names, values)))
    return parsed


def _poll(transport, response, sleeper):
    job = response.get("jobReference") if isinstance(response, dict) else None
    if not isinstance(job, dict) or not job.get("jobId"):
        raise CostError("결제 조회 작업을 확인하지 못했습니다. 기록하지 않습니다.")
    project = job.get("projectId") or PROJECT
    query = urllib.parse.urlencode({
        "location": job.get("location") or LOCATION, "timeoutMs": 60000, "maxResults": 1000})
    url = ("https://bigquery.googleapis.com/bigquery/v2/projects/" + urllib.parse.quote(project, safe="") +
           "/queries/" + urllib.parse.quote(job["jobId"], safe="") + "?" + query)
    sleeper(2)
    try:
        return transport.json("GET", url)
    except CloudError as error:
        if _missing(error):
            return None
        raise


def read_query(transport, response, sleeper):
    schema = None
    rows = []
    for _ in range(40):
        if response is None:
            return None
        if response.get("errors"):
            raise CostError("결제 조회에 실패했습니다. 노션에 쓰지 않습니다.")
        # A later page is already part of a finished job. Polling again would reread the first page.
        if response.get("jobComplete") is not True and schema is None:
            if not (isinstance(response.get("jobReference"), dict) and response["jobReference"].get("jobId")):
                raise CostError("결제 조회가 끝나지 않았습니다. 기록하지 않습니다.")
            response = _poll(transport, response, sleeper)
            continue
        if response.get("jobComplete") is False:
            raise CostError("결제 조회 결과 페이지를 확인하지 못했습니다. 기록하지 않습니다.")
        if schema is None:
            schema = response.get("schema")
        rows.extend(response.get("rows") or [])
        token = response.get("pageToken")
        if not token:
            return _parse_rows(rows, schema)
        try:
            response = transport.json("GET", _results_url(response, token))
        except CloudError as error:
            if _missing(error):
                return None
            raise
    raise CostError("결제 조회 결과가 끝나지 않아 기록하지 않습니다.")


def fetch_rows(transport, start, end, sleeper=time.sleep):
    """Grouped billing rows, None when the table does not exist, or an empty list."""
    table = load_table(transport)
    if table is None:
        return None
    body = {
        "query": billing_sql(schema_has_resource(table)),
        "useLegacySql": False,
        "parameterMode": "NAMED",
        "location": LOCATION,
        "labels": {"app": "sns-comments-monthly-cost"},
        "timeoutMs": 60000,
        "maxResults": 1000,
        "queryParameters": [
            _string_param("project", PROJECT),
            _timestamp_param("start", start),
            _timestamp_param("end", end),
        ],
    }
    try:
        response = transport.json("POST", _query_url(), body)
    except CloudError as error:
        if _missing(error):
            return None
        raise
    return read_query(transport, response, sleeper)


def plain(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if isinstance(value.get("name"), str):
            return value["name"].strip()
        return plain(value.get("plain_text") or value.get("text") or value.get("content"))
    if isinstance(value, list):
        return " ".join(plain(item) for item in value).strip()
    return str(value).strip()


def _checked(result, action):
    if isinstance(result, dict) and result.get("warnings"):
        raise CostError(action + "에 경고가 있어 완료로 보지 않습니다. 노션을 확인한 뒤 다시 실행해주세요.")
    if isinstance(result, dict) and (result.get("async_task") or result.get("type") == "async_task"):
        raise CostError(action + "이 끝나기 전에 응답이 돌아왔습니다. 노션을 확인한 뒤 다시 실행해주세요.")
    return result


def existing_pages(mcp, month):
    result = mcp.call("query-data-sources", {"data": {
        "mode": "sql",
        "data_source_urls": [DATA_SOURCE_URL],
        "query": 'SELECT url, "월", "기록 방식" FROM "' + DATA_SOURCE_URL + '" WHERE "월" = ?',
        "params": [month],
    }})
    if not isinstance(result, dict):
        raise CostError("노션 조회 결과를 확인하지 못했습니다. 기록하지 않습니다.")
    if result.get("has_more") or result.get("next_cursor"):
        raise CostError("같은 월의 노션 행을 한 번에 읽지 못했습니다. 기록하지 않습니다.")
    rows = result.get("results")
    if not isinstance(rows, list):
        raise CostError("노션 조회 결과를 확인하지 못했습니다. 기록하지 않습니다.")
    pages = []
    for row in rows:
        if not isinstance(row, dict):
            raise CostError("노션 조회 결과를 확인하지 못했습니다. 기록하지 않습니다.")
        if plain(row.get("월")) != month:
            raise CostError("노션 조회 결과가 요청한 월과 다릅니다. 기록하지 않습니다.")
        pages.append({"url": plain(row.get("url")), "mode": plain(row.get("기록 방식"))})
    return pages


def record(mcp, summary):
    """Create the month, or update rows marked 자동. Never change a 수동 row."""
    pages = existing_pages(mcp, summary["month"])
    automatic = [page for page in pages if page["mode"] == "자동"]
    if pages and not automatic:
        log("monthly_cost_skipped", month=summary["month"], reason="manual_or_unknown",
            message="이 월은 자동 기록이 아니라서 바꾸지 않습니다.")
        return "skipped"
    for page in automatic:
        if not page["url"]:
            raise CostError("노션 행 주소를 확인하지 못했습니다. 기록하지 않습니다.")
    props = properties_of(summary)
    if "합계" in props:
        raise CostError("합계는 노션 수식입니다. 값을 쓰지 않습니다.")
    if not pages:
        _checked(mcp.call("create-pages", {
            "parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
            "pages": [{"properties": props}],
            "allow_async": False,
        }), "월별 비용 추가")
        return "created"
    for page in automatic:
        _checked(mcp.call("update-page", {
            "page_id": page["url"],
            "command": "update_properties",
            "properties": props,
            "allow_async": False,
        }), "월별 비용 수정")
    return "updated"


def project_id():
    configured = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if configured and configured != PROJECT:
        raise CostError("프로젝트 설정을 확인해주세요.")
    return PROJECT


def open_notion(transport):
    from .mcp import McpClient
    from .notion_oauth import cloud_credentials
    secret = os.environ.get("SNS_NOTION_OAUTH_SECRET", "").strip()
    if not secret:
        raise CostError("Notion 연결 설정을 확인해주세요.")
    auth = cloud_credentials(transport, secret)
    auth.refresh()
    return McpClient(auth)


def run(month, now, transport, notion=None, sleeper=time.sleep):
    project_id()
    chosen, start, end = resolve_month(month, now)
    rows = fetch_rows(transport, start, end, sleeper)
    if rows is None:
        log("monthly_cost_skipped", month=chosen, reason="billing_table_missing",
            message="결제 내보내기 표가 아직 없습니다. 노션에 쓰지 않습니다.")
        return 0
    if not rows:
        log("monthly_cost_skipped", month=chosen, reason="no_usage",
            message="해당 월 결제 데이터가 없습니다. 노션에 쓰지 않습니다.")
        return 0
    summary = summarize(rows, chosen)
    writer = notion if notion is not None else open_notion(transport)
    outcome = record(writer, summary)
    log("monthly_cost_done", month=chosen, outcome=outcome, scope=summary["scope"],
        gcp=summary["gcp"], ai=summary["ai"], memo=summary["memo"], rows=len(rows))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="지난달 인스타그램 댓글 관리 비용을 노션에 기록합니다.")
    parser.add_argument("month", nargs="?", help="YYYY-MM. 비우면 한국시간 기준 지난달.")
    args = parser.parse_args(argv)
    try:
        return run(args.month, dt.datetime.now(dt.timezone.utc), GoogleTransport())
    except (CostError, PilotError, CloudError) as error:
        log("monthly_cost_failed", severity="ERROR", error=str(error))
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
