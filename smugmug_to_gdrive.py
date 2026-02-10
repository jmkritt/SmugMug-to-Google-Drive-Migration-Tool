#!/usr/bin/env python3
"""
SmugMug to Google Drive Photo Migration Tool
=============================================
Migrates all photos (and videos) from a SmugMug account to Google Drive,
preserving the album folder structure.

Prerequisites:
    pip install requests requests-oauthlib google-auth google-auth-oauthlib google-api-python-client tqdm

Setup:
    1. SmugMug API: Apply for an API key at https://api.smugmug.com/api/developer/apply
       - You'll get an API Key (consumer key) and API Secret (consumer secret).
       - SmugMug uses OAuth 1.0a.

    2. Google Drive API:
       - Go to https://console.cloud.google.com/
       - Create a project, enable the Google Drive API.
       - Create OAuth 2.0 credentials (Desktop App).
       - Download the credentials JSON file and save it as 'google_credentials.json'.

    3. Create a .env file or set environment variables:
         SMUGMUG_API_KEY=your_api_key
         SMUGMUG_API_SECRET=your_api_secret

    4. Run the script:
         python smugmug_to_gdrive.py
"""

import os
import sys
import json
import time
import logging
import argparse
import tempfile
from pathlib import Path
from typing import Optional

import requests
from requests_oauthlib import OAuth1Session
from tqdm import tqdm

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SMUGMUG_API_BASE = "https://api.smugmug.com"
SMUGMUG_REQUEST_TOKEN_URL = "https://secure.smugmug.com/services/oauth/1.0a/getRequestToken"
SMUGMUG_AUTHORIZE_URL = "https://secure.smugmug.com/services/oauth/1.0a/authorize"
SMUGMUG_ACCESS_TOKEN_URL = "https://secure.smugmug.com/services/oauth/1.0a/getAccessToken"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
GOOGLE_CREDS_FILE = "google_credentials.json"
GOOGLE_TOKEN_FILE = "google_token.json"
SMUGMUG_TOKEN_FILE = "smugmug_token.json"

STATE_FILE = "migration_state.json"  # tracks progress for resume capability

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("migration.log"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SmugMug OAuth 1.0a Authentication
# ---------------------------------------------------------------------------
class SmugMugClient:
    """Handles SmugMug API authentication and requests."""

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session: Optional[OAuth1Session] = None

    def authenticate(self) -> None:
        """Authenticate with SmugMug via OAuth 1.0a (3-legged)."""
        # Check for saved tokens
        if os.path.exists(SMUGMUG_TOKEN_FILE):
            with open(SMUGMUG_TOKEN_FILE, "r") as f:
                tokens = json.load(f)
            self.session = OAuth1Session(
                self.api_key,
                client_secret=self.api_secret,
                resource_owner_key=tokens["oauth_token"],
                resource_owner_secret=tokens["oauth_token_secret"],
            )
            # Verify the token is still valid
            resp = self._get("/api/v2!authuser")
            if resp is not None:
                logger.info("Authenticated with saved SmugMug token.")
                return
            logger.warning("Saved SmugMug token is invalid. Re-authenticating...")

        # Step 1: Get request token
        oauth = OAuth1Session(self.api_key, client_secret=self.api_secret)
        fetch_response = oauth.fetch_request_token(SMUGMUG_REQUEST_TOKEN_URL)
        resource_owner_key = fetch_response.get("oauth_token")
        resource_owner_secret = fetch_response.get("oauth_token_secret")

        # Step 2: User authorizes
        authorization_url = oauth.authorization_url(
            SMUGMUG_AUTHORIZE_URL, Access="Full", Permissions="Read"
        )
        print(f"\n{'='*60}")
        print("SMUGMUG AUTHORIZATION")
        print(f"{'='*60}")
        print(f"Please visit this URL to authorize:\n\n{authorization_url}\n")
        print("After authorizing, you'll see a 6-digit verification code.")
        verifier = input("Enter the verification code: ").strip()

        # Step 3: Get access token
        oauth = OAuth1Session(
            self.api_key,
            client_secret=self.api_secret,
            resource_owner_key=resource_owner_key,
            resource_owner_secret=resource_owner_secret,
            verifier=verifier,
        )
        oauth_tokens = oauth.fetch_access_token(SMUGMUG_ACCESS_TOKEN_URL)

        # Save tokens
        with open(SMUGMUG_TOKEN_FILE, "w") as f:
            json.dump(oauth_tokens, f)
        logger.info("SmugMug tokens saved.")

        self.session = OAuth1Session(
            self.api_key,
            client_secret=self.api_secret,
            resource_owner_key=oauth_tokens["oauth_token"],
            resource_owner_secret=oauth_tokens["oauth_token_secret"],
        )

    def _get(self, uri: str, params: Optional[dict] = None) -> Optional[dict]:
        """Make a GET request to the SmugMug API."""
        url = uri if uri.startswith("http") else f"{SMUGMUG_API_BASE}{uri}"
        headers = {"Accept": "application/json"}
        try:
            resp = self.session.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"SmugMug API error for {url}: {e}")
            return None

    def get_authenticated_user(self) -> dict:
        """Get the authenticated user's info."""
        data = self._get("/api/v2!authuser")
        return data["Response"]["User"]

    def get_albums(self, user_uri: str) -> list[dict]:
        """Get all albums for a user, handling pagination."""
        albums = []
        uri = f"{user_uri}!albums"
        params = {"count": 100, "start": 1}

        while True:
            data = self._get(uri, params=params)
            if not data or "Response" not in data:
                break
            page_albums = data["Response"].get("Album", [])
            if not page_albums:
                break
            albums.extend(page_albums)
            # Check for more pages
            pages = data["Response"].get("Pages", {})
            if params["start"] + len(page_albums) > pages.get("Total", 0):
                break
            params["start"] += len(page_albums)
            logger.info(f"  Fetched {len(albums)} albums so far...")

        return albums

    def get_album_images(self, album_key: str) -> list[dict]:
        """Get all images in an album, handling pagination."""
        images = []
        uri = f"/api/v2/album/{album_key}!images"
        params = {"count": 100, "start": 1}

        while True:
            data = self._get(uri, params=params)
            if not data or "Response" not in data:
                break
            page_images = data["Response"].get("AlbumImage", [])
            if not page_images:
                break
            images.extend(page_images)
            pages = data["Response"].get("Pages", {})
            if params["start"] + len(page_images) > pages.get("Total", 0):
                break
            params["start"] += len(page_images)

        return images

    def get_image_download_url(self, image_uri: str) -> Optional[str]:
        """Get the largest available download URL for an image."""
        # Request the largest available size
        data = self._get(f"{image_uri}!largestimage")
        if data and "Response" in data:
            largest = data["Response"].get("LargestImage", {})
            url = largest.get("Url")
            if url:
                return url

        # Fallback: try to get the archive (original) URL
        data = self._get(f"{image_uri}!download")
        if data and "Response" in data:
            download = data["Response"].get("ImageDownload", {})
            return download.get("Url")

        return None

    def download_image(self, url: str, dest_path: str) -> bool:
        """Download an image/video file to a local path."""
        try:
            resp = self.session.get(url, stream=True)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Download failed for {url}: {e}")
            return False


# ---------------------------------------------------------------------------
# Google Drive Authentication & Upload
# ---------------------------------------------------------------------------
class GoogleDriveClient:
    """Handles Google Drive API authentication and uploads."""

    def __init__(self):
        self.service = None
        self._folder_cache: dict[str, str] = {}  # name -> folder_id

    def authenticate(self) -> None:
        """Authenticate with Google Drive via OAuth 2.0."""
        creds = None
        if os.path.exists(GOOGLE_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(GOOGLE_CREDS_FILE):
                    print(f"\nERROR: '{GOOGLE_CREDS_FILE}' not found.")
                    print("Download OAuth 2.0 credentials from Google Cloud Console")
                    print("and save as 'google_credentials.json' in this directory.")
                    sys.exit(1)
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDS_FILE, GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(GOOGLE_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
            logger.info("Google Drive token saved.")
        self.service = build("drive", "v3", credentials=creds)
        logger.info("Authenticated with Google Drive.")

    def get_or_create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        """Get or create a folder in Google Drive. Returns the folder ID."""
        cache_key = f"{parent_id or 'root'}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        # Search for existing folder
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        results = self.service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])

        if files:
            folder_id = files[0]["id"]
        else:
            metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            if parent_id:
                metadata["parents"] = [parent_id]
            folder = self.service.files().create(body=metadata, fields="id").execute()
            folder_id = folder["id"]
            logger.info(f"Created Google Drive folder: {name}")

        self._folder_cache[cache_key] = folder_id
        return folder_id

    def upload_file(self, filepath: str, filename: str, folder_id: str, mime_type: Optional[str] = None) -> str:
        """Upload a file to Google Drive. Returns the file ID."""
        if not mime_type:
            import mimetypes
            mime_type, _ = mimetypes.guess_type(filepath)
            mime_type = mime_type or "application/octet-stream"

        metadata = {"name": filename, "parents": [folder_id]}
        media = MediaFileUpload(filepath, mimetype=mime_type, resumable=True)

        file = self.service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        return file["id"]

    def file_exists(self, filename: str, folder_id: str) -> bool:
        """Check if a file already exists in a folder."""
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        results = self.service.files().list(q=query, fields="files(id)").execute()
        return len(results.get("files", [])) > 0


# ---------------------------------------------------------------------------
# Migration State (for resume capability)
# ---------------------------------------------------------------------------
class MigrationState:
    """Track migration progress so we can resume if interrupted."""

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.migrated: set[str] = set()  # set of SmugMug image keys
        self.failed: dict[str, str] = {}  # image_key -> error message
        self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                data = json.load(f)
            self.migrated = set(data.get("migrated", []))
            self.failed = data.get("failed", {})
            logger.info(f"Loaded state: {len(self.migrated)} migrated, {len(self.failed)} failed.")

    def save(self):
        with open(self.state_file, "w") as f:
            json.dump(
                {"migrated": list(self.migrated), "failed": self.failed},
                f,
                indent=2,
            )

    def mark_done(self, image_key: str):
        self.migrated.add(image_key)
        self.failed.pop(image_key, None)

    def mark_failed(self, image_key: str, error: str):
        self.failed[image_key] = error

    def is_done(self, image_key: str) -> bool:
        return image_key in self.migrated


# ---------------------------------------------------------------------------
# Main Migration Logic
# ---------------------------------------------------------------------------
def migrate(
    root_folder_name: str = "SmugMug Migration",
    dry_run: bool = False,
    skip_existing: bool = True,
    retry_failed: bool = False,
):
    """
    Main migration function.

    Args:
        root_folder_name: Name of the root folder in Google Drive.
        dry_run: If True, list what would be migrated without downloading/uploading.
        skip_existing: If True, skip files that already exist in Google Drive.
        retry_failed: If True, retry previously failed images.
    """
    # --- Load environment ---
    api_key = os.environ.get("SMUGMUG_API_KEY", "")
    api_secret = os.environ.get("SMUGMUG_API_SECRET", "")
    if not api_key or not api_secret:
        # Try loading from .env file
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
            api_key = os.environ.get("SMUGMUG_API_KEY", "")
            api_secret = os.environ.get("SMUGMUG_API_SECRET", "")

    if not api_key or not api_secret:
        print("\nERROR: SmugMug API credentials not found.")
        print("Set SMUGMUG_API_KEY and SMUGMUG_API_SECRET in your environment or .env file.")
        sys.exit(1)

    # --- Authenticate ---
    smugmug = SmugMugClient(api_key, api_secret)
    smugmug.authenticate()

    gdrive = GoogleDriveClient()
    gdrive.authenticate()

    state = MigrationState()

    # --- Get SmugMug user & albums ---
    user = smugmug.get_authenticated_user()
    nickname = user.get("NickName", user.get("Name", "Unknown"))
    logger.info(f"SmugMug user: {nickname}")

    logger.info("Fetching albums...")
    albums = smugmug.get_albums(user["Uris"]["UserAlbums"]["Uri"])
    logger.info(f"Found {len(albums)} albums.")

    if not albums:
        print("No albums found. Nothing to migrate.")
        return

    # --- Create root folder in Google Drive ---
    root_folder_id = gdrive.get_or_create_folder(root_folder_name)

    # --- Process each album ---
    total_images = 0
    migrated_count = 0
    skipped_count = 0
    failed_count = 0

    for album in albums:
        album_name = album.get("Name", "Untitled Album")
        album_key = album.get("AlbumKey", "")
        url_path = album.get("UrlPath", "")

        # Handle nested folder paths (e.g., /Family/Vacation/2024)
        path_parts = [p for p in url_path.strip("/").split("/") if p and p != nickname]
        if not path_parts:
            path_parts = [album_name]

        logger.info(f"\nAlbum: {album_name} (Key: {album_key})")

        # Create nested folders in Google Drive
        parent_id = root_folder_id
        for part in path_parts:
            parent_id = gdrive.get_or_create_folder(part, parent_id)
        album_folder_id = parent_id

        # Get images in the album
        images = smugmug.get_album_images(album_key)
        logger.info(f"  {len(images)} images/videos in album")
        total_images += len(images)

        if dry_run:
            for img in images:
                fname = img.get("FileName", "unknown")
                key = img.get("ImageKey", "")
                status = "DONE" if state.is_done(key) else "PENDING"
                print(f"  [{status}] {album_name}/{fname}")
            continue

        # Migrate each image
        for img in tqdm(images, desc=f"  {album_name}", unit="file"):
            image_key = img.get("ImageKey", "")
            filename = img.get("FileName", f"{image_key}.jpg")

            # Skip if already migrated
            if state.is_done(image_key) and not retry_failed:
                skipped_count += 1
                continue

            # Skip if retry_failed is False and this was a failed item
            if not retry_failed and image_key in state.failed:
                skipped_count += 1
                continue

            # Check if file already exists in Google Drive
            if skip_existing and gdrive.file_exists(filename, album_folder_id):
                state.mark_done(image_key)
                skipped_count += 1
                continue

            # Get download URL
            image_uri = img.get("Uris", {}).get("Image", {}).get("Uri", "")
            if not image_uri:
                # Try alternative URI paths
                image_uri = img.get("Uri", "")

            download_url = smugmug.get_image_download_url(image_uri)
            if not download_url:
                error_msg = "Could not get download URL"
                logger.warning(f"  SKIP {filename}: {error_msg}")
                state.mark_failed(image_key, error_msg)
                failed_count += 1
                continue

            # Download to temp file, then upload to Google Drive
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix) as tmp:
                    tmp_path = tmp.name

                if not smugmug.download_image(download_url, tmp_path):
                    raise Exception("Download returned False")

                gdrive.upload_file(tmp_path, filename, album_folder_id)
                state.mark_done(image_key)
                migrated_count += 1

            except Exception as e:
                error_msg = str(e)
                logger.error(f"  FAIL {filename}: {error_msg}")
                state.mark_failed(image_key, error_msg)
                failed_count += 1

            finally:
                # Clean up temp file
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            # Save state periodically (every 10 files)
            if (migrated_count + failed_count) % 10 == 0:
                state.save()

            # Small delay to avoid rate-limiting
            time.sleep(0.2)

    # Final state save
    state.save()

    # --- Summary ---
    print(f"\n{'='*60}")
    print("MIGRATION SUMMARY")
    print(f"{'='*60}")
    print(f"Total albums:      {len(albums)}")
    print(f"Total images:      {total_images}")
    print(f"Migrated:          {migrated_count}")
    print(f"Skipped (exists):  {skipped_count}")
    print(f"Failed:            {failed_count}")
    if state.failed:
        print(f"\nFailed images saved in {STATE_FILE}.")
        print("Run with --retry-failed to retry them.")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Migrate all photos from SmugMug to Google Drive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--folder",
        default="SmugMug Migration",
        help="Root folder name in Google Drive (default: 'SmugMug Migration')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be migrated without actually doing it",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-upload files even if they already exist in Google Drive",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry previously failed images",
    )
    args = parser.parse_args()

    migrate(
        root_folder_name=args.folder,
        dry_run=args.dry_run,
        skip_existing=not args.no_skip_existing,
        retry_failed=args.retry_failed,
    )


if __name__ == "__main__":
    main()
