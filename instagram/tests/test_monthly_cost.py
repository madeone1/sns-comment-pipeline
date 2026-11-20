"""Month boundaries, billing split, and Notion idempotency for the monthly cost job."""

import contextlib
import datetime as dt
import io
import os
from decimal import Decimal
import unittest
from unittest import mock

from sns_comment.cloud_state import CloudError
from sns_comment.monthly_cost import (
    DATA_SOURCE_ID, DATA_SOURCE_URL, INSTAGRAM_NOTE, LOCATION, PARTIAL_NOTE,
    PROJECT, PROJECT_NOTE, QUERY_COLUMNS, CostError, billing_sql, build_memo, existing_pages,
    fetch_rows, json_number, month_window, properties_of, record, resolve_month, run, schema_has_resource,
    summarize, workload,
)
from sns_comment import monthly_cost


SEOUL = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc


def krw(**overrides):
    row = {
        "service": "Cloud Run", "service_id": "152E-C115-5142", "sku": "Cloud Run CPU", "sku_id": "sku",
        "currency": "KRW", "currency_conversion_rate": "1400", "labels_json": "[]", "system_labels_json": "[]",
        "resource_name": "", "resource_global_name": "", "cost": "0", "credits": "0",
    }
    row.update(overrides)
    return row


def bq_schema():
    return {"fields": [{"name": name, "type": "STRING"} for name in QUERY_COLUMNS]}


def bq_cells(row):
    values = []
    for name in QUERY_COLUMNS:
        value = row.get(name, "")
        values.append({"v": None if value is None else value})
    return {"f": values}


class Transport:
    def __init__(self, table="plain", responses=None, status=None):
        self.table = table
        self.responses = list(responses or [])
        self.status = status
        self.calls = []

    def json(self, method, url, value=None):
        self.calls.append((method, url, value))
        if self.status:
            raise CloudError("클라우드 API 요청 실패", self.status)
        if method == "GET" and "/tables/" in url:
            if self.table is None:
                raise CloudError("없음", 404)
            if self.table == "resource":
                return {"schema": {"fields": [
                    {"name": "cost", "type": "FLOAT"},
                    {"name": "resource", "type": "RECORD", "fields": [
                        {"name": "name", "type": "STRING"}, {"name": "global_name", "type": "STRING"}]},
                ]}}
            return {"schema": {"fields": [{"name": "cost", "type": "FLOAT"}]}}
        if not self.responses:
            raise AssertionError("unexpected query " + url)
        return self.responses.pop(0)


class Notion:
    def __init__(self, rows=None, warnings=False, has_more=False):
        self.rows = rows or []
        self.warnings = warnings
        self.has_more = has_more
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "query-data-sources":
            payload = {"results": self.rows}
            if self.has_more:
                payload["has_more"] = True
                payload["next_cursor"] = "more"
            return payload
        if self.warnings:
            return {"warnings": ["확인 필요"]}
        return {}


def money(value):
    return Decimal(str(value))


class MonthWindowTests(unittest.TestCase):
    def test_previous_month_follows_seoul_calendar_edges(self):
        # 2026-11-30 15:00 UTC is 2026-12-01 00:00 in Seoul, so the previous month is November.
        month, start, end = resolve_month(None, dt.datetime(2026, 11, 30, 15, 0, tzinfo=UTC))
        self.assertEqual(month, "2026-11")
        self.assertEqual(start, dt.datetime(2026, 11, 20, tzinfo=SEOUL))
        self.assertEqual(end, dt.datetime(2026, 12, 1, tzinfo=SEOUL))
        # One minute earlier is still November in Seoul, so the previous month is October.
        month, start, end = resolve_month(None, dt.datetime(2026, 11, 30, 14, 59, tzinfo=UTC))
        self.assertEqual((month, start, end), ("2026-10", dt.datetime(2026, 10, 1, tzinfo=SEOUL), dt.datetime(2026, 11, 1, tzinfo=SEOUL)))
        month, start, end = resolve_month(None, dt.datetime(2026, 1, 1, tzinfo=SEOUL))
        self.assertEqual((month, start, end), ("2025-12", dt.datetime(2025, 12, 1, tzinfo=SEOUL), dt.datetime(2026, 1, 1, tzinfo=SEOUL)))
        month, start, end = resolve_month(None, dt.datetime(2026, 3, 1, tzinfo=SEOUL))
        self.assertEqual((month, start, end), ("2026-02", dt.datetime(2026, 2, 1, tzinfo=SEOUL), dt.datetime(2026, 3, 1, tzinfo=SEOUL)))
        self.assertEqual(month_window("2026-12"), (dt.datetime(2026, 12, 1, tzinfo=SEOUL), dt.datetime(2027, 1, 1, tzinfo=SEOUL)))

    def test_override_month_and_rejects_bad_input(self):
        month, start, end = resolve_month(" 2026-11 ", dt.datetime(2026, 12, 1, tzinfo=SEOUL))
        self.assertEqual(month, "2026-11")
        self.assertEqual(start, dt.datetime(2026, 11, 20, tzinfo=SEOUL))
        self.assertEqual(end, dt.datetime(2026, 12, 1, tzinfo=SEOUL))
        for bad in ("2026-13", "2026-1", "2026-11-01", "eleven", None):
            if bad is None:
                with self.assertRaises(CostError):
                    resolve_month(None, dt.datetime(2026, 12, 1))
            else:
                with self.assertRaises(CostError):
                    resolve_month(bad, dt.datetime(2026, 12, 1, tzinfo=SEOUL))


class SplitTests(unittest.TestCase):
    def test_vertex_and_credits_and_krw_rate_is_not_applied_twice(self):
        summary = summarize([
            krw(service="Vertex AI", service_id="C7E2-9256-1C43", sku="Gemini 2.5 Flash", cost="10", credits="-1"),
            krw(cost="4", credits="-0.5"),
            krw(service="Cloud Storage", cost="1.004", credits="0"),
            krw(service="Cloud Storage", sku="Storage", cost="1.004", credits="0"),
        ], "2026-10")
        self.assertEqual(money(summary["ai"]), Decimal("9"))
        self.assertEqual(money(summary["gcp"]), Decimal("5.51"))
        self.assertEqual(summary["scope"], "project")
        self.assertIn(PROJECT_NOTE, summary["memo"])
        self.assertNotIn("환율", summary["memo"])
        self.assertNotIn("합계", properties_of(summary))

    def test_service_id_marks_vertex_even_when_the_description_differs(self):
        summary = summarize([krw(service="Something else", service_id="c7e2-9256-1c43", cost="3", credits="0")], "2026-10")
        self.assertEqual(summary["ai"], 3)
        self.assertEqual(summary["gcp"], 0)
        other = summarize([krw(service="Gemini API", sku="Gemini", cost="3", credits="0")], "2026-10")
        self.assertEqual(other["ai"], 0)
        self.assertEqual(other["gcp"], 3)

    def test_instagram_rows_are_kept_when_every_workload_is_named(self):
        instagram = "//run.googleapis.com/projects/waterbomb-sns-pipeline/locations/asia-northeast3/jobs/sns-comments-moderate"
        youtube = "//run.googleapis.com/projects/waterbomb-sns-pipeline/locations/asia-northeast3/jobs/youtube-comments-daily"
        summary = summarize([
            krw(service="Vertex AI", sku="Gemini", cost="20", credits="0", resource_global_name=instagram),
            krw(cost="80", credits="-5", resource_global_name=instagram),
            krw(cost="40", credits="0", resource_global_name=youtube,
                labels_json='[{"key":"app","value":"youtube-comments"}]'),
        ], "2026-10")
        self.assertEqual(summary["scope"], "instagram")
        self.assertEqual(summary["ai"], 20)
        self.assertEqual(summary["gcp"], 75)
        self.assertIn(INSTAGRAM_NOTE, summary["memo"])
        self.assertNotIn(PROJECT_NOTE, summary["memo"])

    def test_shared_row_uses_the_project_total(self):
        summary = summarize([
            krw(cost="80", credits="0", resource_name="sns-comments-webhook"),
            krw(service="Vertex AI", sku="Gemini", cost="20", credits="0"),
            krw(cost="10", credits="0", labels_json='[{"key":"app","value":"youtube-notion-report"}]'),
            krw(cost="1", credits="-1", system_labels_json='[{"key":"goog-resource-type","value":"cloud_run_job"}]'),
        ], "2026-11")
        self.assertEqual(summary["scope"], "project")
        self.assertEqual(summary["ai"], 20)
        self.assertEqual(summary["gcp"], 90)
        self.assertIn(PROJECT_NOTE, summary["memo"])
        self.assertIn(PARTIAL_NOTE, summary["memo"])
        self.assertEqual(workload(krw(system_labels_json='[{"key":"goog-resource-type","value":"cloud_run_job"}]')), "shared")
        self.assertEqual(workload(krw(resource_name="notion-daily-merge")), "other")

    def test_only_other_workloads_record_zero_instagram_cost(self):
        summary = summarize([
            krw(cost="15", credits="0", sku="youtube-comments-daily"),
        ], "2026-10")
        self.assertEqual(summary["scope"], "instagram")
        self.assertEqual(summary["gcp"], 0)
        self.assertEqual(summary["ai"], 0)

    def test_usd_converts_with_the_exported_rate_and_krw_does_not(self):
        converted = summarize([krw(currency="USD", currency_conversion_rate="1000", cost="2", credits="-0.5")], "2026-10")
        self.assertEqual(converted["gcp"], 1500)
        self.assertIn("원본 통화 USD, 환율 1000", converted["memo"])
        with self.assertRaises(CostError):
            summarize([krw(currency="USD", currency_conversion_rate="1", cost="2", credits="0")], "2026-10")
        with self.assertRaises(CostError):
            summarize([krw(currency="EUR", currency_conversion_rate="1400", cost="2", credits="0")], "2026-10")
        with self.assertRaises(CostError):
            summarize([krw(currency="", cost="2", credits="0")], "2026-10")

    def test_negative_net_and_half_up_rounding(self):
        self.assertEqual(json_number(Decimal("1.005")), 1.01)
        self.assertEqual(json_number(Decimal("-2.00")), -2)
        summary = summarize([krw(cost="1", credits="-2")], "2026-10")
        self.assertEqual(summary["gcp"], -1)


class QueryTests(unittest.TestCase):
    def test_sql_uses_resource_names_only_when_the_table_has_them(self):
        self.assertTrue(schema_has_resource({"schema": {"fields": [
            {"name": "resource", "type": "RECORD", "fields": [{"name": "name"}, {"name": "global_name"}]}]}}))
        self.assertFalse(schema_has_resource({"schema": {"fields": [{"name": "cost"}]}}))
        self.assertIn("resource.global_name", billing_sql(True))
        self.assertNotIn("resource.name", billing_sql(False))
        self.assertIn("gcp_billing_export_v1_01152F_3EE377_FB410D", billing_sql(False))
        self.assertIn("UNNEST(credits)", billing_sql(False))

    def test_missing_table_and_empty_month_write_nothing(self):
        secret = "notion-oauth-secret-value"
        sleeps = []
        missing = Transport(table=None)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"SNS_NOTION_OAUTH_SECRET": secret}), contextlib.redirect_stdout(output):
            code = run(None, dt.datetime(2026, 12, 1, tzinfo=SEOUL), missing, notion=Notion(), sleeper=sleeps.append)
        self.assertEqual(code, 0)
        self.assertEqual(missing.calls[0][0], "GET")
        self.assertIn("/tables/gcp_billing_export_v1_01152F_3EE377_FB410D", missing.calls[0][1])
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("결제 내보내기 표가 아직 없습니다", output.getvalue())
        self.assertEqual(sleeps, [])

        empty = Transport(responses=[{"jobComplete": True, "schema": bq_schema(), "rows": [], "totalRows": "0"}])
        notion = Notion()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run("2026-10", dt.datetime(2026, 12, 1, tzinfo=SEOUL), empty, notion=notion)
        self.assertEqual(code, 0)
        self.assertEqual(notion.calls, [])
        self.assertIn("결제 데이터가 없습니다", output.getvalue())
        posted = empty.calls[1][2]
        self.assertEqual(posted["location"], LOCATION)
        self.assertFalse(posted["useLegacySql"])
        self.assertEqual(posted["labels"], {"app": "sns-comments-monthly-cost"})
        params = {item["name"]: item["parameterValue"]["value"] for item in posted["queryParameters"]}
        self.assertEqual(params["project"], PROJECT)
        self.assertEqual(params["start"], "2026-09-30 15:00:00.000000")
        self.assertEqual(params["end"], "2026-10-31 15:00:00.000000")

    def test_query_pages_until_complete_and_a_later_404_writes_nothing(self):
        first = bq_cells(krw(cost="4", credits="0", resource_global_name="sns-comments-moderate"))
        second = bq_cells(krw(cost="6", credits="0", resource_global_name="sns-comments-webhook"))
        transport = Transport(responses=[
            {"jobComplete": False, "jobReference": {"jobId": "job-1", "projectId": PROJECT, "location": LOCATION}},
            {"jobComplete": True, "schema": bq_schema(), "rows": [first], "pageToken": "next",
             "jobReference": {"jobId": "job-1", "projectId": PROJECT, "location": LOCATION}},
            {"jobComplete": True, "schema": bq_schema(), "rows": [second],
             "jobReference": {"jobId": "job-1", "location": LOCATION}},
        ])
        sleeps = []
        rows = fetch_rows(transport, *month_window("2026-10"), sleeper=sleeps.append)
        self.assertEqual(sleeps, [2])
        self.assertIn("pageToken=next", transport.calls[-1][1])
        self.assertIn("location=" + LOCATION, transport.calls[-1][1])
        summary = summarize(rows, "2026-10")
        self.assertEqual(summary["gcp"], 10)
        self.assertEqual(summary["scope"], "instagram")

        gone = Transport(responses=[
            {"jobComplete": False, "jobReference": {"jobId": "job-2", "location": LOCATION}},
        ], status=None)
        def json(method, url, value=None):
            gone.calls.append((method, url, value))
            if method == "GET" and "/tables/" in url:
                return {"schema": {"fields": [{"name": "cost"}]}}
            if "/queries/job-2" in url:
                raise CloudError("없음", 404)
            return {"jobComplete": False, "jobReference": {"jobId": "job-2", "location": LOCATION}}
        gone.json = json
        self.assertIsNone(fetch_rows(gone, *month_window("2026-10"), sleeper=lambda _seconds: None))

    def test_bad_schema_does_not_invent_a_total(self):
        transport = Transport(responses=[{"jobComplete": True, "schema": {"fields": [{"name": "cost"}]}, "rows": []}])
        with self.assertRaises(CostError):
            fetch_rows(transport, *month_window("2026-10"), sleeper=lambda _seconds: None)


class NotionTests(unittest.TestCase):
    def summary(self, month="2026-10"):
        return summarize([krw(cost="12", credits="-2", resource_name="sns-comments-moderate")], month)

    def test_creates_an_automatic_row_without_the_formula(self):
        notion = Notion()
        summary = self.summary()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(record(notion, summary), "created")
        query, create = notion.calls
        self.assertEqual(query[0], "query-data-sources")
        self.assertEqual(query[1]["data"]["data_source_urls"], [DATA_SOURCE_URL])
        self.assertIn('"월" = ?', query[1]["data"]["query"])
        self.assertEqual(query[1]["data"]["params"], ["2026-10"])
        self.assertEqual(create[1]["parent"], {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID})
        props = create[1]["pages"][0]["properties"]
        self.assertEqual(props["월"], "2026-10")
        self.assertEqual(props["구글 클라우드"], 10)
        self.assertEqual(props["AI 비용"], 0)
        self.assertEqual(props["기록 방식"], "자동")
        self.assertEqual(props["출처"], "BigQuery billing_export")
        self.assertNotIn("합계", props)
        self.assertIsInstance(props["구글 클라우드"], int)

    def test_updates_only_automatic_rows(self):
        notion = Notion(rows=[
            {"url": "https://app.notion.com/p/auto", "월": "2026-10", "기록 방식": "자동"},
            {"url": "https://app.notion.com/p/manual", "월": "2026-10", "기록 방식": {"name": "수동"}},
            {"url": "https://app.notion.com/p/auto-2", "월": "2026-10", "기록 방식": "자동"},
        ])
        self.assertEqual(record(notion, self.summary()), "updated")
        updates = [call for call in notion.calls if call[0] == "update-page"]
        self.assertEqual([call[1]["page_id"] for call in updates],
                         ["https://app.notion.com/p/auto", "https://app.notion.com/p/auto-2"])
        self.assertTrue(all(call[1]["command"] == "update_properties" for call in updates))
        self.assertTrue(all("합계" not in call[1]["properties"] for call in updates))
        self.assertFalse(any(call[0] == "create-pages" for call in notion.calls))

    def test_manual_unknown_and_incomplete_reads_do_not_write(self):
        manual = Notion(rows=[{"url": "https://app.notion.com/p/manual", "월": "2026-10", "기록 방식": "수동"}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(record(manual, self.summary()), "skipped")
        self.assertEqual([call[0] for call in manual.calls], ["query-data-sources"])

        blank = Notion(rows=[{"url": "https://app.notion.com/p/blank", "월": "2026-10", "기록 방식": ""}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(record(blank, self.summary()), "skipped")
        self.assertFalse(any(call[0] in {"create-pages", "update-page"} for call in blank.calls))

        more = Notion(rows=[{"url": "https://app.notion.com/p/auto", "월": "2026-10", "기록 방식": "자동"}], has_more=True)
        with self.assertRaises(CostError):
            record(more, self.summary())
        self.assertFalse(any(call[0] == "create-pages" for call in more.calls))

        wrong = Notion(rows=[{"url": "https://app.notion.com/p/other", "월": "2026-09", "기록 방식": "자동"}])
        with self.assertRaises(CostError):
            existing_pages(wrong, "2026-10")

        warned = Notion(warnings=True)
        with self.assertRaises(CostError):
            record(warned, self.summary())

    def test_run_records_a_labeled_month_and_a_permission_error_does_not(self):
        row = bq_cells(krw(service="Vertex AI", sku="Gemini", cost="2", credits="0",
                           resource_global_name="jobs/sns-comments-notion-summary"))
        transport = Transport(responses=[{"jobComplete": True, "schema": bq_schema(), "rows": [row]}])
        notion = Notion()
        secret = "super-secret-notion-token"
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"SNS_NOTION_OAUTH_SECRET": secret, "GOOGLE_CLOUD_PROJECT": PROJECT}), \
             contextlib.redirect_stdout(output):
            code = run("2026-11", dt.datetime(2026, 12, 1, tzinfo=SEOUL), transport, notion=notion)
        self.assertEqual(code, 0)
        props = notion.calls[-1][1]["pages"][0]["properties"]
        self.assertEqual(props["AI 비용"], 2)
        self.assertEqual(props["구글 클라우드"], 0)
        self.assertIn(PARTIAL_NOTE, props["메모"])
        self.assertIn(INSTAGRAM_NOTE, props["메모"])
        self.assertNotIn(secret, output.getvalue())

        denied = Transport(status=403)
        output = io.StringIO()
        with mock.patch.object(monthly_cost, "GoogleTransport", return_value=denied), contextlib.redirect_stdout(output):
            self.assertEqual(monthly_cost.main(["2026-10"]), 1)
        self.assertIn("monthly_cost_failed", output.getvalue())
        self.assertNotIn(secret, output.getvalue())
        with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "other-project"}):
            with self.assertRaises(CostError):
                run("2026-10", dt.datetime(2026, 12, 1, tzinfo=SEOUL), Transport())


class DocTests(unittest.TestCase):
    def test_cloud_doc_has_the_one_time_commands(self):
        path = os.path.join(os.path.dirname(__file__), "..", "CLOUD.md")
        with open(path, encoding="utf-8") as handle:
            body = handle.read().split("## 월별 비용 기록", 1)[1]
        self.assertIn("sns-comments-monthly-cost", body)
        self.assertIn("--update-env-vars", body)
        self.assertNotIn("--set-env-vars", body)
        self.assertIn("roles/bigquery.dataViewer", body)
        self.assertIn("roles/bigquery.jobUser", body)
        self.assertIn("billing_export", body)
        self.assertIn("0 10 1 * *", body)
        self.assertIn("Asia/Seoul", body)
        self.assertIn("sns-scheduler@waterbomb-sns-pipeline.iam.gserviceaccount.com", body)
        self.assertIn("roles/run.invoker", body)
        self.assertIn("sns_comment.monthly_cost", body)
        self.assertIn(PROJECT_NOTE, body)
        self.assertIn(PARTIAL_NOTE, body)
        self.assertTrue(build_memo("2026-11", "project", ["원본 통화 USD, 환율 1350"]).startswith(PARTIAL_NOTE))


if __name__ == "__main__":
    unittest.main()
