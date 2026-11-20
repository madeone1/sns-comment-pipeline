# 워터밤 YouTube 댓글 수집 운영

대상: `WATERBOMB 워터밤` / `@waterbomb_official` / `UCSk-o7uCpprI1GNvmt070cQ`.

결과 시트: [워터밤 공식 YouTube 댓글 · 수집 파이프라인](https://docs.google.com/spreadsheets/d/11fNxuDWtf3hz9rmNtgeop4YUdR16jCP-ydNn4Mx8M1Q/edit).
소유자는 `syan@madeone.kr`이며 파이프라인은 전용 실행 계정에 편집 권한을 부여한다.
시트 소유자가 관리하는 다른 공유 설정은 배포 도구에서 변경하지 않는다.

## 수집과 보존

`pipeline.py`가 `published`, `heldForReview`, `likelySpam` 스레드를 교대로 끝까지 조회한다.
답글이 있는 원댓글은 `comments.list(parentId=...)`의 마지막 페이지까지 별도로 조회한다.
스레드 응답에 포함된 답글만으로 전체 답글 수집을 완료했다고 처리하지 않는다.

페이지의 원문·댓글 ID·다음 페이지 커서를 SQLite 트랜잭션으로 함께 저장한다.
오류·예산 소진·실행 중단은 미완료로 남기며 다음 실행이 같은 전체 순회를 이어간다.
20페이지마다 전용 버킷에 DB 체크포인트와 인증 헤더 없는 성공 API 응답을 보존한다.
버킷 세대 번호와 실행 잠금으로 중복 실행의 상태 덮어쓰기를 막는다.

채널 소유와 핸들의 일치 확인 후 수집하며 YouTube 전송기는 GET만 허용한다.
댓글·검토 상태 수정, 답글 작성, 삭제는 구현하지 않는다. Google Sheets 쓰기 권한은
채널 OAuth와 분리된 Cloud 실행 계정의 토큰으로 사용한다.

삭제·숨김 댓글 복원, 라이브 채팅은 범위 밖이다. 과거 댓글이 이번에 미관측되어도
저장 이력을 삭제하지 않는다. 수집 중 변동 가능성이 있어 단일 시점의 원자적 스냅샷은 아니다.
API가 개별 검토 상태를 제공하지 않은 대댓글은 `상태 미제공`으로 남긴다.
스레드 조회 상태 이력을 개별 댓글의 현재 상태로 단정하지 않는다.

## 시트 구조

| 탭 | 내용 |
|---|---|
| 수집 현황 | 전체·원댓글·대댓글 수, 검토 대기, 상태 미제공, 상태별 페이지 완료, 인증·예약 상태 |
| 댓글 | A:R에 댓글 ID·상위 ID·원문·작성자·KST 작성일·좋아요·검토 상태·영상/댓글 링크·관측 이력 |
| 수집 이력 | 최근 200회 실행의 상태·요청 수·신규 ID·전체 순회 관측 수·오류 |

댓글 ID를 기준으로 명시적인 행 주소에 `RAW` 값을 갱신한다. 원문이 `=`로 시작해도
수식으로 실행하지 않는다. 같은 ID는 재실행해도 추가하지 않는다. 사용자가 정렬하면
다음 실행에서 ID열을 다시 읽어 새 행 위치를 찾는다. 중복 ID가 있으면 덮어쓰기를 중단한다.
날짜는 실제 날짜 숫자로 저장하고 KST 형식으로 표시한다.

`댓글`의 A:R과 다른 두 운영 탭은 파이프라인 소유 범위다. 메모는 `댓글`의 S열 이후에
추가할 수 있다. 행·열 헤더를 바꾸거나 댓글 ID를 편집하면 안전 확인에서 실행이 중단된다.
현재 생성된 시트의 행 높이와 열 너비는 원문을 빠르게 훑도록 고정되어 있다.
긴 원문 전체는 셀을 선택하거나 확장해 확인할 수 있다.

## 독립 클라우드 리소스

프로젝트 `waterbomb-sns-pipeline`, 리전 `asia-northeast3`.

- Cloud Run Job / Scheduler: `youtube-comments-daily`
- 실행 계정: `youtube-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com`
- 예약 계정: `youtube-scheduler@waterbomb-sns-pipeline.iam.gserviceaccount.com`
- 비공개 버킷: `waterbomb-sns-pipeline-youtube`
- OAuth Secret: `youtube-comment-oauth`, 이미지에서 제외하고 지정 버전만 파일로 마운트
- 컨테이너: `containers/youtube-comments:<release timestamp>`

기존 `sns-build` 계정·빌드용 버킷·Artifact Registry 저장소는 빌드에만 함께 사용한다.
기존 두 Job과 Scheduler의 코드·이미지·예약·인증정보는 수정하지 않는다.
`release verify-legacy`는 배포 전후 기존 설정 해시를 대조한다.

예약은 매시 30분(`30 * * * *`, Asia/Seoul)에 새 Job을 호출한다.
작업 내부에서 매일 08:30 이후 첫 호출만 새 전체 순회를 시작한다.
미완료 순회는 이후 호출에서 계속하며, 이미 끝난 날의 호출은 YouTube API를 호출하지 않는다.
이 경우에도 시트 상태를 대조해 직전 시트 전송 실패를 복구한다. 바뀌지 않은 댓글 행은 다시 쓰지 않는다.
API GET은 실행당 2,000회, 태평양 일자당 8,000회, 수집 시간은 2,400초로 제한한다.
이 예산은 이 파이프라인의 요청 수이며 같은 프로젝트의 다른 YouTube 호출과 공유되는
실제 Google 할당량 잔여량을 보장하지 않는다. 제한 도달 시 다음 실행에서 재개한다.

## 재실행과 인증 갱신

```sh
# 로컬 전체 수집: 진행 중 순회가 있으면 재개
.venv/bin/python -m youtube_validation.pipeline_run

# Cloud Run 일회 실행: 완료된 날이면 API 수집 없이 종료
.venv/bin/python -m youtube_validation.release execute

# 명시적 검증을 위한 일회 전체 재수집 (예약 Job 설정은 바꾸지 않음)
.venv/bin/python -m youtube_validation.release execute --force-cycle

# 기존 파이프라인 설정 보존 확인
.venv/bin/python -m youtube_validation.release verify-legacy
```

2026-11-16 Google Cloud 콘솔에서 외부 OAuth 앱의 게시 상태를 **프로덕션 단계**로 확인했다.
동일 날짜 홈페이지 소유권 확인과 브랜딩 재인증·게시를 완료했다.
민감한 범위의 데이터 액세스 검증은 별도 절차이며 아직 완료되지 않았다.
이전 Testing 상태에서 발급한 토큰의 만료 조건이 게시 전환만으로 바뀌었다고 가정하지 않는다.
장기 무인 운영 전 홈페이지·개인정보처리방침과 검증 요건을 정리하고,
필요한 경우 채널 소유 계정으로 다시 인증해야 한다. 준비 현황은 [OAUTH-VERIFICATION.md](OAUTH-VERIFICATION.md)를 참조한다.
앱의 OAuth 게시 상태는 프로젝트 공통이므로 기존 서비스 영향도 함께 확인한다.

재인증은 `python -m youtube_validation auth`의 로컬 화면에서 사용자가 직접 진행한다.
인증 코드·토큰을 채팅이나 명령 인수로 전달하지 않는다. 새 토큰을 Cloud에 적용할 때는
`release credentials`로 새 Secret 버전을 만들고 `release deploy`로 기존 성공 이미지에
그 버전을 마운트한다. 코드 변경에 따른 새 이미지 배포는 `release prepare`,
`release build`, `release deploy` 순서로 진행한다. Secret의 `latest` 별칭을 자동 추종하지 않는다.
기존 버킷의 상태에 `release seed`를 다시 실행해 덮어쓰지 않는다.

## 확인

```sh
.venv/bin/python -m unittest discover -s tests -q
python3 -m unittest discover -s tests -q
```

실행 상태는 Cloud Run 실행의 종료 결과와 버킷 `state/latest.json`을 함께 확인한다.
API 실패는 0건 성공으로 취급하지 않으며, 독립 Sheets 접근이 가능하면 오류 상태도 시트에 반영한다.
OAuth 실패가 지속될 때는 `수집 현황` 탭의 최근 갱신·전체 순회 완료 여부와 인증 상태를 확인한다.
