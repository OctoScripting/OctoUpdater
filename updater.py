#!/usr/bin/env python3
"""
OctoWoW client updater — single-file, stdlib-only.
"""

from __future__ import annotations

import hashlib
import http.client
import math
import os
import shutil
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --- Configuration (edit these) -----------------------------------------------

CDN = "https://dl.octowow.st/client/latest"
TORRENT_URL = "https://dl.octowow.st/download/client.torrent"

# Linux
CLIENT_DIR = Path("/path/to/OctoWoW/")

# Windows (Uncomment CLIENT_DIR)
# CLIENT_DIR = Path(r"C:\Path\To\OctoWoW")

ALLOWED_HOSTS = {"dl.octowow.st", "octowow.st"}
UA = "OctoUpdater/1.2"
DOWNLOAD_RETRY_COUNT = 10
STALL_TIMEOUT_S = 60
PATCHED_WOW_HASH_FILE = ".octo-updater-wow.sha1"
WOW_EXE = "WoW.exe"
# DXVK / launcher-owned; the torrent lists it but the official launcher skips it.
SKIP_FILES = {os.path.normcase("d3d9.dll")}

# Obsolete archives from the pre-torrent client. Matched by name AND size so
# similarly named player files are never removed.
LEGACY_ARCHIVES = {
    "patch-6.mpq": 451195806,
    "patch-7.mpq": 175256564,
    "patch-8.mpq": 484649870,
    "patch-9.mpq": 506808141,
    "patch-a.mpq": 241751337,
}

# -------------------------------------------------------------------------------


def sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def format_size(n: int | float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def assert_allowed_url(url: str) -> None:
    host = urllib.parse.urlparse(url).netloc
    if host not in ALLOWED_HOSTS:
        raise SystemExit(f"Refusing request to unexpected host: {host}")


def open_url(
    url: str, timeout: float, headers: dict[str, str] | None = None
):
    assert_allowed_url(url)
    req_headers = {"User-Agent": UA}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    return urllib.request.urlopen(req, timeout=timeout)


def cdn_url(rel_path: str) -> str:
    posix = rel_path.replace(os.sep, "/")
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in posix.split("/"))
    return f"{CDN}/{encoded}"


def bdecode(data: bytes, i: int = 0) -> tuple[object, int]:
    if i >= len(data):
        raise ValueError("truncated bencode")
    ch = data[i : i + 1]
    if ch == b"i":
        j = data.index(b"e", i + 1)
        return int(data[i + 1 : j]), j + 1
    if ch == b"l":
        out: list[object] = []
        i += 1
        while data[i : i + 1] != b"e":
            v, i = bdecode(data, i)
            out.append(v)
        return out, i + 1
    if ch == b"d":
        out: dict[bytes, object] = {}
        i += 1
        while data[i : i + 1] != b"e":
            k, i = bdecode(data, i)
            v, i = bdecode(data, i)
            if not isinstance(k, bytes):
                raise ValueError("bencode dict key must be bytes")
            out[k] = v
        return out, i + 1
    if ch.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        start = colon + 1
        return data[start : start + n], start + n
    raise ValueError(f"invalid bencode at offset {i}")


def torrent_file_list(torrent: bytes) -> list[tuple[str, int]]:
    meta, _ = bdecode(torrent)
    if not isinstance(meta, dict):
        raise ValueError("torrent root is not a dict")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent missing info dict")

    files_node = info.get(b"files")
    out: list[tuple[str, int]] = []
    if isinstance(files_node, list):
        for entry in files_node:
            if not isinstance(entry, dict):
                continue
            path_parts = entry.get(b"path")
            length = entry.get(b"length")
            if not isinstance(path_parts, list) or not isinstance(length, int):
                continue
            parts = [p.decode("utf-8") for p in path_parts if isinstance(p, bytes)]
            if not parts:
                continue
            out.append((os.path.join(*parts), length))
        return out

    name = info.get(b"name")
    length = info.get(b"length")
    if isinstance(name, bytes) and isinstance(length, int):
        return [(name.decode("utf-8"), length)]
    raise ValueError("torrent has no file list")


def fetch_file_list() -> dict[str, int]:
    with open_url(TORRENT_URL, timeout=120) as resp:
        torrent = resp.read()
    files = torrent_file_list(torrent)
    if not files:
        raise SystemExit("Torrent contained no files.")
    return dict(files)


def is_skipped(rel: str) -> bool:
    return os.path.normcase(rel) in SKIP_FILES or os.path.normcase(
        os.path.basename(rel)
    ) in SKIP_FILES


def _wait_and_retry(rel_path: str, attempt: int, error: Exception, tmp: Path) -> None:
    if attempt >= DOWNLOAD_RETRY_COUNT:
        print()
        raise RuntimeError(f"Failed to download {rel_path}: {error}") from error
    partial = tmp.stat().st_size if tmp.is_file() else 0
    print(f"\n  Retry {attempt}/{DOWNLOAD_RETRY_COUNT} failed: {error}")
    if partial:
        print(f"  Resuming from {format_size(partial)}")
    time.sleep(min(attempt, 8))


def download_file(rel_path: str, dest: Path, expected_size: int) -> None:
    url = cdn_url(rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(1, DOWNLOAD_RETRY_COUNT + 1):
        resume_from = tmp.stat().st_size if tmp.is_file() else 0
        if resume_from > expected_size:
            tmp.unlink(missing_ok=True)
            resume_from = 0
        if expected_size > 0 and resume_from == expected_size:
            tmp.replace(dest)
            print(
                f"  {format_size(expected_size)} / {format_size(expected_size)} "
                f"(100.0%)  done"
            )
            return

        extra_headers: dict[str, str] = {}
        if resume_from > 0:
            extra_headers["Range"] = f"bytes={resume_from}-"

        done = resume_from
        rate_at = time.time()
        rate_done = resume_from
        try:
            with open_url(url, timeout=STALL_TIMEOUT_S, headers=extra_headers) as resp:
                status = getattr(resp, "status", 200)
                if resume_from > 0 and status == 200:
                    done = 0
                    rate_done = 0
                    mode = "wb"
                else:
                    mode = "ab" if resume_from > 0 else "wb"

                with tmp.open(mode) as out:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)

                        now = time.time()
                        if now - rate_at >= 0.25:
                            elapsed = now - rate_at
                            speed = (done - rate_done) / elapsed if elapsed > 0 else 0
                            rate_at = now
                            rate_done = done
                            pct = 100 * done / expected_size if expected_size else 0
                            eta = (
                                (expected_size - done) / speed
                                if speed > 0 and expected_size
                                else 0
                            )
                            sys.stdout.write(
                                f"\r  {format_size(done)} / {format_size(expected_size)} "
                                f"({pct:.1f}%)  {format_size(speed)}/s  ETA {format_duration(eta)}   "
                            )
                            sys.stdout.flush()

            got = tmp.stat().st_size if tmp.is_file() else 0
            if got != expected_size:
                if got > expected_size:
                    tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"incomplete or size mismatch: got {got}, expected {expected_size}"
                )
            tmp.replace(dest)
            if expected_size > 0:
                sys.stdout.write(
                    f"\r  {format_size(expected_size)} / {format_size(expected_size)} "
                    f"(100.0%)  done\n"
                )
                sys.stdout.flush()
            return
        except urllib.error.HTTPError as e:
            if e.code in {404, 410}:
                tmp.unlink(missing_ok=True)
                raise
            if e.code == 416:
                tmp.unlink(missing_ok=True)
            _wait_and_retry(rel_path, attempt, e, tmp)
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            RuntimeError,
        ) as e:
            _wait_and_retry(rel_path, attempt, e, tmp)


def load_patched_wow_hash(client: Path) -> str | None:
    path = client / PATCHED_WOW_HASH_FILE
    if path.is_file():
        return path.read_text(encoding="utf-8").strip().upper()
    return None


def save_patched_wow_hash(client: Path) -> None:
    (client / PATCHED_WOW_HASH_FILE).write_text(
        sha1_of_file(client / WOW_EXE), encoding="utf-8"
    )


def build_tweaks(buf: bytearray) -> list[tuple[str, str, int | None, object]]:
    default_fov_degrees = 90  # for 16:9 screen ratio
    fov_radians = default_fov_degrees * (math.pi / 180.0)
    current_flags = struct.unpack_from("<H", buf, 0x126)[0]
    large_address_value = current_flags | 0x20

    # fmt: off
    return [
        ("largeAddress",          "uint16", 0x126,     large_address_value),
        ("fieldOfView",           "float",  0x4089b4,  fov_radians),
        ("cameraDistance",        "float",  0x4089a4,  50.0),
        ("farClip",               "float",  0x40fed8,  777.0),
        ("frillDistance",         "float",  0x467958,  70.0),
        ("nameplateRange",        "float",  0x40c448,  20.0),
        ("soundInBackground",     "int8",   0x3a4869,  0x27),
        ("alwaysAutoLoot",        "bytes",  None, [
            (0x0c1ecf, bytes([0x75])),
            (0x0c2b25, bytes([0x75])),
        ]),
        ("crossFactionResurrect", "bytes",  None, [
            (0x006e5fb8, bytes([0x006e5fb9 & 0xff])),
            (0x006e62a8, bytes([0x006e62a9 & 0xff])),
        ]),
        ("cameraSkipFix",         "bytes",  None, [
            (0x02ccd0, bytes([
                0x55, 0x8b, 0x05, 0x48, 0x4e, 0x88, 0x00, 0x8b, 0x0d, 0x44, 0x4e, 0x88, 0x00, 0xe9, 0x33, 0x90,
                0x32, 0x00, 0x83, 0xc0, 0x32, 0x83, 0xc1, 0x32, 0x3b, 0x0d, 0xa8, 0xeb, 0xc4, 0x00, 0x7e, 0x03,
                0x83, 0xe9, 0x01, 0x3b, 0x05, 0xac, 0xeb, 0xc4, 0x00, 0x7e, 0x03, 0x83, 0xe8, 0x01, 0x83, 0xe9,
                0x32, 0x83, 0xe8, 0x32, 0x89, 0x05, 0x48, 0x4e, 0x88, 0x00, 0x89, 0x0d, 0x44, 0x4e, 0x88, 0x00,
                0x5d, 0xeb, 0x0d,
            ])),
            (0x02d326, bytes([0xe9, 0xb1, 0x8a, 0x32, 0x00])),
            (0x02d334, bytes([0x8b, 0x35, 0x48, 0x4e, 0x88, 0x00])),
            (0x355d15, bytes([
                0x83, 0xf8, 0x32, 0x7d, 0x03, 0x83, 0xc0, 0x01, 0x83, 0xf9, 0x32,
                0x7d, 0x03, 0x83, 0xc1, 0x01, 0xe9, 0xb8, 0x6f, 0xcd, 0xff,
            ])),
            (0x355ddc, bytes([
                0x8d, 0x4d, 0xf0, 0x51,
                0xff, 0x35, 0x00, 0x4e, 0x88, 0x00, 0xff, 0x15, 0x50, 0xf6, 0x7f, 0x00, 0x8b, 0x45, 0xf0, 0x8b,
                0x15, 0x44, 0x4e, 0x88, 0x00, 0xe9, 0x35, 0x75, 0xcd, 0xff,
            ])),
        ]),
        ("skillUiGateHijack",     "bytes",  None, [
            (0x002ddf90, bytes([
                0x55, 0x8b, 0xec, 0x83, 0xec, 0x08, 0x53, 0x56,
                0x57, 0x8b, 0x3d, 0x60, 0xab, 0xce, 0x00, 0x83,
                0xff, 0xff, 0x89, 0x55, 0xfc, 0x89, 0x4d, 0xf8,
                0x74, 0x79, 0x8b, 0x75, 0x08, 0x8b, 0x15, 0x58,
                0xab, 0xce, 0x00, 0x8b, 0xc7, 0x23, 0xc6, 0x8d,
                0x04, 0x40, 0x8b, 0x4c, 0x82, 0x08, 0xf6, 0xc1,
                0x01, 0x8d, 0x44, 0x82, 0x04, 0x75, 0x04, 0x85,
                0xc9, 0x75, 0x05, 0x33, 0xc9, 0x8d, 0x49, 0x00,
                0xf6, 0xc1, 0x01, 0x75, 0x4e, 0x85, 0xc9, 0x74,
                0x4a, 0x39, 0x31, 0x74, 0x13, 0x8b, 0xc7, 0x23,
                0xc6, 0x8d, 0x04, 0x40, 0x8d, 0x04, 0x82, 0x8b,
                0x00, 0x03, 0xc1, 0x8b, 0x48, 0x04, 0xeb, 0xe0,
                0x8b, 0x59, 0x1c, 0x8b, 0x71, 0x18, 0x33, 0xff,
                0x85, 0xdb, 0x7e, 0x27, 0x8d, 0x64, 0x24, 0x00,
                0x8b, 0x4e, 0x0c, 0x8b, 0x56, 0x08, 0x6a, 0x00,
                0x6a, 0x00, 0x51, 0x8b, 0x4d, 0xf8, 0x52, 0x8b,
                0x55, 0xfc, 0xe8, 0xb9, 0xfd, 0xff, 0xff, 0x84,
                0xc0, 0x75, 0x13, 0x47, 0x83, 0xc6, 0x20, 0x3b,
                0xfb, 0x7c, 0xdd, 0x5f, 0x5e, 0x33, 0xc0, 0x5b,
                0x8b, 0xe5, 0x5d, 0xc2, 0x04, 0x00, 0x5f, 0x8b,
                0xc6, 0x5e, 0x5b, 0x8b, 0xe5, 0x5d, 0xc2, 0x04,
                0x00, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90,
            ])),
        ]),
    ]
    # fmt: on


def _fits(buf: bytearray, offset: int, size: int) -> bool:
    return 0 <= offset and offset + size <= len(buf)


def patch_wow_exe(exe_path: Path, exe_bytes: bytes) -> None:
    buf = bytearray(exe_bytes)
    for label, kind, offset, value in build_tweaks(buf):
        if kind == "float" and offset is not None:
            if not _fits(buf, offset, 4):
                print(f"  Skipping {label}: offset {hex(offset)} past end of WoW.exe")
                continue
            print(f"  Applying: {label}")
            struct.pack_into("<f", buf, offset, float(value))
        elif kind == "int8" and offset is not None:
            if not _fits(buf, offset, 1):
                print(f"  Skipping {label}: offset {hex(offset)} past end of WoW.exe")
                continue
            print(f"  Applying: {label}")
            struct.pack_into("<b", buf, offset, int(value))
        elif kind == "uint16" and offset is not None:
            if not _fits(buf, offset, 2):
                print(f"  Skipping {label}: offset {hex(offset)} past end of WoW.exe")
                continue
            print(f"  Applying: {label}")
            struct.pack_into("<H", buf, offset, int(value))
        elif kind == "bytes":
            patches = value
            assert isinstance(patches, list)
            in_range = [
                (off, data) for off, data in patches if _fits(buf, off, len(data))
            ]
            skipped = len(patches) - len(in_range)
            if not in_range:
                print(f"  Skipping {label}: offsets past end of WoW.exe")
                continue
            extra = f" ({skipped} site(s) skipped)" if skipped else ""
            print(f"  Applying: {label}{extra}")
            for off, data in in_range:
                buf[off : off + len(data)] = data
        else:
            raise ValueError(f"Unknown tweak type: {kind}")
    exe_path.write_bytes(buf)


def scan_legacy_archives(client: Path) -> list[str]:
    data_dir = client / "Data"
    if not data_dir.is_dir():
        return []
    found: list[str] = []
    for name in data_dir.iterdir():
        if not name.is_file():
            continue
        expected = LEGACY_ARCHIVES.get(name.name.lower())
        if expected is None:
            continue
        if name.stat().st_size == expected:
            found.append(str(Path("Data") / name.name))
    return sorted(found)


def scan_data_changes(
    files: dict[str, int], client: Path
) -> tuple[list[str], list[str], list[str], int]:
    missing: list[str] = []
    outdated: list[str] = []
    download_bytes = 0

    for rel, size in files.items():
        if rel == WOW_EXE or is_skipped(rel):
            continue
        local = client / rel
        if not local.is_file():
            missing.append(rel)
            download_bytes += size
        elif local.stat().st_size != size:
            outdated.append(rel)
            download_bytes += size

    deletions = scan_legacy_archives(client)
    return missing, outdated, deletions, download_bytes


def download_bytes_for(files: dict[str, int], rel_paths: list[str]) -> int:
    return sum(files[rel] for rel in rel_paths if rel in files)


def print_data_changes(
    missing: list[str], outdated: list[str], deletions: list[str], total: int
) -> None:
    if missing:
        print("\nMissing (new):")
        for p in missing:
            print(f"  + {p}")
    if outdated:
        print("\nOutdated (size mismatch):")
        for p in outdated:
            print(f"  ~ {p}")
    if deletions:
        print("\nObsolete (legacy archives):")
        for p in deletions:
            print(f"  - {p}")
    if total:
        print(f"\nTotal download size (all changes): {format_size(total)}")


def prompt_download_choice(
    missing: list[str], outdated: list[str], deletions: list[str], files: dict[str, int]
) -> str:
    new_bytes = download_bytes_for(files, missing)
    outdated_bytes = download_bytes_for(files, outdated)
    all_bytes = new_bytes + outdated_bytes

    print("\nWhat would you like to download?")
    print(f"  1. Download new only ({len(missing)} file(s), {format_size(new_bytes)})")
    print(
        f"  2. Download outdated only ({len(outdated)} file(s), "
        f"{format_size(outdated_bytes)})"
    )
    print(
        f"  3. Download all ({len(missing) + len(outdated)} file(s), "
        f"{format_size(all_bytes)})"
    )
    if deletions:
        print(f"     (also removes {len(deletions)} obsolete file(s))")
    print("  4. Download none (cancel)")

    while True:
        choice = input("\nChoose an option [1-4]: ").strip()
        if choice == "1":
            return "new"
        if choice == "2":
            return "outdated"
        if choice == "3":
            return "all"
        if choice == "4":
            return "none"
        print("Invalid option.")


def apply_data_changes(
    files: dict[str, int],
    client: Path,
    missing: list[str],
    outdated: list[str],
    deletions: list[str],
    *,
    fetch_missing: bool = True,
    fetch_outdated: bool = True,
    apply_deletions: bool = True,
) -> None:
    to_fetch: set[str] = set()
    if fetch_missing:
        to_fetch |= set(missing)
    if fetch_outdated:
        to_fetch |= set(outdated)

    if apply_deletions:
        for rel in deletions:
            target = client / rel
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)

    if not to_fetch:
        return

    count = len(to_fetch)
    for i, rel in enumerate(sorted(to_fetch), 1):
        size = files[rel]
        print(f"[{i}/{count}] {rel}  ({format_size(size)})")
        download_file(rel, client / rel, size)


def wow_needs_update(client: Path, expected_size: int) -> str | None:
    wow = client / WOW_EXE
    patched_hash = load_patched_wow_hash(client)
    if wow.is_file() and patched_hash and sha1_of_file(wow) == patched_hash:
        return None
    if not wow.is_file():
        return "missing"
    if wow.stat().st_size != expected_size:
        return "outdated"
    return "not patched"


def run_data_updater(client: Path) -> None:
    print("\n--- Check for updates (game data) ---\n")
    print("WoW.exe is not included. Use menu option 2 for the executable.\n")

    print("Fetching torrent file list...")
    files = fetch_file_list()

    print("Scanning local files...")
    missing, outdated, deletions, total = scan_data_changes(files, client)

    if not missing and not outdated and not deletions:
        print("Game data is up to date.")
        return

    print_data_changes(missing, outdated, deletions, total)

    choice = prompt_download_choice(missing, outdated, deletions, files)
    if choice == "none":
        print("Cancelled.")
        return

    apply_data_changes(
        files,
        client,
        missing,
        outdated,
        deletions,
        fetch_missing=choice in ("new", "all"),
        fetch_outdated=choice in ("outdated", "all"),
        apply_deletions=choice == "all",
    )
    print("Done.")


def run_full_download(client: Path) -> None:
    print("\n--- Full download (game data + WoW.exe) ---\n")
    run_data_updater(client)
    run_wow_updater(client)


def run_wow_updater(client: Path) -> None:
    print("\n--- Download WoW.exe ---\n")

    print("Fetching torrent file list...")
    files = fetch_file_list()
    size = files.get(WOW_EXE)
    if size is None:
        print(f"{WOW_EXE} not found in torrent.")
        return

    reason = wow_needs_update(client, size)
    if reason is None:
        print(f"{WOW_EXE} is already installed and patched.")
        return

    wow = client / WOW_EXE
    need_download = reason != "not patched"
    action = "download and patch" if need_download else "patch"
    print(f"\nWill {action} {WOW_EXE} ({format_size(size)}).")
    print(
        "Patches: large-address, FOV 90°, camera, far clip, auto-loot, "
        "sound-in-background, camera skip, cross-faction resurrect, skill UI."
    )

    if input("\nProceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return

    if need_download:
        print(f"\nDownloading {WOW_EXE}...")
        download_file(WOW_EXE, wow, size)

    print("Patching WoW.exe...")
    patch_wow_exe(wow, wow.read_bytes())
    save_patched_wow_hash(client)
    print("Done.")


def ensure_client_dir(client: Path) -> bool:
    if client.is_dir():
        return True
    print(f"Client directory does not exist: {client}")
    print("Edit CLIENT_DIR at the top of updater.py")
    return False


def main() -> int:
    client = CLIENT_DIR.resolve()
    print("OctoWoW Client Updater")
    print(f"CDN: {CDN}")
    print(f"Client: {client}")

    if not ensure_client_dir(client):
        return 1

    while True:
        print("\n  1. Check for updates (game data, not WoW.exe)")
        print("  2. Download WoW.exe")
        print("  3. Full download (game data + WoW.exe)")
        print("  4. Quit")
        choice = input("\nChoose an option [1-4]: ").strip()

        if choice == "1":
            run_data_updater(client)
        elif choice == "2":
            run_wow_updater(client)
        elif choice == "3":
            run_full_download(client)
        elif choice == "4":
            print("Goodbye.")
            break
        else:
            print("Invalid option.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130) from None
    except urllib.error.URLError as e:
        print(f"Network error: {e}")
        raise SystemExit(1) from e
