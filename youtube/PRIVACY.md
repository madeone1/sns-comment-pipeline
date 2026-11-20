# YouTube 저장본 보유·삭제 운영 절차

2026-11-16 적용. 정기 수집기는 YouTube GET만 허용한다. 아래 삭제는 앱이 저장한 사본에 적용하며 YouTube 원본 댓글은 변경하지 않는다.

## 보유와 메모

전용 Cloud Run은 매시간 실행 시 수집 전에 개인정보 제어 파일을 확인한다. 댓글은 게시일이 아니라 마지막 API 확인 시점부터 21일이 지나면 만료한다. raw 응답과 처리 이력도 21일 기준이다. `raw/`에는 GCS 21일 lifecycle도 적용했다. 기존 7일 soft delete는 유지했다.

자동 만료는 댓글 탭 A:R의 값만 정리하고 S:Z 담당자 메모와 셀 주석은 유지한다. 재관측된 댓글은 같은 행에서 갱신한다. 만료 후 남은 메모의 행과 댓글·작성자 해시를 등록하여 나중에 본인 삭제 요청이 들어오면 그 메모까지 찾는다. 이 해시는 재수집 차단과 요청 처리에만 사용한다.

## 접수와 검증

담당자는 `syan@madeone.kr`로 요청을 받는다. 앱 이름, 채널/댓글 URL, 요청 시각, 요청 범위와 7일 처리 기한을 기록한다. 채널 소유자의 OAuth 소유 확인 또는 작성자 본인의 채널 통제 증거로 요청자와 대상을 확인한다. 비밀번호·인증 코드를 요청하거나 저장하지 않는다. 불분명한 요청은 파괴적 처리를 시작하지 않고 필요한 대상 확인을 진행한다.

검증한 요청 JSON의 `kind`는 `comment`, `author`, `all` 중 하나다. `targets`는 확인한 댓글 ID 또는 작성자 채널 ID 배열이며 `all`에는 필요 없다. `verified_by`는 `syan@madeone.kr`이다. 같은 요청의 재시도에는 같은 `request_id`를 쓴다. 원문 내용은 JSON에 포함하지 않는다.

```sh
.venv/bin/python -m youtube_validation.privacy_admin --request-file /private/path/verified-request.json
.venv/bin/python -m youtube_validation.privacy_admin --request-file /private/path/verified-request.json --apply-verified-request
```

첫 명령은 미리보기이며 두 번째 명령만 기존 프로젝트 IAM을 사용해 해시화한 대상을 GCS 제어 파일에 등록한다. CLI·로그에 토큰이나 댓글 본문을 출력하지 않는다. 실제 운영 요청은 이번 검증에서 생성하지 않았다.

## 실행과 확인

전용 작업을 실행하거나 다음 매시 30분 실행을 기다린다. 운영 DB는 Cloud Run 내부에서만 복원·정리·VACUUM한다. 댓글/관측/이력/캐시를 정리하고 관련 Sheet 값·메모·주석과 별도 처리 이력을 지운다. raw 파일은 여러 댓글이 섞인 응답이므로 요청 처리 시 raw 캐시 전체를 회차당 최대 500개씩 제거한다. 해당 댓글의 canonical 사본만 선택 삭제하며 다른 댓글의 canonical 사본은 유지한다. 미처리 raw 개수가 0이 될 때까지 정기 실행을 계속 확인한다.

`all`은 수집을 중단하고 시트의 연결되지 않은 잔존 행·메모까지 정리한 뒤 OAuth 토큰을 철회한다. Google OAuth의 영구 `invalid_grant`도 같은 경로로 처리한다. 일시적인 네트워크 오류는 철회로 간주하지 않는다. 재연결은 소유자가 다시 공식 OAuth를 완료하고 올바른 채널임을 확인한 뒤 담당자가 새 Secret 버전을 적용하고, 과거 all 요청을 별도 영수증으로 보존하면서 제어 파일의 수집 중단을 해제하는 수동 운영 절차다. 요청 확인 없이 자동 재연결하지 않는다.

`privacy/latest.json`, `state/latest.json`, Cloud Run 성공 여부, 대상 행 부재 및 raw 남은 개수 0을 확인한다. 재수집 차단과 메모 연결은 합성 데이터 테스트로 검증했다. 실제 고객 댓글을 테스트 목적으로 삭제하지 않았다. 최소 요청 영수증에는 ID 원문·댓글 내용 대신 요청 ID·시간·유형·처리 수만 남긴다.

## 복구본과 별도 복사본

GCS의 삭제된 객체는 7일 동안 복구 가능하며 이후 제공자가 자동 만료한다. Sheets 버전 이력 등 제공자 관리 복구본의 즉시 물리 삭제를 보장하지 않는다. 활성 저장본 정리와 제공자 복구 만료를 구분해 안내한다. 원문·토큰이 든 로컬 검증 디렉터리, raw 파일, 내보내기 및 복구용 복사본은 아래 목록을 함께 점검한다. 요청 범위에 해당하는 로컬 사본은 전용 작업을 정지한 뒤 처리하고, 원문이 남은 SQLite는 VACUUM 후 확인한다. 다른 SNS 데이터·인증정보는 건드리지 않는다.

- `.local/youtube-validation/`: 과거 로컬 수집 사본 및 인증정보. 운영 정본으로 복원하지 않는다.
- `.local/youtube-oauth-review-2026-11-16/demo/`: 로컬 소유 확인용 인증정보와 본문 없는 집계. 비공개 파일 권한 유지. 불필요한 인증정보는 담당자가 삭제한다.
- Drive 복구 보존본 `185JcxRBYo-Bi_D6QebOe0Tdkf878C9HvIF6d6VdNits`: 2026-11-16 중복 복구 전 전체 native 사본, syan 소유자 전용. 정기 수집 대상이 아니다. 원문·메모 보존 요청에 따라 이번 작업에서는 유지했다. 요청 처리·보유 점검 시 원본 시트와 함께 처리한다.
- Google Secret `youtube-comment-oauth`: 버전 2로 갱신. 연결 해제 시 토큰 철회 결과를 확인하고 더 이상 사용하는 버전이 없도록 접근/사용을 중단한다. Secret의 복구·파기 기한도 제공자 정책에 따라 안내한다.

## 공개 안내와 승인된 접근

앱 소개/이용 안내: https://www.waterbombfestival.com/comment-validation

개인정보처리방침: https://www.waterbombfestival.com/privacy 의 MadeOne Comment Validation 조항.

2026-11-16 사용자는 기존 madeone.kr 도메인 공유 유지를 명시적으로 선택했다. 기존 개별 공유와 전용 실행 계정도 유지했다. 복구 보존본은 소유자 전용이며 원본 시트의 도메인 공유가 복사되지 않았음을 확인했다.
