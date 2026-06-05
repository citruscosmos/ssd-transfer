# ssd-transfer

A CLI daemon that automatically detects external SSDs and copies their contents to a destination folder.

Plug in an SSD and the files copy themselves. Disconnect mid-transfer, reconnect, and it resumes from where it left off.

## Features

- **Auto-detection**: Monitors udev events and detects SSD connections instantly
- **Resume support**: Skips size-matched files on reconnect; re-copies size-mismatched (partial) files
- **Duplicate guard**: Prompts on second connection of the same SSD — skip, copy to new folder, or overwrite
- **Filters**: Limit copies by file extension or directory name
- **Parallel transfers**: Process multiple SSDs simultaneously
- **Disk space check**: Verifies free space before starting and monitors mid-transfer
- **Progress display**: Real-time transfer speed and ETA via `rich`

## Supported devices

- USB-native SSDs (e.g. SanDisk Extreme Pro)
- USB-to-SATA adapter SSDs (e.g. ORICO)
- Devices with or without a partition table

## Requirements

- Ubuntu 22.04+
- Python 3.10+
- `udev` / `systemd-udevd` running
- `blkid` command (`util-linux` package)

## Installation

```bash
git clone https://github.com/citruscosmos/ssd-transfer.git
cd ssd-transfer

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

## Usage

### Basic

```bash
ssd-transfer --dest /mnt/backup
```

Start the daemon, then plug in an SSD — copying begins automatically. Press `Ctrl+C` to exit cleanly.

### Options

| Option | Default | Description |
|---|---|---|
| `--dest DIR` | (required) | Destination folder |
| `--mode sequential\|parallel` | `sequential` | Multi-SSD processing mode |
| `--filter-ext .jpg .mp4 ...` | all files | Copy only these extensions |
| `--filter-dir DCIM Pictures ...` | all dirs | Copy only from these directories |
| `--max-concurrent N` | `2` | Max simultaneous transfers (parallel mode) |

### Examples

```bash
# Copy only jpg and mp4 files
ssd-transfer --dest /mnt/backup --filter-ext .jpg .mp4

# Copy only from the DCIM directory
ssd-transfer --dest /mnt/backup --filter-dir DCIM

# Process multiple SSDs in parallel
ssd-transfer --dest /mnt/backup --mode parallel

# Use an external SSD as the destination
# Attach the destination SSD first, then start the daemon pointing to its mount
ssd-transfer --dest /media/user/BACKUP/transfer
```

## Destination folder structure

A timestamped subfolder is created per connection:

```
/mnt/backup/
├── 20260605_143022/        # connection timestamp
│   └── PHOTOS_SSD/         # SSD label (or first 8 chars of UUID if no label)
│       ├── DCIM/
│       └── Documents/
└── 20260605_150011/
    └── WORK_DATA/
        └── ...
```

A `.transfer_complete` JSON file is written on successful completion and used to detect duplicate connections.

## Behaviour details

### Resume on reconnect

| Situation | Action |
|---|---|
| File not in destination | Copy |
| File exists, sizes match | Skip (already transferred) |
| File exists, sizes differ | Overwrite (partial file) |

### Duplicate SSD prompt

```
[ssd-transfer] SSD "PHOTOS_SSD" (UUID: a1b2c3d4-...) was previously transferred.
  Destination: /mnt/backup/20260605_143022/PHOTOS_SSD

  What would you like to do?
  [s] Skip (do nothing)
  [c] Copy to a new folder (no overwrite)
  [r] Overwrite copy (re-copy all files)
  Choice [s/c/r]:
```

Automatically selects `[s]` skip after 30 seconds with no input.

### Ctrl+C shutdown

Cancels in-flight transfers, deletes all `.tmp` files, then exits cleanly.

## Logs

Transfer logs are written to `~/.local/share/ssd-transfer/transfers.log`.

## Dependencies

| Library | Purpose |
|---|---|
| `pyudev` | udev event monitoring |
| `psutil` | Disk partition info |
| `rich` | Progress bars and console output |
