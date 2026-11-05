"""Create an Ubuntu self-extracting installer from the captured live sources."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile

BASE = Path(__file__).resolve().parent
STAGE = BASE / 'server-payload'
DIST = BASE / 'dist'
OLD_ID = 'kbnibljeinmjmmpholicjkaiehmilbbk'


def replace_exact(path, before, after, count):
    text = path.read_text(encoding='utf-8')
    if text.count(before) != count:
        raise RuntimeError('程序版本已变化，停止打包：' + str(path.name))
    path.write_text(text.replace(before, after), encoding='utf-8')


def main():
    if STAGE.exists():
        raise SystemExit('服务端封装目录已存在，已停止以避免覆盖。')
    extension = json.loads((BASE / 'client-extension.json').read_text(encoding='utf-8'))['id']
    if len(extension) != 32 or any(c not in 'abcdefghijklmnop' for c in extension):
        raise RuntimeError('插件身份格式不正确。')
    STAGE.mkdir()
    shutil.copyfile(BASE / 'server/installer.py', STAGE / 'installer.py')
    shutil.copyfile(BASE / 'server/discovery.py', STAGE / 'discovery.py')
    shutil.copyfile(BASE / 'server/discovery_install.py', STAGE / 'discovery_install.py')
    shutil.copytree(BASE / 'snapshot', STAGE / 'snapshot')
    shutil.copytree(BASE / 'wheelhouse-linux', STAGE / 'wheels')
    # Keep the original PoolDesk request core and upstream reference next to
    # the adapted source and its AGPL notice; no account or runtime data.
    pooldesk = BASE / 'pooldesk-upstream'
    upstream = STAGE / 'snapshot/workbench/upstream'
    upstream.mkdir()
    shutil.copyfile(pooldesk / 'pool.py', upstream / 'pool.py')
    shutil.copyfile(pooldesk / 'upstream/client.reference.py', upstream / 'client.reference.py')
    notice_path = STAGE / 'snapshot/workbench/POOL_DESK_NOTICE.md'
    replace_exact(notice_path, 'Complete local source is included in this directory.',
                  'The adapted request core and integration are included with this package; the original request core and reference client are in upstream/.', 1)
    server = STAGE / 'snapshot/flow/console/server.py'
    app = STAGE / 'snapshot/flow/console/static/app.js'
    replace_exact(server, OLD_ID, extension, 2)
    replace_exact(app, OLD_ID, extension, 1)
    replace_exact(app, 'Flow2API 插件', 'StudioWright 账号助手', 4)
    replace_exact(app, '请安装或重新加载 1.4.2', '请安装或重新加载账号助手', 1)
    replace_exact(app, "if(['succeeded','running'].includes(job.status))return;",
                  "if(job.status!=='waiting')return;", 1)
    replace_exact(app, '未安装或旧版插件，请打开此用户的扩展管理页，安装／重新加载 1.4.2；完成后这里会自动继续。',
                  '请确认已在此用户的扩展管理页安装并启用账号助手。', 1)
    retry = ("let ready=false;for(let i=0;i<75;i++){try{await pluginCommand('" + extension +
             "',{action:'sw_probe'},3000);ready=true;break;}catch(e){await report('waiting',e.message);"
             "await new Promise(r=>setTimeout(r,8000));}}\n"
             "  if(!ready)throw Error('等待插件超过 10 分钟，请安装后从本机账号管理重新同步。');")
    single = ("try{await pluginCommand('" + extension + "',{action:'sw_probe'},3000);}"
              "catch(e){throw Error('插件未连接，请安装或重新加载后，在本机账号管理中手动重新发起。');}")
    replace_exact(app, retry, single, 1)
    replace_exact(server,
                  'return JSONResponse({"ok": True, "application": "studiowright-console", "test": test})',
                  'return JSONResponse({"ok": True, "application": "studiowright-console", "test": test, "bridge_id": "' + extension + '"})',
                  1)
    replace_exact(server,
                  '        plugin_path = request.url.path == "/api/plugin/session" or request.url.path.startswith(\n',
                  '        if request.method == "GET" and request.url.path == "/health" and request.headers.get("x-studiowright-probe") == "1":\n'
                  '            return await call_next(request)\n'
                  '        plugin_path = request.url.path == "/api/plugin/session" or request.url.path.startswith(\n', 1)
    original_manifest = STAGE / 'snapshot/snapshot-manifest.json'
    captured = json.loads(original_manifest.read_text(encoding='utf-8'))
    for changed in ('flow/console/server.py', 'flow/console/static/app.js',
                    'workbench/POOL_DESK_NOTICE.md', 'workbench/upstream/pool.py',
                    'workbench/upstream/client.reference.py'):
        captured[changed] = hashlib.sha256((STAGE / 'snapshot' / changed).read_bytes()).hexdigest()
    original_manifest.write_text(json.dumps(captured, indent=2, sort_keys=True), encoding='utf-8')
    notice = ('创作工作台 Ubuntu 服务端安装包\n\n'
              '复用现有服务：bash 安装包.run --mode reuse\n'
              '复用并开启局域网发现：bash 安装包.run --mode reuse --enable-discovery\n'
              '全新安装：bash 安装包.run --mode new\n'
              '仅查看状态：bash 安装包.run --mode inspect\n\n'
              '复用模式不修改现有服务或数据；启用发现会新增独立的局域网发现服务。全新安装发现已有程序、数据、服务或端口时会停止。\n'
              '支持 Ubuntu x86-64、Python 3.12，需 Chrome/Chromium、Xvfb、xauth、dpkg-deb 及用户级 systemd。\n'
              '程序依赖已随包提供；账号、密钥、素材和历史记录不在安装包中。\n')
    (STAGE / '使用说明.txt').write_text(notice, encoding='utf-8')
    manifest = {}
    for path in sorted(STAGE.rglob('*')):
        if path.is_file() and not path.is_symlink() and path.name != 'manifest.json':
            manifest[path.relative_to(STAGE).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (STAGE / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    DIST.mkdir(exist_ok=True)
    archive = BASE / 'server-payload.tar.gz'
    with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
        for path in sorted(STAGE.rglob('*')):
            if path.is_file() and not path.is_symlink():
                tar.add(path, arcname=path.relative_to(STAGE).as_posix(), recursive=False)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    lines = [
        '#!/bin/sh',
        'set -eu',
        'payload_line=PAYLOAD_LINE',
        'expected=' + sha,
        'actual=$(tail -n +"$payload_line" "$0" | sha256sum | cut -d " " -f 1)',
        'if [ "$actual" != "$expected" ]; then echo "安装包校验失败。" >&2; exit 1; fi',
        'scratch=$(mktemp -d "${TMPDIR:-/tmp}/studiowright-installer.XXXXXXXX")',
        'cleanup() { case "$scratch" in "${TMPDIR:-/tmp}"/studiowright-installer.*) rm -rf -- "$scratch";; *) echo "临时目录未清理" >&2;; esac; }',
        'trap cleanup EXIT HUP INT TERM',
        'tail -n +"$payload_line" "$0" | tar -xzf - -C "$scratch"',
        'if command -v python3.12 >/dev/null 2>&1; then runner=python3.12; else runner=python3; fi',
        '"$runner" -B "$scratch/installer.py" "$@"',
        'exit $?']
    header = ('\n'.join(lines).replace('PAYLOAD_LINE', str(len(lines) + 1)) + '\n').encode('utf-8')
    output = DIST / 'StudioWright-Ubuntu-LAN-Setup.run'
    with output.open('xb') as stream, archive.open('rb') as payload:
        stream.write(header)
        shutil.copyfileobj(payload, stream, 1024 * 1024)
    print(json.dumps({'server_installer': str(output), 'files': len(manifest),
                      'bytes': output.stat().st_size, 'extension_id': extension}))


if __name__ == '__main__':
    main()
