# OAuth 소수 사용자 예외 판단과 운영 조건

확인일: 2026-11-16 (Asia/Seoul). 앱: MadeOne Comment Validation.

## 현재 운영 판단

현재 확인한 사용 방식은 Google의 Personal Use / 소수 사용자 예외 조건에 부합하는 것으로 판단한다. Google의 별도 면제 승인이나 민감 범위 인증을 받은 상태를 뜻하지 않는다. 이 예외에는 별도의 면제 등록·승인 절차가 안내되어 있지 않으며, 미인증 앱 상태로 사용한다.

- Google Cloud 대상 화면: External, In production, 누적 OAuth 사용자 2명 / 한도 100명.
- 사용 계정은 이 작업에서 직접 연결한 프로젝트 담당자 및 WATERBOMB 공식 채널 소유 계정이다. 소비자 Google 계정인 채널 소유자가 있어 Internal로 바꾸지 않는다.
- OAuth 클라이언트는 전용 웹 클라이언트 하나다. 인증 인터페이스는 127.0.0.1에서만 실행하며 외부 방문자를 위한 Google 로그인·가입 경로를 제공하지 않는다.
- 채널 소유 검증 후 정기 수집 대상은 WATERBOMB 공식 채널 UCSk-o7uCpprI1GNvmt070cQ 하나로 제한한다.
- YouTube API 전송은 GET 조회만 허용한다. 공개 댓글·답글·소유자 검토 대기열을 수집하며, 영상 업로드나 원본 댓글 변경은 수행하지 않는다.
- Google Sheets에는 전용 실행 계정으로 쓴다. 사용자 선택에 따라 기존 madeone.kr 도메인 공유를 유지한다. 시트 조회를 위해 앱에 OAuth 동의를 새로 받을 필요는 없다. 댓글 작성자 수·댓글 행 수는 OAuth 승인 사용자 수가 아니다.

## 유지하는 설정과 처리

External / In production, 기존 단일 youtube.force-ssl 범위, 채널 소유 검증, 전용 Secret 버전, Cloud Run 및 기존 매시 30분 예약을 유지한다. 승인된 브랜딩과 공개 개인정보처리방침도 유지한다. Testing으로 되돌리거나 새 OAuth 권한을 요청하지 않는다. Google 경고 화면을 숨기거나 우회하는 기능을 추가하지 않는다.

민감 범위는 미인증이므로 재연결 또는 새 사용자 동의 시 Google의 미인증 경고가 표시될 수 있고 누적 사용자 한도 100명은 유지된다. 현재 소수 사용자 예외의 운영 경계는 100명 미만이며, 새 연결은 운영자가 알고 있는 담당자로 한정한다. 이 예외를 공개 가입 서비스의 면제로 적용하지 않는다.

Testing 상태의 외부 앱에 적용되는 7일 갱신 토큰 제한은 현재의 In production 상태와 구분한다. 현재 연결은 Production 전환 후 새로 승인한 동일 채널 grant를 사용한다. 갱신 토큰이 영구적으로 유효하다는 보장은 없으며 철회·만료·보안 정책 등에 따라 재인증이 필요할 수 있다.

영상 업로드는 수행하지 않았고 심사 작성 화면을 종료했다. 민감 범위 인증을 받는 경로를 다시 선택하는 경우에는 별도의 데모와 심사 자료가 필요하다. 그 경로의 빈 데모 URL을 현재 수집 운영의 장애로 처리하지 않는다.

## 재검토 조건

일반 외부 사용자의 가입을 받거나, 운영자가 모르는 사용자에게 앱을 제공하거나, OAuth 사용자 수가 늘어 100명에 접근하거나, 다른 고객·채널에 서비스로 제공할 경우 예외 적용을 다시 판단한다. 다른 범위·권한·데이터 목적을 추가하는 경우에도 다시 검토한다. Google이 별도 검토를 요구하거나 차단하는 경우에는 해당 안내를 확인하고 대응하며 승인되지 않은 우회 경로를 만들지 않는다.

OAuth 심사 예외가 개인정보 보호 의무를 면제하지는 않는다. 21일 갱신·보유 정리, 검증된 삭제 요청, 재수집 차단, 연결 해제, 메모 보존, 제공자 복구본 및 별도 로컬·복구 사본 점검은 [PRIVACY.md](PRIVACY.md)대로 계속 적용한다.

## 판단 근거

- [Google OAuth 2.0 Policies](https://developers.google.com/identity/protocols/oauth2/policies): 혼자 사용하거나, 서로 아는 사용자 100명 미만이 사용하는 앱의 personal use 정의.
- [Sensitive scope verification의 Exceptions](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification#exceptions): 본인 또는 소수의 알려진 사용자에 대한 심사 예외.
- [When is verification not needed](https://support.google.com/cloud/answer/13464323?hl=en): 소수 사용 앱의 미인증 운영 및 경고·사용자 한도.
- [OAuth app state overview](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview): 게시 상태와 검증 상태의 구분.
- [OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration): Testing 7일 제한과 기타 만료·철회 사유.

이는 공식 정책과 실제 앱 구성을 대조한 운영 판단이다. Google 심사팀의 개별 승인 결과로 표기하지 않는다.
