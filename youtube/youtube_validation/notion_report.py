"""Append a YouTube summary inside the existing Instagram daily report.

Uses a Notion connection token. It never changes Instagram's summary, decision
table or other daily-page content. Source collection and OAuth are read-only.
"""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import requests

from .api import ValidationError
from .google_cloud import CloudState, Google
from .notion_summary import KST, ReportDeferred, lines, summarize
from .storage import STATE, private_dir, read_json


LABEL = '🎬 유튜브 댓글'
CONFIG = Path(__file__).with_name('notion_config.json')


class NotionReportError(ValidationError):
    pass


def text(item):
    value = item.get(item.get('type'), {})
    return ''.join(part.get('plain_text', part.get('text', {}).get('content', ''))
                   for part in value.get('rich_text', []))


def paragraph(value, link=None):
    rich = {'type': 'text', 'text': {'content': value}}
    if link:
        rich['text']['link'] = {'url': link}
    return {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [rich]}}


def checked_config(config):
    identifier = str(config.get('daily_data_source_id', ''))
    if not re.fullmatch(r'[0-9a-fA-F-]{32,36}', identifier) or len(identifier.replace('-', '')) != 32:
        raise NotionReportError('invalid_notion_data_source')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', str(config.get('spreadsheet_id', ''))):
        raise NotionReportError('invalid_report_spreadsheet')
    if not re.fullmatch(r'projects/[a-zA-Z0-9-]+/secrets/[a-zA-Z0-9_-]+', str(config.get('token_secret', ''))):
        raise NotionReportError('invalid_notion_secret_reference')
    return config


class NotionClient:
    def __init__(self, token, config, session=None):
        if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
            raise NotionReportError('invalid_notion_token')
        self.config = checked_config(config)
        self._token = token
        self.session = session or requests.Session()
        self._last_request = 0

    def request(self, method, path, payload=None):
        if path.startswith(('http:', 'https:', '/')) or '..' in path:
            raise NotionReportError('invalid_notion_api_path')
        wait = 0.35 - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        try:
            response = self.session.request(method, 'https://api.notion.com/v1/' + path,
                json=payload, headers={'Authorization': 'Bearer ' + self._token,
                    'Content-Type': 'application/json', 'Notion-Version': '2026-03-11'},
                timeout=40, allow_redirects=False)
        except requests.RequestException:
            # Exception strings and HTTP bodies may contain private data.
            raise NotionReportError('notion_transport_failed') from None
        if response.status_code >= 300:
            raise NotionReportError('notion_api_failed', response.status_code)
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError):
            raise NotionReportError('notion_response_invalid') from None

    def verify_connection(self):
        identity = self.request('GET', 'users/me')
        expected = self.config.get('expected_connection_name')
        if identity.get('type') != 'bot' or (expected and identity.get('name', '').strip() != expected):
            raise NotionReportError('notion_connection_identity_mismatch')
        self.request('GET', 'data_sources/' + self.config['daily_data_source_id'])
        return {'connection_name': identity.get('name'), 'daily_data_source_readable': True}

    def children(self, parent):
        rows, cursor, seen = [], None, set()
        while True:
            query = '?page_size=100' + ('&start_cursor=' + cursor if cursor else '')
            value = self.request('GET', 'blocks/' + parent + '/children' + query)
            if not isinstance(value.get('results'), list) or value.get('truncated'):
                raise NotionReportError('notion_children_incomplete')
            rows.extend(value['results'])
            if not value.get('has_more'):
                return rows
            cursor = value.get('next_cursor')
            if not isinstance(cursor, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', cursor) or cursor in seen or len(rows) > 3000:
                raise NotionReportError('notion_children_cursor_invalid')
            seen.add(cursor)

    def daily_page(self, day):
        value = self.request('POST', 'data_sources/' + self.config['daily_data_source_id'] + '/query',
            {'filter': {'and': [{'property': '유형', 'select': {'equals': '오늘 업무'}},
                               {'property': '이벤트 시간', 'date': {'equals': day}}]}, 'page_size': 100})
        rows = value.get('results')
        if not isinstance(rows, list) or value.get('has_more') or value.get('truncated'):
            raise NotionReportError('daily_page_query_incomplete')
        if not rows:
            raise ReportDeferred('daily_page_missing')
        if len(rows) != 1:
            raise NotionReportError('daily_page_ambiguous')
        page = rows[0]
        props = page.get('properties', {})
        actual = (props.get('이벤트 시간', {}).get('date') or {}).get('start')
        category = (props.get('유형', {}).get('select') or {}).get('name')
        parent = page.get('parent', {})
        if actual != day or category != '오늘 업무' or parent.get('data_source_id', '').replace('-', '') != self.config['daily_data_source_id'].replace('-', ''):
            raise NotionReportError('daily_page_identity_mismatch')
        return page

    def report_container(self, page, day):
        wanted = '💬 인스타 댓글 관리 (' + day + ')'
        found, active = [], False
        for block in self.children(page['id']):
            kind, label = block.get('type'), text(block).strip()
            toggle_heading = bool(block.get(kind, {}).get('is_toggleable'))
            if kind in {'heading_1', 'heading_2', 'heading_3'} and not toggle_heading:
                active = label == 'Develop'
            elif kind == 'divider':
                active = False
            if active and label == wanted and (kind == 'toggle' or toggle_heading):
                found.append(block)
        if not found:
            raise ReportDeferred('instagram_report_not_ready')
        if len(found) != 1:
            raise NotionReportError('instagram_report_ambiguous')
        return found[0]

    def expected_children(self, summary):
        content, url = lines(summary, self.config['spreadsheet_id'])
        return [paragraph(line, url if index == len(content) - 1 else None)
                for index, line in enumerate(content)]

    def matches(self, block, expected):
        actual = self.children(block['id'])
        if [text(row) for row in actual] != [text(row) for row in expected]:
            return False
        links = [(part.get('text', {}).get('link') or {}).get('url')
                 for row in actual for part in row.get(row.get('type'), {}).get('rich_text', [])]
        wanted = expected[-1]['paragraph']['rich_text'][0]['text']['link']['url']
        return wanted in links

    def publish(self, summary):
        day = summary['report_day']
        page = self.daily_page(day)
        container = self.report_container(page, day)
        expected = self.expected_children(summary)

        def existing():
            return [row for row in self.children(container['id']) if row.get('type') == 'toggle' and text(row).strip() == LABEL]

        found = existing()
        if len(found) > 1:
            raise NotionReportError('youtube_report_ambiguous')
        if found:
            if not self.matches(found[0], expected):
                raise NotionReportError('youtube_report_content_differs')
            outcome = 'unchanged'
        else:
            self.request('PATCH', 'blocks/' + container['id'] + '/children', {
                'children': [{'object': 'block', 'type': 'toggle', 'toggle': {
                    'rich_text': [{'type': 'text', 'text': {'content': LABEL}}], 'children': expected}}],
                'notifications': {'mode': 'silent'}})
            found = existing()
            if len(found) != 1 or not self.matches(found[0], expected):
                raise NotionReportError('youtube_report_readback_failed')
            outcome = 'inserted'
        return {'status': outcome, 'day': day, 'page_id': page['id'], 'page_url': page.get('url'),
                'container_id': container['id'], 'youtube_block_id': found[0]['id'],
                'period_start': summary['period_start'], 'period_end': summary['period_end']}


def secret_token(google, resource):
    try:
        response = requests.get('https://secretmanager.googleapis.com/v1/' + resource + '/versions/latest:access',
            headers={'Authorization': 'Bearer ' + google.token()}, timeout=30, allow_redirects=False)
    except requests.RequestException:
        raise NotionReportError('notion_secret_transport_failed') from None
    if response.status_code != 200:
        raise NotionReportError('notion_secret_access_failed', response.status_code)
    try:
        return base64.b64decode(response.json()['payload']['data'], validate=True).decode().strip()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise NotionReportError('notion_secret_invalid') from None


@contextmanager
def report_lease(cloud, now=time.time):
    key = 'notion/lease.json'
    raw, generation = cloud.get(key)
    if raw and json.loads(raw).get('expires_at', 0) > now():
        raise ReportDeferred('report_already_running')
    generation = cloud.put(key, json.dumps({'owner': uuid.uuid4().hex, 'expires_at': now() + 600}).encode(), generation)
    try:
        yield
    finally:
        try:
            cloud.google.request('DELETE', cloud.url(key, ifGenerationMatch=generation))
        except ValidationError:
            pass


def run(config, cloud=None, now=None, factory=None):
    config = checked_config(config)
    now = (now or datetime.now(timezone.utc)).astimezone(KST)
    if now.weekday() >= 5 or now.hour < 9:
        return {'status': 'outside_report_hours', 'day': now.date().isoformat()}
    cloud = cloud or CloudState()
    with report_lease(cloud):
        raw, generation = cloud.get('notion/report-state.json')
        state = json.loads(raw) if raw else {}
        if state.get('last_report_day') == now.date().isoformat():
            return {'status': 'already_reported', 'day': now.date().isoformat()}
        folder = private_dir(STATE / 'notion-report')
        path = folder / 'comments.sqlite3'
        if not cloud.restore(path):
            raise ReportDeferred('collection_database_missing')
        summary = summarize(path, now, state.get('covered_through'))
        client = factory(config) if factory else NotionClient(secret_token(cloud.google, config['token_secret']), config)
        client.verify_connection()
        result = client.publish(summary)
        # Advance coverage only after the exact Notion content has been read back.
        cloud.put('notion/report-state.json', json.dumps({
            'last_report_day': summary['report_day'], 'covered_through': summary['period_end'],
            'last_report': result}, ensure_ascii=False).encode(), generation)
        cloud.put('notion/latest.json', json.dumps(result, ensure_ascii=False).encode())
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='기존 인스타 댓글 보고 안에 유튜브 요약 추가')
    parser.add_argument('mode', choices=['run', 'probe'], nargs='?', default='run')
    parser.add_argument('--local', action='store_true', help='등록된 gcloud 운영 계정 사용')
    parser.add_argument('--token-file', type=Path, help='대체 커넥션 토큰의 로컬 비공개 JSON 경로')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = checked_config(read_json(CONFIG))
        if args.mode == 'probe':
            if args.token_file:
                token = read_json(args.token_file)['token']
            elif args.local:
                value = subprocess.run(['gcloud', 'secrets', 'versions', 'access', 'latest',
                    '--secret=' + config['token_secret'].rsplit('/', 1)[-1], '--project=waterbomb-sns-pipeline',
                    '--account=syan@madeone.kr', '--quiet'], capture_output=True, text=True, timeout=60)
                if value.returncode:
                    raise NotionReportError('cloud_operator_login_required')
                token = value.stdout.strip()
            else:
                token = secret_token(Google(), config['token_secret'])
            client = NotionClient(token, config)
            result = client.verify_connection()
            today = datetime.now(KST).date().isoformat()
            container = client.report_container(client.daily_page(today), today)
            result.update(status='read_verified', shared_report_found=True, container_id=container['id'])
        else:
            result = run(config)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ReportDeferred as error:
        print(json.dumps({'status': 'deferred', 'reason': str(error)}, ensure_ascii=False))
        return 0
    except ValidationError as error:
        print(json.dumps({'status': 'error', **error.public()}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({'status': 'error', 'reason': 'notion_report_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
