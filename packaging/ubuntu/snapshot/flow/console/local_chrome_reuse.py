"""Copy encrypted Google login data into a new isolated Ubuntu profile.

Never open an original profile with automation or change its files.
"""
import json
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

HOSTS = ("google.com", ".google.com", "flow.google.com", ".flow.google.com", "accounts.google.com", ".accounts.google.com", "www.google.com", ".www.google.com")
EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)


def discover(native):
    native = Path(native).resolve()
    state = json.loads((native / "Local State").read_text())
    profiles = []
    for directory, item in state.get("profile", {}).get("info_cache", {}).items():
        email = item.get("user_name", "").strip().lower()
        if not EMAIL.fullmatch(email):
            continue
        if not re.fullmatch(r"Default|Profile [0-9]+", directory):
            continue
        source = (native / directory).resolve()
        if not source.is_relative_to(native) or not source.is_dir():
            continue
        cookies = next((p for p in [source / "Network/Cookies", source / "Cookies"] if p.is_file()), None)
        if cookies:
            profiles.append({"directory": directory, "email": email, "label": item.get("name", email)})
    return profiles


def snapshot(native, directory, destination):
    native, destination = Path(native).resolve(), Path(destination).resolve()
    if not re.fullmatch(r"Default|Profile [0-9]+", directory):
        raise ValueError("源浏览器资料名称不正确。")
    source = (native / directory).resolve()
    if not source.is_relative_to(native):
        raise ValueError("源浏览器资料位置不正确。")
    if destination.exists():
        raise ValueError("目标已有账号资料，未覆盖。")
    cookie_file = next((p for p in [source / "Network/Cookies", source / "Cookies"] if p.is_file()), None)
    if not cookie_file or not cookie_file.resolve().is_relative_to(source):
        raise ValueError("该浏览器资料未找到可复用的登录 Cookie。")
    state = json.loads((native / "Local State").read_text())
    staging = destination.parent / (".local-reuse-" + secrets.token_hex(12))
    staging.mkdir(mode=0o700)
    try:
        target = staging / "Default"
        target.mkdir(mode=0o700)
        copied = target / "Cookies"
        with sqlite3.connect(cookie_file.as_uri()+"?mode=ro", uri=True, timeout=5) as src, sqlite3.connect(copied) as dst:
            deadline = time.monotonic()+20
            def progress(_status, _remaining, _total):
                if time.monotonic()>deadline:
                    raise ValueError("源浏览器正在写入登录资料，本次未改变原资料。请稍后重试。")
            src.backup(dst, pages=128, progress=progress)
            dst.execute("PRAGMA secure_delete=ON")
            marks = ",".join("?" for _ in HOSTS)
            dst.execute("DELETE FROM cookies WHERE host_key NOT IN ("+marks+")", HOSTS)
            count = dst.execute("SELECT count(*) FROM cookies").fetchone()[0]
            if not count:
                raise ValueError("该资料没有 Google 登录 Cookie，未接入后台。")
            # Secure deletion applies to this derived copy only, never the source.
            dst.commit()
            dst.execute("VACUUM")
        copied.chmod(0o600)
        local_state = {"os_crypt": state.get("os_crypt", {})}
        source_info = state.get("profile", {}).get("info_cache", {}).get(directory)
        if source_info:
            local_state["profile"] = {"info_cache": {"Default": source_info}, "last_used": "Default", "last_active_profiles": ["Default"]}
        preferences_file = source / "Preferences"
        preferences = json.loads(preferences_file.read_text()) if preferences_file.is_file() else {}
        login_preferences = {key: preferences[key] for key in ("account_info", "account_values", "account_tracker_service_last_update", "gaia_cookie", "signin") if key in preferences}
        # Chrome associates token_service with its primary account through these
        # preferences. info_cache is display metadata, not that association.
        # Copy only the sign-in namespace; unrelated Google settings stay local.
        services = preferences.get("google", {}).get("services")
        if isinstance(services, dict):
            login_preferences["google"] = {"services": services}
        login_preferences.update({"profile":{"exit_type":"Normal","exited_cleanly":True},"session":{"restore_on_startup":5}})
        from local_chrome_tokens import copy_login_tokens
        token_count = copy_login_tokens(source/"Web Data", target/"Web Data")
        (staging / "Local State").write_text(json.dumps(local_state))
        (staging / "Local State").chmod(0o600)
        (target / "Preferences").write_text(json.dumps(login_preferences))
        (target / "Preferences").chmod(0o600)
        staging.rename(destination)
        return {"google_cookies": count, "source_directory": directory, "encrypted_login_tokens": token_count}
    finally:
        if staging.exists():
            assert staging.is_relative_to(destination.parent) and staging.name.startswith(".local-reuse-")
            shutil.rmtree(staging)
