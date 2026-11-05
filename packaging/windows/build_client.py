"""Build a self-contained Windows installer with a locally embedded runtime."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

BASE = Path(__file__).resolve().parent
PAYLOAD = BASE / 'client-payload'
DIST = BASE / 'dist'
CSC = Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')


def main():
    if not CSC.is_file():
        raise SystemExit('缺少 Windows .NET 编译器。')
    payload_zip = BASE / 'client-payload.zip'
    if payload_zip.exists():
        raise SystemExit('客户端压缩包已存在，已停止以避免覆盖。')
    if not (PAYLOAD / 'runtime/python.exe').is_file():
        raise SystemExit('请先准备 Python 嵌入式运行环境。')
    sdk = ['Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll', 'WebView2Loader.dll']
    if not all((PAYLOAD / name).is_file() for name in sdk):
        raise SystemExit('请先准备 WebView2 SDK。')
    desktop = PAYLOAD / 'StudioWright.exe'
    compile_desktop = [str(CSC), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
                       '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll',
                       *['/r:' + str(PAYLOAD / name) for name in sdk[:2]],
                       '/win32icon:' + str(BASE / 'studiowright.ico'), '/out:' + str(desktop),
                       str(BASE / 'client/Desktop.cs')]
    subprocess.run(compile_desktop, check=True)
    count = 0
    with zipfile.ZipFile(payload_zip, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as zipf:
        for path in sorted(PAYLOAD.rglob('*')):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
                zipf.write(path, path.relative_to(PAYLOAD).as_posix())
                count += 1
    DIST.mkdir(exist_ok=True)
    output = DIST / 'StudioWright-Windows-Desktop-Setup.exe'
    if output.exists():
        raise SystemExit('客户端安装包已存在，已停止以避免覆盖。')
    cmd = [str(CSC), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
           '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.IO.Compression.dll',
           '/r:System.IO.Compression.FileSystem.dll', '/r:System.Web.Extensions.dll',
           '/win32icon:' + str(BASE / 'studiowright.ico'),
           '/resource:' + str(payload_zip) + ',payload.zip', '/out:' + str(output),
           str(BASE / 'client/Setup.cs')]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('操作端编译未完成：\n' + result.stdout + result.stderr)
    print(json.dumps({'windows_installer': str(output), 'files': count,
                      'bytes': output.stat().st_size,
                      'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
