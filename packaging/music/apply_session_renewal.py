"""Apply the tested FlowMusic2API session-renewal patch without copying secrets."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE = '34296d89e830e42b1b9582215404c7c5fae2ed27'
FILES = ('internal/service/accounts.go', 'internal/service/flowmusic.go',
         'internal/service/flowmusic_test.go', 'internal/httpapi/server_test.go')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.source.resolve()
    def git(*argv):
        return subprocess.check_output(['git', *argv], cwd=root)
    if git('rev-parse', 'HEAD').decode().strip() != BASE:
        raise SystemExit('Upstream revision changed; review the patch before applying.')
    original = {}
    for name in FILES:
        original[name] = (root / name).read_bytes()
        if original[name] != git('show', 'HEAD:' + name):
            raise SystemExit('Existing edits must be preserved: ' + name)
    patch = Path(__file__).with_name('session-renewal.patch').resolve()
    with tempfile.TemporaryDirectory(prefix='music-session-patch-') as directory:
        staging = Path(directory)
        # The upstream source contains CRCRLF. Normalize only the patched files.
        for name in FILES:
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((root / name).read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
        for flags in (['--check'], []):
            subprocess.run(['git', 'apply', *flags, str(patch)], cwd=staging, check=True)
        if args.apply:
            for name in FILES:
                if (root / name).read_bytes() != original[name]:
                    raise SystemExit('Source changed during verification: ' + name)
            for name in FILES:
                shutil.copyfile(staging / name, root / name)
        print('Applied' if args.apply else 'Patch verified; use --apply to write source.')

if __name__ == '__main__':
    main()
