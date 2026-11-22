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
    async def test_foreground_preparation_refreshes_before_activation_and_preserves_drafts(self):
        with flow_modules():
            from studiowright.flow import page as flow_page

            project = str(uuid.uuid4())
            url = flow_page.FLOW_ROOT + 'project/' + project
            registry = Mock()
            registry.bind.return_value = 'reused'
            registration = {'pid': 42, 'port': 9333, 'targets': {'task-target'}}
            channel = SimpleNamespace(
                send=AsyncMock(side_effect=lambda name, *_: (
                    {'targetInfo': {'targetId': 'task-target'}} if name == 'Target.getTargetInfo'
                    else {'windowId': 17}
                )),
                detach=AsyncMock(),
            )
            for outcome in ('ready', 'draft', 'timeout', 'redirect', 'http_error'):
                with self.subTest(outcome=outcome), ExitStack() as cleanup:
                    actions = []
                    title = 'Flow project'
                    page = SimpleNamespace(url=url)

                    async def check_editor(_):
                        actions.append('check editor')
                        if outcome == 'draft':
                            raise flow_page.FlowError('existing_flow_draft')

                    async def reload_page(**_):
                        actions.append('reload')
                        if outcome == 'timeout':
                            raise flow_page.PlaywrightTimeout('reload timeout')
                        if outcome == 'redirect':
                            page.url = 'https://flow.google.com/'
                        return SimpleNamespace(ok=outcome != 'http_error')

                    async def set_title(_, arg):
                        nonlocal title
                        previous = title
                        title = arg if isinstance(arg, str) else arg['previous']
                        return previous

                    page.reload = AsyncMock(side_effect=reload_page)
                    page.bring_to_front = AsyncMock(side_effect=lambda: actions.append('bring to front'))
                    page.evaluate = AsyncMock(side_effect=set_title)
                    context = SimpleNamespace(pages=[page], new_cdp_session=AsyncMock(return_value=channel))
                    session = SimpleNamespace(context=context, _safe_editor=check_editor)
                    driver = SimpleNamespace(
                        activate_and_confirm=AsyncMock(side_effect=lambda _: actions.append('activate') or True),
                        _os_focus_confirmed=AsyncMock(return_value=True),
                    )
                    layout = Mock(side_effect=lambda *_: [{'title': title, 'id': 8, 'workspace': 0}])
                    cleanup.enter_context(patch('flow_task_window.registration', return_value=registration))
                    cleanup.enter_context(patch('task_window_registry.TaskWindowRegistry', return_value=registry))
                    cleanup.enter_context(patch('task_window_registry.process_identity', return_value=('fixture', '99')))
                    cleanup.enter_context(patch('desktop_layout.call', layout))
                    cleanup.enter_context(patch('desktop_layout.gui_env', return_value={}))
                    cleanup.enter_context(patch('wayland_desktop_driver.WaylandDesktopDriver', return_value=driver))
                    cleanup.enter_context(patch.object(flow_page.FlowPage, 'ready', AsyncMock(
                        side_effect=lambda: actions.append('ready'))))
                    if outcome == 'ready':
                        flow = await flow_page.FlowPage.open_foreground(session, project, 'fixture', object())
                        self.assertIs(flow.page, page)
                        self.assertEqual(actions, ['check editor', 'reload', 'bring to front', 'activate',
                                                   'bring to front', 'check editor', 'ready'])
                    else:
                        expected = {'draft': 'existing_flow_draft', 'timeout': 'project_page_not_ready',
                                    'redirect': 'task_window_project_unconfirmed',
                                    'http_error': 'project_page_not_ready'}
                        error = TimeoutError if outcome == 'timeout' else flow_page.FlowError
                        with self.assertRaisesRegex(error, expected[outcome]):
                            await flow_page.FlowPage.open_foreground(session, project, 'fixture', object())
                        page.bring_to_front.assert_not_awaited()
                        driver.activate_and_confirm.assert_not_awaited()
                        if outcome == 'draft':
                            page.reload.assert_not_awaited()

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

    async def test_original_video_address_fallback_does_not_take_foreground(self):
        with flow_modules(), tempfile.TemporaryDirectory() as directory:
            from studiowright.studio import Studio

            project, media, workflow = (str(uuid.uuid4()) for _ in range(3))
            video_url = 'https://flow-content.google/video/' + media
            video = b'\x00\x00\x00\x18ftypmp42'
            page = SimpleNamespace(goto=AsyncMock())

            async def save(_url, target):
                target.write_bytes(video)

            flow = SimpleNamespace(
                project_id=project, url='https://flow.google.com/project/' + project, page=page,
                project_video_posters=AsyncMock(return_value={}),
                edit_view_urls=AsyncMock(return_value={'video': video_url}),
                download=AsyncMock(side_effect=save),
                foreground_interaction=Mock(side_effect=AssertionError('original video download took foreground')),
            )
            studio = Studio.__new__(Studio)
            studio.store = SimpleNamespace(add_asset=Mock())
            paths = await studio._download_all(flow, [(media, None, workflow)], Path(directory), 'clip', '.mp4')
            self.assertEqual(Path(paths[0]).read_bytes(), video)
            flow.edit_view_urls.assert_awaited_once_with(media, workflow_id=workflow)
            page.goto.assert_awaited_once_with(flow.url, wait_until='domcontentloaded')
