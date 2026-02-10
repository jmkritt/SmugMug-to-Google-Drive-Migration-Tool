# SmugMug → Google Drive Migration Tool

A simple desktop app that migrates all your photos and videos from SmugMug to Google Drive, preserving your complete album and folder structure.

Built entirely through vibecoding — no prior programming experience. Just an idea, a conversation with AI, and a working app.

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Mac%20%7C%20Linux-lightgrey)

---

## What It Does

- **Migrates everything** — all photos, videos, and albums from your SmugMug account to Google Drive
- **Preserves folder structure** — your SmugMug album hierarchy is recreated in Drive
- **Simple GUI** — no command line needed; point-and-click interface with progress bars
- **Resume anytime** — stop and restart without losing progress; picks up where it left off
- **Skip duplicates** — won't re-upload files already in Google Drive
- **Retry failures** — one-click retry for any files that failed
- **Your photos stay safe** — read-only SmugMug access; nothing is modified or deleted

## Screenshot

```
┌──────────────────────────────────────────────────────┐
│  ⚙ Setup  │  🚀 Migrate  │  📋 Log  │  ℹ About     │
├──────────────────────────────────────────────────────┤
│  Status: Migrating...                                │
│  Album: Family / Vacation 2023 (47 files)            │
│                                                      │
│  Overall:    ████████████░░░░░░░░  847 / 1,432       │
│  Album:      ██████████████░░░░░░  31 / 47           │
│                                                      │
│  Migrated: 812    Skipped: 35    Failed: 0           │
│                                                      │
│  [ ▶ Start Migration ]  [ ⏹ Stop ]  [ 🔄 Reset ]   │
└──────────────────────────────────────────────────────┘
```

## Quick Start

### Run from Python
```bash
pip install requests requests-oauthlib google-auth google-auth-oauthlib google-api-python-client
python smugmug_to_gdrive_gui.py
```

### Build a Windows .exe
```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name "SmugMug2GDrive" smugmug_to_gdrive_gui.py
```

## Setup (One-Time)

You need two sets of free API credentials:

### SmugMug API Key
1. Go to https://api.smugmug.com/api/developer/apply
2. Apply for a key with **Read** access
3. Copy your **API Key** and **API Secret**

### Google Drive API Credentials
1. Go to https://console.cloud.google.com/
2. Create a project and enable the **Google Drive API**
3. Create **OAuth 2.0 credentials** (Desktop App)
4. Download the credentials JSON file

### In the App
1. Open the **Setup** tab
2. Enter your SmugMug credentials → click **Connect SmugMug**
3. Browse to your Google JSON file → click **Connect Google Drive**
4. Click **Save Settings**
5. Go to the **Migrate** tab → click **Start Migration**

That's it. See [HELP.txt](HELP.txt) for the full step-by-step guide with troubleshooting.

## How It Works

1. Connects to SmugMug and fetches all your albums
2. Creates matching folders in Google Drive
3. Downloads each photo to a temp file, uploads to Drive, deletes the temp file
4. Saves progress after every file — fully resumable

Your SmugMug photos are **never modified or deleted**. The tool uses read-only permissions.

## Files

| File | Description |
|------|-------------|
| `smugmug_to_gdrive_gui.py` | Main app (GUI version) |
| `smugmug_to_gdrive.py` | Command-line version |
| `build_exe.bat` | Windows .exe build script |
| `HELP.txt` | Complete help guide |
| `LICENSE` | MIT License |
| `.env.example` | Template for CLI credentials |

## Performance

| Library Size | Estimated Time |
|---|---|
| 100 photos | ~5-10 minutes |
| 1,000 photos | ~1-2 hours |
| 5,000 photos | ~5-8 hours |
| 10,000+ photos | Overnight |

## License

MIT License © 2026 Jeremy Kritt

This software is provided "as is" without warranty of any kind. Not affiliated with SmugMug, Inc. or Google LLC.

---

*Built through vibecoding. First program ever. 🤙*
