#!/usr/bin/env python3
"""
ventoy_sync.py — Automated ISO updater for Ventoy USB drives.

Checks upstream sources for new ISO versions and downloads updates
using curl with resume support. Generates a summary report after each run.

Usage:
    python ventoy_sync.py                        # Full sync
    python ventoy_sync.py --dry-run              # Check only, no downloads
    python ventoy_sync.py --check KEY            # Check a single ISO entry
    python ventoy_sync.py --backup /mnt/iso      # Sync then copy new ISOs to backup path
"""

import os
import sys

# Re-exec under the project venv if we're not already in it.
_VENV_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            ".venv", "bin", "python3")
if os.path.isfile(_VENV_PYTHON) and sys.executable != _VENV_PYTHON:
    os.execv(_VENV_PYTHON, [_VENV_PYTHON] + sys.argv)

import argparse
import json
import re
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# ANSI colours for terminal output
# ---------------------------------------------------------------------------
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"

REQUEST_TIMEOUT = 30  # seconds for HTTP requests
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class SyncResult:
    """Outcome of processing a single ISO entry."""

    def __init__(self, key: str, name: str):
        self.key = key
        self.name = name
        self.status = "skipped"   # skipped | updated | error | disabled
        self.version = ""
        self.old_version = ""
        self.message = ""
        self.filename = ""

    def __repr__(self):
        return f"<SyncResult {self.key} {self.status}>"


# ---------------------------------------------------------------------------
# Config / state helpers
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    """Read and validate config.yaml."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if "ventoy_path" not in cfg:
        sys.exit(f"{RED}Error:{RESET} config.yaml missing 'ventoy_path'")
    if "isos" not in cfg or not isinstance(cfg["isos"], dict):
        sys.exit(f"{RED}Error:{RESET} config.yaml missing 'isos' section")
    return cfg


def load_state(state_path: Path) -> dict:
    """Load state.json from the Ventoy drive (or return empty dict)."""
    if state_path.exists():
        with open(state_path) as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    return {}


def save_state(state_path: Path, state: dict) -> None:
    """Persist state.json atomically."""
    tmp = state_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    tmp.rename(state_path)


# ---------------------------------------------------------------------------
# Update-checking logic
# ---------------------------------------------------------------------------

def check_regex(entry: dict) -> tuple[str, str, str] | None:
    """
    Scrape a page for the latest version string using a regex.

    Returns (version, download_url, filename) or None on failure.
    """
    url = entry.get("url", "")
    pattern = entry.get("regex", "")
    dl_template = entry.get("download_url_template", "")
    fn_template = entry.get("filename_template", "")

    if not all([url, pattern, dl_template, fn_template]):
        return None

    ua = entry.get("user_agent", USER_AGENT)
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": ua})
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

    # Some pages list versions chronologically (e.g. mirror directory listings).
    # When regex_last is set, grab the *last* match on the page instead of first.
    if entry.get("regex_last", False):
        matches = list(re.finditer(pattern, resp.text))
        if not matches:
            raise RuntimeError(f"Regex {pattern!r} found no match on {url}")
        match = matches[-1]
    else:
        match = re.search(pattern, resp.text)
        if not match:
            raise RuntimeError(f"Regex {pattern!r} found no match on {url}")

    version = match.group(1)

    # Build template substitutions: {version} is always group 1,
    # plus any named groups (?P<name>...) are available as {name}.
    subs = {"version": version}
    subs.update({k: (v or "") for k, v in match.groupdict().items()})

    download_url = dl_template.format_map(subs)
    filename = fn_template.format_map(subs)
    return version, download_url, filename


def check_headers(entry: dict, state_entry: dict) -> tuple[bool, dict, str]:
    """
    Use HTTP HEAD to check ETag / Content-Length against stored state.

    Returns (needs_update, new_headers_dict, filename).
    """
    dl_url = entry.get("download_url", "")
    if not dl_url:
        raise RuntimeError("No download_url configured for headers method")

    ua = entry.get("user_agent", USER_AGENT)
    try:
        resp = requests.head(dl_url, timeout=REQUEST_TIMEOUT,
                             allow_redirects=True,
                             headers={"User-Agent": ua})
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"HEAD request failed for {dl_url}: {exc}") from exc

    new_headers = {}
    if "ETag" in resp.headers:
        new_headers["etag"] = resp.headers["ETag"]
    if "Content-Length" in resp.headers:
        new_headers["content_length"] = resp.headers["Content-Length"]
    if "Last-Modified" in resp.headers:
        new_headers["last_modified"] = resp.headers["Last-Modified"]

    if not new_headers:
        raise RuntimeError(
            f"HEAD response for {dl_url} returned no usable headers "
            "(no ETag, Content-Length, or Last-Modified)"
        )

    # Determine filename from URL or Content-Disposition
    cd = resp.headers.get("Content-Disposition", "")
    fn_match = re.search(r'filename="?([^";\s]+)"?', cd)
    if fn_match:
        filename = fn_match.group(1)
    else:
        filename = dl_url.rstrip("/").rsplit("/", 1)[-1]
        # Strip query strings
        filename = filename.split("?")[0]

    # Compare with stored state
    old_etag = state_entry.get("etag")
    old_length = state_entry.get("content_length")

    if old_etag and new_headers.get("etag"):
        needs_update = old_etag != new_headers["etag"]
    elif old_length and new_headers.get("content_length"):
        needs_update = old_length != new_headers["content_length"]
    else:
        # No prior state — need to download
        needs_update = True

    return needs_update, new_headers, filename


# ---------------------------------------------------------------------------
# Download engine
# ---------------------------------------------------------------------------

def _fmt_speed(bps: float) -> str:
    """Format bytes/sec into a human-readable rate."""
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} MB/s"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} KB/s"
    return f"{bps:.0f} B/s"


def _fmt_size(b: float) -> str:
    """Format bytes into a human-readable size."""
    if b >= 1_000_000_000:
        return f"{b / 1_000_000_000:.2f} GB"
    if b >= 1_000_000:
        return f"{b / 1_000_000:.1f} MB"
    return f"{b / 1_000:.0f} KB"


def download_iso(url: str, dest: Path, user_agent: str = USER_AGENT) -> None:
    """Download an ISO using curl, with resume support where the server allows it."""
    # curl writes -w output to stdout; progress bar goes to stderr (tty).
    def _build_cmd(resume: bool, http1: bool = False) -> list[str]:
        cmd = ["curl", "-L"]
        if resume:
            cmd += ["-C", "-"]      # resume if partial file exists
        if http1:
            cmd += ["--http1.1"]    # force HTTP/1.1 (fallback for H2 stream errors)
        cmd += [
            "-o", str(dest),
            "--progress-bar",
            "--fail",               # fail on HTTP errors
            "--retry", "3",
            "--retry-delay", "5",
            "-A", user_agent,
            "-w", "%{speed_download} %{size_download} %{time_total}",
            url,
        ]
        return cmd

    print(f"  Downloading to {dest.name} ...")
    result = subprocess.run(_build_cmd(resume=True), stdout=subprocess.PIPE, text=True)

    # curl exit code 33 = byte-range request rejected by server (no resume support).
    # Delete any partial file and retry as a fresh download.
    if result.returncode == 33:
        if dest.exists():
            dest.unlink()
        result = subprocess.run(_build_cmd(resume=False), stdout=subprocess.PIPE, text=True)

    # curl exit code 92 = HTTP/2 stream error.
    # Retry forcing HTTP/1.1; keep any partial file so resume can still work.
    if result.returncode == 92:
        result = subprocess.run(_build_cmd(resume=True, http1=True), stdout=subprocess.PIPE, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"curl exited with code {result.returncode} for {url}"
        )

    # Parse the -w output: "speed_bytes size_bytes time_secs"
    try:
        parts = result.stdout.strip().split()
        speed = float(parts[0])
        size = float(parts[1])
        elapsed = float(parts[2])
        print(f"  {GREEN}{_fmt_size(size)}{RESET} in {elapsed:.0f}s "
              f"({_fmt_speed(speed)})")
    except (ValueError, IndexError):
        pass  # non-critical; just skip the summary line


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_to(ventoy_path: Path, backup_path: Path, dry_run: bool,
              isos: dict | None = None) -> None:
    """
    Copy ISOs from *ventoy_path* to *backup_path*, skipping files that are
    already present with the same size.  Old versions of replaced ISOs in the
    backup directory are removed (same prefix-based logic used on the drive).

    Uses rsync (--checksum) when available for efficient delta-copy and
    resume support; falls back to shutil.copy2 otherwise.

    Only .iso and .img files are considered; state.json, summary.md, etc.
    are intentionally excluded.
    """
    if not backup_path.exists():
        if dry_run:
            print(f"  {YELLOW}(dry-run){RESET} Would create backup dir {backup_path}")
        else:
            backup_path.mkdir(parents=True, exist_ok=True)
            print(f"  Created backup directory {backup_path}")

    # Build a map of filename prefix → current filename for every configured
    # ISO entry.  This lets us find and remove stale old versions in the backup.
    # Each entry contributes up to two prefixes:
    #   1. The raw filename prefix (e.g. "archlinux-")
    #   2. The friendly-name prefix when rename is enabled (e.g. "Arch Linux - ")
    prefix_to_current: dict[str, str] = {}
    if isos:
        for key, entry in isos.items():
            # raw prefix from filename_template
            raw_prefix = iso_prefix(entry, key)
            # friendly prefix from name (used when rename: true)
            friendly_prefix = entry.get("name", "") if entry.get("rename") else ""

            # The current filename on the drive is whichever .iso/.img file
            # matches this entry's prefix — look for it live so we don't need
            # to thread state through here.
            current = next(
                (
                    f.name for f in ventoy_path.iterdir()
                    if f.is_file()
                    and f.suffix.lower() in (".iso", ".img")
                    and (
                        f.name.startswith(raw_prefix)
                        or (friendly_prefix and f.name.startswith(friendly_prefix))
                    )
                ),
                None,
            )
            if current:
                if raw_prefix:
                    prefix_to_current[raw_prefix] = current
                if friendly_prefix:
                    prefix_to_current[friendly_prefix] = current

    # Collect all ISO/image files on the Ventoy drive
    candidates = sorted(
        f for f in ventoy_path.iterdir()
        if f.is_file() and f.suffix.lower() in (".iso", ".img")
    )

    if not candidates:
        print("  No ISO/image files found on Ventoy drive — nothing to back up.")
        return

    copied = []
    skipped = []
    deleted = []
    errors = []

    # Detect rsync once
    has_rsync = subprocess.run(
        ["which", "rsync"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0

    for src in candidates:
        dst = backup_path / src.name

        # Before copying, remove any stale old version in the backup that
        # shares the same prefix but has a different name.
        for prefix, current_name in prefix_to_current.items():
            if current_name != src.name:
                continue  # this prefix maps to a different entry; skip
            for old in backup_path.iterdir() if backup_path.exists() else []:
                if (
                    old.is_file()
                    and old.name != src.name
                    and old.name.startswith(prefix)
                    and old.suffix.lower() in (".iso", ".img")
                ):
                    if dry_run:
                        print(f"  {YELLOW}(dry-run){RESET} Would remove stale backup: {old.name}")
                        deleted.append(old.name)
                    else:
                        old.unlink()
                        print(f"  Removed stale backup: {old.name}")
                        deleted.append(old.name)

        # Skip if destination exists and sizes match
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            skipped.append(src.name)
            continue

        reason = "missing" if not dst.exists() else "size mismatch"
        if dry_run:
            print(f"  {CYAN}(dry-run){RESET} Would copy {src.name}  [{reason}]")
            copied.append(src.name)
            continue

        print(f"  Copying {src.name}  [{reason}] ...")
        try:
            if has_rsync:
                subprocess.run(
                    [
                        "rsync",
                        "--checksum",       # compare by checksum, not mtime
                        "--partial",        # keep partial file on interrupt
                        "--progress",
                        str(src),
                        str(dst),
                    ],
                    check=True,
                )
            else:
                import shutil
                shutil.copy2(src, dst)
                print(f"  Copied {src.name}")
            copied.append(src.name)
        except Exception as exc:
            print(f"  {RED}ERROR{RESET} copying {src.name}: {exc}")
            errors.append(src.name)

    # Summary
    print(f"\n  Backup to {backup_path}:")
    if copied:
        verb = "Would copy" if dry_run else "Copied"
        print(f"    {GREEN}{verb}:{RESET} {len(copied)} file(s)")
        for name in copied:
            print(f"      {name}")
    if deleted:
        verb = "Would remove" if dry_run else "Removed"
        print(f"    {YELLOW}{verb} stale:{RESET} {len(deleted)} file(s)")
        for name in deleted:
            print(f"      {name}")
    if skipped:
        print(f"    {YELLOW}Already up-to-date:{RESET} {len(skipped)} file(s)")
    if errors:
        print(f"    {RED}Errors:{RESET} {len(errors)} file(s)")
        for name in errors:
            print(f"      {name}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_old(ventoy_path: Path, current_filename: str,
                prefix: str) -> list[str]:
    """
    Remove old ISOs matching *prefix* that aren't *current_filename*.

    Returns list of deleted filenames.
    """
    deleted = []
    for f in ventoy_path.iterdir():
        if f.is_file() and f.name != current_filename and f.name.startswith(prefix):
            if f.suffix.lower() in (".iso", ".img"):
                f.unlink()
                deleted.append(f.name)
    return deleted


def iso_prefix(entry: dict, key: str) -> str:
    """
    Derive a stable filename prefix for glob-based cleanup.

    E.g. for archlinux with template "archlinux-{version}-x86_64.iso"
    returns "archlinux-".  For named groups like "Zorin-OS-{major}-..."
    returns "Zorin-OS-".
    """
    tmpl = entry.get("filename_template", "")
    if tmpl:
        # Find the first placeholder (any {name})
        m = re.search(r"\{[^}]+\}", tmpl)
        if m and m.start() > 0:
            return tmpl[:m.start()]

    # Fall back to key-based prefix
    return key.replace("_", "-") + "-"


def friendly_filename(entry: dict, version: str, original_filename: str) -> str:
    """
    Build a friendly filename from the entry's name and version.

    Uses the pattern: "{name} - {version}.{ext}"
    For headers-method entries (no version): "{name}.{ext}"

    Returns the original filename if rename is not enabled.
    """
    if not entry.get("rename", False):
        return original_filename

    name = entry.get("name", "")
    if not name:
        return original_filename

    # Preserve the original file extension
    ext = Path(original_filename).suffix  # e.g. ".iso" or ".img"
    if not ext:
        ext = ".iso"

    if version:
        return f"{name} - {version}{ext}"
    else:
        return f"{name}{ext}"


# ---------------------------------------------------------------------------
# Core sync loop
# ---------------------------------------------------------------------------

def sync_one(key: str, entry: dict, state: dict, ventoy_path: Path,
             dry_run: bool, state_path: Path | None = None) -> SyncResult:
    """Process a single ISO entry."""
    result = SyncResult(key, entry.get("name", key))

    # Check if explicitly disabled
    if not entry.get("enabled", True):
        result.status = "disabled"
        result.message = "Disabled in config"
        return result

    method = entry.get("method", "")
    state_entry = state.get(key, {})
    ua = entry.get("user_agent", USER_AGENT)

    try:
        if method == "regex":
            info = check_regex(entry)
            if info is None:
                result.status = "error"
                result.message = "Missing required config fields"
                return result

            version, download_url, filename = info
            result.version = version
            result.filename = filename
            result.old_version = state_entry.get("version", "")

            if version == state_entry.get("version"):
                result.status = "skipped"
                result.message = f"Already at {version}"
                return result

            # New version available
            if dry_run:
                result.status = "available"
                result.message = f"Update available: {result.old_version or '(none)'} -> {version}"
                return result

            # If unzip is set, download the .zip first, then extract
            if entry.get("unzip", False):
                zip_dest = ventoy_path / (filename + ".zip")
                download_iso(download_url, zip_dest, ua)

                if not zip_dest.exists() or zip_dest.stat().st_size == 0:
                    raise RuntimeError("Download produced empty or missing zip")

                print(f"  Extracting {zip_dest.name} ...")
                with zipfile.ZipFile(zip_dest, "r") as zf:
                    # Find the .iso inside the zip
                    iso_members = [
                        n for n in zf.namelist()
                        if n.lower().endswith((".iso", ".img"))
                    ]
                    if not iso_members:
                        zip_dest.unlink()
                        raise RuntimeError(
                            f"No .iso/.img found inside {zip_dest.name}"
                        )
                    # Extract the first matching member
                    member = iso_members[0]
                    zf.extract(member, ventoy_path)
                    extracted = ventoy_path / member
                    dest = ventoy_path / filename
                    if extracted != dest:
                        extracted.rename(dest)

                zip_dest.unlink()
                print(f"  Extracted {filename}")
            else:
                dest = ventoy_path / filename
                download_iso(download_url, dest, ua)

            # Verify file exists and has size > 0
            if not dest.exists() or dest.stat().st_size == 0:
                raise RuntimeError("Download produced empty or missing file")

            # Rename to friendly name if enabled
            final_filename = friendly_filename(entry, version, filename)
            if final_filename != filename:
                final_dest = ventoy_path / final_filename
                dest.rename(final_dest)
                print(f"  Renamed to {final_filename}")

            # Cleanup old versions (by upstream prefix)
            prefix = iso_prefix(entry, key)
            deleted = cleanup_old(ventoy_path, final_filename, prefix)

            # Also cleanup old friendly-named files if renaming is active
            if entry.get("rename", False):
                friendly_prefix = entry.get("name", "")
                if friendly_prefix:
                    deleted += cleanup_old(
                        ventoy_path, final_filename, friendly_prefix
                    )

            if deleted:
                result.message = f"Removed old: {', '.join(deleted)}"

            # Update state
            result.filename = final_filename
            state[key] = {
                "version": version,
                "filename": final_filename,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if state_path:
                save_state(state_path, state)

            result.status = "updated"
            if not result.message:
                result.message = f"{result.old_version or '(new)'} -> {version}"

        elif method == "headers":
            dl_url = entry.get("download_url", "")
            if not dl_url:
                result.status = "error"
                result.message = "No download_url configured"
                return result

            needs_update, new_headers, filename = check_headers(
                entry, state_entry
            )
            result.filename = filename

            if not needs_update:
                result.status = "skipped"
                result.message = "Headers unchanged"
                return result

            if dry_run:
                result.status = "available"
                result.message = "Remote file has changed (headers differ)"
                return result

            dest = ventoy_path / filename
            download_iso(dl_url, dest, ua)

            if not dest.exists() or dest.stat().st_size == 0:
                raise RuntimeError("Download produced empty or missing file")

            # Rename to friendly name if enabled (no version for headers method)
            final_filename = friendly_filename(entry, "", filename)
            if final_filename != filename:
                final_dest = ventoy_path / final_filename
                dest.rename(final_dest)
                print(f"  Renamed to {final_filename}")

            # For headers method, cleanup by exact previous filename
            old_fn = state_entry.get("filename", "")
            if old_fn and old_fn != final_filename:
                old_path = ventoy_path / old_fn
                if old_path.exists():
                    old_path.unlink()
                    result.message = f"Removed old: {old_fn}"

            state[key] = {
                **new_headers,
                "filename": final_filename,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if state_path:
                save_state(state_path, state)

            result.filename = final_filename
            result.status = "updated"
            if not result.message:
                result.message = "Downloaded (headers changed)"

        else:
            result.status = "error"
            result.message = f"Unknown method: {method!r}"

    except Exception as exc:
        result.status = "error"
        result.message = str(exc)

    return result


def sync_all(config: dict, state: dict, ventoy_path: Path,
             dry_run: bool, only_key: str | None = None,
             state_path: Path | None = None) -> list[SyncResult]:
    """Run sync for all (or one) configured ISOs."""
    results = []
    isos = config["isos"]

    if only_key:
        if only_key not in isos:
            r = SyncResult(only_key, only_key)
            r.status = "error"
            r.message = f"Key {only_key!r} not found in config"
            results.append(r)
            return results
        isos = {only_key: isos[only_key]}

    for key, entry in isos.items():
        label = entry.get("name", key)
        print(f"\n{BOLD}[{label}]{RESET}")
        result = sync_one(key, entry, state, ventoy_path, dry_run, state_path)

        # Print status line
        if result.status == "updated":
            icon = f"{GREEN}UPDATED{RESET}"
        elif result.status == "available":
            icon = f"{CYAN}AVAILABLE{RESET}"
        elif result.status == "skipped":
            icon = f"{YELLOW}SKIPPED{RESET}"
        elif result.status == "disabled":
            icon = f"{YELLOW}DISABLED{RESET}"
        else:
            icon = f"{RED}ERROR{RESET}"

        print(f"  {icon}  {result.message}")
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def generate_summary(results: list[SyncResult], ventoy_path: Path,
                     dry_run: bool) -> None:
    """Write summary.md to the Ventoy drive root."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = "DRY RUN" if dry_run else "SYNC"

    lines = [
        f"# Ventoy Sync Summary",
        f"",
        f"**Run:** {now}  ",
        f"**Mode:** {mode}  ",
        f"**Drive:** `{ventoy_path}`",
        f"",
        f"| ISO | Status | Version | Details |",
        f"|-----|--------|---------|---------|",
    ]

    updated = 0
    skipped = 0
    errors = 0
    available = 0

    for r in results:
        status_badge = {
            "updated": "Updated",
            "available": "Available",
            "skipped": "Up to date",
            "disabled": "Disabled",
            "error": "ERROR",
        }.get(r.status, r.status)

        version_col = r.version or "-"
        lines.append(f"| {r.name} | {status_badge} | {version_col} | {r.message} |")

        if r.status == "updated":
            updated += 1
        elif r.status == "skipped":
            skipped += 1
        elif r.status == "error":
            errors += 1
        elif r.status == "available":
            available += 1

    lines.append("")
    lines.append(f"**Totals:** {updated} updated, {skipped} up-to-date, "
                 f"{available} available, {errors} errors")
    lines.append("")

    summary_path = ventoy_path / "summary.md"
    summary_path.write_text("\n".join(lines))
    print(f"\n  Summary written to {summary_path}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync ISOs on a Ventoy USB drive with upstream sources."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Check for updates without downloading anything."
    )
    parser.add_argument(
        "--check", metavar="KEY",
        help="Check/sync only a single ISO by its config key."
    )
    parser.add_argument(
        "--config", metavar="FILE", default=None,
        help="Path to config.yaml (default: alongside this script)."
    )
    parser.add_argument(
        "--backup", metavar="PATH", default=None,
        help="Copy new/missing ISOs to this directory after syncing."
    )
    args = parser.parse_args()

    # Resolve config path
    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config) if args.config else script_dir / "config.yaml"
    if not config_path.exists():
        sys.exit(f"{RED}Error:{RESET} Config not found: {config_path}")

    config = load_config(config_path)
    ventoy_path = Path(config["ventoy_path"])

    # Check drive is mounted
    if not ventoy_path.exists():
        sys.exit(
            f"{RED}Error:{RESET} Ventoy drive not found at {ventoy_path}\n"
            f"  Is the drive plugged in and mounted?"
        )
    if not os.access(ventoy_path, os.W_OK):
        sys.exit(
            f"{RED}Error:{RESET} Ventoy path {ventoy_path} is not writable."
        )

    state_path = ventoy_path / "state.json"
    state = load_state(state_path)

    mode_label = "DRY RUN" if args.dry_run else "SYNC"
    print(f"\n{BOLD}=== Ventoy ISO {mode_label} ==={RESET}")
    print(f"  Drive: {ventoy_path}")
    print(f"  Time:  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    start = time.monotonic()
    results = sync_all(config, state, ventoy_path, args.dry_run, args.check,
                       state_path=state_path)
    elapsed = time.monotonic() - start

    # Save state (even on dry-run we don't modify state, but this is safe)
    if not args.dry_run:
        save_state(state_path, state)

    generate_summary(results, ventoy_path, args.dry_run)

    # Optional backup pass
    if args.backup:
        print(f"\n{BOLD}=== Backup ==={RESET}")
        backup_to(ventoy_path, Path(args.backup), args.dry_run, isos=config["isos"])

    # Final console summary
    updated = sum(1 for r in results if r.status == "updated")
    skipped = sum(1 for r in results if r.status == "skipped")
    avail = sum(1 for r in results if r.status == "available")
    errors = sum(1 for r in results if r.status == "error")

    print(f"\n{BOLD}--- Done in {elapsed:.1f}s ---{RESET}")
    parts = []
    if updated:
        parts.append(f"{GREEN}{updated} updated{RESET}")
    if skipped:
        parts.append(f"{YELLOW}{skipped} up-to-date{RESET}")
    if avail:
        parts.append(f"{CYAN}{avail} available{RESET}")
    if errors:
        parts.append(f"{RED}{errors} errors{RESET}")
    print(f"  {', '.join(parts)}")

    # Exit with error code if any ISOs failed
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
