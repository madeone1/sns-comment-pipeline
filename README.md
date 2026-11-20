# SNS Comment Pipeline

MadeOne의 Instagram·YouTube 댓글 파이프라인을 함께 보관하는 비공개 저장소다.
플랫폼마다 코드·테스트·의존성·실행 파일·운영 문서를 별도 폴더에서 관리한다.

| 폴더 | 내용 | 시작 문서 |
| --- | --- | --- |
| `instagram/` | Instagram 댓글 수집·분류·숨김, 사람 판단, Notion 요약 | [Instagram 안내](instagram/README.md) |
| `youtube/` | YouTube 댓글·답글 수집, 중단 후 재개, Google Sheets 동기화, 저장본 보유·삭제 | [YouTube 안내](youtube/README.md) |

```text
instagram/
  sns_comment/       # 기존 Instagram Python 패키지
  tests/             # Instagram 자동 검증
  scripts/           # Instagram 배포·진단 도구
  config.json        # 인증값 없는 운영 설정
  Dockerfile*        # 댓글 관리·웹훅 컨테이너
  change-access-token.*
youtube/
  youtube_validation/ # 기존 YouTube Python 패키지
  tests/              # YouTube 자동 검증
  requirements*.txt   # YouTube 전용 의존성
  Dockerfile
  launch.command
```

실행 전에 해당 플랫폼 폴더로 이동한다. Python 패키지 이름은 기존 클라우드와
동일하게 `sns_comment`, `youtube_validation`을 유지한다.

```sh
# Instagram: Python 표준 라이브러리 사용
cd instagram
python3 -m unittest discover -s tests -q
./change-access-token.command --help
```

```sh
# YouTube: 별도 가상환경 사용
cd youtube
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m unittest discover -s tests -q
```

Windows Instagram 실행 파일은 `instagram/change-access-token.bat`이다.
배포 도구도 각 플랫폼 폴더에서 실행한다. 폴더 정리는 소스 저장 구조 변경이며,
운영 중인 Cloud Run 작업·예약·Secret·시트는 이 커밋으로 변경하지 않는다.

## 인증정보와 실행 데이터

토큰 원문, OAuth 클라이언트·사용자 인증 JSON, 서비스 계정 키, `.env`,
댓글 원본, SQLite DB, 실행 로그, 가상환경, 개인 검증 자료는 커밋하지 않는다.
실제 인증정보는 Secret Manager 또는 로컬 비공개 저장소에서 별도로 준비한다.
설정에 있는 프로젝트·채널·시트 ID와 Secret 이름은 인증정보가 아니다.

새 체크아웃의 로컬 상태는 `instagram/.local/`, `youtube/.local/`로 분리된다.
기존 컴퓨터의 인증정보와 수집 데이터는 자동 이동하거나 복사하지 않는다.
GitHub Actions는 플랫폼별 자동 검증만 수행하며 클라우드 배포를 실행하지 않는다.
