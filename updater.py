#!/usr/bin/env python3
"""
OctoWoW client updater — single-file, stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --- Configuration (edit these) -----------------------------------------------

SERVER = "https://octowow.st"
VERSION = "latest"

# Linux
CLIENT_DIR = Path("/path/to/OctoWoW/")

# Windows (Uncomment CLIENT_DIR)
# CLIENT_DIR = Path(r"C:\Path\To\OctoWoW")

ALLOWED_HOST = urllib.parse.urlparse(SERVER).netloc
PATCHED_WOW_HASH_FILE = ".octo-updater-wow.sha1"
WOW_EXE = "WoW.exe"

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


def join_rel(*parts: str) -> str:
    cleaned = [p for p in parts if p]
    return os.path.join(*cleaned) if cleaned else ""


def manifest_paths(node: dict, prefix: list[str] | None = None) -> list[tuple[str, dict]]:
    if prefix is None:
        prefix = []
    name = node.get("name", "")
    here = prefix + ([name] if name else [])
    kind = node.get("type")

    if kind == "file":
        return [(join_rel(*here), node)]
    if kind == "del":
        return [(join_rel(*here), node)]
    if kind == "mpq":
        mpq_name = f"{name}.mpq"
        mpq_path = join_rel(*prefix, mpq_name) if prefix else mpq_name
        return [(mpq_path, {"type": "file", "name": mpq_name, "hash": node["hash"], "size": node["size"]})]
    if kind == "dir":
        out: list[tuple[str, dict]] = []
        for child in node.get("files", []):
            out.extend(manifest_paths(child, here))
        return out
    return []


def wow_manifest_entry(root: dict) -> dict | None:
    for rel, node in manifest_paths(root):
        if rel == WOW_EXE and node.get("type") == "file":
            return node
    return None


def fetch_manifest() -> dict:
    url = f"{SERVER}/api/file/{VERSION}/manifest.json"
    host = urllib.parse.urlparse(url).netloc
    if host != ALLOWED_HOST:
        raise SystemExit(f"Refusing request to unexpected host: {host}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["root"]


def download_file(rel_path: str, dest: Path, expected_size: int) -> None:
    url = f"{SERVER}/client/{VERSION}/{urllib.parse.quote(rel_path.replace(os.sep, '/'))}"
    host = urllib.parse.urlparse(url).netloc
    if host != ALLOWED_HOST:
        raise SystemExit(f"Refusing download from unexpected host: {host}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    done = 0
    rate_at = time.time()
    rate_done = 0

    with urllib.request.urlopen(url, timeout=300) as resp:
        with tmp.open("wb") as out:
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
                    eta = (expected_size - done) / speed if speed > 0 and expected_size else 0
                    sys.stdout.write(
                        f"\r  {format_size(done)} / {format_size(expected_size)} "
                        f"({pct:.1f}%)  {format_size(speed)}/s  ETA {format_duration(eta)}   "
                    )
                    sys.stdout.flush()

    if tmp.stat().st_size != expected_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Size mismatch for {rel_path}")
    tmp.replace(dest)
    if expected_size > 0:
        sys.stdout.write(
            f"\r  {format_size(expected_size)} / {format_size(expected_size)} "
            f"(100.0%)  done\n"
        )
        sys.stdout.flush()


def load_patched_wow_hash(client: Path) -> str | None:
    path = client / PATCHED_WOW_HASH_FILE
    if path.is_file():
        return path.read_text(encoding="utf-8").strip().upper()
    return None


def save_patched_wow_hash(client: Path) -> None:
    (client / PATCHED_WOW_HASH_FILE).write_text(
        sha1_of_file(client / WOW_EXE), encoding="utf-8"
    )


def patch_wow_exe(exe_path: Path, exe_bytes: bytes) -> None:
    """Always-on launcher patches only."""
    buf = bytearray(exe_bytes)
    buf[0x006E5FB8 : 0x006E5FB8 + 1] = bytes([0xB9])
    buf[0x006E62A8 : 0x006E62A8 + 1] = bytes([0xA9])
    buf[0x002DDF90 : 0x002DDF90 + 136] = bytes([
        0x55, 0x8B, 0xEC, 0x83, 0xEC, 0x08, 0x53, 0x56, 0x57, 0x8B, 0x3D, 0x60, 0xAB, 0xCE,
        0x00, 0x83, 0xFF, 0xFF, 0x89, 0x55, 0xFC, 0x89, 0x4D, 0xF8, 0x74, 0x79, 0x8B, 0x75,
        0x08, 0x8B, 0x15, 0x58, 0xAB, 0xCE, 0x00, 0x8B, 0xC7, 0x23, 0xC6, 0x8D, 0x04, 0x40,
        0x8B, 0x4C, 0x82, 0x08, 0xF6, 0xC1, 0x01, 0x8D, 0x44, 0x82, 0x04, 0x75, 0x04, 0x85,
        0xC9, 0x75, 0x05, 0x33, 0xC9, 0x8D, 0x49, 0x00, 0xF6, 0xC1, 0x01, 0x75, 0x4E, 0x85,
        0xC9, 0x74, 0x4A, 0x39, 0x31, 0x74, 0x13, 0x8B, 0xC7, 0x23, 0xC6, 0x8D, 0x04, 0x40,
        0x8D, 0x04, 0x82, 0x8B, 0x00, 0x03, 0xC1, 0x8B, 0x48, 0x04, 0xEB, 0xE0, 0x8B, 0x59,
        0x1C, 0x8B, 0x71, 0x18, 0x33, 0xFF, 0x85, 0xDB, 0x7E, 0x27, 0x8D, 0x64, 0x24, 0x00,
        0x8B, 0x4E, 0x0C, 0x8B, 0x56, 0x08, 0x6A, 0x00, 0x6A, 0x00, 0x51, 0x8B, 0x4D, 0xF8,
        0x52, 0x8B, 0x55, 0xFC, 0xE8, 0xB9, 0xFD, 0xFF, 0xFF, 0x84, 0xC0, 0x75, 0x13, 0x47,
        0x83, 0xC6, 0x20, 0x3B, 0xFB, 0x7C, 0xDD, 0x5F, 0x5E, 0x33, 0xC0, 0x5B, 0x8B, 0xE5,
        0x5D, 0xC2, 0x04, 0x00, 0x5F, 0x8B, 0xC6, 0x5E, 0x5B, 0x8B, 0xE5, 0x5D, 0xC2, 0x04,
        0x00, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90,
    ])
    exe_path.write_bytes(buf)


def scan_data_changes(
    root: dict, client: Path
) -> tuple[list[str], list[str], list[str], int]:
    """Scan manifest for game data; WoW.exe is handled separately."""
    missing: list[str] = []
    outdated: list[str] = []
    deletions: list[str] = []
    download_bytes = 0

    for rel, node in manifest_paths(root):
        if rel == WOW_EXE:
            continue

        if node.get("type") == "del":
            if (client / rel).exists():
                deletions.append(rel)
            continue

        expected_hash = node["hash"]
        size = int(node["size"])
        local = client / rel

        if not local.is_file():
            missing.append(rel)
            download_bytes += size
        elif sha1_of_file(local) != expected_hash:
            outdated.append(rel)
            download_bytes += size

    return missing, outdated, deletions, download_bytes


def download_bytes_for(root: dict, rel_paths: list[str]) -> int:
    sizes = {
        rel: int(node["size"])
        for rel, node in manifest_paths(root)
        if node.get("type") == "file"
    }
    return sum(sizes[rel] for rel in rel_paths if rel in sizes)


def print_data_changes(
    missing: list[str], outdated: list[str], deletions: list[str], total: int
) -> None:
    if missing:
        print("\nMissing (new):")
        for p in missing:
            print(f"  + {p}")
    if outdated:
        print("\nOutdated:")
        for p in outdated:
            print(f"  ~ {p}")
    if deletions:
        print("\nObsolete (local files not in manifest):")
        for p in deletions:
            print(f"  - {p}")
    if total:
        print(f"\nTotal download size (all changes): {format_size(total)}")


def prompt_download_choice(
    missing: list[str], outdated: list[str], deletions: list[str], root: dict
) -> str:
    new_bytes = download_bytes_for(root, missing)
    outdated_bytes = download_bytes_for(root, outdated)
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
    root: dict,
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
    paths = {
        rel: node
        for rel, node in manifest_paths(root)
        if node.get("type") == "file" and rel != WOW_EXE
    }

    if apply_deletions:
        for rel in deletions:
            if rel == WOW_EXE:
                continue
            target = client / rel
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)

    if not to_fetch:
        return

    count = len(to_fetch)
    for i, rel in enumerate(sorted(to_fetch), 1):
        node = paths[rel]
        size = int(node["size"])
        print(f"[{i}/{count}] {rel}  ({format_size(size)})")
        download_file(rel, client / rel, size)


def wow_needs_update(client: Path, manifest_entry: dict) -> str | None:
    """Return reason WoW.exe needs work, or None if already patched and current."""
    wow = client / WOW_EXE
    patched_hash = load_patched_wow_hash(client)
    if wow.is_file() and patched_hash and sha1_of_file(wow) == patched_hash:
        return None
    if not wow.is_file():
        return "missing"
    return "outdated or not patched"


def print_wow_warning() -> None:
    print(
        "\n"
        "WARNING: This only downloads WoW.exe and applies required server patches\n"
        "(cross-faction resurrect, skill UI). It does NOT enable launcher tweak options\n"
        "such as auto-loot, FOV, camera distance, or far clip. Use the official\n"
        "launcher if you want those features.\n"
    )


def run_data_updater(client: Path) -> None:
    print("\n--- Check for updates (game data) ---\n")
    print("WoW.exe is not included. Use menu option 2 for the executable.\n")

    print("Fetching manifest...")
    root = fetch_manifest()

    print("Scanning local files...")
    missing, outdated, deletions, total = scan_data_changes(root, client)

    if not missing and not outdated and not deletions:
        print("Game data is up to date.")
        return

    print_data_changes(missing, outdated, deletions, total)

    choice = prompt_download_choice(missing, outdated, deletions, root)
    if choice == "none":
        print("Cancelled.")
        return

    apply_data_changes(
        root,
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
    print("\n--- Download WoW.exe ---")
    print_wow_warning()

    print("Fetching manifest...")
    root = fetch_manifest()
    entry = wow_manifest_entry(root)
    if not entry:
        print(f"{WOW_EXE} not found in manifest.")
        return

    reason = wow_needs_update(client, entry)
    if reason is None:
        print(f"{WOW_EXE} is already installed and patched.")
        return

    size = int(entry["size"])
    print(f"\nWill download {WOW_EXE} ({format_size(size)}) and apply required patches.")

    if input("\nProceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return

    print(f"\nDownloading {WOW_EXE}...")
    download_file(WOW_EXE, client / WOW_EXE, size)

    print("Patching WoW.exe...")
    wow = client / WOW_EXE
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
    print(f"Server: {SERVER}")
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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130) from None
    except urllib.error.URLError as e:
        print(f"Network error: {e}")
        raise SystemExit(1) from e
