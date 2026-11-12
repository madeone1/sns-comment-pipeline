# Historical release tooling. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Reviewable GCP setup. Default is a read-only plan; apply/activate are explicit."""

import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from sns_comment.credentials import get_token,verify_meta_token
from sns_comment.notion_oauth import read_local as read_notion_oauth
from sns_comment.api import PilotError
from sns_comment.gcloud_account import permission_help,resolve_operator,run_gcloud

PROJECT = "waterbomb-sns-pipeline"
BILLING = "01152F-3EE377-FB410D"
ORGANIZATION = "265289771512"
REGION = "asia-northeast3"
JOB = "sns-comments-daily"
MERGE_JOB = "notion-daily-merge"
RUNTIME = f"sns-pipeline@{PROJECT}.iam.gserviceaccount.com"
BUILD = f"sns-build@{PROJECT}.iam.gserviceaccount.com"
SCHEDULER = f"sns-scheduler@{PROJECT}.iam.gserviceaccount.com"


_resolved = None


def operator():
    global _resolved
    if _resolved is None:
        email, explicit = resolve_operator(project=PROJECT)
        print("Google Cloud 계정: "+email, file=sys.stderr, flush=True)
        _resolved = (email, explicit)
    return _resolved


def run(args, value=None, optional=False):
    email, explicit = operator()
    result = run_gcloud(args, project=PROJECT, account=email if explicit else None, input=value, cwd=ROOT)
    if result.returncode and not optional:
        # Secret payloads only enter stdin on the dedicated versions-add call.
        if value is None and result.stderr:
            print(result.stderr, file=sys.stderr)
        raise PilotError(permission_help(email, PROJECT)+"\n클라우드 단계 실패: "+" ".join(args[:3]))
    return result


def create_if_absent(describe,create):
    result = run([*describe,"--format=json"],optional=True)
    if result.returncode:
        run(create)


def apply():
    # Fail before resource creation if either initialization credential is absent.
    meta, notion = get_token("meta"), json.dumps(read_notion_oauth())
    verify_meta_token(meta)
    print("클라우드 초기 인증 확인 완료",flush=True)
    existing = run(["projects","describe",PROJECT,"--format=json"],optional=True)
    if existing.returncode:
        run(["projects","create",PROJECT,"--organization="+ORGANIZATION,
             "--name=WATERBOMB SNS Pipeline","--labels=app=sns-comment"])
    elif json.loads(existing.stdout).get("labels",{}).get("app") != "sns-comment":
        raise PilotError("기존 프로젝트를 SNS 프로젝트로 임의 전용하지 않습니다.")
    run(["billing","projects","link",PROJECT,"--billing-account="+BILLING])
    print("전용 프로젝트·결제 연결 완료",flush=True)
    run(["services","enable","run.googleapis.com","cloudbuild.googleapis.com","artifactregistry.googleapis.com",
         "cloudscheduler.googleapis.com","secretmanager.googleapis.com","storage.googleapis.com","aiplatform.googleapis.com"])
    print("필요한 클라우드 API 활성화 완료",flush=True)
    for name in ("sns-pipeline","sns-build","sns-scheduler"):
        email = f"{name}@{PROJECT}.iam.gserviceaccount.com"
        create_if_absent(["iam","service-accounts","describe",email],
                         ["iam","service-accounts","create",name,"--display-name="+name])
    for suffix in ("state","build"):
        bucket = f"gs://{PROJECT}-{suffix}"
        create_if_absent(["storage","buckets","describe",bucket],
            ["storage","buckets","create",bucket,"--location="+REGION,"--uniform-bucket-level-access","--public-access-prevention"])
        run(["storage","buckets","update",bucket,"--versioning"])
        member = RUNTIME if suffix == "state" else BUILD
        run(["storage","buckets","add-iam-policy-binding",bucket,"--member=serviceAccount:"+member,"--role=roles/storage.objectAdmin"])
        if suffix=="build":
            run(["storage","buckets","add-iam-policy-binding",bucket,"--member=serviceAccount:"+member,"--role=roles/storage.legacyBucketReader"])
    create_if_absent(["artifacts","repositories","describe","containers","--location="+REGION],
        ["artifacts","repositories","create","containers","--location="+REGION,"--repository-format=docker"])
    run(["artifacts","repositories","add-iam-policy-binding","containers","--location="+REGION,
         "--member=serviceAccount:"+BUILD,"--role=roles/artifactregistry.writer"])
    for email,role in ((BUILD,"roles/logging.logWriter"),(RUNTIME,"roles/logging.logWriter"),(RUNTIME,"roles/aiplatform.user")):
        run(["projects","add-iam-policy-binding",PROJECT,"--member=serviceAccount:"+email,"--role="+role])
    for name,token in (("sns-meta-token",meta),("sns-notion-oauth",notion)):
        create_if_absent(["secrets","describe",name],["secrets","create",name,"--replication-policy=automatic"])
        existing_versions=run(["secrets","versions","list",name,"--limit=1","--filter=state=ENABLED","--format=value(name)"],optional=True)
        # Cloud refresh rotates independently of the initialization Mac. Never
        # overwrite a newer cloud refresh token with the initial Keychain copy.
        if name!="sns-notion-oauth" or not existing_versions.stdout.strip():
            run(["secrets","versions","add",name,"--data-file=-"],value=token)
        run(["secrets","add-iam-policy-binding",name,"--member=serviceAccount:"+RUNTIME,"--role=roles/secretmanager.secretAccessor"])
    run(["secrets","add-iam-policy-binding","sns-notion-oauth","--member=serviceAccount:"+RUNTIME,"--role=roles/secretmanager.secretVersionAdder"])
    print("서비스 계정·비공개 저장소·비밀정보 연결 완료",flush=True)
    # Seed the existing raw history without changing the local DB or overwriting
    # a newer remote checkpoint. WAL is consolidated with SQLite's backup API.
    destination = f"gs://{PROJECT}-state/state/comments.sqlite3"
    if run(["storage","objects","describe",destination],optional=True).returncode:
        path = ROOT/".local/comments.sqlite3"
        if path.exists():
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp)/"comments.sqlite3"
                with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as source, sqlite3.connect(target) as backup:
                    source.backup(backup)
                run(["storage","cp",str(target),destination,"--if-generation-match=0"])
    deploy_job()


def deploy_job():
    tag = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    image = f"{REGION}-docker.pkg.dev/{PROJECT}/containers/sns-comments:{tag}"
    print("클라우드 컨테이너 이미지 빌드 시작",flush=True)
    run(["builds","submit",str(ROOT),"--tag="+image,"--service-account=projects/"+PROJECT+"/serviceAccounts/"+BUILD,
         "--gcs-log-dir=gs://"+PROJECT+"-build/logs","--gcs-source-staging-dir=gs://"+PROJECT+"-build/source"])
    print("클라우드 컨테이너 이미지 빌드 완료",flush=True)
    action = "create" if run(["run","jobs","describe",JOB,"--region="+REGION],optional=True).returncode else "update"
    run(["run","jobs",action,JOB,"--region="+REGION,"--image="+image,"--service-account="+RUNTIME,
         "--args=--all-accounts",
         "--tasks=1","--parallelism=1","--max-retries=0","--task-timeout=3600s","--cpu=1","--memory=512Mi",
         "--set-env-vars=GOOGLE_CLOUD_PROJECT="+PROJECT+",SNS_STATE_BUCKET="+PROJECT+"-state,SNS_NOTION_OAUTH_SECRET=projects/"+PROJECT+"/secrets/sns-notion-oauth",
         "--set-secrets=SNS_IG_ACCESS_TOKEN=sns-meta-token:latest"])
    if action == "create":
        run(["run","jobs","add-iam-policy-binding",JOB,"--region="+REGION,"--member=serviceAccount:"+SCHEDULER,"--role=roles/run.invoker"])
    print(json.dumps({"status":"cloud_job_ready","project":PROJECT,"job":JOB,"scheduler_created":False}))
    action = "create" if run(["run","jobs","describe",MERGE_JOB,"--region="+REGION],optional=True).returncode else "update"
    run(["run","jobs",action,MERGE_JOB,"--region="+REGION,"--image="+image,"--service-account="+RUNTIME,
         "--args=--merge-only","--tasks=1","--parallelism=1","--max-retries=0","--task-timeout=3600s","--cpu=1","--memory=512Mi",
         "--set-env-vars=GOOGLE_CLOUD_PROJECT="+PROJECT+",SNS_STATE_BUCKET="+PROJECT+"-state"])
    if action == "create":
        run(["run","jobs","add-iam-policy-binding",MERGE_JOB,"--region="+REGION,"--member=serviceAccount:"+SCHEDULER,"--role=roles/run.invoker"])
    print(json.dumps({"status":"merge_job_ready","project":PROJECT,"job":MERGE_JOB,"image":image,"scheduler_created":False}))


def activate():
    proof = ROOT/".local/cloud-smoke-verification.json"
    if not proof.exists():
        raise PilotError("실제 클라우드 실행과 Notion 기록 검증 전에는 예약을 활성화하지 않습니다.")
    verified = json.loads(proof.read_text(encoding="utf-8"))
    if verified.get("project") != PROJECT or verified.get("cloud_execution_succeeded") is not True or verified.get("notion_verified") is not True:
        raise PilotError("클라우드 실행·Notion 기록 검증 결과가 맞지 않습니다.")
    uri = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run"
    action = "create" if run(["scheduler","jobs","describe",JOB,"--location="+REGION],optional=True).returncode else "update"
    header_flag="--headers=" if action=="create" else "--update-headers="
    run(["scheduler","jobs",action,"http",JOB,"--location="+REGION,"--schedule=0 8 * * *","--time-zone=Asia/Seoul",
         "--uri="+uri,"--http-method=POST","--oauth-service-account-email="+SCHEDULER,
         header_flag+"Content-Type=application/json","--message-body={}","--attempt-deadline=180s","--max-retry-attempts=0"])
    print(json.dumps({"status":"scheduled","timezone":"Asia/Seoul","hour":8,"project":PROJECT}))


def update_meta():
    token=get_token("meta")
    verify_meta_token(token)
    run(["secrets","versions","add","sns-meta-token","--data-file=-"],value=token)
    print(json.dumps({"status":"meta_token_validated_and_updated","token_displayed":False}))


def activate_merge():
    native = ROOT/".local/recovery-2026-11-09/native-merge-smoke-verification.json"
    cloud = ROOT/".local/recovery-2026-11-09/cloud-daily-merge-verification.json"
    native_proof = json.loads(native.read_text(encoding="utf-8")) if native.exists() else {}
    if native_proof.get("status") != "verified" or not native_proof.get("cleanup") or not all(native_proof["cleanup"].values()):
        raise PilotError("실제 템플릿 복사본의 보존·복구 검증이 필요합니다.")
    proof = json.loads(cloud.read_text(encoding="utf-8")) if cloud.exists() else {}
    if proof.get("project") != PROJECT or proof.get("merge_only_execution_succeeded") is not True or proof.get("daily_merge_apply_enabled") is not True:
        raise PilotError("클라우드 병합 전용 실행 검증 후 예약을 활성화합니다.")
    uri = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{MERGE_JOB}:run"
    action = "create" if run(["scheduler","jobs","describe",MERGE_JOB,"--location="+REGION],optional=True).returncode else "update"
    header_flag = "--headers=" if action == "create" else "--update-headers="
    run(["scheduler","jobs",action,"http",MERGE_JOB,"--location="+REGION,"--schedule=0 4 * * MON","--time-zone=Asia/Seoul",
         "--uri="+uri,"--http-method=POST","--oauth-service-account-email="+SCHEDULER,
         header_flag+"Content-Type=application/json","--message-body={}","--attempt-deadline=180s","--max-retry-attempts=0"])
    print(json.dumps({"status":"merge_scheduled","timezone":"Asia/Seoul","schedule":"월요일 04:00","job":MERGE_JOB}))


def main():
    parser = argparse.ArgumentParser(description="SNS 전용 클라우드 배치 계획·적용")
    parser.add_argument("action",choices=["plan","apply","deploy","activate","activate-merge","update-meta"],default="plan",nargs="?")
    action = parser.parse_args().action
    if action == "plan":
        print(json.dumps({"project":PROJECT,"account":"현재 활성 gcloud 계정 (SNS_GCLOUD_ACCOUNT로 지정 가능)","billing_account":BILLING,"organization":ORGANIZATION,
            "region":REGION,"job":JOB,"buckets":[PROJECT+"-state",PROJECT+"-build"],
            "service_accounts":[RUNTIME,BUILD,SCHEDULER],"secrets":["sns-meta-token","sns-notion-oauth"],
            "notion_auth":"파이프라인 전용 Notion MCP OAuth, 자동 갱신 결과는 Secret Manager에 보존",
            "ai_model":"gemini-3.8-flash","schedule":"매일 08:00 Asia/Seoul",
            "scheduled_accounts":["waterbomb_official","waterbomb_busan_official","waterbomb_sokcho_official"],
            "missing_daily_page":"다음 일일 업무 페이지에 원래 날짜별로 보고",
            "daily_merge":{"job":MERGE_JOB,"schedule":"월요일 04:00 Asia/Seoul","also_before_daily_sns":True,"weekly_collision_validation":"실제 주간 생성 이후 별도 확인"},
            "notion_destination":"일일 업무 날짜별 페이지 > SNS Comments",
            "scheduler_activation":"실제 클라우드 수집·Notion 기록 검증 후 별도 활성화",
            "google_sheets_sync":"인증·공유 별도 연결 전 비활성","drive_backup":"미연결, 원본은 전용 Cloud Storage 보존"},ensure_ascii=False,indent=2))
    else:
        try:
            {"apply":apply,"deploy":deploy_job,"activate":activate,"activate-merge":activate_merge,"update-meta":update_meta}[action]()
        except PilotError as error:
            print(str(error),file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
