"""Apply the tested FlowMusic2API patches without copying secrets."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE = '34296d89e830e42b1b9582215404c7c5fae2ed27'
FILES = ('internal/service/accounts.go', 'internal/service/flowmusic.go',
         'internal/service/flowmusic_test.go', 'internal/httpapi/server_test.go')
PRIORITY_FILES = ('internal/service/accounts.go', 'internal/service/generation.go',
                  'internal/service/priority.go', 'internal/service/priority_test.go',
                  'internal/httpapi/handlers.go', 'internal/httpapi/server.go')


def normalize(raw):
    return None if raw is None else raw.replace(b'\r\n', b'\n').replace(b'\r', b'\n')


def read_optional(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit('Unexpected source path: ' + str(path))
    return path.read_bytes() if path.is_file() else None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--priority-dispatch', action='store_true',
                        help='verify or apply both session renewal and ordered Music dispatch')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.source.resolve()
    def git(*argv):
        return subprocess.check_output(['git', *argv], cwd=root)
    if git('rev-parse', 'HEAD').decode().strip() != BASE:
        raise SystemExit('Upstream revision changed; review the patch before applying.')
    files = tuple(dict.fromkeys(FILES + (PRIORITY_FILES if args.priority_dispatch else ())))
    patch_dir = Path(__file__).resolve().parent
    tracked = set(git('ls-tree','-r','--name-only','HEAD').decode().splitlines())
    original = {name:read_optional(root / name) for name in files}
    baseline = {name:normalize(git('show','HEAD:'+name)) if name in tracked else None for name in files}
    with tempfile.TemporaryDirectory(prefix='music-session-patch-') as directory:
        staging = Path(directory)
        # The upstream source contains CRCRLF. Normalize only the patched files.
        for name,content in baseline.items():
            if content is None:
                continue
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        def apply_patch(filename):
            for flags in (['--check'], []):
                subprocess.run(['git','apply',*flags,str(patch_dir / filename)], cwd=staging, check=True)

        apply_patch('session-renewal.patch')
        accepted = [baseline]
        if args.priority_dispatch:
            accepted.append({name:read_optional(staging / name) for name in files})
            apply_patch('priority-dispatch.patch')
        final = {name:read_optional(staging / name) for name in files}
        current = {name:normalize(content) for name,content in original.items()}
        if current == final:
            print('Requested patches are already present.')
            return
        if current not in accepted:
            raise SystemExit('Existing edits must be preserved. Apply on a clean or verified session-renewal checkout.')
        if args.apply:
            for name in files:
                if read_optional(root / name) != original[name]:
                    raise SystemExit('Source changed during verification: ' + name)
            for name in files:
                if final[name] != current[name]:
                    shutil.copyfile(staging / name, root / name)
        print('Applied' if args.apply else 'Patch verified; use --apply to write source.')

if __name__ == '__main__':
    main()
