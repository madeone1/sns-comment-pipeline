"""Render the adopted regional layout without touching other daily work."""

import re

from .api import PilotError


LABELS = {'waterbomb_official': '대표(공식)', 'waterbomb_busan_official': '부산',
          'waterbomb_sokcho_official': '속초'}
ARCHIVE='이전 수집 대상 · 이력 보존'


def preserve_evidence(previous, current):
    """Keep cited evidence when a regenerated narrative chooses other examples."""
    from .notion_mcp import normalized_markdown
    present = set(re.findall(r'댓글 ID\s+(\d+)', normalized_markdown(current)))
    kept = []
    paragraphs = re.split(r'\n\s*\n', previous)
    for i, paragraph in enumerate(paragraphs):
        ids = set(re.findall(r'댓글 ID\s+(\d+)', normalized_markdown(paragraph)))
        if ids - present:
            paragraph = re.sub(r'(?:\n</(?:toggle|callout)>)+\s*$', '', paragraph)
            quote = paragraphs[i-1] if i and (paragraphs[i-1].startswith('> ') or paragraphs[i-1].startswith('<quote>')) else ''
            # Evidence paragraphs contain no container boundaries. Unknown shapes
            # must be inspected, never copied as malformed Markdown.
            if re.search(r'</?(?:toggle|callout|page|database)\b', paragraph):
                raise PilotError('이전 보고의 근거 경계를 확인하지 못해 보존 갱신을 보류합니다.')
            kept.append((quote+'\n\n' if quote else '')+paragraph)
            present.update(ids)
    if not kept:
        return current
    position = current.rfind('</toggle>\n</toggle>')
    if position < 0:
        raise PilotError('근거 댓글을 보존할 상세 위치가 없습니다.')
    return (current[:position]+'\n\n<toggle>\n이전 보고에서 보존한 근거 댓글\n\n'+
            '\n\n'.join(kept)+'\n</toggle>\n'+current[position:])


def containers(text, kind):
    from .notion_mcp import root_matches
    starts = root_matches(re.compile(r'<' + kind + r'(?:\s[^>]*)?>\n'), text)
    result = []
    for start in starts:
        depth = 1
        for tag in re.finditer(r'</?' + kind + r'(?:\s[^>]*)?>', text[start.end():]):
            depth += -1 if tag.group().startswith('</') else 1
            if depth == 0:
                end = start.end() + tag.end()
                title, _, inner = text[start.end():start.end()+tag.start()].partition('\n')
                result.append((start.start(), end, title, inner.strip()))
                break
        else:
            raise PilotError('지역 보고의 토글·콜아웃 경계를 확인하지 못했습니다.')
    return result


def region(name, content, label=None):
    from .notion_mcp import literal, root_matches
    # Earlier workflow output already folded the whole report once.
    toggles = containers(content, 'toggle')
    if len(toggles) == 1 and content.strip().startswith('<toggle>') and ' · 새 댓글 ' in toggles[0][2]:
        title, content = toggles[0][2:]
    else:
        title = ''
    count = re.search(r'댓글·답글 (\d+)건', content) or re.search(r'새 댓글 (\d+)건', title)
    if not count:
        raise PilotError('지역 보고의 일일 댓글 수를 확인하지 못했습니다.')
    bullets = root_matches(re.compile(r'- [^\n]+'), content)
    summary = [m.group()[2:] for m in bullets[:3]]
    if not summary:
        raise PilotError('지역 보고의 요약을 확인하지 못했습니다.')
    detail = content
    for match in reversed(bullets[:3]):
        detail = detail[:match.start()] + detail[match.end():]
    if count.group(1) == '0':
        # Keep the previous generated statements in the folded evidence.
        detail += '\n\n' + '\n\n'.join('- ' + line for line in summary)
        summary = ['새 댓글 없음(현재 확인된 범위).']
    label = literal(label or LABELS.get(name, name))
    week = re.search(r'반응 분석 범위: 최근 7일[^\n]*댓글·답글 (\d+)건',content)
    week_line = ('\n\n7일 참고 · 댓글·답글 '+week.group(1)+'건(일일 목록 포함).') if week else ''
    return ('<toggle>\n' + label + ' · 새 댓글 ' + count.group(1) + '건\n\n' +
            '\n\n'.join('- '+line for line in summary) + week_line + '\n\n<toggle>\n상세·근거 댓글\n\n' +
            detail.strip() + '\n</toggle>\n</toggle>')


def update(section, day, name, markdown, region_label=None, account_roster=None, connection_revision=None):
    """Migrate only owned callouts; then upsert one account, retaining others."""
    from .notion_mcp import literal, normalized_markdown
    title = f'인스타그램 댓글 자동 보고 · {day} 08시'
    regions, owned = {}, []
    for start, end, heading, inner in containers(section, 'callout'):
        plain = normalized_markdown(heading)
        if plain == title:
            if owned:
                raise PilotError('같은 날짜의 지역 보고가 중복되어 갱신을 보류합니다.')
            owned.append((start, end))
            for _, _, region_title, body in containers(inner, 'toggle'):
                if region_title == '공통 수집 기록':
                    continue
                if region_title==ARCHIVE:
                    for _,_,archived_title,archived_body in containers(body,'toggle'):
                        match=re.search(r'수집 계정: @([a-z0-9_.]+)',normalized_markdown(archived_body))
                        if not match or match.group(1) in regions:raise PilotError('보존 지역의 계정이 중복되거나 불명확합니다.')
                        regions[match.group(1)]='<toggle>\n'+archived_title+'\n\n'+archived_body+'\n</toggle>'
                    continue
                match = re.search(r'수집 계정: @([a-z0-9_.]+)', normalized_markdown(body))
                if not match or match.group(1) in regions:
                    raise PilotError('지역 보고의 계정 구분을 확인하지 못했습니다.')
                regions[match.group(1)] = '<toggle>\n'+region_title+'\n\n'+body+'\n</toggle>'
        elif plain.startswith(title+' · @'):
            account = plain[len(title+' · @'):]
            if not re.fullmatch(r'[a-z0-9_.]+', account) or account in regions:
                raise PilotError('기존 자동 보고 계정이 중복되었습니다.')
            owned.append((start, end))
            regions[account] = region(account, inner)
    if any(re.search(r'<(?:page|database|folder)\b', section[a:b]) for a, b in owned):
        raise PilotError('자동 보고 안의 하위 콘텐츠를 보존하기 위해 갱신을 보류합니다.')
    replacement = region(name, markdown, region_label)
    regions[name] = preserve_evidence(regions.get(name, ''), replacement)
    archived=[]
    if account_roster is not None:
        active={a['username']:a for a in account_roster if a['active']}
        if name not in active:raise PilotError('현재 토큰의 수집 대상에 없는 계정 보고는 갱신하지 않습니다.')
        for account,entry in active.items():
            previous=regions.get(account,'')
            stale=connection_revision and ('연결 검증 ID: '+connection_revision) not in normalized_markdown(previous)
            if account==name or (previous and not stale):continue
            label=literal(entry['label'])
            old=('<toggle>\n이전 보고 · 현재 연결 갱신 확인 전\n\n'+previous+'\n</toggle>') if previous else ''
            regions[account]=('<toggle>\n'+label+' · 수집 확인 전\n\n- 새 연결의 수집·분석 반영 확인 대기.\n\n'+
                '수집 계정: @'+literal(account)+'\n\n'+('연결 검증 ID: '+connection_revision+'\n\n' if connection_revision else '')+old+'\n</toggle>')
        archived=[regions[key] for key in sorted(regions) if key not in active]
        regions={key:regions[key] for key in active}
    ordered = sorted(regions, key=lambda key: (list(LABELS).index(key) if key in LABELS else len(LABELS), key))
    summaries, warnings, labels = [], [], {}
    positives = []
    for account in ordered:
        _, _, heading, inner = containers(regions[account], 'toggle')[0]
        labels[account] = heading.rsplit(' · 새 댓글 ',1)[0].rsplit(' · 수집 확인 전',1)[0]
        lines = [m.group(1) for m in re.finditer(r'(?m)^- (.+)$', inner.split('<toggle>', 1)[0])]
        if re.search(r' · 새 댓글 [1-9]\d*건$',heading):
            positives.append((account, lines))
        if re.search(r'수집 범위 확인 대기|과거 게시물 대조 pending|수집 (?:partial|failed)|수집 실패|새 연결의 수집·분석 반영 확인 대기', inner):
            warnings.append(labels[account])
    if len(positives) == 1:
        account, lines = positives[0]
        summaries = [labels[account]+' · '+line for line in lines[:3]]
    elif positives:
        summaries = [labels[account]+' · '+lines[0] for account, lines in positives[:3]]
    else:
        summaries = (['새 연결의 일부 지역은 수집·분석 반영 확인 대기.']
            if any(' · 수집 확인 전' in containers(regions[key],'toggle')[0][2] for key in ordered)
            else ['현재 확인된 범위에서 새 댓글 없음.'])
    alert = ('\n\n수집 범위 확인 중: '+'·'.join(warnings)+'. 확인되지 않은 댓글·답글 범위가 있어 현재 건수는 잠정입니다.' if warnings else '')
    floors = [re.findall(r'게시물 (\d{4}-\d{2}-\d{2}) 이후', value) for value in regions.values()]
    scope = ('게시물 작성일 제한은 없습니다. ' if all('게시물 작성일 제한 없음' in value for value in regions.values()) else
             '게시물 수집 범위는 각 지역 상세의 실제 수집 기준을 확인해주세요. ')
    if all(floors) and len({at for values in floors for at in values})==1:
        scope = f'대상 게시물은 {floors[0][0]} 00:00 KST 이후에 작성된 게시물입니다. '
    record = ('<toggle>\n공통 수집 기록\n\n일일 목록은 전날 08:00 이상부터 당일 08:00 미만(KST)에 작성된 댓글입니다. '
              '7일 참고는 같은 종료 시각 직전 7일이며 일일 목록을 포함합니다. '+scope+
              '계정별 정확한 기간·분류·검토 목록·원본 링크·수집 상태는 각 지역의 상세에 보존합니다.\n</toggle>')
    report = ('<callout icon="📊" color="blue_bg">\n'+literal(title)+'\n\n'+
              '\n\n'.join('- '+line for line in summaries)+alert+'\n\n'+
              '\n\n'.join(regions[key] for key in ordered)+'\n\n'+record+
              ('\n\n<toggle>\n'+ARCHIVE+'\n\n'+'\n\n'.join(archived)+'\n</toggle>' if archived else '')+'\n</callout>')
    if owned:
        result = section
        for start, end in sorted(owned, reverse=True):
            result = result[:start] + (report if start == min(a for a, _ in owned) else '') + result[end:]
    else:
        placeholder = next((x for x in ('- <b>SNS Comments:</b>', '- **SNS Comments:**') if section.count(x)==1), None)
        result = section.replace(placeholder, report, 1) if placeholder else '\n\n'+report+section
    return result, report
