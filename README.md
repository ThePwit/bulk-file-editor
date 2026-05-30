# Bulk File Editor

A Tkinter desktop app for safely previewing and applying bulk file renames, episode numbering, filename cleanup, filtering, content edits, backups, and undo support.

## Features

- Preview bulk rename and content-edit plans before writing changes
- Filter files by glob, extension, name, size, and modified date
- Rename with templates such as `S{season:02}E{n:02}{dotext}`
- Detect season numbers from folder names, including multi-season folders
- Reset episode counters per season
- Update only the first episode number in existing filenames
- Add prefixes, suffixes, extension changes, case changes, and regex replacements
- Perform simple text content edits with optional backups
- Skip unsafe rename targets, duplicate targets, and existing-file collisions
- Use two-phase renaming for swaps and case-only renames
- Undo the most recent rename batch
- Switch between light and dark themes

## Requirements

- Python 3.10 or newer
- Tkinter, which is included with most standard Python installers

No third-party packages are required to run the app.

## Run

On Windows, double-click:

```text
Launch Bulk File Editor.bat
```

The launcher uses your normal `python` command when Tkinter is working. If that Python install cannot start Tkinter, it falls back to the working Inkscape-bundled Python if it is available.

You can also run the script directly:

```powershell
python .\bulk_name_edit.py
```

The app starts in dry-run mode. Leave dry-run on while building a plan, use **Preview Plan** to inspect the changes, then turn dry-run off only when you are ready to apply them.

## Test

```powershell
python -m unittest discover -s . -p "test_*.py"
```

## Safety Notes

Bulk rename tools can make large changes very quickly. This app is designed to reduce that risk by previewing plans, validating target names, skipping collisions, keeping optional backups for content edits, and recording rename batches for undo.

For important files, keep dry-run enabled until the preview looks correct and make sure you have a backup outside the target folder.
