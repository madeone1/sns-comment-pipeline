import contextlib
import io
import os
import unittest
from unittest import mock

from sns_comment import credentials
from sns_comment.api import PilotError


class CredentialRegressionTests(unittest.TestCase):
    def test_long_token_is_preserved_validated_and_never_written_to_argv_or_output(self):
        token="EAA"+"x"*600
        output=io.StringIO()
        events=[]
        def validate(value):
            self.assertEqual(value,token)
            events.append("validated")
        def save(value,**kwargs):
            self.assertEqual(value,token)
            self.assertFalse(kwargs["json_value"])
            events.append("saved")
        with mock.patch.object(credentials.sys,"platform","darwin"),mock.patch.object(credentials.sys.stdin,"isatty",return_value=True),mock.patch.object(credentials.getpass,"getpass",return_value=token),mock.patch.object(credentials,"verify_meta_token",side_effect=validate),mock.patch.object(credentials,"save_local",side_effect=save),mock.patch.object(credentials,"get_token",return_value=token),mock.patch.object(credentials.subprocess,"run") as run,contextlib.redirect_stdout(output):
            self.assertEqual(credentials.main(["setup"]),0)
            run.assert_not_called()
        self.assertEqual(events,["validated","saved"])
        self.assertNotIn(token,output.getvalue())

    def test_invalid_token_does_not_replace_stored_credential(self):
        with mock.patch.object(credentials.sys,"platform","darwin"),mock.patch.object(credentials.sys.stdin,"isatty",return_value=True),mock.patch.object(credentials.getpass,"getpass",return_value="EAA"+"x"*500),mock.patch.object(credentials,"verify_meta_token",side_effect=PilotError("invalid")),mock.patch.object(credentials,"save_local") as save,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(credentials.main(["setup"]),1)
            save.assert_not_called()

    def test_long_token_readback_keeps_all_bytes(self):
        value="EAA"+"y"*800
        with mock.patch.dict(os.environ,{},clear=True),mock.patch.object(credentials.sys,"platform","darwin"),mock.patch.object(credentials,"read_local",return_value=value):
            self.assertEqual(credentials.get_meta_token(),value)


if __name__=="__main__":unittest.main()
