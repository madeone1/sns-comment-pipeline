# SNS Comments 클라우드 운영

2026-11-17부터 Instagram daily 수집·분석 보고는 종료한다. 같은 이름의
`sns-comments-daily` Scheduler와 Cloud Run Job을 제거한다. 저장된 원문·DB·
기존 시트·Notion 보고와 Secret 이력은 삭제하지 않는다.

| 항목 | 운영 구성 |
| --- | --- |
| 프로젝트 / 리전 | `waterbomb-sns-pipeline` / `asia-northeast3` |
| 댓글 관리 | `sns-comments-moderate`, 매시 00·30분, Asia/Seoul |
| 댓글 관리 제한 | 1,500초, 잠금 1,620초, 자동 재시도 0회 |
| 관리 보고 | `sns-comments-notion-summary`, 매일 09시 예약, 코드에서 주말 제외 |
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
배포 시 `--set-env-vars`로 기존 환경을 교체하지 않는다.

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

같은 이미지의 성공한 검증 증빙이 있어야 두 운영 Job 이미지를 바꾼다. 댓글 관리의
제한 시간만 1,500초로 맞추며 기존 기능 스위치·예약 시각은 보존한다. 배포 후 실제
실행 로그, 대기열·진행 위치와 시트·Notion 상태를 재조회한다.

기존 daily 리소스 정리는 한 번만 수행한다. 이 명령은 Instagram daily만 제거하고
Cloud Storage 원본, 기준표, Notion 내용, YouTube와 병합 작업은 보존한다.

```sh
python3 scripts/token_connection_release.py retire-daily
```

GitHub Actions는 Linux·Windows 검증만 실행한다. PR 병합만으로 배포되지 않는다.
기존 daily용 모듈은 과거 자료·호환 검증을 위해 남아 있으나 `daily_enabled=false`인
운영 설정에서는 수집·AI·Notion 인증을 시작하기 전에 종료한다.
