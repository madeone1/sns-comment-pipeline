"""Shared paths and CLI transport for the existing company cloud project."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sns_comment.api import PilotError
from sns_comment.gcloud_account import permission_help, resolve_operator, run_gcloud

PROJECT = 'waterbomb-sns-pipeline'
REGION = 'asia-northeast3'
_resolved = None


def operator():
    """Active gcloud account, or an explicit SNS_GCLOUD_ACCOUNT override."""
    global _resolved
    if _resolved is None:
        try:
            email, explicit = resolve_operator(project=PROJECT)
        except PilotError as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(1) from None
        print('Google Cloud 계정: ' + email, file=sys.stderr, flush=True)
        _resolved = (email, email if explicit else None)
    return _resolved


def gcloud(args, optional=False, value=None):
    email, account = operator()
    result = run_gcloud(args, project=PROJECT, account=account, cwd=ROOT, input=value)
    if result.returncode and not optional:
        print(permission_help(email, PROJECT), file=sys.stderr)
        # Stdin may be a secret. Do not echo process output in that case.
        if value is None and result.stderr:
            print(result.stderr, file=sys.stderr)
        raise SystemExit('클라우드 단계 실패: '+' '.join(args[:3]))
    return result
