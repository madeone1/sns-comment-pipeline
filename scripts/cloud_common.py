"""Shared paths and CLI transport for the existing company cloud project."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'waterbomb-sns-pipeline'
REGION = 'asia-northeast3'
ACCOUNT = 'syan@madeone.kr'


def gcloud(args, optional=False):
    result = subprocess.run(
        ['gcloud', *args, '--project='+PROJECT, '--account='+ACCOUNT, '--quiet'],
        capture_output=True, text=True, cwd=ROOT)
    if result.returncode and not optional:
        print(result.stderr, file=sys.stderr)
        raise SystemExit('클라우드 단계 실패: '+' '.join(args[:3]))
    return result
