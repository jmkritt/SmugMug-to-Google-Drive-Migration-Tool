#!/usr/bin/env python3
"""
SmugMug → Google Drive Migration Tool (GUI) v2.0
==================================================
A Windows-friendly desktop app with a simple menu to migrate
all photos and videos from SmugMug to Google Drive.

Created by Jeremy Kritt
Licensed under the MIT License
https://github.com/jeremykritt/smugmug-to-gdrive

Build as .exe:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "SmugMug2GDrive" --icon=icon.ico smugmug_to_gdrive_gui.py
"""

import os
import sys
import json
import time
import threading
import webbrowser
import tempfile
import mimetypes
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
from pathlib import Path
from typing import Optional
from datetime import datetime

import requests
from requests_oauthlib import OAuth1Session

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# ---------------------------------------------------------------------------
# Resolve paths — works both in dev and inside a PyInstaller bundle
# ---------------------------------------------------------------------------
def get_app_dir() -> Path:
    """Get the directory where the app stores its config/state files."""
    if getattr(sys, "frozen", False):
        # Running as compiled .exe
        app_dir = Path(os.environ.get("APPDATA", Path.home())) / "SmugMug2GDrive"
    else:
        app_dir = Path(__file__).parent
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


APP_DIR = get_app_dir()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SMUGMUG_API_BASE = "https://api.smugmug.com"
SMUGMUG_REQUEST_TOKEN_URL = "https://secure.smugmug.com/services/oauth/1.0a/getRequestToken"
SMUGMUG_AUTHORIZE_URL = "https://secure.smugmug.com/services/oauth/1.0a/authorize"
SMUGMUG_ACCESS_TOKEN_URL = "https://secure.smugmug.com/services/oauth/1.0a/getAccessToken"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CONFIG_FILE = APP_DIR / "config.json"
GOOGLE_TOKEN_FILE = APP_DIR / "google_token.json"
SMUGMUG_TOKEN_FILE = APP_DIR / "smugmug_token.json"
STATE_FILE = APP_DIR / "migration_state.json"
LOG_FILE = APP_DIR / "migration.log"


# ---------------------------------------------------------------------------
# Config Persistence
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# SmugMug Client
# ---------------------------------------------------------------------------
class SmugMugClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session: Optional[OAuth1Session] = None

    def has_saved_token(self) -> bool:
        return SMUGMUG_TOKEN_FILE.exists()

    def authenticate_with_saved_token(self) -> bool:
        if not SMUGMUG_TOKEN_FILE.exists():
            return False
        with open(SMUGMUG_TOKEN_FILE, "r") as f:
            tokens = json.load(f)
        self.session = OAuth1Session(
            self.api_key,
            client_secret=self.api_secret,
            resource_owner_key=tokens["oauth_token"],
            resource_owner_secret=tokens["oauth_token_secret"],
        )
        resp = self._get("/api/v2!authuser")
        return resp is not None

    def get_authorization_url(self):
        oauth = OAuth1Session(self.api_key, client_secret=self.api_secret, callback_uri="oob")
        fetch_response = oauth.fetch_request_token(SMUGMUG_REQUEST_TOKEN_URL)
        self._request_key = fetch_response["oauth_token"]
        self._request_secret = fetch_response["oauth_token_secret"]
        return oauth.authorization_url(SMUGMUG_AUTHORIZE_URL, Access="Full", Permissions="Read")

    def complete_authorization(self, verifier: str) -> bool:
        oauth = OAuth1Session(
            self.api_key,
            client_secret=self.api_secret,
            resource_owner_key=self._request_key,
            resource_owner_secret=self._request_secret,
            verifier=verifier,
        )
        tokens = oauth.fetch_access_token(SMUGMUG_ACCESS_TOKEN_URL)
        with open(SMUGMUG_TOKEN_FILE, "w") as f:
            json.dump(tokens, f)
        self.session = OAuth1Session(
            self.api_key,
            client_secret=self.api_secret,
            resource_owner_key=tokens["oauth_token"],
            resource_owner_secret=tokens["oauth_token_secret"],
        )
        return True

    def _get(self, uri: str, params: Optional[dict] = None) -> Optional[dict]:
        url = uri if uri.startswith("http") else f"{SMUGMUG_API_BASE}{uri}"
        try:
            resp = self.session.get(url, headers={"Accept": "application/json"}, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def get_authenticated_user(self) -> dict:
        data = self._get("/api/v2!authuser")
        return data["Response"]["User"]

    def get_user_albums_uri(self, user: dict) -> str:
        """Extract the albums URI from user data, handling different API response formats."""
        # Try the nested Uris format
        uris = user.get("Uris", {})
        
        # Format 1: Uris contain objects with Uri key
        user_albums = uris.get("UserAlbums", {})
        if isinstance(user_albums, dict) and "Uri" in user_albums:
            return user_albums["Uri"]
        
        # Format 2: Uris contain direct URI strings
        if isinstance(user_albums, str):
            return user_albums
        
        # Format 3: Build from NickName
        nickname = user.get("NickName", user.get("Name", ""))
        if nickname:
            return f"/api/v2/user/{nickname}!albums"
        
        return ""

    def get_albums(self, albums_uri: str) -> list:
        albums = []
        # If the URI doesn't end with !albums, append it
        if albums_uri and not albums_uri.endswith("!albums"):
            albums_uri = f"{albums_uri}!albums"
        
        params = {"count": 100, "start": 1}
        while True:
            data = self._get(albums_uri, params=params)
            if not data or "Response" not in data:
                break
            page = data["Response"].get("Album", [])
            if not page:
                break
            albums.extend(page)
            pages = data["Response"].get("Pages", {})
            if params["start"] + len(page) > pages.get("Total", 0):
                break
            params["start"] += len(page)
        return albums

    def get_album_images(self, album_key: str) -> list:
        images = []
        uri = f"/api/v2/album/{album_key}!images"
        params = {"count": 100, "start": 1}
        while True:
            data = self._get(uri, params=params)
            if not data or "Response" not in data:
                break
            page = data["Response"].get("AlbumImage", [])
            if not page:
                break
            images.extend(page)
            pages = data["Response"].get("Pages", {})
            if params["start"] + len(page) > pages.get("Total", 0):
                break
            params["start"] += len(page)
        return images

    def get_image_download_url(self, image_uri: str) -> Optional[str]:
        data = self._get(f"{image_uri}!largestimage")
        if data and "Response" in data:
            url = data["Response"].get("LargestImage", {}).get("Url")
            if url:
                return url
        data = self._get(f"{image_uri}!download")
        if data and "Response" in data:
            return data["Response"].get("ImageDownload", {}).get("Url")
        return None

    def download_image(self, url: str, dest: str) -> bool:
        try:
            resp = self.session.get(url, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Google Drive Client
# ---------------------------------------------------------------------------
class GoogleDriveClient:
    def __init__(self):
        self.service = None
        self._folder_cache = {}

    def authenticate(self, creds_file: str) -> bool:
        creds = None
        if GOOGLE_TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_FILE), GOOGLE_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(GoogleAuthRequest())
            else:
                if not os.path.exists(creds_file):
                    return False
                flow = InstalledAppFlow.from_client_secrets_file(creds_file, GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(GOOGLE_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        self.service = build("drive", "v3", credentials=creds)
        return True

    def get_or_create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        cache_key = f"{parent_id or 'root'}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        results = self.service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])
        if files:
            fid = files[0]["id"]
        else:
            meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
            if parent_id:
                meta["parents"] = [parent_id]
            fid = self.service.files().create(body=meta, fields="id").execute()["id"]
        self._folder_cache[cache_key] = fid
        return fid

    def upload_file(self, filepath: str, filename: str, folder_id: str) -> str:
        mime_type, _ = mimetypes.guess_type(filepath)
        mime_type = mime_type or "application/octet-stream"
        meta = {"name": filename, "parents": [folder_id]}
        media = MediaFileUpload(filepath, mimetype=mime_type, resumable=True)
        return self.service.files().create(body=meta, media_body=media, fields="id").execute()["id"]

    def file_exists(self, filename: str, folder_id: str) -> bool:
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        results = self.service.files().list(q=query, fields="files(id)").execute()
        return len(results.get("files", [])) > 0


# ---------------------------------------------------------------------------
# Migration State
# ---------------------------------------------------------------------------
class MigrationState:
    def __init__(self):
        self.migrated: set = set()
        self.failed: dict = {}
        self._load()

    def _load(self):
        if STATE_FILE.exists():
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            self.migrated = set(data.get("migrated", []))
            self.failed = data.get("failed", {})

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump({"migrated": list(self.migrated), "failed": self.failed}, f)

    def mark_done(self, key: str):
        self.migrated.add(key)
        self.failed.pop(key, None)

    def mark_failed(self, key: str, err: str):
        self.failed[key] = err

    def is_done(self, key: str) -> bool:
        return key in self.migrated

    def reset(self):
        self.migrated.clear()
        self.failed.clear()
        if STATE_FILE.exists():
            STATE_FILE.unlink()


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------
class MigrationApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SmugMug to Google Drive Migration v2.0")
        self.geometry("750x750")
        self.resizable(True, True)
        self.minsize(650, 600)

        self.config_data = load_config()
        self.smugmug: Optional[SmugMugClient] = None
        self.gdrive: Optional[GoogleDriveClient] = None
        self.migration_thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.fetched_albums: list = []
        self.album_vars: dict = {}

        self._build_ui()
        self._load_saved_values()

    # ---------------------------------------------------------------
    # UI Construction
    # ---------------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        # --- Notebook (tabs) ---
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Tab 1: Setup
        setup_frame = ttk.Frame(notebook, padding=15)
        notebook.add(setup_frame, text="  ⚙  Setup  ")

        # Tab 2: Migrate
        migrate_frame = ttk.Frame(notebook, padding=15)
        notebook.add(migrate_frame, text="  🚀  Migrate  ")

        # Tab 3: Log
        log_frame = ttk.Frame(notebook, padding=15)
        notebook.add(log_frame, text="  📋  Log  ")

        # Tab 4: Help
        help_frame = ttk.Frame(notebook, padding=15)
        notebook.add(help_frame, text="  ❓  Help  ")

        # Tab 5: About
        about_frame = ttk.Frame(notebook, padding=15)
        notebook.add(about_frame, text="  ℹ  About  ")

        self._build_setup_tab(setup_frame)
        self._build_migrate_tab(migrate_frame)
        self._build_log_tab(log_frame)
        self._build_help_tab(help_frame)
        self._build_about_tab(about_frame)

    def _build_setup_tab(self, parent):
        # --- SmugMug Section ---
        sm_frame = ttk.LabelFrame(parent, text="SmugMug API Credentials", padding=10)
        sm_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(sm_frame, text="API Key:").grid(row=0, column=0, sticky="w", pady=3)
        self.sm_key_var = tk.StringVar()
        ttk.Entry(sm_frame, textvariable=self.sm_key_var, width=55).grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(sm_frame, text="API Secret:").grid(row=1, column=0, sticky="w", pady=3)
        self.sm_secret_var = tk.StringVar()
        ttk.Entry(sm_frame, textvariable=self.sm_secret_var, width=55, show="•").grid(row=1, column=1, padx=5, pady=3)

        sm_btn_frame = ttk.Frame(sm_frame)
        sm_btn_frame.grid(row=2, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(sm_btn_frame, text="Get API Key →", command=self._open_smugmug_apply).pack(side="left", padx=3)
        ttk.Button(sm_btn_frame, text="Connect SmugMug", command=self._connect_smugmug).pack(side="left", padx=3)
        self.sm_status = ttk.Label(sm_btn_frame, text="", foreground="gray")
        self.sm_status.pack(side="left", padx=10)

        # --- Google Drive Section ---
        gd_frame = ttk.LabelFrame(parent, text="Google Drive Credentials", padding=10)
        gd_frame.pack(fill="x", pady=(0, 10))

        gd_file_frame = ttk.Frame(gd_frame)
        gd_file_frame.pack(fill="x")
        ttk.Label(gd_file_frame, text="Credentials JSON:").pack(side="left")
        self.gd_path_var = tk.StringVar()
        ttk.Entry(gd_file_frame, textvariable=self.gd_path_var, width=42).pack(side="left", padx=5)
        ttk.Button(gd_file_frame, text="Browse…", command=self._browse_google_creds).pack(side="left")

        gd_btn_frame = ttk.Frame(gd_frame)
        gd_btn_frame.pack(pady=(8, 0))
        ttk.Button(gd_btn_frame, text="How to Get Credentials →", command=self._open_google_console).pack(side="left", padx=3)
        ttk.Button(gd_btn_frame, text="Connect Google Drive", command=self._connect_google).pack(side="left", padx=3)
        self.gd_status = ttk.Label(gd_btn_frame, text="", foreground="gray")
        self.gd_status.pack(side="left", padx=10)

        # --- Options ---
        opt_frame = ttk.LabelFrame(parent, text="Migration Options", padding=10)
        opt_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(opt_frame, text="Google Drive folder name:").grid(row=0, column=0, sticky="w", pady=3)
        self.folder_var = tk.StringVar(value="SmugMug Migration")
        ttk.Entry(opt_frame, textvariable=self.folder_var, width=40).grid(row=0, column=1, padx=5, pady=3)

        self.skip_existing_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="Skip files already in Google Drive", variable=self.skip_existing_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=3
        )

        self.retry_failed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Retry previously failed files", variable=self.retry_failed_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=3
        )

        # --- Save button ---
        ttk.Button(parent, text="💾  Save Settings", command=self._save_settings).pack(pady=5)

    def _build_migrate_tab(self, parent):
        # --- Album Selection ---
        album_frame = ttk.LabelFrame(parent, text="Album Selection", padding=10)
        album_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Fetch & select buttons
        album_btn_frame = ttk.Frame(album_frame)
        album_btn_frame.pack(fill="x", pady=(0, 8))
        self.fetch_btn = ttk.Button(album_btn_frame, text="📂  Fetch Albums from SmugMug", command=self._fetch_albums)
        self.fetch_btn.pack(side="left", padx=3)
        ttk.Button(album_btn_frame, text="Select All", command=self._select_all_albums).pack(side="left", padx=3)
        ttk.Button(album_btn_frame, text="Select None", command=self._select_no_albums).pack(side="left", padx=3)
        self.album_count_label = ttk.Label(album_btn_frame, text="", foreground="gray")
        self.album_count_label.pack(side="right", padx=5)

        # Scrollable album checklist
        album_list_frame = ttk.Frame(album_frame)
        album_list_frame.pack(fill="both", expand=True)

        self.album_canvas = tk.Canvas(album_list_frame, highlightthickness=0)
        album_scrollbar = ttk.Scrollbar(album_list_frame, orient="vertical", command=self.album_canvas.yview)
        self.album_inner_frame = ttk.Frame(self.album_canvas)

        self.album_inner_frame.bind("<Configure>", lambda e: self.album_canvas.configure(scrollregion=self.album_canvas.bbox("all")))
        self.album_canvas.create_window((0, 0), window=self.album_inner_frame, anchor="nw")
        self.album_canvas.configure(yscrollcommand=album_scrollbar.set)

        self.album_canvas.pack(side="left", fill="both", expand=True)
        album_scrollbar.pack(side="right", fill="y")

        # Enable mousewheel scrolling
        def _on_mousewheel(event):
            self.album_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.album_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # Placeholder text
        self.album_placeholder = ttk.Label(self.album_inner_frame,
            text="Click \"Fetch Albums\" to load your SmugMug albums.\nThen select which ones to migrate.",
            foreground="gray", justify="center")
        self.album_placeholder.pack(pady=30)

        # --- Status & Progress ---
        status_frame = ttk.LabelFrame(parent, text="Status", padding=8)
        status_frame.pack(fill="x", pady=(0, 5))

        self.status_label = ttk.Label(status_frame, text="Fetch albums, select what to migrate, then click Start.", wraplength=650)
        self.status_label.pack(anchor="w")

        self.album_label = ttk.Label(status_frame, text="", foreground="gray")
        self.album_label.pack(anchor="w", pady=(3, 0))

        # Progress bars
        prog_frame = ttk.LabelFrame(parent, text="Progress", padding=8)
        prog_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(prog_frame, text="Overall:").pack(anchor="w")
        self.overall_progress = ttk.Progressbar(prog_frame, mode="determinate", length=600)
        self.overall_progress.pack(fill="x", pady=(2, 4))
        self.overall_pct_label = ttk.Label(prog_frame, text="0 / 0")
        self.overall_pct_label.pack(anchor="e")

        ttk.Label(prog_frame, text="Current album:").pack(anchor="w")
        self.album_progress = ttk.Progressbar(prog_frame, mode="determinate", length=600)
        self.album_progress.pack(fill="x", pady=(2, 4))
        self.album_pct_label = ttk.Label(prog_frame, text="0 / 0")
        self.album_pct_label.pack(anchor="e")

        # Counters
        counter_frame = ttk.Frame(parent)
        counter_frame.pack(fill="x", pady=(0, 5))
        self.migrated_label = ttk.Label(counter_frame, text="Migrated: 0")
        self.migrated_label.pack(side="left", padx=15)
        self.skipped_label = ttk.Label(counter_frame, text="Skipped: 0")
        self.skipped_label.pack(side="left", padx=15)
        self.failed_label = ttk.Label(counter_frame, text="Failed: 0", foreground="red")
        self.failed_label.pack(side="left", padx=15)

        # Buttons
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(pady=5)
        self.start_btn = ttk.Button(btn_frame, text="\u25B6  Start Migration", command=self._start_migration)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="\u23F9  Stop", command=self._stop_migration, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        ttk.Button(btn_frame, text="\U0001F504  Reset Progress", command=self._reset_progress).pack(side="left", padx=5)

    def _build_log_tab(self, parent):
        self.log_text = scrolledtext.ScrolledText(parent, wrap="word", height=25, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        ttk.Button(parent, text="Clear Log", command=self._clear_log).pack(pady=5)

    def _build_help_tab(self, parent):
        help_text = scrolledtext.ScrolledText(parent, wrap="word", font=("Arial", 10), state="disabled")
        help_text.pack(fill="both", expand=True)

        content = """QUICK START GUIDE
══════════════════════════════════════════════════

1. SETUP (one-time)
   • Go to the Setup tab
   • Enter your SmugMug API Key and Secret, then click "Connect SmugMug"
   • Browse to your Google credentials JSON, then click "Connect Google Drive"
   • Click "Save Settings"

2. MIGRATE
   • Go to the Migrate tab
   • Click "Fetch Albums" to load your SmugMug albums
   • Check/uncheck albums to choose what to migrate
   • Click "Start Migration"


GETTING YOUR API CREDENTIALS
══════════════════════════════════════════════════

SmugMug API Key:
   1. Go to https://api.smugmug.com/api/developer/apply
   2. Log in and apply for a key with Read access
   3. Copy the API Key and API Secret

Google Drive Credentials:
   1. Go to https://console.cloud.google.com/
   2. Create a project → enable the "Google Drive API"
   3. Go to APIs & Services → Credentials
   4. Click Create Credentials → OAuth client ID → Desktop App
   5. Download the JSON file


MIGRATION OPTIONS
══════════════════════════════════════════════════

Google Drive folder name:
   The root folder created in your Drive. Default: "SmugMug Migration"

Skip files already in Google Drive:
   When checked, files that already exist in the destination won't be
   re-uploaded. Leave this on for resume capability.

Retry previously failed files:
   When checked, files that failed on a previous run will be attempted again.


HOW IT WORKS
══════════════════════════════════════════════════

   • The tool downloads each photo from SmugMug to a temporary file on
     your computer, then uploads it to Google Drive
   • Your SmugMug album folder structure is recreated in Google Drive
   • Progress is saved after every file — you can stop and resume anytime
   • Temporary files are deleted immediately after each upload
   • The tool does NOT modify or delete anything on SmugMug (read-only)


FREQUENTLY ASKED QUESTIONS
══════════════════════════════════════════════════

Q: Will my SmugMug photos be deleted?
A: No. The tool only reads from SmugMug. Your originals are untouched.

Q: Does it transfer videos too?
A: Yes. All media files in your albums are transferred.

Q: What if my internet drops?
A: Just click Start again — it resumes from where it left off.

Q: Can I run it overnight?
A: Yes. Make sure your computer doesn't go to sleep:
   Settings → System → Power & sleep → Sleep: Never (plugged in)

Q: Does it keep EXIF/metadata?
A: Yes. Original files are transferred without modification.

Q: How much Drive storage do I need?
A: Enough to hold your SmugMug library. Check your SmugMug account
   settings to see total storage used.


TROUBLESHOOTING
══════════════════════════════════════════════════

"python is not recognized"
   → Reinstall Python, check "Add python.exe to PATH"

"pip is not recognized"
   → Use: python -m pip install ...

SmugMug auth error: parameter_absent oauth_callback
   → Update to the latest version of the tool

SmugMug verification code doesn't work
   → Click Connect SmugMug again and enter the code quickly

Google Drive: API has not been used / disabled
   → Go to Google Cloud Console → APIs & Services → Library
   → Search "Google Drive API" → click Enable

No albums found
   → Check the Log tab for details. Try fetching again.

Migration stops partway through
   → Wait a few minutes, then click Start again (it resumes)


SPEED ESTIMATES
══════════════════════════════════════════════════

   100 photos ........... 5-10 minutes
   500 photos ........... 30 min - 1 hour
   1,000 photos ......... 1-2 hours
   5,000 photos ......... 5-8 hours
   10,000+ photos ....... Overnight


REVOKING ACCESS
══════════════════════════════════════════════════

To remove the tool's access to your accounts:

   SmugMug: Go to your account/privacy settings and revoke the app
   Google:  Go to https://myaccount.google.com/permissions
            Find the app and click "Remove Access"


For the complete help guide, see HELP.txt included with the tool.
"""

        help_text.config(state="normal")
        help_text.insert("1.0", content)

        # Style the headers
        help_text.tag_configure("header", font=("Arial", 10, "bold"))
        idx = "1.0"
        for header in ["QUICK START GUIDE", "GETTING YOUR API CREDENTIALS",
                        "MIGRATION OPTIONS", "HOW IT WORKS",
                        "FREQUENTLY ASKED QUESTIONS", "TROUBLESHOOTING",
                        "SPEED ESTIMATES", "REVOKING ACCESS"]:
            start = help_text.search(header, idx, stopindex="end")
            if start:
                end = f"{start}+{len(header)}c"
                help_text.tag_add("header", start, end)
                idx = end

        help_text.config(state="disabled")

    def _build_about_tab(self, parent):
        # App title
        title_label = ttk.Label(parent, text="SmugMug \u2192 Google Drive Migration Tool",
                                font=("Arial", 16, "bold"))
        title_label.pack(pady=(20, 5))

        version_label = ttk.Label(parent, text="Version 2.0", font=("Arial", 11))
        version_label.pack(pady=(0, 20))

        # Credit
        credit_frame = ttk.LabelFrame(parent, text="Created By", padding=15)
        credit_frame.pack(fill="x", pady=(0, 15))
        ttk.Label(credit_frame, text="Jeremy Kritt", font=("Arial", 12, "bold")).pack()
        ttk.Label(credit_frame, text="github.com/jeremykritt", font=("Arial", 10)).pack(pady=(2, 0))

        # License
        license_frame = ttk.LabelFrame(parent, text="License", padding=15)
        license_frame.pack(fill="x", pady=(0, 15))
        license_text = (
            "MIT License \u00A9 2026 Jeremy Kritt\n\n"
            "Permission is hereby granted, free of charge, to any person obtaining a copy "
            "of this software and associated documentation files (the \"Software\"), to deal "
            "in the Software without restriction, including without limitation the rights "
            "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell "
            "copies of the Software, and to permit persons to whom the Software is "
            "furnished to do so, subject to the following conditions:\n\n"
            "The above copyright notice and this permission notice shall be included in all "
            "copies or substantial portions of the Software."
        )
        license_label = ttk.Label(license_frame, text=license_text, wraplength=600, justify="left",
                                  font=("Arial", 9))
        license_label.pack(fill="x")

        # Disclaimer
        disclaimer_frame = ttk.LabelFrame(parent, text="Disclaimer", padding=15)
        disclaimer_frame.pack(fill="x", pady=(0, 15))
        disclaimer_text = (
            "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR "
            "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, "
            "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE "
            "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER "
            "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, "
            "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE "
            "SOFTWARE.\n\n"
            "This tool is not affiliated with, endorsed by, or sponsored by SmugMug, Inc. "
            "or Google LLC. Use at your own risk."
        )
        disclaimer_label = ttk.Label(disclaimer_frame, text=disclaimer_text, wraplength=600,
                                     justify="left", font=("Arial", 9), foreground="gray")
        disclaimer_label.pack(fill="x")

    # ---------------------------------------------------------------
    # Config
    # ---------------------------------------------------------------
    def _load_saved_values(self):
        cfg = self.config_data
        self.sm_key_var.set(cfg.get("smugmug_api_key", ""))
        self.sm_secret_var.set(cfg.get("smugmug_api_secret", ""))
        self.gd_path_var.set(cfg.get("google_creds_path", ""))
        self.folder_var.set(cfg.get("folder_name", "SmugMug Migration"))
        self.skip_existing_var.set(cfg.get("skip_existing", True))
        self.retry_failed_var.set(cfg.get("retry_failed", False))

        # Check saved auth
        if self.sm_key_var.get() and self.sm_secret_var.get():
            sm = SmugMugClient(self.sm_key_var.get(), self.sm_secret_var.get())
            if sm.has_saved_token():
                self.sm_status.config(text="Token saved ✓", foreground="green")

        if GOOGLE_TOKEN_FILE.exists():
            self.gd_status.config(text="Token saved ✓", foreground="green")

    def _save_settings(self):
        self.config_data = {
            "smugmug_api_key": self.sm_key_var.get().strip(),
            "smugmug_api_secret": self.sm_secret_var.get().strip(),
            "google_creds_path": self.gd_path_var.get().strip(),
            "folder_name": self.folder_var.get().strip() or "SmugMug Migration",
            "skip_existing": self.skip_existing_var.get(),
            "retry_failed": self.retry_failed_var.get(),
        }
        save_config(self.config_data)
        self._log("Settings saved.")
        messagebox.showinfo("Saved", "Settings saved successfully.")

    # ---------------------------------------------------------------
    # SmugMug Auth
    # ---------------------------------------------------------------
    def _open_smugmug_apply(self):
        webbrowser.open("https://api.smugmug.com/api/developer/apply")

    def _connect_smugmug(self):
        key = self.sm_key_var.get().strip()
        secret = self.sm_secret_var.get().strip()
        if not key or not secret:
            messagebox.showwarning("Missing", "Enter your SmugMug API Key and Secret first.")
            return

        self.smugmug = SmugMugClient(key, secret)

        # Try saved token first
        if self.smugmug.authenticate_with_saved_token():
            self.sm_status.config(text="Connected ✓", foreground="green")
            self._log("SmugMug: authenticated with saved token.")
            self._save_settings()
            return

        # Need to do OAuth flow
        try:
            auth_url = self.smugmug.get_authorization_url()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start SmugMug auth:\n{e}")
            return

        webbrowser.open(auth_url)
        verifier = self._ask_verifier()
        if not verifier:
            return

        try:
            self.smugmug.complete_authorization(verifier)
            self.sm_status.config(text="Connected ✓", foreground="green")
            self._log("SmugMug: authorized successfully.")
            self._save_settings()
        except Exception as e:
            messagebox.showerror("Error", f"SmugMug authorization failed:\n{e}")

    def _ask_verifier(self) -> Optional[str]:
        dialog = tk.Toplevel(self)
        dialog.title("SmugMug Verification")
        dialog.geometry("380x150")
        dialog.grab_set()
        dialog.transient(self)

        ttk.Label(dialog, text="A browser window has opened.\nAuthorize the app, then enter the\n6-digit code below:", wraplength=340).pack(pady=10)
        entry = ttk.Entry(dialog, width=20, font=("Consolas", 14), justify="center")
        entry.pack(pady=5)
        entry.focus_set()

        result = {"value": None}

        def submit(event=None):
            result["value"] = entry.get().strip()
            dialog.destroy()

        entry.bind("<Return>", submit)
        ttk.Button(dialog, text="Submit", command=submit).pack(pady=5)
        self.wait_window(dialog)
        return result["value"]

    # ---------------------------------------------------------------
    # Google Drive Auth
    # ---------------------------------------------------------------
    def _open_google_console(self):
        webbrowser.open("https://console.cloud.google.com/apis/credentials")

    def _browse_google_creds(self):
        path = filedialog.askopenfilename(
            title="Select Google credentials JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.gd_path_var.set(path)

    def _connect_google(self):
        creds_path = self.gd_path_var.get().strip()
        if not creds_path or not os.path.exists(creds_path):
            messagebox.showwarning("Missing", "Select your Google credentials JSON file first.")
            return

        self.gdrive = GoogleDriveClient()
        try:
            ok = self.gdrive.authenticate(creds_path)
            if ok:
                self.gd_status.config(text="Connected ✓", foreground="green")
                self._log("Google Drive: authenticated successfully.")
                self._save_settings()
            else:
                messagebox.showerror("Error", "Could not authenticate with Google Drive.")
        except Exception as e:
            messagebox.showerror("Error", f"Google Drive auth failed:\n{e}")

    # ---------------------------------------------------------------
    # Album Selection
    # ---------------------------------------------------------------
    def _fetch_albums(self):
        key = self.sm_key_var.get().strip()
        secret = self.sm_secret_var.get().strip()

        if not key or not secret:
            messagebox.showwarning("Setup Needed", "Enter SmugMug API credentials in the Setup tab.")
            return

        if not self.smugmug:
            self.smugmug = SmugMugClient(key, secret)
        if not self.smugmug.session:
            if not self.smugmug.authenticate_with_saved_token():
                messagebox.showwarning("Auth Needed", "Click 'Connect SmugMug' in Setup first.")
                return

        self.fetch_btn.config(state="disabled")
        self._set_status("Fetching albums from SmugMug...")
        threading.Thread(target=self._fetch_albums_thread, daemon=True).start()

    def _fetch_albums_thread(self):
        try:
            user = self.smugmug.get_authenticated_user()
            albums_uri = self.smugmug.get_user_albums_uri(user)
            if not albums_uri:
                self.after(0, lambda: messagebox.showerror("Error", "Could not determine albums URI."))
                return

            albums = self.smugmug.get_albums(albums_uri)
            self.fetched_albums = albums
            self.after(0, lambda: self._populate_album_list(albums))
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Error", f"Failed to fetch albums:\n{e}"))
        finally:
            self.after(0, lambda: self.fetch_btn.config(state="normal"))

    def _populate_album_list(self, albums):
        # Clear existing checkboxes
        for widget in self.album_inner_frame.winfo_children():
            widget.destroy()
        self.album_vars.clear()

        if not albums:
            ttk.Label(self.album_inner_frame, text="No albums found.", foreground="gray").pack(pady=20)
            self._set_status("No albums found.")
            return

        # Header row
        header = ttk.Frame(self.album_inner_frame)
        header.pack(fill="x", padx=5, pady=(0, 5))
        ttk.Label(header, text="Album Name", font=("Arial", 9, "bold")).pack(side="left")
        ttk.Label(header, text="Path", font=("Arial", 9, "bold"), foreground="gray").pack(side="right")

        ttk.Separator(self.album_inner_frame, orient="horizontal").pack(fill="x", padx=5)

        for album in albums:
            album_key = album.get("AlbumKey", "")
            album_name = album.get("Name", "Untitled")
            url_path = album.get("UrlPath", "")
            image_count = album.get("ImageCount", "?")

            var = tk.BooleanVar(value=True)
            self.album_vars[album_key] = var

            row = ttk.Frame(self.album_inner_frame)
            row.pack(fill="x", padx=5, pady=1)

            cb = ttk.Checkbutton(row, variable=var)
            cb.pack(side="left")

            name_text = f"{album_name}  ({image_count} files)"
            ttk.Label(row, text=name_text, font=("Arial", 9)).pack(side="left", padx=(2, 10))
            ttk.Label(row, text=url_path, font=("Arial", 8), foreground="gray").pack(side="right")

        selected = sum(1 for v in self.album_vars.values() if v.get())
        self.album_count_label.config(text=f"{len(albums)} albums found")
        self._set_status(f"Found {len(albums)} albums. Select which ones to migrate, then click Start.")
        self._log(f"Fetched {len(albums)} albums from SmugMug.")

    def _select_all_albums(self):
        for var in self.album_vars.values():
            var.set(True)

    def _select_no_albums(self):
        for var in self.album_vars.values():
            var.set(False)

    def _get_selected_albums(self) -> list:
        """Return only the albums the user has checked."""
        selected_keys = {k for k, v in self.album_vars.items() if v.get()}
        return [a for a in self.fetched_albums if a.get("AlbumKey", "") in selected_keys]

    # ---------------------------------------------------------------
    # Migration
    # ---------------------------------------------------------------
    def _start_migration(self):
        # Validate connections
        key = self.sm_key_var.get().strip()
        secret = self.sm_secret_var.get().strip()
        creds_path = self.gd_path_var.get().strip()

        if not key or not secret:
            messagebox.showwarning("Setup Needed", "Enter SmugMug API credentials in the Setup tab.")
            return
        if not creds_path:
            messagebox.showwarning("Setup Needed", "Select Google Drive credentials JSON in the Setup tab.")
            return

        # Check album selection
        if not self.fetched_albums:
            messagebox.showwarning("Albums Needed", "Click 'Fetch Albums' first to load your SmugMug albums.")
            return

        selected = self._get_selected_albums()
        if not selected:
            messagebox.showwarning("No Albums Selected", "Select at least one album to migrate.")
            return

        # Ensure authenticated
        if not self.smugmug:
            self.smugmug = SmugMugClient(key, secret)
        if not self.smugmug.session:
            if not self.smugmug.authenticate_with_saved_token():
                messagebox.showwarning("Auth Needed", "Click 'Connect SmugMug' in Setup first.")
                return

        if not self.gdrive:
            self.gdrive = GoogleDriveClient()
        if not self.gdrive.service:
            if not self.gdrive.authenticate(creds_path):
                messagebox.showwarning("Auth Needed", "Click 'Connect Google Drive' in Setup first.")
                return

        self._save_settings()
        self.stop_flag.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.fetch_btn.config(state="disabled")

        self.migration_thread = threading.Thread(target=self._run_migration, daemon=True)
        self.migration_thread.start()

    def _stop_migration(self):
        self.stop_flag.set()
        self._log("Stopping after current file...")
        self.stop_btn.config(state="disabled")

    def _reset_progress(self):
        if messagebox.askyesno("Reset", "This will clear all migration progress.\nYou'll start from scratch next time.\n\nContinue?"):
            MigrationState().reset()
            self._update_counters(0, 0, 0)
            self.overall_progress["value"] = 0
            self.album_progress["value"] = 0
            self.overall_pct_label.config(text="0 / 0")
            self.album_pct_label.config(text="0 / 0")
            self._log("Migration progress reset.")

    def _run_migration(self):
        try:
            selected_albums = self._get_selected_albums()
            self._set_status(f"Migrating {len(selected_albums)} selected albums...")
            self._log(f"Starting migration of {len(selected_albums)} selected albums.")

            user = self.smugmug.get_authenticated_user()
            nickname = user.get("NickName", user.get("Name", "Unknown"))
            self._log(f"SmugMug user: {nickname}")

            # Count total images across selected albums
            self._set_status("Counting images across selected albums...")
            album_images = []
            total_images = 0
            for album in selected_albums:
                if self.stop_flag.is_set():
                    break
                images = self.smugmug.get_album_images(album.get("AlbumKey", ""))
                album_images.append((album, images))
                total_images += len(images)
                self._log(f"  Album '{album.get('Name')}': {len(images)} files")

            self._log(f"Total files to process: {total_images}")
            self.after(0, lambda: self.overall_progress.config(maximum=max(total_images, 1)))

            folder_name = self.folder_var.get().strip() or "SmugMug Migration"
            root_id = self.gdrive.get_or_create_folder(folder_name)

            state = MigrationState()
            skip_existing = self.skip_existing_var.get()
            retry_failed = self.retry_failed_var.get()

            migrated = 0
            skipped = 0
            failed = 0
            overall_done = 0

            for album, images in album_images:
                if self.stop_flag.is_set():
                    break

                album_name = album.get("Name", "Untitled")
                album_key = album.get("AlbumKey", "")
                url_path = album.get("UrlPath", "")

                self._set_status(f"Album: {album_name}")
                self._set_album(f"{album_name}  ({len(images)} files)")
                self.after(0, lambda n=len(images): self.album_progress.config(maximum=max(n, 1)))
                self.after(0, lambda: self.album_progress.config(value=0))

                # Create folder path
                parts = [p for p in url_path.strip("/").split("/") if p and p != nickname]
                if not parts:
                    parts = [album_name]
                parent_id = root_id
                for part in parts:
                    parent_id = self.gdrive.get_or_create_folder(part, parent_id)

                for idx, img in enumerate(images):
                    if self.stop_flag.is_set():
                        break

                    image_key = img.get("ImageKey", "")
                    filename = img.get("FileName", f"{image_key}.jpg")

                    if state.is_done(image_key) and not retry_failed:
                        skipped += 1
                        overall_done += 1
                        self._update_progress(overall_done, idx + 1)
                        self._update_counters(migrated, skipped, failed)
                        continue

                    if not retry_failed and image_key in state.failed:
                        skipped += 1
                        overall_done += 1
                        self._update_progress(overall_done, idx + 1)
                        self._update_counters(migrated, skipped, failed)
                        continue

                    if skip_existing and self.gdrive.file_exists(filename, parent_id):
                        state.mark_done(image_key)
                        skipped += 1
                        overall_done += 1
                        self._update_progress(overall_done, idx + 1)
                        self._update_counters(migrated, skipped, failed)
                        continue

                    image_uri = img.get("Uris", {}).get("Image", {}).get("Uri", "") or img.get("Uri", "")
                    download_url = self.smugmug.get_image_download_url(image_uri)

                    if not download_url:
                        state.mark_failed(image_key, "No download URL")
                        self._log(f"  FAIL (no URL): {album_name}/{filename}")
                        failed += 1
                        overall_done += 1
                        self._update_progress(overall_done, idx + 1)
                        self._update_counters(migrated, skipped, failed)
                        continue

                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix) as tmp:
                            tmp_path = tmp.name
                        if not self.smugmug.download_image(download_url, tmp_path):
                            raise Exception("Download failed")
                        self.gdrive.upload_file(tmp_path, filename, parent_id)
                        state.mark_done(image_key)
                        migrated += 1
                        self._log(f"  ✓ {album_name}/{filename}")
                    except Exception as e:
                        state.mark_failed(image_key, str(e))
                        self._log(f"  ✗ {album_name}/{filename}: {e}")
                        failed += 1
                    finally:
                        if tmp_path and os.path.exists(tmp_path):
                            os.remove(tmp_path)

                    overall_done += 1
                    self._update_progress(overall_done, idx + 1)
                    self._update_counters(migrated, skipped, failed)

                    if overall_done % 10 == 0:
                        state.save()
                    time.sleep(0.2)

            state.save()

            if self.stop_flag.is_set():
                self._set_status(f"Stopped. Migrated: {migrated}, Skipped: {skipped}, Failed: {failed}")
                self._log("Migration stopped by user.")
            else:
                self._set_status(f"Done! Migrated: {migrated}, Skipped: {skipped}, Failed: {failed}")
                self._log(f"Migration complete. {migrated} migrated, {skipped} skipped, {failed} failed.")

        except Exception as e:
            self._set_status(f"Error: {e}")
            self._log(f"ERROR: {e}")

        self._finish()

    # ---------------------------------------------------------------
    # UI Helpers (thread-safe)
    # ---------------------------------------------------------------
    def _log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}\n"
        self.after(0, lambda: self._append_log(line))
        # Also write to file
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line)
        except Exception:
            pass

    def _append_log(self, line: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _set_status(self, text: str):
        self.after(0, lambda: self.status_label.config(text=text))

    def _set_album(self, text: str):
        self.after(0, lambda: self.album_label.config(text=text))

    def _update_progress(self, overall: int, album: int):
        self.after(0, lambda: self.overall_progress.config(value=overall))
        self.after(0, lambda: self.album_progress.config(value=album))
        ot = int(self.overall_progress["maximum"])
        at = int(self.album_progress["maximum"])
        self.after(0, lambda: self.overall_pct_label.config(text=f"{overall} / {ot}"))
        self.after(0, lambda: self.album_pct_label.config(text=f"{album} / {at}"))

    def _update_counters(self, m: int, s: int, f: int):
        self.after(0, lambda: self.migrated_label.config(text=f"Migrated: {m}"))
        self.after(0, lambda: self.skipped_label.config(text=f"Skipped: {s}"))
        self.after(0, lambda: self.failed_label.config(text=f"Failed: {f}"))

    def _finish(self):
        self.after(0, lambda: self.start_btn.config(state="normal"))
        self.after(0, lambda: self.stop_btn.config(state="disabled"))
        self.after(0, lambda: self.fetch_btn.config(state="normal"))


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = MigrationApp()
    app.mainloop()
