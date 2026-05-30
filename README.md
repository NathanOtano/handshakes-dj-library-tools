# DJ Library Automation & Helpers

This repository contains a collection of PowerShell and Python scripts designed to automate, clean up, and synchronize a DJ library (Rekordbox and Serato). 

These scripts help in:
- **Library Auditing**: Finding exact audio duplicates, chromaprint (acoustic) overlap, and orphaned metadata.
- **Metadata Management**: Normalizing tags, adding missing files to Rekordbox, and cleaning up filenames (e.g., removing `_pn` suffixes after using Platinum Notes).
- **Synchronization**: Syncing smart playlists, relinking mounted drive paths, and matching your local library against streaming platform availability (e.g., TIDAL, Spotify).
- **Quality Control**: Measuring audio quality and verifying readiness of your tracks.

## Prerequisites

- **Python 3.x**: Ensure Python is installed.
- **PowerShell 7+**: Many entry point scripts are `.ps1`.
- **Dependencies**: You might need modules like `mutagen`, `requests`, `pyacoustid` (if using chromaprint features), and SQLite drivers for Rekordbox databases. Check the Python files for imports.

## Setup & Configuration

To adapt these scripts to your own setup, you must configure your paths.
Most scripts take arguments like `-MusicRoot` or `--music-root`. Alternatively, you can create a `config/paths.json` (if your fork implements it) or modify the default arguments in the scripts directly.

**Default Path Placeholders used in these scripts:**
- `C:\DJ_Music`: The root directory of your DJ library.
- `C:\DJ_Music\Processed_Library_Root`: The directory containing tracks processed by tools like Platinum Notes.
- `C:\DJ_Music\Playlists\ALL_TRACKS`: The directory containing your main playlist exports.
- `C:\DJ_Music\_DUPLICATE_QUARANTINE`: Where duplicates are moved.

## Structure

- **`scripts/`**: Contains all the automation logic.
  - `.ps1` files: Main entry points and task runners.
  - `.py` files: Core logic, database interactions, and metadata processing.
  - `.js` files: Scripts for integrations like OneTagger.

## Usage Examples

Most tools are designed to have a "Dry-run" or "Plan" mode first.

**Audit your library for duplicates:**
```powershell
pwsh -NoProfile -File .\scripts\Audit-DjLibraryCleanup.ps1 -AudioRoot "C:\DJ_Music" -AudioHashMode none -Json
```

**Add newly processed files to Rekordbox:**
```powershell
pwsh -NoProfile -File .\scripts\Add-RekordboxPlatinumContent.ps1 -Mode Plan -DuplicateCsv reports\local-duplicate-candidates.csv
```

> **Warning**: Modifying the Rekordbox `master.db` directly carries risks. Always ensure Rekordbox is closed and you have backups before running scripts with `-Apply` flags.

## Disclaimer

These scripts interact directly with Rekordbox databases and file systems. They are provided as-is without any warranties. Always back up your `master.db` and your audio files before running any destructive operations (like duplicate removals or relinking).
