"""Bounded, project-scoped staging for reference media. No provider calls."""
from contextlib import closing, contextmanager
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

MAX_BYTES = 8 * 1024 * 1024
MAX_TASK_BYTES = 64 * 1024 * 1024
CAPACITY = 256 * 1024 * 1024
TTL = 24 * 3600
FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}
UPLOAD_ROUTE = re.compile(r'/api/real-pool/flow-references/(flow-project-[0-9a-f]{64})/([0-9a-f-]{36})/(jpg|jpeg|png|webp|mp4)')


def reference_ids(value):
    if not isinstance(value, list) or not value:
        raise ValueError('请至少提供一个参考素材编号。')
    try:
        if any(not isinstance(i, str) or str(uuid.UUID(i)) != i for i in value) or len(set(value)) != len(value):
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ValueError('参考图编号无效或重复。') from None
    return value


def reference_mode(kind, model, mode, ids):
    if (kind, mode) != ('image', 'image-to-image') and (kind, model, mode) != ('video', 'omni-flash', 'ingredients'):
        raise ValueError('参考图仅支持图生图或 Omni Flash 多参考模式。')
    return reference_ids(ids)


FRAME_MODELS = ('veo-lite', 'veo-fast', 'veo-quality', 'omni-flash')
REFERENCE_FIELDS = {'mode', 'reference_ids', 'start_reference_id', 'end_reference_id'}


def task_reference_ids(kind, params):
    """Return the upload order. A tail is never accepted without a start."""
    fields = params.keys() & REFERENCE_FIELDS
    if params.get('mode') == 'frames':
        if (kind != 'video' or params.get('model') not in FRAME_MODELS
                or fields not in ({'mode', 'start_reference_id'},
                                  {'mode', 'start_reference_id', 'end_reference_id'})):
            raise ValueError('首尾帧仅用于支持的视频模型，必须提供首帧，可选尾帧。')
        ids = [params['start_reference_id']]
        if 'end_reference_id' in params:
            ids.append(params['end_reference_id'])
        return reference_ids(ids)
    if fields != {'mode', 'reference_ids'}:
        raise ValueError('请选择参考图模式并提供对应的素材编号。')
    return reference_mode(kind, params.get('model'), params.get('mode'), params['reference_ids'])


def normalized_owner(value):
    if not isinstance(value, str) or value.count('@') != 1 or not all(value.strip().split('@')):
        raise ValueError('项目所属账号的邮箱尚未确认。')
    return value.strip().lower()


def _image_check(data, extension):
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(BytesIO(data), formats=[FORMATS[extension]]) as im:
            if (im.format != FORMATS[extension] or im.width * im.height > 24_000_000
                    or getattr(im, 'n_frames', 1) != 1):
                raise ValueError()
            im.verify()
        with Image.open(BytesIO(data), formats=[FORMATS[extension]]) as im:
            im.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValueError('只接受真实的静态 JPG、PNG、WebP 图片，像素总量不得超过 2400 万。') from None


def _mp4_check(data):
    """Check bounded ISO BMFF structure and require a video track, without decoding."""
    budget = 2048

    def boxes(start, end):
        nonlocal budget
        while start < end:
            budget -= 1
            if budget < 0 or end - start < 8:
                raise ValueError('MP4 结构不正确。')
            size = int.from_bytes(data[start:start+4], 'big')
            kind = data[start+4:start+8]
            header = 8
            if size == 1:
                if end - start < 16:
                    raise ValueError('MP4 结构不正确。')
                size = int.from_bytes(data[start+8:start+16], 'big')
                header = 16
            elif size == 0:
                size = end - start
            if size < header or start + size > end:
                raise ValueError('MP4 结构不正确。')
            yield kind, start + header, start + size
            start += size

    top = list(boxes(0, len(data)))
    if (top and top[0][0] == b'ftyp' and top[0][2] - top[0][1] >= 8
            and data[top[0][1]:top[0][1]+4] != b'qt  '
            and any(kind == b'mdat' and b > a for kind, a, b in top)):
        for kind, a, b in top:
            if kind == b'moov':
                for track, c, d in boxes(a, b):
                    if track == b'trak':
                        for media, e, f in boxes(c, d):
                            if media == b'mdia' and any(
                                name == b'hdlr' and y-x >= 12 and data[x+8:x+12] == b'vide'
                                for name, x, y in boxes(e, f)
                            ):
                                return
    raise ValueError('参考视频须为含视频轨道的 MP4 文件。')


class ReferenceStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'flow-references.sqlite3'

    def _connect(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('参考图暂存目录不可用。')
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        db = sqlite3.connect(self.path, timeout=10)
        if os.name == 'posix':
            self.path.chmod(0o600)
        db.execute('PRAGMA secure_delete=ON')
        db.execute('''CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY, profile TEXT NOT NULL, project TEXT NOT NULL,
            owner TEXT NOT NULL, ext TEXT NOT NULL, digest TEXT NOT NULL,
            expires INTEGER NOT NULL, data BLOB NOT NULL)''')
        return db

    def put(self, ident, profile, project, owner, ext, data):
        reference_ids([ident])
        owner = normalized_owner(owner)
        if ext not in (*FORMATS, 'mp4') or not 0 < len(data) <= MAX_BYTES:
            raise ValueError('参考素材格式或大小不正确，单个文件最多 8 MiB。')
        if ext == 'mp4':
            _mp4_check(data)
        else:
            _image_check(data, ext)
        digest = sha256(data).hexdigest()
        identity = (profile, project, owner, ext, digest)
        now = int(time.time())
        expires = now + TTL
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT profile,project,owner,ext,digest,expires FROM uploads WHERE id=?', (ident,)).fetchone()
            if existing:
                if existing[:5] != identity or existing[5] <= now:
                    raise ValueError('该参考图编号已使用或过期，请换一个编号。')
                return existing[5], False
            # Release bytes, but never allow a reference ID to be rebound after expiry.
            db.execute("UPDATE uploads SET data=X'' WHERE expires<=? AND length(data)>0", (now,))
            used = db.execute('SELECT COALESCE(SUM(length(data)),0) FROM uploads').fetchone()[0]
            if used + len(data) > CAPACITY:
                raise ValueError('参考图暂存区已满，请等待旧参考图过期。')
            db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?)', (ident, *identity, expires, data))
        return expires, True

    def _scoped(self, ids, profile, project, owner, include_data, allow_video):
        owner = normalized_owner(owner)
        ids = reference_ids(ids)
        with closing(self._connect()) as db:
            # Check the total before loading any BLOBs, using the same DB snapshot.
            db.execute('BEGIN')
            suffix = ' FROM uploads WHERE id=? AND profile=? AND project=? AND owner=? AND expires>?'
            now = int(time.time())
            args = [(ident, profile, project, owner, now) for ident in ids]
            metadata = [db.execute('SELECT ext,digest,length(data)' + suffix, key).fetchone() for key in args]
            if any(row is None for row in metadata):
                raise ValueError('参考素材不存在、已过期，或不属于当前账号和项目。')
            if not allow_video and any(row[0] not in FORMATS for row in metadata):
                raise ValueError('这个模式只支持图片；参考视频请使用 Omni 多参考模式。')
            if len({row[1] for row in metadata}) != len(metadata):
                raise ValueError('请使用内容不同的参考素材。')
            if sum(row[2] for row in metadata) > MAX_TASK_BYTES:
                raise ValueError('每个任务的参考素材总大小不能超过 64 MiB。')
            return [db.execute('SELECT ext,digest,data' + suffix, key).fetchone() for key in args] if include_data else None

    def check(self, ids, profile, project, owner, *, allow_video=False):
        self._scoped(ids, profile, project, owner, False, allow_video)

    def clone_retry(self, kind, params, source, target, operation):
        refs = task_reference_ids(kind, params)
        rows = self._scoped(refs, *source, True, params['mode'] == 'ingredients')
        copies = {}
        for ident, (ext, digest, data) in zip(refs, rows):
            if sha256(data).hexdigest() != digest:
                raise ValueError('原参考素材内容校验失败。')
            new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'flow-' + kind + '-retry:' + operation + ':' + ident))
            self.put(new_id, *target, ext, data)
            copies[ident] = new_id
        updated = dict(params)
        for name in ('start_reference_id', 'end_reference_id'):
            if name in updated:
                updated[name] = copies[updated[name]]
        if 'reference_ids' in updated:
            updated['reference_ids'] = [copies[i] for i in updated['reference_ids']]
        return updated

    @contextmanager
    def files(self, ids, profile, project, owner, *, allow_video=False):
        rows = self._scoped(ids, profile, project, owner, True, allow_video)
        directory = self.path.parent / ('.flow-reference-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700 if os.name == 'posix' else 0o777)
        paths = []
        try:
            for n, (ext, digest, data) in enumerate(rows, 1):
                if sha256(data).hexdigest() != digest:
                    raise ValueError('参考图暂存内容校验失败。')
                path = directory / f'ref-{n}-{ids[n-1][:12]}.{ext}'
                paths.append(path)
                with path.open('xb') as out:
                    out.write(data)
            yield tuple(map(str, paths))
        finally:
            for path in paths:
                path.unlink(missing_ok=True)
            directory.rmdir()
