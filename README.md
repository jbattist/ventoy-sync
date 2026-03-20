# ventoy-sync

Automated ISO updater for [Ventoy](https://www.ventoy.net/) USB drives. Checks upstream sources for new ISO versions and downloads updates using `curl` with resume support. Generates a summary report after each run.

## Features

- **Regex scraping** for version detection on upstream download pages
- **HTTP HEAD** checking for ISOs with static URLs (e.g. daily builds)
- **Friendly renaming** of ISOs using the `name` field (e.g. `Arch Linux - 2026.03.01.iso`)
- **Resume support** via `curl -C -` for interrupted downloads
- **Auto-cleanup** of old ISO versions after successful download
- **Named capture groups** for complex version strings (e.g. Fedora `{major}-{build}`)
- **Zip extraction** for ISOs distributed as `.zip` files (e.g. Memtest86+)
- **Systemd timer** for daily automated sync
- **Dry-run mode** to check for updates without downloading
- **Per-ISO checking** via `--check KEY`

## Included ISOs

| ISO | Method | Notes |
|-----|--------|-------|
| Arch Linux | regex | |
| EndeavourOS | regex | |
| CachyOS | regex | `regex_last` for chronological directory listing |
| Ubuntu Desktop (Daily) | headers | |
| Proxmox VE | regex | |
| Proxmox Backup Server | regex | |
| Linux Mint (Cinnamon) | regex | |
| Zorin OS | regex | Named groups `{major}`, `{rev}` |
| TrueNAS SCALE | regex | |
| Hiren's BootCD PE | headers | |
| Clonezilla | regex | |
| Rescuezilla | regex | |
| GParted Live | regex | |
| SystemRescue | regex | SourceForge mirror |
| Windows 11 | headers | Disabled; requires manual URL |
| Fedora Workstation | regex | Named groups `{major}`, `{build}` |
| Debian Live (GNOME) | regex | |
| NixOS (Minimal) | regex | |
| Tails | regex | |
| Kali Linux (Live) | regex | |
| Memtest86+ | regex | `unzip: true` (ships as `.iso.zip`) |
| ShredOS | regex | Named groups `{tag}`, `{fname}` |
| DBAN | regex | Discontinued; custom `user_agent` |

## Requirements

- Python 3.12+
- `curl`
- A Ventoy USB drive

## Setup

```bash
git clone https://github.com/YOUR_USER/ventoy-sync.git
cd ventoy-sync

# Edit config.yaml to set ventoy_path to your Ventoy USB mount point
$EDITOR config.yaml

# Install (creates venv, installs deps, sets up systemd timer)
./install.sh
```

## Usage

```bash
# Full sync (download new ISOs, clean up old ones)
./ventoy-sync.py

# Check for updates without downloading
./ventoy-sync.py --dry-run

# Check a single ISO
./ventoy-sync.py --check archlinux
```

## Configuration

`config.yaml` defines the Ventoy drive path and ISO entries. Each entry needs:

### Regex method

```yaml
archlinux:
  name: "Arch Linux"
  method: "regex"
  url: "https://archlinux.org/download/"           # Page to scrape
  regex: 'archlinux-(\d{4}\.\d{2}\.\d{2})-x86_64\.iso'  # Group 1 = version
  download_url_template: "https://mirror.example.com/archlinux-{version}-x86_64.iso"
  filename_template: "archlinux-{version}-x86_64.iso"
```

Optional fields:
- `rename: true` -- rename the downloaded file to `{name} - {version}.iso` (see [Friendly renaming](#friendly-renaming))
- `regex_last: true` -- use last match instead of first (for chronological listings)
- `unzip: true` -- download is a zip; extract the ISO after download
- `user_agent: "..."` -- override the default User-Agent for this entry
- `enabled: false` -- skip this entry

### Headers method

```yaml
ubuntu_daily:
  name: "Ubuntu Desktop (Daily)"
  method: "headers"
  download_url: "https://cdimage.ubuntu.com/daily-live/current/resolute-desktop-amd64.iso"
```

Uses HTTP HEAD to detect changes via ETag / Content-Length.

### Named capture groups

For ISOs with multi-part version strings, use named groups:

```yaml
fedora:
  regex: 'Fedora-Workstation-Live-(?P<major>\d+)-(?P<build>[\d.]+)\.x86_64\.iso'
  download_url_template: "https://example.com/{major}/Fedora-{major}-{build}.iso"
  filename_template: "Fedora-{major}-{build}.x86_64.iso"
```

`{version}` (always group 1) and any `(?P<name>...)` groups are available in templates.

### Friendly renaming

Set `rename: true` on an entry to rename the downloaded ISO using the `name` field
and version string. The pattern is `{name} - {version}.{ext}`:

```yaml
archlinux:
  name: "Arch Linux"
  rename: true
  method: "regex"
  # ... other fields ...
```

This renames `archlinux-2026.03.01-x86_64.iso` to `Arch Linux - 2026.03.01.iso`.

For headers-method entries (which have no version string), the file is renamed to
`{name}.{ext}`, e.g. `Ubuntu Desktop (Daily).iso`.

When renaming is active, old files with both the upstream name pattern and the
friendly name pattern are cleaned up automatically.

## File layout

```
ventoy-sync/
  ventoy-sync.py          # Main script (self-reexecs under .venv)
  config.yaml             # ISO definitions and drive path
  requirements.txt        # Python deps (requests, PyYAML)
  install.sh              # Setup: venv, deps, systemd timer
  ventoy-sync.service     # Systemd service template
  ventoy-sync.timer       # Systemd daily timer
```

On the Ventoy drive:
```
/run/media/user/VENTOY/
  state.json              # Tracks current versions
  summary.md              # Last sync report
  Arch Linux - 2026.03.01.iso
  Ubuntu Desktop (Daily).iso
  ...
```

## Systemd timer

The timer runs daily with a random delay up to 30 minutes. Manage it with:

```bash
systemctl --user status ventoy-sync.timer     # Timer status
systemctl --user start ventoy-sync.service     # Run sync now
journalctl --user -u ventoy-sync.service       # View logs
systemctl --user disable --now ventoy-sync.timer  # Disable
```

## License

MIT
