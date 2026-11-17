import os
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment import moderation_release_check as check
from sns_comment.profiles import read_settings


class ModerationReleaseCheckTests(unittest.TestCase):
    def test_settings_require_full_overlap_and_daily_retirement(self):
        settings = read_settings()
        check.check_settings(settings)
        settings['moderation']['max_age_minutes'] = 20
        with self.assertRaises(PilotError):
            check.check_settings(settings)
        settings = read_settings()
        settings['cloud']['daily_enabled'] = True
        with self.assertRaises(PilotError):
            check.check_settings(settings)

    def test_candidate_rejects_write_flags_before_reading_credentials(self):
        shared = mock.Mock()
        with mock.patch.dict(os.environ, {'SNS_MODERATION_HIDE': '1'}):
            with self.assertRaises(PilotError):
                check.verify(read_settings(), shared)
        shared.json_get.assert_not_called()

    def test_release_requires_bounded_read_concurrency_and_account_budget(self):
        for key, value in [('poll_workers', 20), ('poll_requests_per_account', 2000)]:
            with self.subTest(key=key):
                settings = read_settings()
                settings['moderation'][key] = value
                with self.assertRaises(PilotError):
                    check.check_settings(settings)


if __name__ == '__main__':
    unittest.main()
