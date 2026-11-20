# YouTube 전용 클라우드 배포 승인 범위

프로젝트: `waterbomb-sns-pipeline` / 리전: `asia-northeast3`

대상 시트: https://docs.google.com/spreadsheets/d/11fNxuDWtf3hz9rmNtgeop4YUdR16jCP-ydNn4Mx8M1Q/edit

소유자: `syan@madeone.kr`. 이 문서 작성 시 클라우드 리소스 생성과 IAM 변경은 승인 대기이며 실행하지 않았다.

| 새 대상 | 접근 주체 | 권한 / 목적 |
|---|---|---|
| 서비스 계정 `youtube-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com` | 계정 자신 및 `syan@madeone.kr` | 이 서비스 계정에 한정한 `roles/iam.serviceAccountTokenCreator`. Sheets 범위 토큰 발급 및 현장 검증 |
| 비공개 버킷 `waterbomb-sns-pipeline-youtube` | 위 실행 계정 | 이 버킷에 한정한 `roles/storage.objectAdmin`. 원문·진행 기록·동시 실행 잠금 보존 |
| Secret `youtube-comment-oauth` | 위 실행 계정 | 이 Secret에 한정한 `roles/secretmanager.secretAccessor`. 사용자가 인증한 워터밤 채널 OAuth 자격 증명 |
| 위 새 스프레드시트 | 위 실행 계정 | `writer`. 댓글 ID 기준으로 행 갱신 |
| Cloud Run Job `youtube-comments-daily` | 새 예약 계정 `youtube-scheduler@waterbomb-sns-pipeline.iam.gserviceaccount.com` | 이 Job에 한정한 `roles/run.invoker` |

새 실행 계정과 예약 계정을 생성한다. 버킷은 uniform access 및 public access prevention을 적용한다. 프로젝트 전체 권한이나 공개 공유는 추가하지 않는다.

기존 `sns-build` 빌드 계정과 빌드용 저장소를 이용해 별도 `containers/youtube-comments` 이미지를 생성한다. 새 Cloud Run Job과 동일 이름의 새 Cloud Scheduler를 만든다. 매일 08:30 KST 전체 수집을 시작하며 미완료 건만 매시 30분 이어서 수집한다. 1회 최대 2,000요청 / 태평양 일자 기준 8,000요청 예산을 둔다.

기존 `sns-comments-daily`, `notion-daily-merge` 작업 및 예약은 수정하지 않고 배포 전후 설정 해시를 대조한다.

초기 검증: 35,199개(원댓글 31,451 / 대댓글 3,748), 검토 대기 79개. 상태 3종과 전체 답글 페이지 순회를 완료했으며 1,826 API 요청이 사용되었다. 신규 코드 테스트 33개 통과. 클라우드 실행 후 실제 시트 반영과 재실행 중복 여부를 추가 검증한다.

OAuth 앱은 현재 외부 테스트 상태로 갱신 토큰의 7일 제한이 있다. 장기 무인 운영을 위해 실제 홈페이지·개인정보처리방침 등 Branding 설정을 정리하고 운영 게시·재인증이 필요하다.
