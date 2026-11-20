# Historical release tooling. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Release comment-time limits, with no publication-date lower bound."""

import argparse
import json
import notion_regional_release as release


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','build','deploy'])
    args=parser.parse_args()
    settings=json.loads((release.ROOT/'.local/web-release/operational-config.json').read_text(encoding="utf-8"))
    assert settings['cloud'].get('post_history') is None
    assert settings['cloud']['comment_history']=='previous_month'
    release.RELEASE=release.ROOT/'.local/comment-frontier-release'
    getattr(release,args.action)()
