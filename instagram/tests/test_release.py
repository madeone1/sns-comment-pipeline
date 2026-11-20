"""A failed candidate must not become an operational worker deployment."""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import token_connection_release as release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.patch = mock.patch.object(release, 'RELEASE', self.folder)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        release.save('release', {'image': 'registry/worker:new'})
        release.save('build', {'id': 'build-1'})

    def test_candidate_failure_deletes_temporary_job_without_touching_worker(self):
        calls = []

        def gcloud(args, **kwargs):
            calls.append(args)
            if args[:2] == ['builds', 'describe']:
                return SimpleNamespace(stdout='SUCCESS')
            if args[:3] == ['run', 'jobs', 'execute']:
                raise SystemExit('candidate failed')
            return SimpleNamespace(stdout='')

        with mock.patch.object(release, 'gcloud', side_effect=gcloud):
            with self.assertRaisesRegex(SystemExit, 'candidate failed'):
                release.verify('2026-11-11', 'a' * 32)
        self.assertEqual(calls[-1][:3], ['run', 'jobs', 'delete'])
        self.assertFalse(any(c[:3] == ['run', 'jobs', 'update'] for c in calls))
        self.assertFalse((self.folder / 'candidate-result.json').exists())

    def test_proof_for_another_image_cannot_update_operational_job(self):
        release.save('candidate-result', {'status': 'verified', 'image': 'registry/worker:old'})
        with mock.patch.object(release, 'gcloud', return_value=SimpleNamespace(stdout='SUCCESS')) as cli:
            with self.assertRaisesRegex(SystemExit, '같은 후보 이미지'):
                release.deploy()
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(cli.call_args.args[0][:2], ['builds', 'describe'])

    def test_verification_uses_moderation_without_daily_report_identifiers(self):
        calls = []
        def gcloud(args, **kwargs):
            calls.append(args)
            if args[:2] == ['builds', 'describe']:
                return SimpleNamespace(stdout='SUCCESS')
            if args[:2] == ['storage', 'cat']:
                return SimpleNamespace(stdout='{"status":"verified","image":"registry/worker:new","accounts":[]}')
            return SimpleNamespace(stdout='')
        with mock.patch.object(release, 'gcloud', side_effect=gcloud):
            release.verify()
        created = next(c for c in calls if c[:3] == ['run', 'jobs', 'create'])
        self.assertIn('--args=-m,sns_comment.moderation_release_check', created)
        self.assertFalse(any('SNS_VERIFY_DAY' in arg or 'SNS_VERIFY_PAGE' in arg for arg in created))
        self.assertEqual(calls[-1][:3], ['run', 'jobs', 'delete'])

    def test_deploy_targets_only_active_jobs_and_preserves_env(self):
        release.save('candidate-result', {'status':'verified','image':'registry/worker:new'})
        calls=[]
        def gcloud(args, **kwargs):
            calls.append(args)
            if args[:3] == ['run', 'jobs', 'describe']:
                return SimpleNamespace(stdout='', returncode=1)
            return SimpleNamespace(stdout='SUCCESS' if args[:2]==['builds','describe'] else '', returncode=0)
        with mock.patch.object(release,'gcloud',side_effect=gcloud), mock.patch.object(release,'ensure_scheduler_invoker'):
            release.deploy()
        updates=[c for c in calls if c[:3]==['run','jobs','update']]
        self.assertEqual([c[3] for c in updates],['sns-comments-moderate','sns-comments-notion-summary'])
        self.assertTrue(all(not any(a.startswith('--set-env-vars') for a in c) for c in updates))
        self.assertIn('--task-timeout=1500s',updates[0])

    def test_deploy_updates_past_sweep_image_without_replacing_env(self):
        release.save('candidate-result', {'status': 'verified', 'image': 'registry/worker:new'})
        calls = []
        bound = []

        def gcloud(args, **kwargs):
            calls.append(args)
            if args[:2] == ['builds', 'describe']:
                return SimpleNamespace(stdout='SUCCESS', returncode=0)
            if args[:3] == ['run', 'jobs', 'describe']:
                return SimpleNamespace(stdout='sns-comments-past-sweep', returncode=0)
            return SimpleNamespace(stdout='', returncode=0)

        with mock.patch.object(release, 'gcloud', side_effect=gcloud), \
             mock.patch.object(release, 'ensure_scheduler_invoker', side_effect=bound.append):
            release.deploy()
        updates = [c for c in calls if c[:3] == ['run', 'jobs', 'update']]
        self.assertEqual([c[3] for c in updates],
                         ['sns-comments-moderate', 'sns-comments-notion-summary', 'sns-comments-past-sweep'])
        sweep = updates[-1]
        self.assertIn('--image=registry/worker:new', sweep)
        self.assertFalse(any(a.startswith('--set-env-vars') or a.startswith('--update-env-vars') or a.startswith('--task-timeout') for a in sweep))
        self.assertEqual(bound, ['sns-comments-moderate', 'sns-comments-notion-summary'])


if __name__ == '__main__':
    unittest.main()
