# YouTube OAuth 인증 준비

확인일: 2026-11-16, Asia/Seoul. 프로젝트: `waterbomb-sns-pipeline`.

## 실제 콘솔에서 확인한 상태

- 앱: MadeOne Comment Validation. 외부 / 프로덕션 단계.
- 브랜딩: **인증 및 게시 완료**. 직전 실패 사유인 `https://www.waterbombfestival.com/`의 소유권 미확인을 해결한 뒤 재인증을 통과하고 게시했다.
- 민감한 범위: `https://www.googleapis.com/auth/youtube.force-ssl` 1개. 제한된 범위 없음.
- 데이터 액세스 인증은 미완료이며, 브랜딩 선행 요건은 충족했다.
- 사용자 지원 및 개발자 연락처: `syan@madeone.kr`.
- 홈페이지, 개인정보처리방침, 약관 URL은 입력되어 있다.
- 권한 사용 사유 848자를 데이터 액세스 화면에 저장했고 저장 완료 알림을 확인했다.
- 시연 동영상 URL은 비어 있다.
- 2026-11-16 후속 작업에서 실제 영어 OAuth 화면·클라이언트 ID·공식 채널 소유 일치·GET 6회 조회 결과를 확보하여 **약 2분 1초의 영어 음성 데모 MP4**를 만들었다. 연속 녹화가 아니라 실제 핵심 화면으로 구성한 검토용 영상이며 아직 외부에 업로드하지 않았다.
- 브랜딩 게시 후 `Prepare for verification`이 활성화되어 실제 제출 준비 화면까지 열었다.
- 제출 화면에 **요청된 범위 중 하나 이상에 데모 동영상 필드가 누락되었습니다** 오류가 표시되어 `확인` 버튼이 비활성화되어 있다. 데이터 액세스 심사는 제출되지 않았다.

## 홈페이지 소유권

처음 점검 시 Search Console 계정 `syan@madeone.kr`에는 홈페이지의 검증된 속성이 없었다.
DNS 네임서버는 `ns10.wixdns.net`, `ns11.wixdns.net`이며 기존 Google 인증 TXT가 2개 있다.
Wix의 현재 계정에서는 도메인 관리 메뉴가 비활성화되어 있다.
Wix에 기존 Search Console 인증 태그가 있으므로 이를 교체하지 않는다.

사용자가 추가 태그 게시와 소유권 등록을 명시적으로 승인한 뒤,
Wix Head에 `Google Search Console verification - syan - OAuth`라는 별도 태그를 추가했다.
기존 Wix 사이트 인증 태그와 Google 인증 TXT는 변경하지 않았다.
URL 접두어 `https://www.waterbombfestival.com/`의 HTML 태그 인증을 실행하여
Search Console의 **소유권이 확인됨 / 확인 방법: HTML 태그** 결과를 확인했다.
태그·DNS 원문과 스크린샷은 Git 제외 `outputs/youtube-oauth-verification-2026-11-16/`에 보관한다.

브랜딩 문제 창에서 `문제를 해결함`을 선택하고 재인증을 요청했다.
자동 검토가 통과하여 `브랜딩이 인증되었지만 아직 사용자에게 표시되고 있지 않습니다`로 바뀌었다.
`브랜딩 게시`를 실행한 뒤 **브랜딩이 인증되었으며 사용자에게 표시되고 있습니다** 결과를 확인했다.
이 경우 소유권 반영을 위해 24시간 대기할 필요는 없었다.

## 제출 설명의 근거

- `auth.py`: `youtube.force-ssl` 1개를 요청하고 채널 소유 일치를 확인한 뒤 토큰을 저장한다.
- `api.py`: YouTube 전송기는 `channels`, `commentThreads`, `comments`에 대한 GET만 허용한다.
- `collector.py`, `pipeline.py`: 공개·검토 대기·스팸 의심 조회와 답글 페이지 순회를 수행한다.
- `google_cloud.py`, `sheet_sync.py`: Sheets 쓰기는 분리된 실행 계정을 사용한다.
- Google 공식 discovery 문서의 `commentThreads.list`, `comments.list`에는 `youtube.force-ssl`만 선언되어 있다.
- `moderation.py`는 쓰기 전송기가 없는 준비 코드다. 실제 댓글 삭제 기능이 있다고 심사 자료에 적지 않는다.

## 제출 전 보완

1. 앱 설명: 현 홈페이지는 축제 소개가 중심이다. 앱 이름과 댓글 조회 기능·Google 데이터 이용 목적을 설명하는 공개 페이지가 필요하다.
2. 개인정보처리방침: 현 `/privacy`는 웹사이트·JENA 중심이다. YouTube 조회 항목, 저장·공유, 철회·삭제 절차를 앱에 맞게 보완해야 한다.
3. 보유·삭제 동작: 현재 클라우드 메타데이터와 기존 로컬 사본을 점검했다. 원본 만료 규칙 부재, DB·시트 고아 행 정리 부재, 복구 보관본의 별도 처리 필요를 확인했다. 구체적인 결과와 적용 전 운영안은 [PRIVACY-RUNBOOK.md](PRIVACY-RUNBOOK.md)에 있다. 실제 운영 적용 전 준수 완료로 확약하지 않는다.
4. 시연 영상: 실제 화면과 실제 API 표본 결과를 사용한 검토용 MP4와 영어 자막·대본을 만들었다. 댓글 원문과 작성자 식별자는 제외했다. 최종 공개 정책·실제 보유 삭제 처리와 정상 시트 반영을 확인한 후 영상을 확정하고 일부 공개로 업로드해야 한다.
5. 브랜딩 인증·게시는 완료했다. 남은 자료와 데이터 처리 동작을 확인한 뒤 데이터 액세스 심사를 진행한다. 허위 완료 체크나 정책 동의의 대리 수락은 하지 않는다.

공개용 문구의 초안과 시연 구성은 `outputs/youtube-oauth-verification-2026-11-16/report.html`에 있다.
해당 초안은 미게시이며, 아직 구현·검증되지 않은 데이터 삭제 약속의 게시를 승인하는 자료가 아니다.

후속 준비 패키지: `outputs/youtube-oauth-submission-2026-11-16/report.html`.
데모: `outputs/youtube-oauth-submission-2026-11-16/demo-preview.mp4`.
새 로컬 검토 서버는 `review_demo.py`, 읽기 전용 점검기는 `privacy_audit.py`이며 전용 테스트 57개를 통과했다. 운영 이미지에는 배포하지 않았다.

현재 정기 실행에는 `duplicate_comment_ids_in_sheet` 실패도 있다. ID 열에서 864개 중복 그룹을 확인했으며 원문·사람 입력을 대조하지 않고 행을 제거하지 않았다. 운영 시트와 정기 예약을 변경하지 않았다.

## 완료 증빙

`outputs/youtube-oauth-verification-2026-11-16/`:

- `ownership-verified.jpg`: Search Console HTML 태그 소유권 확인 성공.
- `wix-tag-applied.jpg`: 기존 코드와 별도로 추가한 인증 태그.
- `branding-published.jpg`: 브랜딩 인증·게시 완료.
- `verification-center-after.jpg`: 브랜딩 완료 / 데이터 액세스 미인증 구분.
- `scope-reason-saved.jpg`: 권한 사유 저장 완료 알림.
- `status.json`: 최종 확인 상태.

## 공식 자료

- [브랜딩 인증](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification)
- [홈페이지 소유권 및 설명](https://support.google.com/cloud/answer/13807376?hl=en)
- [민감한 범위·시연 영상](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification)
- [공식 YouTube discovery](https://raw.githubusercontent.com/googleapis/google-api-python-client/main/googleapiclient/discovery_cache/documents/youtube.v3.json)
- [YouTube 데이터 보유·삭제 정책](https://developers.google.com/youtube/terms/developer-policies)
