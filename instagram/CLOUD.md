# SNS Comments 클라우드 운영

2026-11-17부터 Instagram daily 수집·분석 보고는 종료한다. 같은 이름의
`sns-comments-daily` Scheduler와 Cloud Run Job을 제거한다. 저장된 원문·DB·
기존 시트·Notion 보고와 Secret 이력은 삭제하지 않는다.

| 항목 | 운영 구성 |
| --- | --- |
| 프로젝트 / 리전 | `waterbomb-sns-pipeline` / `asia-northeast3` |
| 댓글 관리 | `sns-comments-moderate`, 매시 00·30분, Asia/Seoul |
| 댓글 관리 제한 | 1,500초, 잠금 1,620초, 자동 재시도 0회 |
| 과거 댓글 점검 | `sns-comments-past-sweep`, 예약 없음, 사람이 실행 버튼을 누를 때만 |
| 관리 보고 | `sns-comments-notion-summary`, 매일 09시 예약, 코드에서 주말 제외 |
| 월별 비용 | `sns-comments-monthly-cost`, 매월 1일 10시, Asia/Seoul |
| 캡션 규정집 | `sns-comments-caption-policy`, 매월 1일 10시, Asia/Seoul |
| 보고 위치 | Notion 일일 업무 Develop의 `인스타 댓글 관리` 토글 |
| 원문·진행 위치 | `gs://waterbomb-sns-pipeline-state/moderation/polling/계정ID.json` |
| 기준표 | `1496AQpUgceCZCNaahpJ89_SSMbiO6nmOtwFi6u4QzMY`의 숨김 대상·사람 판단 |
| 운영 서비스 계정 | `sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com` |

Meta 웹훅 수신기, 별도 YouTube 작업, Notion 일일 업무 병합은 이번 종료 대상이
아니다. 토큰 변경은 macOS의 `change-access-token.command` 또는 Windows의
`change-access-token.bat`으로 한다. 활성 회사 gcloud 계정이나 `--account`,
`SNS_GCLOUD_ACCOUNT`를 사용한다. `sns-meta-token`, 상태 저장소, 댓글 관리
Job 실행 권한이 필요하며 개인 서비스 계정 키 파일을 만들지 않는다.

## 조회 범위와 재개

30분 실행에 최소 60분을 조회하며, 이전 완료 시각에서 10분을 겹친 구간도 포함한다.
실행이 지연돼도 이미 시작한 순회의 시작·종료 시각은 움직이지 않는다. 최신 게시물은
매번 우선 조회하고, 별도 전체 게시물 순회는 댓글·답글의 다음 페이지까지 이어간다.
긴 과거 순회는 여러 실행에 걸칠 수 있다. 최대 4개 독립 페이지를 병렬로 읽고,
그 묶음의 원문 대기열과 다음 커서를 하나의 조건부 쓰기로 함께 저장한 뒤 처리한다.
저장 전에 중단되면 같은 페이지를 다시 읽는다. 분류 실패·40건 처리 상한·API 제한으로 남은
댓글을 다음 실행이 이어받는다. 처리한 ID와 본문·규정 지문으로 중복 실행을 막는다.

원문 대기열과 진행 위치는 댓글 관리 잠금 아래에서 generation 조건으로 기록한다.
저장 전에 실패하면 해당 페이지를 다시 조회한다. 분류 결과 저장 후 대기열에서
제거하기 전에 실패하면 기존 판단 ID로 재대조한다. 이미 숨긴 댓글이나 사람의
유지 결정은 그대로 보존한다. 숨김 해제와 영구 삭제는 구현하지 않는다.

계정당 최신 순회는 최대 32페이지, 전체 게시물 순회는 최대 160페이지를 읽는다.
조회 시간은 계정당 120초, 웹훅·최신 조회·재확인을 합친 요청은 계정당 240회로 제한한다.
공유 Graph 요청 상한 2,000회 중 남는 예산은 기존 분류·숨김·확인에 사용한다.
원격 호출 제한이나 인증 오류는 일반 순회 오류로 남기며 조회 불가로 제외하지 않는다.

## 조회 불가와 완료 판정

개별 객체의 `100/33`은 삭제로 단정하지 않고, 같은 체크포인트의 `unavailable` 목록에
원본 알림·작업·최초 조회 범위·오류·다음 재확인 시각을 보관한다. 알림 원본은 지우지 않는다.
커서가 있는 요청은 먼저 같은 경로의 첫 페이지부터 재시도한다. 계정의 게시물 목록 조회 실패는
제외하지 않는다. 재확인은 최초 1시간 뒤, 두 번째 실패 후 6시간 뒤, 이후 매일 진행하며
실행당 최대 4건을 재확인한다. 접근이 복구되면 원래 시간 범위로 댓글·답글·다음 페이지를
다시 처리하고 해결 이력을 남긴다. 재확인 시각은 처리 가능 시각이며 실행 예산에 따라 늦어질 수 있다.

`pending`은 실제 댓글 대기열·미처리 알림·미완료 순회·일반 오류가 남은 상태다.
두 순회와 처리 대기열이 끝나고 조회 불가만 남으면 `complete_with_exceptions`, 예외도 없으면
`complete`다. 로그에는 순회별 이번 조회 페이지·남은 페이지·완료 시각과 조회 불가 수를 구분한다.
남은 페이지는 앞으로 발견될 작업을 포함하지 않으므로 완료 예상으로 단정하지 않는다.

읽기 전용 확인:

```sh
python3 -m sns_comment.moderation_progress
```

실제 숨김과 사람 판단 반영은 기존 `SNS_MODERATION_HIDE`,
`SNS_HUMAN_DECISION_HIDE`, `SNS_NOTION_DECISIONS` 스위치를 유지한다.
배포 시 환경 변수를 통째로 갈아끼우지 않는다. 필요한 키만 `--update-env-vars`로 더한다.

## 배포

```sh
python3 -m unittest discover -s tests -q
python3 scripts/token_connection_release.py prepare
python3 scripts/token_connection_release.py build
```

빌드 성공 후 현재 활성 계정 API, 실제 기준표 읽기와 Notion 읽기를 검증한다.
댓글 조회 체크포인트는 메모리에서만 시험하며 AI 호출, 숨김, 시트 쓰기, Notion
본문 쓰기, 계정 등록부 쓰기는 하지 않는다. 검증 증빙과 OAuth 갱신만 저장한다.
종료한 daily의 날짜·SNS Comments 항목·과거 보고가 없어도 검증할 수 있다.

```sh
python3 scripts/token_connection_release.py verify
python3 scripts/token_connection_release.py deploy
```

같은 이미지의 성공한 검증 증빙이 있어야 댓글 관리와 관리 보고 Job 이미지를 바꾼다.
과거 댓글 점검 작업이 이미 있으면 그 이미지만 같이 바꾼다. 월별 비용 작업이 이미 있으면
그 이미지도 같이 바꾼다. 댓글 관리의
제한 시간만 1,500초로 맞추며 기존 기능 스위치·예약 시각은 보존한다. 배포 후 실제
실행 로그, 대기열·진행 위치와 시트·Notion 상태를 재조회한다.

기존 daily 리소스 정리는 한 번만 수행한다. 이 명령은 Instagram daily만 제거하고
Cloud Storage 원본, 기준표, Notion 내용, YouTube와 병합 작업은 보존한다.

```sh
python3 scripts/token_connection_release.py retire-daily
```

## 과거 댓글 점검

아래 명령은 `instagram` 폴더에서 실행한다.
예전 댓글에 노션의 개수·단어 규칙(예를 들면 D11, D12, D13, D14)을 다시 적용하는 작업이다.
인스타 게시물 목록을 처음부터 다시 받지 않는다. 상태 저장소에 이미 있는 댓글만 검사하고,
숨길 때만 인스타에 숨김 요청을 보낸다. 30분마다 도는 댓글 관리와 같은 이미지를 쓰고,
명령만 다르다. 예약은 없다.
노트북에 gcloud를 설치하지 않아도, 브라우저의 실행 버튼으로 돌릴 수 있다.

기본은 미리보기다. 댓글을 숨기지 않는다. 숨김은 이번 실행에 `apply`를 적을 때만 한다.
숨기기는 숨김만 한다. 댓글을 지우거나 숨김을 풀지 않는다. 공식 계정이 쓴 댓글과,
사람이 유지로 표시한 댓글은 건너뛴다. 숨긴 댓글은 기준표의 `숨김 대상` 탭에 남긴다.
시트 쓰기가 잠시 실패해도 기록은 대기열에 남고, 다음 실행이 다시 쓴다.
서울·부산·속초·토론토 댓글 탭은 수정하지 않고, 탭을 지우지도 않는다.

한 번에 한 도시만 본다. 인스타 제한이나 실행 시간이 끝나면 그 위치에서 멈추고,
같은 방법으로 다시 실행하면 이어서 한다. 30분 댓글 관리와 같은 잠금을 쓴다.
그 작업이 돌아가는 중이면 바로 물러나고, 정시·30분 2분 전부터는 시작하지 않는다.
그래서 긴 점검이 16시 실행을 건너뛰게 만들지 않는다. 로그에 잠시 멈춤이나
호출 제한이 보이면 같은 실행을 한 번 더 하면 된다.

환경 변수는 필요한 키만 `--update-env-vars`로 더한다. 환경 변수를 통째로 갈아끼우지 않는다.
규칙이 여러 개면 쉼표 대신 `+`로 잇는다.
gcloud가 쉼표로 값을 나누기 때문이다. 예: `D11+D12+D14`. 규칙을 비우면 노션에서
사용 중인 개수·단어 규칙을 모두 본다.

### 관리자가 한 번만 만드는 작업

댓글 관리 이미지를 먼저 배포한 뒤, 그 이미지 주소로 작업을 만든다. 스케줄은 만들지 않는다.
저장된 인수에는 `--apply`를 넣지 않는다.

```sh
IMAGE=$(gcloud run jobs describe sns-comments-moderate --region=asia-northeast3 --format='value(spec.template.spec.template.spec.containers[0].image)')
gcloud run jobs create sns-comments-past-sweep \
  --region=asia-northeast3 \
  --image="$IMAGE" \
  --service-account=sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --command=python \
  --args=-m,sns_comment.past_sweep \
  --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=600s --memory=512Mi --cpu=1 \
  --update-env-vars=GOOGLE_CLOUD_PROJECT=waterbomb-sns-pipeline,SNS_STATE_BUCKET=waterbomb-sns-pipeline-state,SNS_NOTION_OAUTH_SECRET=projects/waterbomb-sns-pipeline/secrets/sns-notion-oauth
```

이후 배포 스크립트는 이 작업이 있으면 이미지만 바꾼다. 환경 변수와 인수는 그대로 둔다.
작업이 아직 없으면 배포는 건너뛰고, 댓글 관리 작업 배포는 계속한다.

### 미리보기

숨기지 않는다. 로그에 `미리보기입니다`가 보이면 된다.

```sh
gcloud run jobs execute sns-comments-past-sweep \
  --region=asia-northeast3 \
  --update-env-vars=SNS_PAST_SWEEP_ACCOUNT=토론토,SNS_PAST_SWEEP_RULES=D11+D12+D13+D14 \
  --wait
```

콘솔: Cloud Run → 작업 → `sns-comments-past-sweep` → **실행**.
실행 창에서 이번 실행에만 쓰는 환경 변수에 `SNS_PAST_SWEEP_ACCOUNT`=`토론토`,
`SNS_PAST_SWEEP_RULES`=`D11+D12+D13+D14`를 적고 실행한다.
`SNS_PAST_SWEEP_APPLY`는 적지 않는다. 작업 수정 화면에 변수를 저장하지 않는다.

### 실제로 숨기기

미리보기 로그를 확인한 뒤에만 한다. `SNS_PAST_SWEEP_APPLY` 값이 `apply`일 때만 숨긴다.
`1`이나 `true`는 숨기지 않는다.

```sh
gcloud run jobs execute sns-comments-past-sweep \
  --region=asia-northeast3 \
  --update-env-vars=SNS_PAST_SWEEP_ACCOUNT=토론토,SNS_PAST_SWEEP_RULES=D11+D12+D13+D14,SNS_PAST_SWEEP_APPLY=apply \
  --wait
```

콘솔에서도 같은 변수에 `SNS_PAST_SWEEP_APPLY`=`apply`를 추가한 뒤 **실행**을 누른다.
이 값은 이번 실행에만 둔다. 작업에 저장해 두면 다음 버튼이 계속 숨긴다.
계정 값은 `서울`, `부산`, `속초`, `토론토` 중 하나다.

## 월별 비용 기록

아래 명령은 `instagram` 폴더에서 실행한다.
매월 1일 오전 10시(한국시간)에 지난달 비용을 노션 `💰 월별 비용 기록`에 적는다.
직원이 노트북에서 gcloud에 로그인할 필요가 없다. 댓글 관리와 같은 서비스 계정,
같은 컨테이너 이미지를 쓴다. 명령만 `python -m sns_comment.monthly_cost`이다.

기본은 한국시간 기준 지난달이다. 특정 달을 다시 적을 때는 그 실행에만 `YYYY-MM`을
붙인다. 작업에 저장해 두면 다음 달에도 그 달만 다시 적는다.

노션의 `합계`는 수식이라 코드가 쓰지 않는다. 같은 달이 이미 `자동`이면 그 행만
고친다. `수동` 행은 읽기만 하고 바꾸지 않으며, 그 달의 새 행도 만들지 않는다.
결제 표가 아직 없거나 그 달 사용량이 없으면 성공으로 끝나고 노션에 아무것도
쓰지 않는다. 숫자를 지어 내지 않는다.

Vertex AI(Gemini)는 `AI 비용`이고, 나머지 서비스는 `구글 클라우드`이다.
라벨, `goog-resource-type`, Cloud Run 작업 이름, SKU로 인스타그램 작업
(`sns-comments-moderate`, `sns-comments-notion-summary`, `sns-comments-past-sweep`,
`sns-comments-webhook`, `sns-comments-monthly-cost`, `sns-comments-caption-policy`)과 유튜브·병합 작업을 가를 수 있으면 인스타그램만 적는다.
한 줄이라도 어느 쪽인지 모르면 프로젝트 합계를 적고, 메모에
`프로젝트 전체, 유튜브 등 포함`이라고 남긴다. 2026년 11월은 결제 내보내기가
11월 20일에 켜졌으므로 11월 20일 0시(한국시간) 이후만 더하고, 메모에
`11/20 이후만 집계`라고 남긴다. 통화가 원화가 아니면 내보내기에 있는 환율로만 바꾸고,
환율이 없으면 기록하지 않는다. 바꾼 경우에는 메모에 원래 통화를 적는다.

환경 변수는 필요한 키만 `--update-env-vars`로 더한다. 환경 변수를 통째로
갈아끼우지 않는다. 배포 스크립트는 이 작업을 새로 만들지 않는다. 이미 있으면
이미지만 바꾸고, 환경 변수와 인수는 그대로 둔다.

프로젝트는 `waterbomb-sns-pipeline`, 리전은 `asia-northeast3`이다.
지금 gcloud에 선택된 프로젝트가 이것과 같은지 확인한 뒤 아래를 순서대로
한 번만 실행한다.

### 1. 작업 만들기

댓글 관리 이미지를 먼저 배포한 뒤, 그 이미지 주소로 작업을 만든다.

```sh
IMAGE=$(gcloud run jobs describe sns-comments-moderate --region=asia-northeast3 --format='value(spec.template.spec.template.spec.containers[0].image)')
gcloud run jobs create sns-comments-monthly-cost \
  --region=asia-northeast3 \
  --image="$IMAGE" \
  --service-account=sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --command=python \
  --args=-m,sns_comment.monthly_cost \
  --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=600s --memory=512Mi --cpu=1 \
  --update-env-vars=GOOGLE_CLOUD_PROJECT=waterbomb-sns-pipeline,SNS_NOTION_OAUTH_SECRET=projects/waterbomb-sns-pipeline/secrets/sns-notion-oauth
```

### 2. 결제 표 읽기 권한

데이터셋 명령은 권한을 하나 더한다. 데이터셋에 있던 다른 권한은 지우지 않는다.
작업 실행 권한은 프로젝트에 하나 더한다. 다른 구성원의 권한은 지우지 않는다.

```sh
bq query --project_id=waterbomb-sns-pipeline --location=asia-northeast3 --use_legacy_sql=false \
  'GRANT `roles/bigquery.dataViewer` ON SCHEMA `waterbomb-sns-pipeline`.billing_export TO "serviceAccount:sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com"'
gcloud projects add-iam-policy-binding waterbomb-sns-pipeline \
  --member=serviceAccount:sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --role=roles/bigquery.jobUser \
  --condition=None
```

표 이름은 `billing_export.gcp_billing_export_v1_01152F_3EE377_FB410D`이다.
결제 계정 `01152F-3EE377-FB410D`의 표준 사용 비용 내보내기이다. 표가 아직
없어도 작업을 만들 수 있다. 그 달에는 노션에 쓰지 않고 끝난다.

### 3. 매월 1일 10시 예약

스케줄러가 이 작업을 부르려면 실행 권한이 먼저 필요하다. 권한을 통째로
바꾸지 않고, 스케줄러 계정만 더한다. 댓글 관리 예약과 같은 계정이다.

```sh
gcloud run jobs add-iam-policy-binding sns-comments-monthly-cost \
  --region=asia-northeast3 \
  --member=serviceAccount:sns-scheduler@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --role=roles/run.invoker
gcloud scheduler jobs create http sns-comments-monthly-cost \
  --location=asia-northeast3 \
  --schedule='0 10 1 * *' \
  --time-zone=Asia/Seoul \
  --uri='https://run.googleapis.com/v2/projects/waterbomb-sns-pipeline/locations/asia-northeast3/jobs/sns-comments-monthly-cost:run' \
  --http-method=POST \
  --oauth-service-account-email=sns-scheduler@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --headers=Content-Type=application/json \
  --message-body={} \
  --attempt-deadline=180s \
  --max-retry-attempts=0
```

### 특정 달만 다시 적기

이번 실행에만 달을 지정한다. 작업 수정 화면에 인수를 저장하지 않는다.

```sh
gcloud run jobs execute sns-comments-monthly-cost \
  --region=asia-northeast3 \
  --args=-m,sns_comment.monthly_cost,2026-11 \
  --wait
```

콘솔: Cloud Run → 작업 → `sns-comments-monthly-cost` → **실행**.
컨테이너 인수에 `-m` `sns_comment.monthly_cost` `2026-11`을 이번 실행에만 적는다.

## 캡션 규정집 동기화

아래 명령은 `instagram` 폴더에서 실행한다.
매월 1일 오전 10시(한국시간)에 최근 약 35일의 Meta·Instagram 변경을 조사한다.
대상은 캡션, 해시태그, Reels, 광고 문구다. 출처 주소와 날짜가 있는 공식 항목만
기존 페이지 [✍🏻 워터밤 캡션 작성 규정집 | 실무 가이드](https://app.notion.com/p/32c0761a512d8214bf84819e674e6177)
를 그 자리에서 고친다. 새 페이지는 만들지 않는다. 공식 변경이 없으면 규정 문장,
버전, 변경 기록, DB 속성은 그대로 둔다.

공식 주소는 Instagram·Meta 고객센터, 블로그, @creators, 광고 정책 페이지다.
업계 매체 기사는 월간 보고의 `업계 매체 보도`에 링크와 날짜만 남기고 규정집은
고치지 않는다. 인정하는 매체는 Social Media Today, Adweek, Marketing Dive,
TechCrunch, The Verge, Search Engine Land, Search Engine Journal이다.
Later, Hootsuite, Sprout Social, Buffer, Metricool, Social Media Examiner처럼
도구를 파는 회사의 블로그는 출처로 쓰지 않는다.

댓글 관리와 다른 잠금 `state/caption-policy-lease.json`만 잡는다.
`state/lease.json`과 `state/moderation-lease.json`은 잡지 않으므로, 이 작업이
30분 댓글 관리를 미루지 않고 그 작업에 밀려 시작을 포기하지도 않는다.
해시태그 최대 5개(게시 목적에 맞게 고른다, CEO 요청 2026-11-26,
<https://help.instagram.com/351460621611097>)는 그 날짜보다 나중의 공식 출처가
다른 한도를 말할 때만 고친다. 공식 출처 없이 기존 규칙을 지우지 않는다.

조사는 Vertex AI Gemini의 Google Search grounding이다. 모델은 운영 설정의
`cloud.ai_model`이다. 댓글 관리와 같은 서비스 계정, 같은 컨테이너 이미지를
쓰고 명령만 `python -m sns_comment.caption_policy`이다.

보고는 그 규정집 페이지의 Notion 댓글이다. 봉재는 GCP를 열지 않고 페이지
댓글에서 본다. 연결에 댓글 도구가 없으면 페이지 맨 아래 `메타 정책 월간 점검`
토글에 같은 글을 남긴다. 공식 정책이 그대로면 그 토글만 생기고 규정 문장은
바뀌지 않는다. 공식 변경이 없으면 보고 문장은 `이번 달 메타 정책 변경 없음, 규정집 그대로`다.
업계 보도만 있으면 그 문장 아래에 `업계 매체 보도`를 붙이고 규정집은 그대로 둔다.
Cloud Logging에는 `caption_policy_done` 또는 `caption_policy_failed`가 한 줄로 남는다.

환경 변수는 필요한 키만 `--update-env-vars`로 더한다. 통째로 갈아끼우지 않는다.
배포 스크립트는 이 작업과 스케줄을 새로 만들지 않는다. 이미 있으면 이미지만
바꾸고, 환경 변수·인수·제한 시간·예약은 그대로 둔다.

프로젝트는 `waterbomb-sns-pipeline`, 리전은 `asia-northeast3`이다.
지금 gcloud에 선택된 프로젝트가 이것과 같은지 확인한 뒤, 댓글 관리 이미지를
먼저 배포하고 아래를 순서대로 한 번만 실행한다. 이미지를 배포하기 전에
작업을 만들면 새 명령이 들어 있지 않다.

작업이 이미 있을 때 이 작업의 이미지만 바꿀 때는 `deploy`를 쓰지 않는다.
`deploy`는 댓글 관리와 관리 보고의 이미지도 바꾼다. `instagram` 폴더에서
`prepare`와 `build`만 하고, 빌드가 `SUCCESS`가 된 뒤 이 작업만 가리킨다.
환경 변수, 인수, 제한 시간, 스케줄러는 그대로 둔다. `--set-env-vars`와
`--update-env-vars`는 붙이지 않는다.

```sh
python3 scripts/token_connection_release.py prepare
python3 scripts/token_connection_release.py build
BUILD_ID=$(python3 -c 'import json; print(json.load(open(".local/token-connection-release/build.json"))["id"])')
gcloud builds describe "$BUILD_ID" --project=waterbomb-sns-pipeline --format='value(status)'
IMAGE=$(python3 -c 'import json; print(json.load(open(".local/token-connection-release/release.json"))["image"])')
gcloud run jobs update sns-comments-caption-policy \
  --image="$IMAGE" \
  --region=asia-northeast3 \
  --project=waterbomb-sns-pipeline
```

`gcloud builds describe`가 `SUCCESS`이기 전에 이미지를 바꾸지 않는다.
`verify`와 `deploy`는 실행하지 않는다.

### Notion 공유

파이프라인이 쓰는 연결은 Secret Manager `sns-notion-oauth`의 MCP OAuth이고,
Notion에 보이는 이름은 `MADEONE SNS Comments Pipeline`이다. 일일 업무 병합용
`WSH`(`sns-notion-wsh-token`)가 아니다.

이 저장소를 만든 환경의 Cursor Notion 연결(integration
`1f8d872b-594c-80a4-b2f4-00370af2b13f`)은 대상 페이지, 상위 페이지
<https://app.notion.com/p/26bfd25c45914b27b3af0bf98197ba5b>, 그리고 파이프라인의
댓글 규정집 페이지를 모두 열지 못했다(404). 그 연결은 파이프라인 연결이 아니다.
이 환경에는 gcloud가 없어 `sns-notion-oauth`를 읽어 확인하지 못했다.

첫 실행 로그가 `caption_policy_access_denied`이면 Notion에서 아래 두 페이지를
연 뒤 공유에 `MADEONE SNS Comments Pipeline`을 편집 가능으로 초대한다.
행 속성을 고치므로 `규정집 목록` 데이터베이스도 같은 연결에 공유한다.

- <https://app.notion.com/p/32c0761a512d8214bf84819e674e6177>
- <https://app.notion.com/p/26bfd25c45914b27b3af0bf98197ba5b>

연결 목록에 그 이름이 없으면 `connect-notion.command`로 규정집이 있는
워크스페이스에서 다시 승인한 뒤 같은 공유를 한다. 다른 주소
`4000761a512d81928337dc6fb5754951`는 수정하지 않는다.

### 1. 작업 만들기

```sh
IMAGE=$(gcloud run jobs describe sns-comments-moderate --region=asia-northeast3 --format='value(spec.template.spec.template.spec.containers[0].image)')
gcloud run jobs create sns-comments-caption-policy \
  --region=asia-northeast3 \
  --image="$IMAGE" \
  --service-account=sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --command=python \
  --args=-m,sns_comment.caption_policy \
  --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=900s --memory=512Mi --cpu=1 \
  --update-env-vars=GOOGLE_CLOUD_PROJECT=waterbomb-sns-pipeline,SNS_STATE_BUCKET=waterbomb-sns-pipeline-state,SNS_NOTION_OAUTH_SECRET=projects/waterbomb-sns-pipeline/secrets/sns-notion-oauth
```

`SNS_CAPTION_POLICY_DRY_RUN`은 작업에 저장하지 않는다. 저장하면 매월 실행이
계속 미리보기만 한다.

### 2. 매월 1일 10시 예약

스케줄러가 이 작업을 부르려면 실행 권한이 먼저 필요하다. 권한을 통째로
바꾸지 않고, 댓글 관리 예약과 같은 스케줄러 계정만 더한다.

```sh
gcloud run jobs add-iam-policy-binding sns-comments-caption-policy \
  --region=asia-northeast3 \
  --member=serviceAccount:sns-scheduler@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --role=roles/run.invoker
gcloud scheduler jobs create http sns-comments-caption-policy \
  --location=asia-northeast3 \
  --schedule='0 10 1 * *' \
  --time-zone=Asia/Seoul \
  --uri='https://run.googleapis.com/v2/projects/waterbomb-sns-pipeline/locations/asia-northeast3/jobs/sns-comments-caption-policy:run' \
  --http-method=POST \
  --oauth-service-account-email=sns-scheduler@waterbomb-sns-pipeline.iam.gserviceaccount.com \
  --headers=Content-Type=application/json \
  --message-body={} \
  --attempt-deadline=180s \
  --max-retry-attempts=0
```

### 배포 직후 한 번 실행

미리보기는 노션에 쓰지 않는다. 로그에 `미리보기입니다`와 `caption_policy_done`이
보이면 된다. 이 환경 변수는 이번 실행에만 둔다. 작업 수정 화면에 저장하지 않는다.
실행이 끝난 뒤 작업 환경 변수에 `SNS_CAPTION_POLICY_DRY_RUN`이 남아 있으면 지운다.

```sh
gcloud run jobs execute sns-comments-caption-policy \
  --region=asia-northeast3 \
  --update-env-vars=SNS_CAPTION_POLICY_DRY_RUN=1 \
  --wait
```

미리보기에서 출처와 반영 위치가 맞으면 같은 방식으로 한 번 실제로 실행한다.
인수를 바꾸지 않는다. `SNS_CAPTION_POLICY_DRY_RUN`도 넣지 않는다.

```sh
gcloud run jobs execute sns-comments-caption-policy \
  --region=asia-northeast3 \
  --wait
```

콘솔에서 수동 실행: [Cloud Run 작업 실행 목록](https://console.cloud.google.com/run/jobs/details/asia-northeast3/sns-comments-caption-policy/executions?project=waterbomb-sns-pipeline).
미리보기는 이번 실행의 환경 변수에만 `SNS_CAPTION_POLICY_DRY_RUN`=`1`을 적는다.

### 어디를 보면 되는지

- 작업 실행: [sns-comments-caption-policy 실행](https://console.cloud.google.com/run/jobs/details/asia-northeast3/sns-comments-caption-policy/executions?project=waterbomb-sns-pipeline)
- 예약: [Cloud Scheduler](https://console.cloud.google.com/cloudscheduler?project=waterbomb-sns-pipeline)의 `sns-comments-caption-policy`. 크론 `0 10 1 * *`, 시간대 `Asia/Seoul`.
- 로그: [Cloud Logging](https://console.cloud.google.com/logs/query;query=resource.type%3D%22cloud_run_job%22%0Aresource.labels.job_name%3D%22sns-comments-caption-policy%22%0AtextPayload%3A%22caption_policy_%22?project=waterbomb-sns-pipeline). 성공은 `caption_policy_done`, 실패는 `caption_policy_failed`, 권한은 `caption_policy_access_denied`, 이미 실행 중이면 `caption_policy_deferred`다.
- 노션 보고: [캡션 규정집](https://app.notion.com/p/32c0761a512d8214bf84819e674e6177) 페이지 댓글. 댓글 도구가 없으면 같은 페이지 맨 아래 `메타 정책 월간 점검` 토글.

GitHub Actions는 Linux·Windows 검증만 실행한다. PR 병합만으로 배포되지 않는다.
기존 daily용 모듈은 과거 자료·호환 검증을 위해 남아 있으나 `daily_enabled=false`인
운영 설정에서는 수집·AI·Notion 인증을 시작하기 전에 종료한다.
