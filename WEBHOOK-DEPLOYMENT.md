# 웹훅 배포 결과 (2026-11-10)

웹훅 수신 서비스, Meta 콜백, 세 계정의 comments 구독, 기존 08시 수집기 이미지를 적용했다. Meta 자체 테스트 알림은 HTTP 200으로 수신했다. 자연 발생 댓글이 실제 수신함과 원본 DB를 거쳐 일일 검수 목록에 반영되는 운영 검증은 아직 남아 있다.

2026-11-10 후속 변경: [댓글 작성일 기준](COMMENT-WINDOWS.md)으로 수집기를 갱신하고 오늘 누락 4건을 보정했다. 아래 이미지는 웹훅 최초 연결 시점의 기록이다.

## 현재 연결

- 프로젝트 / 리전: `waterbomb-sns-pipeline` / `asia-northeast3`
- 수신 서비스: `sns-comments-webhook`
- 콜백: `https://sns-comments-webhook-hephs7jvlq-du.a.run.app/webhooks/instagram`
- Meta 앱: `SNS Comments Analyze` / `1607262264749356`
- 구독 필드: `comments` v26.0
- 일일 수집: Cloud Run Job `sns-comments-daily`, Cloud Scheduler `sns-comments-daily`, `0 8 * * *`, `Asia/Seoul`, `ENABLED`
- 처리 방식: 알림을 보관하고 매일 08시 원문을 API로 확인한다. 댓글마다 AI·Notion 작업을 실행하지 않는다.

| 지역 | 계정 | Instagram 계정 ID | 구독 확인 |
| --- | --- | --- | --- |
| 대표(공식) | @waterbomb_official | `17841401245057628` | app_id 일치, comments |
| 부산 | @waterbomb_busan_official | `17841411527874641` | app_id 일치, comments |
| 속초 | @waterbomb_sokcho_official | `17841455831114060` | app_id 일치, comments |

## 검증 결과

| 항목 | 확인 결과 |
| --- | --- |
| 운영 토큰의 앱 일치 | Graph `/app`에서 ID·이름 확인 |
| Meta 콜백 | active=true, callback_url 일치, comments v26.0 |
| 계정 구독 | 세 계정 모두 GET `fields=app_id,subscribed_fields`로 명시적 앱 ID와 `[comments]` 확인 |
| 잘못된 서명 | HTTP 403 |
| 정상 콜백 challenge | HTTP 200, challenge 일치 |
| 정상 서명·빈 알림 | HTTP 200, 계정 등록부 읽기 확인 |
| Meta 자체 샘플 | 2026-11-10 14:10:31 KST, User-Agent `Webhooks/1.0 (https://fb.me/webhooks)`, HTTP 200, 약 76ms |
| 샘플의 운영 수신함 오염 | 테스트 후 `webhook/pending/` 객체 없음 |
| 수신 전용 권한 | 계정 목록 한 객체·done 경로 읽기, pending 생성, 두 비밀 읽기만 대조 |
| 실행 자원 | CPU 1, 메모리 256MiB, 최소 0·최대 2 인스턴스 |
| 기존 작업 보존 | 이미지 외 실행 계정·환경 변수·자원·재시도·제한 시간 동일 |
| 자동 수집 시간 | 매일 08:00 KST 예약 활성 유지 |
| 로컬 회귀 검증 | 이미지 빌드 시점 345개 테스트 통과 |
| 자연 발생 댓글의 저장·소비 | 아직 미확인 |

Cloud Run 공개 상태 점검은 실제 `/webhooks/instagram` 경로의 검증 응답을 사용한다. 플랫폼의 `/healthz` 외부 응답은 404여서 수신 서비스의 가동 판단 기준으로 사용하지 않았다.

## 계정에 수행한 작업

댓글 알림 구독 설정을 추가했다. Instagram 게시물·댓글 작성·수정·삭제·숨김·DM 발송·팔로워 변경은 수행하지 않았다. 앱 시크릿은 기존 값을 비공개 경로로 등록했으며 재설정하지 않았다. Meta 비밀번호는 사용자가 직접 재인증했고 수집하지 않았다.

실제 기존 댓글을 재전송하여 운영 DB에 반영하는 통제 테스트는 자동 승인 검토에서 운영 기록 변경 위험을 이유로 거부되어 실행되지 않았다. 해당 실행 코드는 제거했고 Meta 자체 샘플과 기존 격리 테스트로 검증했다. 이 결과를 자연 댓글의 종단 검증 완료로 표시하지 않는다. 앱 게시나 앱 검수 요청도 제출하지 않았다.

## 비밀·로그

`sns-meta-app-secret`, `sns-webhook-verify-token`을 Secret Manager에 등록하고 `sns-webhook@waterbomb-sns-pipeline.iam.gserviceaccount.com`에 해당 두 비밀의 읽기만 부여했다. 수신기에 Meta 액세스 토큰·Notion 토큰·원본 DB 권한을 부여하지 않았다.

Meta 인증 GET의 URL에 포함되는 Verify Token이 저장되지 않도록 `_Default`에 서비스·경로·GET에 한정한 제외를 추가했다. POST 요청 지표와 감사 로그는 유지한다. 임시 비밀 등록 화면과 서버는 종료하고 브라우저 제어 세션의 비밀 변수도 초기화했다.

## 배포 이미지와 복구 기준

수신 이미지: `asia-northeast3-docker.pkg.dev/waterbomb-sns-pipeline/containers/sns-comments-webhook:20261110-043613-webhook`

현재 수집 이미지: `asia-northeast3-docker.pkg.dev/waterbomb-sns-pipeline/containers/sns-comments:20261110-043613-webhook`

변경 전 수집 이미지: `asia-northeast3-docker.pkg.dev/waterbomb-sns-pipeline/containers/sns-comments:20261109-044731`

이미지 빌드: `1d2c4332-83b9-4836-bd22-8e490f6a261f`, SUCCESS. 변경 전·후 작업 구성은 `.local/webhook-release/job-before.json`, `job-after.json`에 보존했다. 연결 해제·이미지 복귀는 필요할 때 해당 리소스에만 적용하며 이번 작업에서는 수행하지 않았다.

## 확인이 남은 부분

자연 발생 댓글의 서명 수신, pending 내구성 저장, 다음 08시 API 원문 확인·DB 반영·작성일별 목록 보완을 확인해야 한다. 아직 수신한 자연 댓글이 없어 저장·소비 검증을 완료로 표시하지 않는다. 계정을 추가하거나 다른 앱의 토큰으로 옮기면 계정 구독·앱 일치를 별도로 확인해야 한다.

별도 관리 웹앱은 2026-11-11 종료했다. 현재 토큰 변경은 로컬 실행 파일을 사용한다. 웹훅 수신 서비스는 유지한다.

증빙은 `.local/webhook-release/verification.json`, `runtime-permissions-verification.json`, `receiver-check.json`, `meta-connection.json`, `account-subscriptions-after.json`, `meta-dashboard-test.json`에 보존했다.
