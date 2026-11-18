import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging/ubuntu/snapshot/flow/console"))
from login_sessions import ImportRestored, inject_verified


class ImportSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old = [{"name": "fixture-old"}]
        self.bundle = {"email": "fixture@example.com", "cookies": [{"name": "fixture-new"}]}
        self.context = Mock(cookies=AsyncMock(return_value=self.old),
                            clear_cookies=AsyncMock(), add_cookies=AsyncMock())
        self.state = {"signed_in": True, "emails": [self.bundle["email"]], "projects": []}
        self.page = Mock(goto=AsyncMock(), evaluate=AsyncMock(return_value=self.state))
        self.vault = Mock(put=Mock(return_value="backup"), get=Mock(return_value={"cookies": self.old}))

    async def test_commit_only_after_finalization(self):
        finalize = AsyncMock(return_value={"email": self.bundle["email"], "projects": []})
        result = await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.assertEqual(result["email"], self.bundle["email"])
        finalize.assert_awaited_once_with(self.state)
        self.context.add_cookies.assert_awaited_once_with(self.bundle["cookies"])
        self.vault.remove.assert_called_once_with("backup")

    async def test_window_failure_restores_previous_login(self):
        finalize = AsyncMock(side_effect=ValueError("window failed"))
        with self.assertRaises(ImportRestored):
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.context.add_cookies.assert_awaited_with(self.old)
        self.assertEqual(self.context.clear_cookies.await_count, 2)

    async def test_cancel_restores_previous_login(self):
        with self.assertRaises(asyncio.CancelledError):
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault,
                                  finalize=AsyncMock(side_effect=asyncio.CancelledError))
        self.context.add_cookies.assert_awaited_with(self.old)

    async def test_wrong_email_never_registers(self):
        self.page.evaluate.return_value = dict(self.state, emails=["other@example.com"])
        finalize = AsyncMock()
        with self.assertRaises(ImportRestored):
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        finalize.assert_not_called()
        self.context.add_cookies.assert_awaited_with(self.old)


class ImportedProjectsTest(unittest.TestCase):
    def test_blank_home_title_preserves_known_name_and_duplicate_title(self):
        from adapter import flow_home_projects
        ident = "72591c35-415f-41c7-b2a2-835e716eb303"
        href = "https://flow.google.com/project/" + ident
        empty = {"url": href, "title": " "}
        self.assertEqual(flow_home_projects({"projects": [empty]}, {ident: "Saved name"})[0]["title"], "Saved name")
        named = {"url": href, "title": "Observed name"}
        self.assertEqual(flow_home_projects({"projects": [named, empty]}, {ident: "Saved name"})[0]["title"], "Observed name")
