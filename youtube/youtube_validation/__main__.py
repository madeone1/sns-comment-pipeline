"""Explicit commands; nothing is collected or deployed during import/setup."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

from .api import HANDLE, ValidationError, YouTube
from .auth import PORT, import_client, load_credentials
from .collector import collect
from .storage import STATE, private_dir, read_json, write_json


def display_summary(summary):
    result = {key: value for key, value in summary.items() if key != 'reply_queries'}
    replies = summary.get('reply_queries', {})
    result['reply_coverage'] = {'parents': len(replies),
        'complete': sum(item['complete'] for item in replies.values()),
        'errors': dict(Counter(item['error']['reason'] for item in replies.values() if item['error']))}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='독립 YouTube 댓글 API 검증')
    sub = parser.add_subparsers(dest='command', required=True)
    client = sub.add_parser('import-client', help='OAuth JSON을 비공개 저장소에 등록')
    client.add_argument('file', type=Path)
    serve = sub.add_parser('auth', help='이 컴퓨터에서 채널 인증 페이지 실행')
    serve.add_argument('--port', type=int, default=PORT)
    gather = sub.add_parser('collect', help='상태별 댓글과 전체 답글 수집')
    gather.add_argument('--max-requests', type=int, default=100)
    gather.add_argument('--pages-per-status', type=int,
                        help='각 상태의 조회 페이지 수를 제한하는 명시적 샘플 검증')
    sub.add_parser('status', help='토큰을 표시하지 않고 연결·수집 상태 조회')
    baseline = sub.add_parser('baseline', help='기존 Cloud Run·Scheduler 설정의 읽기 전용 지문')
    baseline.add_argument('--stage', choices=('before', 'after'), required=True)
    args = parser.parse_args(argv)
    private_dir(STATE)
    try:
        if args.command == 'import-client':
            result = import_client(args.file)
        elif args.command == 'auth':
            from .server import make_server
            server = make_server(STATE, args.port)
            print('YouTube 인증 페이지: ' + server.launch_url, flush=True)
            print('종료: Ctrl+C. 기존 수집기와 예약에는 영향을 주지 않습니다.', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        elif args.command == 'collect':
            if not 2 <= args.max_requests <= 1000:
                parser.error('--max-requests는 2 이상 1000 이하입니다.')
            if args.pages_per_status is not None and not 1 <= args.pages_per_status <= 1000:
                parser.error('--pages-per-status는 1 이상 1000 이하입니다.')
            if not (STATE / 'authorized-user.json').is_file():
                raise ValidationError('채널 인증이 아직 없습니다. OAuth 클라이언트 등록과 채널 소유 계정 인증을 먼저 완료해주세요.')
            run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8]
            raw_dir = STATE / 'runs' / run_id / 'raw'
            def record(number, resource, payload):
                write_json(raw_dir / f'{number:04d}-{resource}.json', payload)
            credentials = load_credentials()
            api = YouTube(credentials, args.max_requests, record=record)
            result = collect(api, STATE, HANDLE, run_id=run_id,
                             pages_per_status=args.pages_per_status)
            # Google may rotate credentials when refreshing. Keep the same isolated file.
            write_json(STATE / 'authorized-user.json', json.loads(credentials.to_json()))
        elif args.command == 'baseline':
            from .cloud_baseline import snapshot
            result = snapshot()
            if args.stage == 'after':
                result['unchanged'] = result == read_json(STATE / 'baseline-before.json')
            write_json(STATE / ('baseline-' + args.stage + '.json'), result)
        else:
            result = {'client_configured': (STATE / 'client.json').is_file(), 'connected': False}
            try:
                result['connection'] = read_json(STATE / 'connection.json')
                result['connected'] = (STATE / 'authorized-user.json').is_file()
            except FileNotFoundError:
                pass
            try:
                result['latest_run'] = display_summary(read_json(STATE / 'latest.json'))
            except FileNotFoundError:
                pass
        print(json.dumps(display_summary(result) if args.command == 'collect' else result,
                         ensure_ascii=False, indent=2))
        if args.command == 'collect' and not result['requested_scope_complete']:
            return 2
        if args.command == 'baseline' and result.get('unchanged') is False:
            return 2
        return 0
    except (ValidationError, OSError, ValueError, RuntimeError) as error:
        reason = error.reason if isinstance(error, ValidationError) else '검증 파일·환경을 확인해주세요.'
        print('YouTube 검증 중단: ' + reason, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
