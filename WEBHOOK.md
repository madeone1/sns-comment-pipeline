# SNS Comments 웹훅 연결 v2 (2026-11-10)

상태: **수신 서버 배포·Meta 콜백 등록·대표/부산/속초 comments 구독·기존 08시 작업 이미지 반영 완료**. Meta 자체 샘플 알림의 수신 HTTP 200을 확인했다. 실제 자연 발생 댓글의 수신함 저장·원문 조회·다음 08시 목록 반영은 아직 확인 전이다. 운영 DB에 실제 댓글을 재전송하는 통제 테스트는 자동 승인 검토에서 거부되어 실행하지 않았다.

현재 배포 기록과 검증 증빙: [WEBHOOK-DEPLOYMENT.md](WEBHOOK-DEPLOYMENT.md).

현재 작성일 기준: [COMMENT-WINDOWS.md](COMMENT-WINDOWS.md). 2026-11-10 후속 변경으로 최근 7일 게시물 제한을 제거하고 댓글 작성일의 24시간·7일로 집계한다. 아래 기존 최근 게시물/이전 게시물 분리 설명은 초기 구현 기록이다.

## 확정한 연결 구조

```text
Meta comments 알림
  → 공개 Cloud Run sns-comments-webhook /webhooks/instagram
  → 원본 바이트의 HMAC 서명 확인 + 활성 계정 확인
  → 기존 비공개 GCS webhook/pending/<계정 ID>/<배달 해시>.json
  → 내구성 저장 완료 후 HTTP 200

매일 08:00 KST 기존 sns-comments-daily (기존 단일 쓰기 잠금)
  → 기존 최근 7일 게시물 수집·분석·Notion 보고
  → 알림 목록 확정 → 해당 게시물 소유자 검증
  → 해당 게시물의 댓글·답글만 제한된 API GET으로 확인
  → 기존 SQLite 원본 및 재개 위치를 원격 체크포인트로 저장
  → 실제 작성 시각 기준 일일 원문 목록 보완
  → 처리 이력을 webhook/done/에 보존한 뒤 pending에서 조건부 제거

비공개 마케팅 웹앱
  → 기존 일일 원본 + 추가 확인 원본을 댓글 ID로 중복 제거
  → 지역(계정)별 전날 08:00 이상 ~ 당일 08:00 미만 검수
```

수신기는 AI·Graph API 조회·Notion·SQLite를 실행하지 않는다. 전체 원본을 수신함에 복제하지 않고 배달 해시, 계정·게시물·댓글 ID, 이벤트 시각과 수신 시각만 보관한다. 수신 시각으로 댓글 작성 시각을 추정하지 않는다.

## 현재 구현

- `sns_comment/webhook.py`: GET 콜백 인증, POST 원본 HMAC-SHA256 확인, 1MB 제한, 활성 계정·comments 필드 대조, 개인정보 제거, 조건부 생성과 중복 배달 처리. 저장 실패는 503으로 응답한다. 임의 계정이나 중지 계정은 저장하지 않는다.
- `sns_comment/webhook_worker.py`: 계정별 한 번에 알림 최대 100개·게시물 최대 25개. 실제 소유자를 확인한 게시물만 정확한 scope에 고정한다. Graph 조회는 이 단계에서 계정당 최대 1,000회, 기존 작업 전체 5,000회 예산 안에서 실행한다. 댓글 ID 직접 조회의 지원 필드를 추측하지 않고 기존 댓글·답글 페이지 수집기를 재사용한다.
- 부분 완료나 중단은 원격 DB에 보존한 같은 scope에서 재개한다. 새 알림에는 새 scope를 만들어 과거 complete를 재사용하지 않는다. 큰 게시물의 전체 댓글 확인은 여러 날에 걸칠 수 있으며 완료 전 알림은 pending에 남는다.
- 실제 API 원문 저장과 일일 목록 보완이 성공한 뒤만 ACK한다. 처리 완료 직전 중단·재전송은 원본 PK와 배달 키로 중복 반영하지 않는다. 확인 완료 후에도 찾지 못한 댓글은 `not_observed_after_complete_scan`으로 기록하며 삭제됐다고 단정하지 않는다.
- 지연 도착 댓글은 원래 작성 구간의 `runs/<날짜>/webhook-daily.json`을 보완한다. 기본 `source.json`, AI 보고서, Notion 원문 보고는 덮어쓰지 않는다. 해당 날짜 기본 수집이 없으면 앱에서 부분 자료로 표시한다.
- 기존 Notion의 최근 7일 게시물 기준 집계는 유지한다. 이전 게시물의 별도 AI 요약·Notion 보고는 이번 구현에 포함하지 않는다. 앱에는 추가 확인 원문이 표시되고 담당자가 검수할 수 있다.
- `cloud.webhook_enabled=true`인 릴리스에서만 소비기를 실행한다. 이 모드에서는 기존의 알려진 이전 게시물 전체 재조회 대신 수신 알림의 게시물만 확인해 비용을 제한한다. 기본 최근 게시물 수집은 유지한다.
- 처리 완료 보존 기간 자동 삭제는 설정하지 않았다. 미처리 알림을 시간 경과로 삭제하지 않는다.

## 적용한 운영 배포 범위

프로젝트 `waterbomb-sns-pipeline`, 리전 `asia-northeast3`, 기존 비공개 버킷 `waterbomb-sns-pipeline-state`를 사용한다.

| 대상 | 추가 권한·변경 | 범위 |
| --- | --- | --- |
| 새 수신 서비스 계정 `sns-webhook@waterbomb-sns-pipeline.iam.gserviceaccount.com` | `storage.objectViewer` 조건부 | `web/registry.json` 한 객체와 `webhook/done/` 읽기만 |
| 같은 수신 계정 | `storage.objectCreator` 조건부 | `webhook/pending/` 생성만 |
| 같은 수신 계정 | `secretmanager.secretAccessor` | 새 `sns-meta-app-secret`, `sns-webhook-verify-token` 두 비밀만 |
| 새 `sns-comments-webhook` 서비스 | `allUsers`에 `run.invoker` | Meta가 호출할 공개 수신 주소 하나. 검수 웹앱·버킷은 공개하지 않음 |
| 기존 `sns-comments-daily` | 이미지 변경 | 기존 실행 계정·환경 변수·08시 스케줄 유지 |
| 로그 저장 | `_Default`에 한정된 제외 조건 추가 | 수신기의 GET 콜백 요청 URL에 포함되는 Verify Token 저장 방지. POST 요청 지표와 감사 로그는 유지 |

수신기에는 Meta 액세스 토큰, Notion OAuth, 원본 DB 읽기·수정 권한을 부여하지 않는다. 기존 배치의 버킷 권한은 그대로 사용하며 신규 권한을 추가하지 않는다. 비공개 웹앱의 IAP와 사용자 권한 구성은 앞서 보류된 별도 범위다.

수신 서비스는 CPU 1개, 메모리 256MiB, 최소 0·최대 2 인스턴스, 동시 요청 8, 요청 제한 60초로 준비했다. 별도 DB, Pub/Sub, 로드밸런서를 추가하지 않는다.

2026-11-10 적용 확인: 로그 싱크는 `_Default`, `_Required`이며 `_Default`에 지정된 GET 검증 요청 제외 하나를 추가했다. 웹훅 비밀 두 개를 만들고 전용 수신 계정에는 각 Secret의 읽기만 부여했다. 원본 DB·Meta 액세스 토큰·Notion 토큰을 수신 서비스에 마운트하지 않았다.

## Meta에서 확인한 상태와 남은 연결

초기에는 Chrome의 `SNS Comments Analyze` (앱 ID `1607262264749356`)에서 콜백이 미등록이었다. 현재는 실제 콜백 URL이 저장됐고 `comments` v26.0이 구독 중으로 표시된다. 앱 게시·심사 제출은 이번 작업에서 변경하지 않았다. 운영 토큰으로 Graph API `/app`을 조회하여 앱 ID `1607262264749356`과 이름 `SNS Comments Analyze`의 일치를 확인했다. 대표·부산·속초의 `/subscribed_apps`에 comments 구독을 적용했다. GET의 `fields=app_id,subscribed_fields`를 통해 세 계정 모두 app_id `1607262264749356`, subscribed_fields `[comments]`를 직접 대조했다.

등록 순서:

1. 운영 토큰의 앱 ID·로그인 경로·계정 자산 권한을 확인한다. 앱 ID 일치 및 세 계정의 구독 목록 GET은 완료했다. 기존 수집이 되는 인증 경로를 임의로 전환하지 않는다.
2. 위 권한 범위 승인 후 수신 계정을 구성하고 두 비밀을 Secret Manager에 등록한다. App Secret은 해당 Meta 앱의 기존 값을 비공개 입력으로 전달하고, Verify Token은 별도의 난수로 생성한다. 채팅·명령 인수·로그에 값을 쓰지 않는다.
3. GET 콜백 로그 제외를 먼저 적용한 뒤 수신 이미지를 배포한다. 운영에서 확인한 URL에 `/webhooks/instagram`을 붙인다. 실제 콜백은 `https://sns-comments-webhook-hephs7jvlq-du.a.run.app/webhooks/instagram`이다.
4. 로컬 서명 테스트 외에 배포된 주소의 잘못된 서명 거절·정상 알림 내구성 저장을 확인한다.
5. Meta에서 콜백 확인 후 `comments` 필드와 실제 연결 경로에 필요한 계정 구독을 설정한다. Facebook Login·Unified Login의 계정 구독 경로를 추측해 POST하지 않는다. 신규 지역 계정도 수집 권한 확인과 알림 구독을 각각 확인해야 한다. 웹앱에 계정을 추가하는 것만으로 Meta 구독이 완료된다고 표시하지 않는다.
6. Meta 대시보드 테스트와 자연 발생 댓글 각각을 확인한다. 실제 수신·원문 조회·작성일별 목록 반영을 확인한 후 연결 완료로 표시한다. 테스트 댓글을 대신 게시하지 않는다.
7. 기존 일일 작업 이미지를 변경하고 다음 08시 실행을 검증한다. 원문 누락·중복·대기 상태와 기존 보고 수치를 비교한다.

App Secret은 사용자 본인 재인증 후 비공개 로컬 입력으로 검증해 Secret Manager에 등록했다. 앱 시크릿을 재설정하지 않았고 비밀번호·비밀 값을 채팅이나 파일에 저장하지 않았다. 콜백 저장·comments 구독은 완료했고 앱 게시·검수 제출·댓글 게시·DM 발송은 수행하지 않았다.

## 승인 검토 상태

초기 IAM 검토 거부 후 사용자가 전체 리소스 주소·조건·로그 제외까지 명시적으로 승인했다. 이후 전용 계정·조건부 버킷 권한·두 비밀·로그 제외·서비스와 작업 이미지 변경·Meta 구독은 적용됐다. 실제 댓글을 운영 DB에 재전송하는 별도 테스트는 자동 검토가 거부해 실행하지 않았고, Meta 자체 샘플 알림과 기존 격리 테스트를 사용했다.

## 배포 준비물·검증

```sh
python3 scripts/webhook_release.py prepare
python3 scripts/webhook_release.py build
.local/web-venv/bin/python -m unittest discover -s tests -q
```

준비 도구는 기존 운영 이미지에서 확보한 `.local/web-release/operational-config.json`을 사용한다. 변경은 `cloud.webhook_enabled=true` 하나이며, 개발용 Sheets/Drive workflow를 운영에서 켜지 않는다. `.local/webhook-release/permission-plan.json`, `release.json`, `cloudbuild.json`과 이미지별 소스 해시를 남긴다. 이 도구의 prepare/build는 운영을 변경하지 않는다. configure, deploy-worker, deploy-receiver 액션을 추가하여 명시적으로 승인된 범위만 적용했다. 배포 결과 및 변경 전/후 스냅샷은 같은 폴더에 보존했다.

345개 테스트 통과: 서명 거절, 인증 challenge·모호한 쿼리, 다른 계정·중지 계정, 중복·일부 저장 실패 재전송, 개인정보 제거, 잘못된 소유자 거절, 중단·부분 수집 재개, ACK 실패 후 중복 조회 방지, 새 배달의 재조회, 08시 경계·지연 작성일 반영, 웹앱 원본 합산·중복 제거, 원문 확인 실패 시 기본 보고 보존을 포함한다. 실제 Meta 자체 테스트 payload의 서명·HTTP 200, 앱과 계정 구독은 운영에서 확인했다. 자연 댓글의 DB 반영은 아직 미확인이다.

비용은 기존 배치와 저장소를 재사용한다. 소규모 사용(사용자 2명, 계정 3~10개, 월 알림 약 1만 건)에서 웹앱·수신기 추가 운영비는 월 약 USD 0~5(계획 환율 1,500원 기준 0~7,500원)를 우선 예산으로 잡는다. 무료 구간은 결제 계정에서 공유하며, 댓글량·이전 게시물 재조회·버전 보관량 증가에 따라 초과할 수 있다. 기존 AI 분석비·기존 원본 보관비·개발 인건비는 별도다. [Cloud Run 요금](https://cloud.google.com/run/pricing), [Secret Manager 요금](https://cloud.google.com/secret-manager/pricing), [Storage 요금](https://cloud.google.com/storage/pricing)을 기준으로 한 추정이며 청구 확정액은 아니다.

공식 참조: [Storage 조건부 요청](https://docs.cloud.google.com/storage/docs/request-preconditions), [Meta Webhooks](https://developers.facebook.com/docs/instagram-platform/webhooks/), [Meta 공식 API 컬렉션](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api). Meta 문서는 현재 도구에서 429·접근 실패가 있어 등록 필드와 앱 상태는 실제 대시보드로 확인했고, 계정별 구독 경로는 연결 전에 추가 확인한다.

## 보고서 의도 구분 보완

주 카테고리 7개와 감정은 유지한다. 기존의 포괄적인 “요청” 의도를 다음과 같이 구분한다.

| 유형 | 예 | 보고 표현 | 후속 판단 |
| --- | --- | --- | --- |
| 희망·선호 (want) | “OO가 나왔으면 좋겠어요”, “Australia soon please” | 출연 희망, 개최 희망 반응 | 기획·콘텐츠 수요 참고. 문의·접수 업무·미해결 건으로 자동 전환하지 않음 |
| 정보 문의 (inquiry) | “OO 출연하나요?”, “티켓은 언제 열리나요?” | 출연 여부 문의, 예매 일정 문의 | 사실 확인·안내·FAQ 검토 후보 |
| 구체적 행동 요청 (action request) | “태민 무대 풀영상 올려주세요”, “환불해 주세요” | 영상 공개를 바라는 댓글, 환불 처리 요청 댓글 | 실행 가능 여부·사람의 검토 필요. 접수·승인·처리 약속을 추정하지 않음 |
| 단순 언급·불명확 | “권은비”, 미완성 문장 | 이름 언급, 의미 판단 보류 | 희망이나 문의로 임의 확정하지 않음 |

please나 물음표 하나로 분류하지 않는다. 댓글 수와 고유 작성자 수를 함께 보며 동일 작성자의 반복은 별도 표시한다. “요청 접수”는 사용하지 않는다. 희망 반응이 여러 건이어도 미해결 문의가 늘었다고 쓰지 않는다.

3줄 요약 예시(형식 설명이며 실제 검수 완료 수치가 아님):

1. 신규 댓글은 없으며 선미 무대 영상에 대한 긍정 반응이 확인됐습니다.
2. 호주 개최 희망과 태민 풀영상 공개를 바라는 댓글이 있었고, 명확한 정보 문의와 구분했습니다.
3. 개최 희망은 기획 참고로, 영상 공개는 가능 여부 검토로 나누어 확인합니다.
