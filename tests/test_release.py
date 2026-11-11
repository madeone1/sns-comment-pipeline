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

    def test_missing_report_identifiers_stop_before_any_cloud_call(self):
        with mock.patch.object(release, 'gcloud') as cli:
            with self.assertRaises(SystemExit):
                release.verify(None, None)
        cli.assert_not_called()


if __name__ == '__main__':
    unittest.main()
