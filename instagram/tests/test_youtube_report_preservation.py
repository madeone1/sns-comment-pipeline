import unittest

from sns_comment.api import PilotError
from sns_comment.notion_moderation import layout_updates, toggle_markdown


DAY = '2026-11-20'
HIDDEN = {'total': 0, 'by_account': {'서울': 0, '부산': 0, '속초': 0, '토론토': 0},
          'start': '2026-11-19', 'end': '2026-11-19'}
YOUTUBE = '<toggle>\n🎬 유튜브 댓글\n\n작성 댓글 12건\n\n[댓글 원본 보기 (구글시트)](https://docs.google.com/spreadsheets/d/youtube-sheet/edit)\n</toggle>'


class SharedCommentReportTests(unittest.TestCase):
    def test_instagram_rewrite_preserves_youtube_counts_sheet_and_nested_content(self):
        section = YOUTUBE.replace('작성 댓글 12건', '작성 댓글 12건\n\n<toggle>\n유튜브 메모\n\n보존할 메모\n</toggle>')
        original = toggle_markdown(DAY, HIDDEN, 0)
        original = original[:-len('</toggle>')] + section + '\n</toggle>'
        page = '### Develop\n\n' + original + '\n\n### Info\n다른 내용'
        changes = layout_updates(page, DAY, HIDDEN, 2, {}, replace_existing=True)
        self.assertEqual(len(changes), 1)
        self.assertIn(section, changes[0]['new_str'])
        updated = page.replace(changes[0]['old_str'], changes[0]['new_str'], 1)
        self.assertIn('사람 판단 필요 2건', updated)
        self.assertIn('### Info\n다른 내용', updated)
        self.assertEqual(layout_updates(updated, DAY, HIDDEN, 2, {}, replace_existing=True), [])

    def test_duplicate_youtube_sections_stop_a_destructive_rewrite(self):
        original = toggle_markdown(DAY, HIDDEN, 0)
        original = original[:-len('</toggle>')] + YOUTUBE + '\n' + YOUTUBE + '\n</toggle>'
        with self.assertRaises(PilotError):
            layout_updates('### Develop\n\n' + original, DAY, HIDDEN, 1, {}, replace_existing=True)


if __name__ == '__main__':
    unittest.main()
