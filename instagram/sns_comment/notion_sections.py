"""Locate one owned daily section while preserving its heading or toggle."""

import re

from .api import PilotError


def locate(content, title='SNS Comments'):
    from .notion_mcp import normalized_markdown, root_matches
    markers = root_matches(re.compile(
        r'(?m)^(?:#{1,3}[ \t]+[^\n]+|<h[123](?:\s[^>]*)?>[\s\S]*?</h[123]>|<toggle(?:\s[^>]*)?>\n)'), content)
    candidates = []
    for marker in markers:
        opening = marker.group()
        if opening.startswith('<toggle'):
            first_end = content.find('\n', marker.end())
            if first_end < 0:
                continue
            name = content[marker.end():first_end]
            if normalized_markdown(name) != title:
                continue
            depth, fence, offset, closing = 1, None, first_end+1, None
            for line in content[offset:].splitlines(keepends=True):
                code = re.match(r'\s*(`{3,}|~{3,})', line)
                if code:
                    value = code.group(1)
                    if fence is None:
                        fence = value
                    elif value[0] == fence[0] and len(value) >= len(fence):
                        fence = None
                elif fence is None:
                    for tag in re.finditer(r'</?toggle(?:\s[^>]*)?>', line):
                        depth += -1 if tag.group().startswith('</') else 1
                        if depth == 0:
                            closing = offset+tag.start(), offset+tag.end()
                            break
                if closing:
                    break
                offset += len(line)
            if not closing:
                raise PilotError('SNS Comments 토글의 끝을 확인하지 못했습니다.')
            candidates.append({'start': marker.start(), 'body_start': first_end+1,
                'body_end': closing[0], 'end': closing[1], 'kind': 'toggle'})
        else:
            name = re.sub(r'^#{1,3}[ \t]+|^<h[123](?:\s[^>]*)?>|</h[123]>$', '', opening)
            if normalized_markdown(name) != title:
                continue
            end = len(content)
            for following in markers:
                if following.start() <= marker.start():
                    continue
                if (not following.group().startswith('<toggle')
                        or re.search(r'\bheading=["\']h[123]["\']', following.group())):
                    end = following.start()
                    break
            # Keep a root divider and following unrelated work outside the edit.
            dividers = root_matches(re.compile(r'(?m)^---[ \t]*$'), content)
            end = min([end, *[d.start() for d in dividers if marker.end() < d.start() < end]])
            candidates.append({'start': marker.start(), 'body_start': marker.end(),
                'body_end': end, 'end': end, 'kind': 'heading'})
    if len(candidates) != 1:
        raise PilotError('일일 업무의 SNS Comments 제목이 없거나 중복입니다.')
    return candidates[0]


def replace_body(content, area, replacement):
    old = content[area['start']:area['end']]
    if area['kind'] == 'toggle':
        # The section itself is an ordinary heading; only regional report
        # items fold. Accept old outer toggles as input and repair that wrapper.
        new = '### SNS Comments\n' + replacement.rstrip() + '\n\n'
    else:
        new = (content[area['start']:area['body_start']] + replacement
               + content[area['body_end']:area['end']])
    return old, new
