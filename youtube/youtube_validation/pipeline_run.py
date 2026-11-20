"""Local resumable collection entry point, separate from the validation commands."""

import argparse
import fcntl
import json
import uuid

from .api import YouTube, ValidationError
from .auth import load_credentials
from .pipeline import run
from .storage import STATE, private_dir, write_json


def main():
    parser = argparse.ArgumentParser(description='워터밤 전체 댓글 수집·재개')
    parser.add_argument('--max-requests', type=int, default=2000)
    parser.add_argument('--seconds', type=int, default=2400)
    parser.add_argument('--force-new-cycle', action='store_true')
    args = parser.parse_args()
    if not 5 <= args.max_requests <= 8000 or not 1 <= args.seconds <= 3600:
        parser.error('요청 수는 5~8000, 실행 시간은 1~3600초입니다.')
    private_dir(STATE)
    capture = uuid.uuid4().hex
    with (STATE / 'pipeline.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('다른 YouTube 전체 수집이 실행 중입니다.')
            return 1
        credentials = load_credentials()
        def record(number, resource, value):
            write_json(STATE / 'pipeline-raw' / capture / f'{number:05d}-{resource}.json', value)
        def progress(value):
            print(json.dumps(value, ensure_ascii=False), flush=True)
        api = YouTube(credentials, max_requests=args.max_requests, record=record)
        result = run(api, STATE, max_requests=args.max_requests, seconds=args.seconds,
                     force_new_cycle=args.force_new_cycle, progress=progress)
        write_json(STATE / 'authorized-user.json', json.loads(credentials.to_json()))
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 1 if result['state'] == 'error' else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValidationError, OSError, ValueError):
        raise SystemExit('YouTube 수집에 실패했습니다. 인증·연결 상태와 수집 이력을 확인해주세요.') from None
