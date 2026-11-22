"""Read-only local checks for a managed Flow Chrome profile."""
from pathlib import Path
import json
import re
import shlex

from adapter import browser_profile_matches
from flow_proxy_guard import mapped_ports


PROFILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
EXTENSION_ID = re.compile(r'[a-p]{32}')
CHROME_NAMES = {'chrome', 'chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable'}
DISALLOWED_FLAGS = (
    '--load-extension', '--disable-extensions-except', '--no-proxy-server',
    '--proxy-pac-url', '--proxy-auto-detect', '--proxy-bypass-list',
    '--host-resolver-rules',
)


def _chrome_processes(proc_root, target):
    for cmdline in proc_root.glob('[0-9]*/cmdline'):
        try:
            args = [arg.decode(errors='replace') for arg in cmdline.read_bytes().split(b'\0') if arg]
            if len(args) == 1:
                args = shlex.split(args[0])
            if (args and Path(args[0]).name in CHROME_NAMES
                    and not any(arg.startswith('--type=') for arg in args)
                    and browser_profile_matches(args, target)):
                yield cmdline.parent.name, args
        except (OSError, ValueError):
            # A process can disappear during the scan.
            continue


def _system_timezone():
    zoneinfo = Path('/usr/share/zoneinfo')
    try:
        name = Path('/etc/localtime').resolve(strict=True).relative_to(zoneinfo).as_posix()
        return name.removeprefix('posix/').removeprefix('right/')
    except (OSError, ValueError):
        # Ubuntu can also be configured with a copied /etc/localtime file.
        name = Path('/etc/timezone').read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*', name) or not (zoneinfo / name).is_file():
            raise ValueError('invalid system timezone')
        return name


def check_profile(root, profile, *, expected_languages=None, expected_timezone=None, proc_root=Path('/proc')):
    """Return (ok, reasons), without opening Chrome or making network requests.

    `root` is the Flow install directory. Optional expectations come from the
    caller's local policy. An absent Chrome process is normal before launch.
    """
    if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile):
        return False, ['invalid_profile']
    root = Path(root)
    base = (root / 'data/profiles').resolve()
    target = base / profile
    default = target / 'Default'
    if target.is_symlink() or default.is_symlink() or not default.is_dir():
        return False, ['invalid_or_missing_profile']

    reasons = []
    extension_ids = set()
    extensions = default / 'Extensions'
    try:
        if extensions.is_symlink():
            raise ValueError('symlink')
        if extensions.exists():
            extension_ids.update(item.name for item in extensions.iterdir()
                                 if EXTENSION_ID.fullmatch(item.name))
    except (OSError, ValueError):
        reasons.append('extension_directory_unreadable')

    prefs_path = default / 'Preferences'
    preferences = {}
    try:
        if prefs_path.is_symlink() or not prefs_path.is_file() or prefs_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('invalid preferences file')
        preferences = json.loads(prefs_path.read_text(encoding='utf-8'))
        if not isinstance(preferences, dict):
            raise ValueError('invalid preferences object')
    except (OSError, ValueError):
        reasons.append('preferences_unreadable')
        preferences = {}
    extension_settings = preferences.get('extensions', {})
    settings = extension_settings.get('settings', {}) if isinstance(extension_settings, dict) else None
    if not isinstance(settings, dict):
        reasons.append('extension_settings_invalid')
    else:
        extension_ids.update(name for name in settings if EXTENSION_ID.fullmatch(name))
    reasons.extend(f'extension_present:{name}' for name in sorted(extension_ids))

    intl = preferences.get('intl', {})
    if not isinstance(intl, dict):
        intl = {}
    languages = intl.get('accept_languages') or intl.get('selected_languages')
    actual_languages = tuple(code.strip() for code in languages.split(',') if code.strip()) if isinstance(languages, str) else ()
    if not actual_languages:
        reasons.append('profile_languages_missing')
    elif expected_languages is not None and actual_languages != tuple(expected_languages):
        reasons.append('profile_languages_mismatch')
    try:
        timezone = _system_timezone()
    except (OSError, ValueError):
        reasons.append('system_timezone_unavailable')
    else:
        if expected_timezone is not None and timezone != expected_timezone:
            reasons.append('system_timezone_mismatch')

    port = None
    try:
        port = mapped_ports(root / 'bin/exit-map.txt').get(profile)
        if port is None:
            reasons.append('proxy_mapping_missing')
    except (OSError, ValueError, AttributeError):
        reasons.append('proxy_mapping_unreadable_or_invalid')

    proc_root = Path(proc_root)
    if not proc_root.is_dir():
        reasons.append('chrome_process_inventory_unavailable')
    else:
        for pid, args in _chrome_processes(proc_root, target):
            flags = {arg.partition('=')[0] for arg in args}
            reasons.extend(f'chrome_unsafe_flag:{pid}:{flag}' for flag in DISALLOWED_FLAGS if flag in flags)
            proxies = [arg for arg in args if arg == '--proxy-server' or arg.startswith('--proxy-server=')]
            if not proxies:
                reasons.append(f'chrome_proxy_missing:{pid}')
            elif port is None or proxies != [f'--proxy-server=socks5://127.0.0.1:{port}']:
                reasons.append(f'chrome_proxy_mismatch:{pid}')

    return not reasons, reasons
