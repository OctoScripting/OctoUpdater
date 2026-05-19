# OctoUpdater

A small, open-source game client updater for OctoWoW. It does the same basic job as the official launcher’s updater, but in one Python file you can read before running.

**No install step.** Python 3.10+ and the standard library only.

## What it does

On startup you get a simple menu:

1. **Check for updates** — game data (MPQs, DLLs, etc.). Does not touch `WoW.exe`.
2. **Download WoW.exe** — downloads the executable, applies required server patches only, and warns that launcher tweak options (auto-loot, FOV, camera, etc.) are **not** enabled.
3. **Full download** — runs options 1 and 2 in sequence (each step confirms separately if needed).
4. **Quit**

Each download option fetches the manifest from `https://octowow.st`, shows what will change, and asks for confirmation before writing files.

## What it does **not** do

- Does not upload or send your files anywhere
- Does not talk to any server except `octowow.st` (see `SERVER` in the script)
- Does not run other programs (no subprocess)
- Does not install the official launcher

## Quick start

1. Open `python/updater.py` in a text editor.
2. Set `CLIENT_DIR` to your game folder (the folder that should contain `WoW.exe`).
3. Run:

```bash
python3 python/updater.py
```

4. Read the list of changes. Type `y` only if you agree.

## Configuration

At the top of `python/updater.py`:

| Setting | Meaning |
|---------|---------|
| `SERVER` | Update server (default `https://octowow.st`) |
| `VERSION` | Client version channel (default `latest`) |
| `CLIENT_DIR` | Path to your WoW install |

## Trust and auditing

**Network:** All downloads use URLs under `SERVER`. The script checks the host name before each request.

**Disk:** Files are only written under `CLIENT_DIR`. Deletions only remove paths listed in the manifest’s delete entries.

**WoW.exe:** Menu option 2 only. `patch_wow_exe` applies two always-on changes from `OctoLauncher/src/main/modules/patcher.ts` (`crossFactionResurrect`, `skillUiGateHijack`). A hash file (`.octo-updater-wow.sha1`) records the patched exe. Graphics/gameplay tweaks from the launcher UI are not applied.

**Dependencies:** Search the file for `import` — everything is Python’s built-in library.

## Requirements

- Python 3.10 or newer
- Enough disk space for listed downloads
- Internet access to `octowow.st`