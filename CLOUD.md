# SNS Comments 클라우드 운영

현재 기준은 로컬 토큰 입력과 기존 Cloud Run 수집기다. 별도 관리 웹사이트
`sns-comments-web`은 종료했다. Meta 웹훅 수신기는 별도 백엔드로 유지한다.
과거 배포 기록은 `docs/history/`, 이전 초기화 도구는 `scripts/legacy/`에 보존한다.

| 항목 | 현재 설정 |
| --- | --- |
| 프로젝트 | `waterbomb-sns-pipeline` |
| 로컬 실행 Google 계정 | 현재 활성 gcloud 계정. `sns-meta-token`·상태 버킷·수집 Job 권한이 있는 회사 계정 (예: `josh@madeone.kr`). `--account` 또는 `SNS_GCLOUD_ACCOUNT`로 지정 가능 |
| 리전 | `asia-northeast3` |
| 운영 작업 | `sns-comments-daily` |
| 정기 실행 | 매일 08:00, `Asia/Seoul` |
| 실행 서비스 계정 | `sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com` |
| 상태 저장소 | `gs://waterbomb-sns-pipeline-state` |
| Meta 토큰 | Secret Manager `sns-meta-token`의 검증된 고정 버전 |
| Notion MCP OAuth | Secret Manager `sns-notion-oauth` |
| 일일 업무 병합 인증 | Secret Manager `sns-notion-wsh-token` |

2026-11-11 배포 기준 이미지 태그는 `20261111-042358-token-connections`다.
저장소의 `config.json`은 이 배포에서 사용한 설정의 사본이며, 실제 수집 대상과
지역·인증 버전은 클라우드 등록부를 검증한 결과로 확정한다.

## 일반 운영

토큰 변경은 `change-access-token.command`만 사용한다. 토큰을 환경 파일이나
명령 인수에 넣지 않는다. 실행 파일은 현재 활성 회사 Google 계정으로 HTTPS API를
호출해 Meta 접근을 확인하고 Secret Manager와 등록부를 갱신한 뒤 Cloud Run을
시작한다. 사용 계정은 토큰 입력 전에 화면에 나온다. 로그인이 없거나 권한이
없으면 `gcloud auth login` 안내 후 종료하며 비밀 저장소는 바꾸지 않는다.
예약 실행은 로컬 컴퓨터가 꺼져 있어도 계속된다. Cloud Run Job 자체는
서비스 계정으로 돌며, 이 로컬 계정 선택을 쓰지 않는다.

Cloud Run에는 원문 Meta 토큰 환경변수를 주입하지 않는다. 매 실행 시 저장소의
최신 버전을 검증하고 숫자 버전으로 고정해 계정별 작업이 같은 스냅샷을 사용한다.
인증·계정·지역 검증 실패 시 기존 원문과 출력을 유지한다.

## 수집기 코드 배포

회사 클라우드 빌드·실행 권한이 있는 운영자가 명시적으로 수행한다.
GitHub에 코드를 올리는 것만으로 운영 이미지가 바뀌지는 않는다.

```sh
python3 scripts/token_connection_release.py prepare
python3 scripts/token_connection_release.py build
```

빌드 완료 후 기존 보고가 있는 날짜와 실제 일일 업무 페이지를 지정한다.
페이지 ID는 해당 날짜 Notion 페이지의 ID다. 준비한 이미지로 임시 Cloud Run
검증 작업을 만들고 종료 후 삭제한다. 원본·시트·Notion·등록부는 변경하지 않으며,
검증 증빙과 OAuth 갱신 인증만 비공개 클라우드 저장소에 기록한다.

```sh
python3 scripts/token_connection_release.py verify --day YYYY-MM-DD --page NOTION_PAGE_ID
python3 scripts/token_connection_release.py deploy
```

검증은 현재 계정·고정 인증 버전·계정별 원본 경로·시트 투영을 대조하고, 실제
Notion 내용을 읽어 메모리에서 갱신을 시뮬레이션한다. 근거 ID·일반 제목·지역별
수치와 다른 섹션의 보존 여부를 확인한다. 같은 이미지의 성공 증빙이 있어야
배포할 수 있다. 원본이나 일일 분석이 없는 날짜는 검증이 실패한다.

배포 도구는 운영 작업의 이미지와 기존 Meta 환경변수 제거만 적용하고, 나머지
작업 설정의 보존을 대조한다. 예약 시간이나 OAuth 경로를 임의 변경하지 않는다.
운영 반영 후에는 실행·시트·Notion을 실제로 다시 읽어 확인해야 한다.
`--status`의 전달 성공과 댓글 수집 전체 완료는 구분한다.

빌드 입력·검증·배포 전 작업 설정은 `.local/token-connection-release/`에 저장하며
Git에 넣지 않는다. 이 디렉터리는 비공개 운영 증빙이므로 별도로 보관한다.

## 웹훅

`Dockerfile.webhook`은 Meta 댓글 알림 수신용 백엔드다. 관리 웹사이트가 없어도
필요하다. 소스와 연결 도구는 유지한다. [WEBHOOK.md](WEBHOOK.md),
[WEBHOOK-DEPLOYMENT.md](WEBHOOK-DEPLOYMENT.md)는 2026-11-10 연결·검증 기록이다.

새 수신기 후보 준비는 `python3 scripts/webhook_release.py prepare`로 한다.
이 작업은 로컬 빌드 입력과 권한 계획만 만들며, 클라우드 권한이나 Meta 구독을
자동으로 바꾸지 않는다. 실제 변경은 해당 스크립트의 명시적 명령으로 수행한다.

## 권한과 비밀정보

클론만으로 회사 클라우드에 접근할 수 없다. 로컬은 권한 있는 회사 Google 계정,
운영 작업은 기존 서비스 계정을 사용한다. Secret Manager 접근·버전 추가,
상태 저장소 접근과 운영 작업 실행에 필요한 권한은 회사 IAM으로 관리한다.
서비스 계정 키 파일·액세스 토큰·OAuth refresh token을 저장소에 넣지 않는다.
