# 워터밤 YouTube 댓글 파이프라인

기존 Instagram 코드·설정·인증정보·Cloud Run·Scheduler·시트·Notion과 연결하지 않는
별도 수집 파이프라인이다. 새 YouTube 전용 시트·실행 계정·버킷·Secret·Job·Scheduler를 사용한다.
`youtube/` 안에 코드·테스트·의존성·운영 문서를 모으고 Python 가상환경도 별도로 둔다.
기존 `sns_comment` 패키지나 클라우드 배포 도구를 import하지 않는다.

전체 수집·중단 후 재개·시트 갱신의 운영 방법은 [PIPELINE.md](PIPELINE.md),
실제 검증 결과는 [VALIDATION.md](VALIDATION.md), 승인된 리소스와 권한은
[DEPLOYMENT-PLAN.md](DEPLOYMENT-PLAN.md)에 정리한다.

2026-11-16 현재 운영은 [OAuth 소수 사용자 예외 판단과 운영 조건](OAUTH-OPERATING-MODE.md)을 따른다.
External / In production을 유지하며 민감 범위는 미인증 상태로 소수의 알려진 운영자만 사용한다.
영상 업로드와 데이터 액세스 심사 제출은 진행하지 않는다.

검증 대상은 사용자가 지정한 워터밤 공식 채널이다. 공개 안내에서 확인한 핸들 후보는
`@waterbomb_official`이며, 실제 OAuth 인증 후 YouTube API로 핸들의 채널 ID를 조회하고
`channels.list(mine=true)` 결과와 일치하는지 확인한다. 이름만으로 계정을 연결하지 않는다.
대상 불일치·필수 권한 미승인·갱신 토큰 미발급 시 인증정보를 저장하지 않는다.

## 준비와 실행

아래 명령은 저장소의 `youtube/` 폴더에서 실행한다.
기존 환경이나 공용 Python 패키지를 변경하지 않는다.

```sh
cd youtube
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
```

Cloud Console에서 YouTube Data API v3 활성화 후 Google Auth Platform을 구성한다.
2026-11-11 읽기 전용 조사에서 `waterbomb-sns-pipeline`에는 API가 이미 활성화되어
있었으며, OAuth 앱 설정과 클라이언트는 없었다. 기존 작업은 `sns-comments-daily`,
`notion-daily-merge`이고 기존 예약은 각각 매일 08시·월요일 04시였다.
이것은 조사 시점의 상태이며, 추가 작업 전에 다시 확인해야 한다.

OAuth 앱의 대상 계정이 조직 밖이면 External을 사용하고, 테스트 상태에서는 채널 소유
Google 계정 이메일을 테스트 사용자로 추가한다. 앱 이름·대상·게시 상태는 프로젝트 공통이다.
기존 Google OAuth 설정이 발견되면 이를 덮어쓰지 말고 별도 인증용 프로젝트를 검토한다.

OAuth 클라이언트 유형은 **웹 애플리케이션**으로 만든다. 이번 초기 인증 웹앱은
이 컴퓨터의 loopback 주소에서만 실행된다. 승인된 리디렉션 URI는 다음 값과 정확히 맞춘다.

```text
http://127.0.0.1:8765/oauth/youtube/callback
```

필요한 권한은 `https://www.googleapis.com/auth/youtube.force-ssl`이다.
댓글 조회 API의 공식 인증 범위이며, Google 승인 화면에는 수정·삭제 권한도 표시된다.
이 도구의 API 전송기는 `channels`, `commentThreads`, `comments`에 대한 GET만 허용한다.
OAuth 승인과 갱신에 필요한 Google 토큰 요청 외에 댓글 쓰기 API는 구현하지 않는다.

콘솔에서 발급한 클라이언트 JSON 파일을 내려받은 뒤 다음과 같이 등록한다.
파일 내용이나 시크릿을 채팅·명령 인수에 붙여 넣지 않는다. 인수에는 파일 경로만 전달한다.

```sh
.venv/bin/python -m youtube_validation import-client /absolute/path/to/downloaded-client.json
.venv/bin/python -m youtube_validation auth
```

출력되는 로컬 인증 페이지를 열고 채널 소유 계정으로 승인한다.
`launch.command`로 인증 서버만 다시 실행할 수도 있다. OAuth 클라이언트 발급만으로
채널 인증이 완료되는 것은 아니다. 승인 후 채널 ID 일치 검사가 통과해야 연결 완료다.
서버는 상태값·PKCE·10분 유효기간·일회성 콜백을 사용하며 인증 코드·토큰·콜백 URL을
접근 로그에 기록하지 않는다. 다른 사이트의 요청과 다른 Host 헤더를 거부한다.

```sh
.venv/bin/python -m youtube_validation status
.venv/bin/python -m youtube_validation collect --pages-per-status 1 --max-requests 100
.venv/bin/python -m youtube_validation collect --max-requests 100
```

외부 앱이 Testing 상태일 때 YouTube 범위로 발급한 refresh token은 7일 후 만료된다.
장기 예약 실행을 검증하려면 게시 상태·검증 요건을 확인하고 운영 상태에서 다시 인증한다.
인증·표본 검증 명령은 Cloud Run이나 Scheduler를 생성하지 않는다.
새 파이프라인 배포는 승인된 전용 리소스만 대상으로 `youtube_validation/release.py`에서 수행한다.

## 검증 결과의 해석

`published`, `heldForReview`, `likelySpam`을 각각 조회하고 페이지를 끝까지 따라간다.
세 상태는 한 페이지씩 번갈아 조회하여 공개 댓글이 많은 경우에도 다른 상태의 권한 검증을 먼저 시도한다.
스레드 응답의 `replies`는 답글의 일부일 수 있으므로 답글이 있는 원댓글마다 `comments.list`
페이지를 별도 조회한다. 같은 댓글 ID는 한 번 저장하고, 여러 상태에서 관측된 검색 조건을
`thread_query_statuses`에 기록한다. 스레드가 검토 대기로 검색되었다는 이유만으로 그 안의
공개 원댓글 상태까지 검토 대기로 바꾸지 않는다. 개별 상태는 API 반환값을 사용한다.

권한 오류·API 미지원·통신 실패·호출 예산 소진·반복 페이지 토큰은 성공한 0건이 아니다.
상태별 `complete`와 `error`를 저장한다. 요청 범위가 끝나지 않았으면 종료 코드 2를 반환한다.
인증·대상 채널 검사가 실패하면 수집을 시작하지 않고 종료 코드 1을 반환한다.
위 `collect` 명령의 `--max-requests`는 단일 검증에서 프로그램이 시도하는 API GET 요청 수의 상한이며,
기본 100, 최대 1000이다. 실제 Google 할당량 잔여량이나 실댓글 총수는 뜻하지 않는다.
운영용 `pipeline_run`과 Cloud Run은 별도 체크포인트 수집기를 사용하며 기본 한도는
실행당 2,000요청, 태평양 일자당 8,000요청이다.

`complete=true`는 이번 실행의 API 열거와 답글 조회가 끝났다는 뜻이다. 삭제·숨김 댓글,
커뮤니티 게시물 댓글, 라이브 채팅, API가 반환하지 않는 데이터까지 전부 수집했다는 뜻이 아니다.
수집 중 댓글이 달라질 수 있으므로 전체 결과가 한 시점의 스냅샷이라는 보장도 없다.

`--pages-per-status 1`은 각 상태의 첫 페이지와 거기서 발견한 원댓글의 전체 답글을 확인하는
샘플 검증이다. `requested_scope_complete=true`는 이 요청 범위의 검증 완료를 뜻한다.
다음 페이지가 있으면 상태별 `limited=true`, 전체 이력 `complete=false`를 유지한다.
샘플 검증 성공과 전체 이력 수집 완료를 구분한다. 전체 결과는 파일에 보관하고 콘솔에는
대댓글 조회 성공·실패 건수를 집계해서 표시한다.

비공개 결과는 이 플랫폼 폴더 아래의 `.local/youtube-validation/`에 저장하며 Git에서 제외한다.
저장소 루트 기준으로는 `youtube/.local/youtube-validation/`이다. 기존 로컬 인증·데이터는 복사하지 않는다.

| 파일 | 내용 |
| --- | --- |
| `client.json` | 새 OAuth 클라이언트 설정 |
| `authorized-user.json` | 채널 일치 확인 후 저장한 갱신 인증정보 |
| `connection.json` | 연결된 채널 ID와 승인 범위 |
| `comments.sqlite3` | 채널·댓글 ID별 최신 상태와 실행별 관측 기록 |
| `runs/<run-id>/raw/` | 인증 헤더를 포함하지 않는 성공 API 응답 |
| `runs/<run-id>/comments.json` | 중복 제거한 이번 실행의 댓글 |
| `runs/<run-id>/summary.json` | 상태별 조회 결과·미완료 사유·수집 수 |
| `latest.json` | 최근 실행 요약 |
| `baseline-before.json`, `baseline-after.json` | 기존 클라우드 작업·예약 설정의 지문 |

디렉터리 권한은 0700, 인증정보·결과·DB 권한은 0600이다. 재실행은 같은 댓글을 중복 생성하지
않으며, 이후 조회에서 보이지 않는 기존 댓글도 삭제하지 않는다. 실행별 원본 관측을 별도로 보관한다.

## 검증 명령

```sh
.venv/bin/python -m unittest discover -s tests -q
python3 -m unittest discover -s tests -q
.venv/bin/python -m youtube_validation baseline --stage before
.venv/bin/python -m youtube_validation baseline --stage after
```

baseline은 기존 두 작업과 예약을 조회만 한다. 실행 상태처럼 정상 운영 중 바뀔 수 있는 값은
제외하고 작업 사양과 예약 설정의 SHA-256을 대조한다. 전체 설정 원문이나 기존 토큰을 출력하지 않는다.

## 공식 근거

- [OAuth 서버 인증](https://developers.google.com/identity/protocols/oauth2/web-server)
- [인증 토큰 만료 조건](https://developers.google.com/identity/protocols/oauth2#expiration)
- [댓글 상태별 조회](https://developers.google.com/youtube/v3/docs/commentThreads/list)
- [답글 전체 조회](https://developers.google.com/youtube/v3/guides/implementation/comments)
- [채널 핸들과 소유 채널 조회](https://developers.google.com/youtube/v3/docs/channels/list)
- [API 인증 범위 정의](https://github.com/googleapis/google-api-python-client/blob/main/googleapiclient/discovery_cache/documents/youtube.v3.json)
- [초대받은 Studio 사용자의 API 제한](https://support.google.com/youtube/answer/9481328?hl=ko)
- [공식 채널로 안내하는 워터밤 기존 영상](https://www.youtube.com/watch?v=OKUmVpRKVlE)

## 저장소 구성과 배포

- `youtube_validation/`: 운영 수집기, OAuth, 시트 동기화, 저장본 보유·삭제 도구.
- `tests/`: 합성 API와 임시 DB를 사용하는 자동 검증. 실제 인증이나 클라우드 쓰기는 하지 않는다.
- `requirements.lock.txt`: YouTube 전용 전체 의존성 고정 버전.
- `Dockerfile`, `.dockerignore`, `.gcloudignore`: 이 플랫폼의 소스와 인증값 없는 설정만 컨테이너 입력에 포함한다.
- `youtube_validation/pipeline_config.json`: 운영 시트 ID, 수집 예산, 예약 시각. OAuth 값은 포함하지 않는다.

로컬 가상환경과 OAuth 파일은 체크아웃 후 별도로 준비한다.
배포 명령은 이 폴더에서 `.venv/bin/python -m youtube_validation.release --help`로 확인한다.
Cloud Run의 기존 `python -m youtube_validation.worker` 진입점은 유지한다.
`Dockerfile`은 이 폴더를 빌드 컨텍스트로 사용한다.

`MODERATION.md`와 `moderation.py`는 오프라인 요청 계획 프로토타입이다.
실제 댓글 숨김·삭제 실행기는 포함하지 않으며 운영 수집기의 GET 전용 동작과 구분한다.
과거 검증·배포 문서의 날짜와 결과는 당시 기록이다.
