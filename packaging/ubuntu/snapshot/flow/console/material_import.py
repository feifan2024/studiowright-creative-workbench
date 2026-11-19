"""Copy existing media into the workbench library over an operator-owned SSH session."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile


SOURCE = "windows_local"
MAX_BYTES = 2 * 1024**3
CHUNK = 2 * 1024**2
FORMATS = {
    **dict.fromkeys((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"), "image"),
    **dict.fromkeys((".mp4", ".mov", ".webm"), "video"),
    **dict.fromkeys((".wav", ".m4a"), "audio"),
}


def send_json(stream, value):
    stream.write(json.dumps(value, ensure_ascii=True).encode("ascii") + b"\n")
    stream.flush()


def read_json(stream):
    line = stream.readline(32769)
    if not line:
        return None
    if len(line) > 32768 or not line.endswith(b"\n"):
        raise ValueError("Invalid import message size")
    return json.loads(line)


def archive_metadata(offer):
    if not isinstance(offer, dict) or not isinstance(offer.get("path"), str):
        raise ValueError("Missing source path")
    path = PurePosixPath(offer["path"])
    if (path.is_absolute() or not path.parts or ".." in path.parts
            or "\\" in offer["path"] or len(offer["path"]) > 2048):
        raise ValueError("Invalid relative source path")
    kind = FORMATS.get(path.suffix.lower())
    sha, size = offer.get("sha256"), offer.get("size")
    if not kind or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("Unsupported file or invalid checksum")
    if type(size) is not int or not 0 < size <= MAX_BYTES:
        raise ValueError("File is empty or exceeds the import size limit")
    tags = list(dict.fromkeys(("本地素材", *path.parts[:-1])))
    details = {"original_name": path.name, "original_path": path.as_posix()}
    if kind == "audio":
        folders = {part.casefold() for part in path.parts[:-1]}
        if any("音效" in name or name in {"sound effects", "sound_effect", "sfx"} for name in folders):
            details["audio_category"] = "sound_effect"
        elif any(
            any(word in name for word in ("音乐", "配乐")) or name in {"music", "bgm"} for name in folders
        ):
            details["audio_category"] = "music"
    source_id = hashlib.sha256((path.as_posix().casefold() + "\0" + sha).encode()).hexdigest()
    modified = datetime.fromtimestamp(offer["modified"], timezone.utc).isoformat()
    return {"source": SOURCE, "source_id": source_id, "created": modified, "media_type": kind,
            "tags": tags, "details": details}


def receive(root, stdin, stdout):
    from material_library import Library
    library = Library(root)
    while (offer := read_json(stdin)) is not None:
        meta = archive_metadata(offer)
        sha, size = offer["sha256"], offer["size"]
        with library.connect() as db:
            known = db.execute("SELECT asset_id FROM origins WHERE source=? AND source_id=?",
                               (SOURCE, meta["source_id"])).fetchone()
        existing = library.file(sha)
        if known and known[0] == sha and existing and existing.stat().st_size == size:
            send_json(stdout, {"status": "unchanged", "id": sha})
            continue
        if existing and existing.stat().st_size == size:
            library.archive(existing, meta, sha, max_bytes=MAX_BYTES)
            send_json(stdout, {"status": "deduplicated", "id": sha})
            continue
        with tempfile.TemporaryDirectory(prefix="import-", dir=library.root / "staging") as temp:
            path = Path(temp) / "incoming"
            send_json(stdout, {"status": "send"})
            with path.open("xb") as target:
                remaining = size
                while remaining:
                    data = stdin.read(min(CHUNK, remaining))
                    if not data:
                        raise EOFError("Transfer ended before the complete file arrived")
                    target.write(data)
                    remaining -= len(data)
            try:
                library.archive(path, meta, sha, max_bytes=MAX_BYTES)
            except (OSError, ValueError) as error:
                send_json(stdout, {"status": "rejected", "reason": str(error)})
            else:
                send_json(stdout, {"status": "imported", "id": sha})


def local_files(root):
    for folder, dirs, filenames in os.walk(root, followlinks=False):
        parent = Path(folder)
        dirs[:] = sorted(name for name in dirs if not name.startswith(".")
                         and not (parent / name).is_symlink()
                         and not getattr(os.path, "isjunction", lambda _: False)(parent / name))
        for name in sorted(filenames):
            path = parent / name
            if path.suffix.lower() in FORMATS and not path.is_symlink() and path.is_file():
                yield path


def send(root, command, limit):
    counts = dict.fromkeys(("imported", "deduplicated", "unchanged", "rejected"), 0)
    sent_bytes = 0
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as remote:
        try:
            for index, path in enumerate(local_files(root)):
                if limit is not None and index >= limit:
                    break
                with path.open("rb", buffering=CHUNK) as local:
                    stat = os.fstat(local.fileno())
                    size = stat.st_size
                    relative = path.relative_to(root).as_posix()
                    if not 0 < size <= MAX_BYTES:
                        counts["rejected"] += 1
                        print(json.dumps({"path": relative, "status": "rejected",
                                          "reason": "File is empty or exceeds import limit"}), flush=True)
                        continue
                    sha = hashlib.file_digest(local, "sha256").hexdigest()
                    offer = {"path": relative, "size": size, "modified": stat.st_mtime, "sha256": sha}
                    send_json(remote.stdin, offer)
                    result = read_json(remote.stdout)
                    if result and result["status"] == "send":
                        local.seek(0)
                        left = size
                        while left:
                            data = local.read(min(left, CHUNK))
                            if not data:
                                raise EOFError("Source file changed during transfer")
                            remote.stdin.write(data)
                            left -= len(data)
                        remote.stdin.flush()
                        sent_bytes += size
                        result = read_json(remote.stdout)
                if not result or result["status"] not in counts:
                    raise RuntimeError("Remote import did not acknowledge the file")
                status = result["status"]
                if status != "rejected" and result.get("id") != sha:
                    raise RuntimeError("Remote acknowledgement does not match the source checksum")
                counts[status] += 1
                if status == "rejected":
                    print(json.dumps({"path": offer["path"], **result}), flush=True)
                if (index + 1) % 25 == 0:
                    print(json.dumps({"processed": index + 1, "uploaded_bytes": sent_bytes, **counts}), flush=True)
        finally:
            remote.stdin.close()
        if remote.wait() != 0:
            raise RuntimeError("Remote import process did not complete")
    print(json.dumps({"complete": True, "uploaded_bytes": sent_bytes, **counts}), flush=True)
    return 1 if counts["rejected"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    rs = operations.add_parser("receive")
    rs.add_argument("root", type=Path)
    ss = operations.add_parser("send")
    ss.add_argument("--limit", type=int)
    ss.add_argument("root", type=Path)
    ss.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if options.operation == "receive":
        receive(options.root, sys.stdin.buffer, sys.stdout.buffer)
        return 0
    if not options.root.is_dir() or not options.command or (options.limit is not None and options.limit < 1):
        parser.error("An existing source folder, SSH command, and positive limit are required")
    return send(options.root, options.command, options.limit)


if __name__ == "__main__":
    raise SystemExit(main())
