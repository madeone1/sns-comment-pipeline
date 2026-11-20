#!/bin/zsh
set -eu
task_dir="${0:A:h}"
cd "$task_dir"
if [[ ! -x "$task_dir/.venv/bin/python" ]]; then
  print 'YouTube 검증 전용 가상환경 설치가 필요합니다. README.md를 확인해주세요.'
  exit 1
fi
exec "$task_dir/.venv/bin/python" -m youtube_validation auth
