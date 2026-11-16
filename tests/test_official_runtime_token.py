import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from official_runtime import OfficialRuntime, MODEL, DENY

KEY = 'a' * 64
EMAIL = 'user@example.com'


def make_runtime(tmp):
    config = tmp / 'official-runtime.json'
    binary = tmp / 'bin' / 'cli'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\n')
    root = tmp / 'accounts' / 'a'
    cli = root / 'home' / '.gemini' / 'antigravity-cli'
    cli.mkdir(parents=True)
    (cli / 'settings.json').write_text(json.dumps({'permissions': {'deny': sorted(DENY)}}))
    (root / 'workspace').mkdir(parents=True)
    token_body = json.dumps({'id_token_email': EMAIL, 'access_token': 'x'})
    (cli / 'antigravity-oauth-token').write_text(token_body)
    config.write_text(json.dumps({'schema_version': 1, 'enabled': True, 'model': MODEL,
                                  'binary': str(binary),
                                  'accounts': {KEY: {'root': str(root), 'email': EMAIL}}}))
    runtime = OfficialRuntime(config)
    return runtime, root, cli, token_body


class OfficialTokenProtectionTest(unittest.TestCase):
    def test_valid_account_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, cli, token_body = make_runtime(Path(tmp))
            self.assertTrue(runtime.account(KEY))
            backup = Path(tmp) / 'official-token-backups' / f'{KEY}.token'
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(), token_body)

    def test_missing_token_is_restored_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, root, cli, token_body = make_runtime(Path(tmp))
            self.assertTrue(runtime.account(KEY))  # creates the backup
            (cli / 'antigravity-oauth-token').unlink()
            account = runtime.account(KEY)
            self.assertTrue(account)
            self.assertEqual(account['email'], EMAIL)
            self.assertEqual((cli / 'antigravity-oauth-token').read_text(), token_body)

    def test_corrupt_token_is_restored_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, cli, token_body = make_runtime(Path(tmp))
            self.assertTrue(runtime.account(KEY))
            (cli / 'antigravity-oauth-token').write_text('{"id_token_')  # interrupted write
            self.assertTrue(runtime.account(KEY))
            self.assertEqual((cli / 'antigravity-oauth-token').read_text(), token_body)

    def test_without_backup_missing_token_stays_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, cli, _ = make_runtime(Path(tmp))
            (cli / 'antigravity-oauth-token').unlink()
            self.assertIsNone(runtime.account(KEY))
            self.assertFalse((cli / 'antigravity-oauth-token').exists())

    def test_backup_with_foreign_identity_is_not_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, cli, _ = make_runtime(Path(tmp))
            backup = Path(tmp) / 'official-token-backups' / f'{KEY}.token'
            backup.parent.mkdir(parents=True)
            backup.write_text(json.dumps({'id_token_email': 'other@example.com'}))
            (cli / 'antigravity-oauth-token').unlink()
            self.assertIsNone(runtime.account(KEY))
            self.assertFalse((cli / 'antigravity-oauth-token').exists())

    @unittest.skipUnless(os.name == 'posix', 'Windows needs privileges to create symlinks; the guard runs on Linux')
    def test_symlinked_token_is_never_restored_or_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _, cli, _ = make_runtime(Path(tmp))
            self.assertTrue(runtime.account(KEY))
            real = cli / 'antigravity-oauth-token'
            moved = cli / 'real-token'
            real.rename(moved)
            real.symlink_to(moved)
            self.assertIsNone(runtime.account(KEY))

    def test_identity_mismatch_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, root, cli, _ = make_runtime(Path(tmp))
            (cli / 'antigravity-oauth-token').write_text(json.dumps({'id_token_email': 'other@example.com'}))
            self.assertIsNone(runtime.account(KEY))


if __name__ == '__main__':
    unittest.main()
