#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 -m sns_comment.token_cli "$@"
result=$?
if [[ -t 0 ]]; then
  read '?Enter를 누르면 창을 닫습니다. '
fi
exit "$result"
