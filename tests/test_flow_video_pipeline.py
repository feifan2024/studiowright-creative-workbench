import asyncio
import importlib.util
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


SNAPSHOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
CONSOLE = SNAPSHOT / 'flow/console'


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def flow_modules():
    dispatch = load_file('flow_pipeline_dispatch', CONSOLE / 'dispatch_limit.py')
    paths = [CONSOLE, SNAPSHOT / 'flow/source/src', SNAPSHOT / 'workbench']
    with patch.object(sys, 'path', [*map(str, paths), *sys.path]), \
            patch.dict(sys.modules, dispatch_limit=dispatch):
        yield load_file('flow_pipeline_console', CONSOLE / 'server.py')


class FlowVideoPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_submitted_videos_give_slot_to_other_accounts_only(self):
        class ControlledAdapter:
            def __init__(self):
                self.gates = {}

            def discover(self):
                return [f'account-{i}' for i in range(6)]

            def source_jobs(self, profile):
                return []

            async def run(self, task, account, *, on_video_submitted=None):
                started, accept, tracked, finish = self.gates[task['id']]
                started.set()
                await accept.wait()
                on_video_submitted()
                tracked.set()
                await finish.wait()
                return {'stage': 'delivered', 'files': ['fixture.mp4']}

        with flow_modules() as runtime, tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup, \
                patch('task_coordinator.Coordinator', return_value=SimpleNamespace(events=lambda _: [])):
            open_database = runtime.Store.connect

            def connect(store):
                database = open_database(store)
                cleanup.callback(database.close)
                return database

            cleanup.enter_context(patch.object(runtime.Store, 'connect', connect))
            adapter = ControlledAdapter()
            console = runtime.Console(Path(directory), adapter, test=True)
            console.dispatch_limit.set_limit(1)
            status_reader = load_file('workbench_pipeline_dispatch', SNAPSHOT / 'workbench/dispatch_limit.py')
            reader = status_reader.DispatchLimit(console.dispatch_limit.root)

            def enqueue(profile):
                task = console.store.create_task(profile, str(uuid.uuid4()), 'video', {'count': 1}, str(uuid.uuid4()))
                ident = task['id']
                adapter.gates[ident] = [asyncio.Event() for _ in range(4)]
                return ident

            first = enqueue('account-0')
            same_account = enqueue('account-0')
            other_accounts = [enqueue(f'account-{i}') for i in range(1, 6)]
            scheduler = asyncio.create_task(console.scheduler())
            try:
                started, accept, tracked, finish = adapter.gates[first]
                await asyncio.wait_for(started.wait(), 4)
                self.assertEqual(reader.status()['occupied_slots'], 1)
                self.assertEqual(console.store.task(other_accounts[0])['status'], 'queued')
                accept.set()
                await asyncio.wait_for(tracked.wait(), 4)

                for ident in other_accounts:
                    next_started, next_accept, next_tracked, _ = adapter.gates[ident]
                    await asyncio.wait_for(next_started.wait(), 4)
                    next_accept.set()
                    await asyncio.wait_for(next_tracked.wait(), 4)

                state = reader.status()
                self.assertEqual((state['active'], state['occupied_slots'], state['background_videos']), (6, 0, 6))
                self.assertEqual(console.store.task(first)['status'], 'running')
                self.assertEqual(console.store.task(same_account)['status'], 'queued')
                finish.set()
                await asyncio.wait_for(adapter.gates[same_account][0].wait(), 4)
                self.assertEqual(console.store.task(first)['status'], 'succeeded')
            finally:
                scheduler.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler
                for task in console.running:
                    task.cancel()
                await asyncio.gather(*console.running, return_exceptions=True)
            self.assertEqual(reader.status()['active'], 0)

    async def test_handoff_requires_exact_accepted_reply_and_finishes_without_editor_address(self):
        with flow_modules():
            from studiowright.flow import page as flow_page

            project, media, workflow = (str(uuid.uuid4()) for _ in range(3))
            accepted = Mock()

            def record(status):
                return [media, project, workflow, None, None, [None] * 8 + [[status]], None, []]

            class Watch:
                def __init__(self):
                    self.bodies = []

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_):
                    pass

                def mark_submit(self):
                    pass

                def verified_submit(self, index, rpc):
                    return index == 2 and rpc == 'YhhmEf'

                async def wait(self, _):
                    if len(self.bodies) < 3:
                        accepted.assert_not_called()
                    else:
                        accepted.assert_called_once_with([media])
                    self.bodies.append([
                        ('jwpduf', 200, record(flow_page.replies.STATUS_RUNNING)),
                        ('YhhmEf', 200, record(flow_page.replies.STATUS_RUNNING)),
                        ('YhhmEf', 200, record(flow_page.replies.STATUS_QUEUED)),
                        ('jwpduf', 200, record(flow_page.replies.STATUS_DONE)),
                    ][len(self.bodies)])

            page = flow_page.FlowPage(SimpleNamespace(url='https://flow.google.com/project/' + project), project, None)

            async def submit(_prompt, before_click):
                before_click()

            with patch.object(page, '_type_and_submit', AsyncMock(side_effect=submit)), \
                    patch.object(flow_page, 'ReplyWatch', return_value=Watch()), \
                    patch.object(flow_page.be, 'parse_reply', side_effect=lambda data, _: data):
                clips = await page.generate_video('fixture', on_accepted=accepted)

            self.assertEqual([(clip.media_id, clip.url) for clip in clips], [(media, None)])

    async def test_project_grid_does_not_choose_a_different_videos_poster(self):
        with flow_modules():
            from studiowright.flow import page as flow_page

            project, other_project, media, other_media, workflow = (str(uuid.uuid4()) for _ in range(5))
            poster = 'https://lh3.googleusercontent.com/asb/example-ID'

            def record(ident, owner, address):
                return [ident, owner, workflow, None, None, [None] * 5 + [address], None, []]

            page = flow_page.FlowPage(SimpleNamespace(url=flow_page.FLOW_ROOT + 'project/' + project,
                                                      reload=AsyncMock()), project, None)
            watch = AsyncMock()
            watch.__aenter__.return_value = watch
            data = [record(media, project, poster), record(other_media, other_project, poster)]
            watch.bodies = [(flow_page.LIST_RPC, 200, data)]
            with patch.object(flow_page, 'ReplyWatch', return_value=watch), \
                    patch.object(flow_page.be, 'parse_reply', side_effect=lambda body, _: body):
                self.assertEqual(await page.project_video_posters({media}), {media: '/asb/example-ID'})
                data.append(record(other_media, project, poster))
                self.assertEqual(await page.project_video_posters({media}), {})
