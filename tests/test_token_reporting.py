import unittest

from sns_comment.notion_mcp import NotionMcpClient,normalized_markdown
from sns_comment.regional_notion import containers,ARCHIVE
import test_notion_mcp as fixtures
from test_notion_mcp import FakeMcp,SOURCE


class TokenReportTests(unittest.TestCase):
    def setUp(self):
        self.api=FakeMcp();self.helper=fixtures.McpReportTests()
        self.names=['waterbomb_official','waterbomb_busan_official','waterbomb_sokcho_official','new_d','new_e']
        self.roster=[{'id':str(10001+i),'username':name,'label':label,'active':i in (0,3,4)}
            for i,(name,label) in enumerate(zip(self.names,['대표(공식)','부산','속초','대구','제주']))]
        for name in self.names[:3]:
            NotionMcpClient(self.api,SOURCE,username=name,regional=True).publish('2026-11-05',self.helper.regional_sections(name,1,'이전 '+name))
    def publish(self,index,count=1):
        account=self.roster[index]
        return NotionMcpClient(self.api,SOURCE,username=account['username'],regional=True,region_label=account['label'],
            account_roster=self.roster,connection_revision='new').publish('2026-11-05',
                self.helper.regional_sections(account['username'],count,'현재 '+account['username']))
    def report(self):return containers(self.api.content,'callout')[0][3]

    def test_abc_to_ade_archives_old_regions_and_waits_for_uncollected_regions(self):
        self.publish(0,0);report=self.report();root=containers(report,'toggle')
        self.assertIn('대구 · 수집 확인 전',[x[2] for x in root]);self.assertIn('제주 · 수집 확인 전',[x[2] for x in root])
        archived=next(x[3] for x in root if x[2]==ARCHIVE)
        self.assertIn('이전 waterbomb_busan_official',normalized_markdown(archived));self.assertIn('이전 waterbomb_sokcho_official',normalized_markdown(archived))
        outer=report.split('<toggle>',1)[0]
        self.assertNotIn('이전 waterbomb_busan_official',outer);self.assertIn('확인 대기',outer)
        self.publish(3);self.publish(4);report=self.report();root=containers(report,'toggle')
        self.assertIn('대구 · 새 댓글 1건',[x[2] for x in root]);self.assertIn('제주 · 새 댓글 1건',[x[2] for x in root])
        self.assertTrue(self.publish(4)['unchanged'])
        self.assertIn('Keep this note.',self.api.content);self.assertIn('Keep child</page>',self.api.content)

    def test_previous_active_region_with_an_old_revision_is_folded_until_refreshed(self):
        self.publish(3)
        root=containers(self.report(),'toggle')
        pending=next(x[3] for x in root if x[2]=='대표(공식) · 수집 확인 전')
        self.assertIn('이전 waterbomb_official',normalized_markdown(pending))
        self.assertNotIn('이전 waterbomb_official',self.report().split('<toggle>',1)[0])

    def test_excluded_account_cannot_publish_into_the_current_report(self):
        from sns_comment.api import PilotError
        with self.assertRaisesRegex(PilotError,'수집 대상'):
            self.publish(1)


if __name__=='__main__':unittest.main()
