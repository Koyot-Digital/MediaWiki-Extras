#!/usr/bin/env python3
"""
Roblox badge bot for MediaWiki.

Fetches every badge for one or more Roblox universes (games), grabs each
badge's icon image, and uploads the icons to a MediaWiki wiki (works with
Miraheze) using a bot password.

Re-runs are cheap: the bot compares the SHA-1 of each icon against the copy
already on the wiki and only uploads when a file is new or has changed.

Configuration is read from environment variables (see README.md):
  WIKI_API_URL          e.g. https://yourwiki.miraheze.org/w/api.php
  WIKI_BOT_USERNAME     e.g. WikiBot@badgebot   (Special:BotPasswords format)
  WIKI_BOT_PASSWORD     the generated bot password
  ROBLOX_UNIVERSE_IDS   comma separated universe ids, e.g. 123456,789012
  FILENAME_PREFIX       optional, default "Roblox badge"
  GALLERY_PAGE          optional wiki page title to (over)write with a gallery
  FORCE_REUPLOAD        optional, "1" to re-upload even when unchanged
  DRY_RUN               optional, "1" to log actions without writing to the wiki
"""

import hashlib
import os
import re
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BADGES_API = "https://badges.roblox.com/v1/universes/{universe_id}/badges"
THUMBNAILS_API = "https://thumbnails.roblox.com/v1/badges/icons"

# Roblox public endpoints are happier with a real looking user agent.
USER_AGENT = "KoyotBadgeBot/1.0 (+MediaWiki upload bot; contact: wiki team)"

# MediaWiki title characters that are not allowed; we swap them for a space.
TITLE_ILLEGAL = re.compile(r"[#<>\[\]\|\{\}/:\n\r\t]")


def log(message):
    print(message, flush=True)


def build_session(extra_headers=None):
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    session.headers.update(headers)
    return session


# --------------------------------------------------------------------------
# Roblox side
# --------------------------------------------------------------------------

def get_all_badges(session, universe_id):
    """Return a list of badge dicts for a universe, following pagination."""
    badges = []
    cursor = ""
    url = BADGES_API.format(universe_id=universe_id)
    while True:
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        badges.extend(payload.get("data", []))
        cursor = payload.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(0.3)
    return badges


def get_badge_icon_urls(session, badge_ids):
    """
    Map badge id to icon image url using the thumbnails API.

    The thumbnails API is asynchronous: it may return a "Pending" state, so
    we retry those ids a few times before giving up.
    """
    result = {}
    pending = list(badge_ids)
    attempts = 0
    while pending and attempts < 5:
        attempts += 1
        still_pending = []
        # The thumbnails endpoint accepts at most 100 ids per request.
        for i in range(0, len(pending), 100):
            chunk = pending[i:i + 100]
            params = {
                "badgeIds": ",".join(str(b) for b in chunk),
                "size": "150x150",
                "format": "Png",
                "isCircular": "false",
            }
            resp = session.get(THUMBNAILS_API, params=params, timeout=30)
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                target = item.get("targetId")
                state = item.get("state")
                if state == "Completed" and item.get("imageUrl"):
                    result[target] = item["imageUrl"]
                else:
                    still_pending.append(target)
            time.sleep(0.3)
        pending = still_pending
        if pending:
            time.sleep(2.0 * attempts)
    if pending:
        log(f"  warning: {len(pending)} badge icons stayed pending and were skipped")
    return result


def download_bytes(session, url):
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


# --------------------------------------------------------------------------
# MediaWiki side
# --------------------------------------------------------------------------

class Wiki:
    def __init__(self, api_url, session, dry_run=False):
        self.api_url = api_url
        self.session = session
        self.dry_run = dry_run
        self.csrf_token = None

    def _get(self, params):
        params = dict(params, format="json")
        resp = self.session.get(self.api_url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, data, files=None):
        data = dict(data, format="json")
        resp = self.session.post(self.api_url, data=data, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def login(self, username, password):
        token = self._get({
            "action": "query", "meta": "tokens", "type": "login",
        })["query"]["tokens"]["logintoken"]
        result = self._post({
            "action": "login",
            "lgname": username,
            "lgpassword": password,
            "lgtoken": token,
        })
        status = result.get("login", {}).get("result")
        if status != "Success":
            reason = result.get("login", {}).get("reason", status)
            raise RuntimeError(f"Login failed: {reason}")
        self.csrf_token = self._get({
            "action": "query", "meta": "tokens",
        })["query"]["tokens"]["csrftoken"]
        log(f"Logged in as {username}")

    def existing_sha1(self, filenames):
        """Return {File title: sha1} for files that already exist on the wiki."""
        found = {}
        titles = [f"File:{name}" for name in filenames]
        for i in range(0, len(titles), 50):
            chunk = titles[i:i + 50]
            data = self._get({
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "imageinfo",
                "iiprop": "sha1",
            })
            pages = data.get("query", {}).get("pages", {})
            for page in pages.values():
                if "missing" in page:
                    continue
                info = page.get("imageinfo")
                if info:
                    title = page["title"].split(":", 1)[-1]
                    found[title] = info[0].get("sha1")
        return found

    def upload(self, filename, file_bytes, description, force=False):
        """Return True if the file ends up uploaded, False otherwise."""
        if self.dry_run:
            log(f"  [dry run] would upload File:{filename}")
            return True
        data = {
            "action": "upload",
            "filename": filename,
            "comment": description,
            "text": description,
            "token": self.csrf_token,
        }
        if force:
            data["ignorewarnings"] = "1"
        result = self._post(data, files={"file": (filename, file_bytes)})
        if "error" in result:
            error = result["error"]
            log(f"  failed File:{filename}: {error.get('code')}: {error.get('info')}")
            if error.get("code") == "permissiondenied":
                log("  hint: the bot password is missing the upload grant. On "
                    "Special:BotPasswords, edit the bot password and tick "
                    "\"Upload new files\" and \"Upload, replace and move files\".")
            return False
        upload = result.get("upload", {})
        status = upload.get("result")
        if status == "Success":
            log(f"  uploaded File:{filename}")
            return True
        if status == "Warning" and not force:
            # File exists or is a duplicate; retry once ignoring warnings.
            log(f"  warning on File:{filename} ({list(upload.get('warnings', {}))}); retrying with overwrite")
            return self.upload(filename, file_bytes, description, force=True)
        log(f"  failed File:{filename}: {result}")
        return False

    def edit_page(self, title, text, summary):
        if self.dry_run:
            log(f"  [dry run] would edit page {title}")
            return
        result = self._post({
            "action": "edit",
            "title": title,
            "text": text,
            "summary": summary,
            "bot": "1",
            "token": self.csrf_token,
        })
        if result.get("edit", {}).get("result") == "Success":
            log(f"Updated page {title}")
        else:
            log(f"Failed to edit {title}: {result}")


# --------------------------------------------------------------------------
# Glue
# --------------------------------------------------------------------------

def sanitize_title(text):
    text = TITLE_ILLEGAL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # MediaWiki uppercases the first letter of titles anyway; keep it tidy.
    return text or "untitled"


def make_filename(prefix, badge):
    name = sanitize_title(badge.get("name", ""))
    # Badge id keeps the filename unique even when two badges share a name.
    return f"{prefix} {badge['id']} {name}.png"


def build_description(badge, universe_id):
    name = badge.get("name", "")
    desc = (badge.get("description") or "").strip()
    lines = [
        f"Icon for the Roblox badge \"{name}\" (badge id {badge['id']}, universe {universe_id}).",
        "",
        f"* Badge name: {name}",
        f"* Badge id: {badge['id']}",
        f"* Universe id: {universe_id}",
    ]
    if desc:
        lines.append(f"* Description: {desc}")
    lines.append("")
    lines.append("[[Category:Roblox badges]]")
    return "\n".join(lines)


def env_flag(name):
    return os.environ.get(name, "").strip() in ("1", "true", "True", "yes")


def main():
    api_url = os.environ.get("WIKI_API_URL", "").strip()
    username = os.environ.get("WIKI_BOT_USERNAME", "").strip()
    password = os.environ.get("WIKI_BOT_PASSWORD", "").strip()
    universe_raw = os.environ.get("ROBLOX_UNIVERSE_IDS", "").strip()
    prefix = os.environ.get("FILENAME_PREFIX", "Roblox badge").strip() or "Roblox badge"
    gallery_page = os.environ.get("GALLERY_PAGE", "").strip()
    force = env_flag("FORCE_REUPLOAD")
    dry_run = env_flag("DRY_RUN")

    missing = [n for n, v in (
        ("WIKI_API_URL", api_url),
        ("WIKI_BOT_USERNAME", username),
        ("WIKI_BOT_PASSWORD", password),
        ("ROBLOX_UNIVERSE_IDS", universe_raw),
    ) if not v]
    if missing:
        log(f"Missing required environment variables: {', '.join(missing)}")
        return 1

    universe_ids = [u.strip() for u in universe_raw.split(",") if u.strip()]

    roblox = build_session()
    wiki_session = build_session()
    wiki = Wiki(api_url, wiki_session, dry_run=dry_run)
    if not dry_run:
        wiki.login(username, password)

    gallery_entries = []
    total_uploaded = 0
    total_skipped = 0
    total_failed = 0

    for universe_id in universe_ids:
        log(f"Fetching badges for universe {universe_id}")
        badges = get_all_badges(roblox, universe_id)
        log(f"  found {len(badges)} badges")
        if not badges:
            continue

        icon_urls = get_badge_icon_urls(roblox, [b["id"] for b in badges])

        # Look up what is already on the wiki so we can skip unchanged files.
        filenames = {b["id"]: make_filename(prefix, b) for b in badges}
        existing = {} if dry_run else wiki.existing_sha1(list(filenames.values()))

        for badge in badges:
            badge_id = badge["id"]
            filename = filenames[badge_id]
            url = icon_urls.get(badge_id)
            if not url:
                log(f"  no icon url for badge {badge_id} ({badge.get('name')}); skipping")
                continue

            file_bytes = download_bytes(roblox, url)
            local_sha1 = hashlib.sha1(file_bytes).hexdigest()
            remote_sha1 = existing.get(filename)

            if remote_sha1 == local_sha1 and not force:
                total_skipped += 1
                gallery_entries.append((filename, badge.get("name", "")))
            else:
                description = build_description(badge, universe_id)
                if wiki.upload(filename, file_bytes, description, force=force):
                    total_uploaded += 1
                    gallery_entries.append((filename, badge.get("name", "")))
                else:
                    total_failed += 1

            time.sleep(0.2)

    if gallery_page and gallery_entries and not dry_run:
        lines = ["<gallery>"]
        for filename, name in gallery_entries:
            caption = sanitize_title(name)
            lines.append(f"{filename}|{caption}")
        lines.append("</gallery>")
        lines.append("")
        lines.append("[[Category:Roblox badges]]")
        wiki.edit_page(gallery_page, "\n".join(lines),
                       "Bot: refresh Roblox badge gallery")

    log(f"Done. Uploaded/updated {total_uploaded}, skipped unchanged "
        f"{total_skipped}, failed {total_failed}.")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
