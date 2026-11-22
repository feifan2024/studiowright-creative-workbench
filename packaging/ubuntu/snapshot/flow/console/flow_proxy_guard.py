"""Validate and probe the explicit per-profile SOCKS mapping before Chrome starts."""
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def mapped_ports(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('unsafe_flow_proxy_map')
    if info.st_size > 16384:
        raise ValueError('invalid_flow_proxy_map')
    mappings = {}
    used = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        fields = line.partition('#')[0].split()
        if not fields:
            continue
        if (len(fields) != 2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', fields[0])
                or not re.fullmatch(r'[0-9]{1,5}', fields[1])):
            raise ValueError('invalid_flow_proxy_map')
        profile, raw_port = fields
        port = int(raw_port)
        if not 1 <= port <= 65535 or profile in mappings or port in used:
            raise ValueError('duplicate_or_invalid_flow_proxy_map')
        mappings[profile] = port
        used.add(port)
    return mappings


def check_proxy(port):
    response = subprocess.run(
        ['curl', '--disable', '--silent', '--output', '/dev/null', '--write-out', '%{http_code}',
         '--noproxy', '', '--socks5-hostname', f'127.0.0.1:{port}', '--connect-timeout', '4',
         '--max-time', '8', 'https://www.gstatic.com/generate_204'],
        capture_output=True, text=True, timeout=9, check=False,
    )
    if response.returncode != 0 or response.stdout.strip() != '204':
        raise ValueError('flow_proxy_unreachable')


def proxy_argument(root, args):
    if any(arg.startswith(('--proxy-', '--no-proxy', '--host-resolver-rules')) for arg in args):
        raise ValueError('flow_proxy_override_forbidden')
    profiles = [arg.partition('=')[2] for arg in args if arg.startswith('--user-data-dir=')]
    if len(profiles) != 1 or '--user-data-dir' in args:
        raise ValueError('flow_proxy_profile_required')
    profile = Path(profiles[0]).resolve()
    if profile.parent != (root / 'data/profiles').resolve():
        raise ValueError('flow_proxy_profile_required')
    port = mapped_ports(root / 'bin/exit-map.txt').get(profile.name)
    if port is None:
        raise ValueError('flow_proxy_unbound')
    check_proxy(port)
    return f'--proxy-server=socks5://127.0.0.1:{port}'


if __name__ == '__main__':
    try:
        print(proxy_argument(Path(__file__).resolve().parents[1], sys.argv[1:]))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, subprocess.SubprocessError):
        print('flow_proxy_check_unavailable', file=sys.stderr)
        sys.exit(1)
