"""Utility functions: UUID detection, mount point lookup, external device check."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

import psutil


def get_device_uuid(devpath: str, retries: int = 20, interval: float = 0.5) -> str:
    """Return filesystem UUID for devpath, or empty string if not found.

    Tries blkid first, falls back to /dev/disk/by-uuid/ symlinks.
    Retries to handle the race between udev add event and symlink creation.
    """
    for _ in range(retries):
        # Primary: blkid
        try:
            result = subprocess.run(
                ["blkid", "-o", "value", "-s", "UUID", devpath],
                capture_output=True,
                text=True,
                timeout=5,
            )
            uuid = result.stdout.strip()
            if uuid:
                return uuid
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        # Fallback: /dev/disk/by-uuid/ symlinks
        by_uuid = Path("/dev/disk/by-uuid")
        if by_uuid.exists():
            try:
                dev_resolved = Path(devpath).resolve()
                for link in by_uuid.iterdir():
                    if link.resolve() == dev_resolved:
                        return link.name
            except OSError:
                pass

        time.sleep(interval)

    return ""


def get_mount_point(devpath: str, retries: int = 20, interval: float = 0.5) -> Optional[Path]:
    """Return the mount point for devpath, retrying up to 10 seconds."""
    for _ in range(retries):
        for part in psutil.disk_partitions(all=True):
            if part.device == devpath:
                mp = Path(part.mountpoint)
                if mp.exists():
                    return mp
        time.sleep(interval)
    return None


def is_external_device(devname: str) -> bool:
    """Return True if the block device is an external/removable drive.

    devname is the base device name (e.g. 'sdb', not 'sdb1').

    USB-to-SATA adapters report ID_BUS=ata, so bus-name checks alone are
    insufficient. The most reliable signal is the /sys/block symlink target,
    which traverses the full device tree and contains 'usb' for any USB-
    attached device regardless of the intermediate protocol.
    """
    # /sys/block/<dev> is a symlink into /sys/devices/...; 'usb' appears in
    # the path for USB-attached devices including USB-SATA adapters.
    sys_block = Path(f"/sys/block/{devname}")
    try:
        target = str(sys_block.resolve())
        if "usb" in target or "ieee1394" in target:
            return True
    except OSError:
        pass

    # Fallback: removable flag (set to 1 for many USB sticks/drives)
    removable_path = Path(f"/sys/block/{devname}/removable")
    try:
        if removable_path.read_text().strip() == "1":
            return True
    except OSError:
        pass

    return False


def is_system_mount_point(mountpoint: str) -> bool:
    """Return True if the mountpoint belongs to the system (/, /boot, etc.)."""
    system_prefixes = {"/", "/boot", "/boot/efi", "/home", "/var", "/usr", "/opt", "/tmp"}
    return mountpoint in system_prefixes


def get_device_label(devpath: str) -> str:
    """Return filesystem label for devpath, or empty string."""
    try:
        result = subprocess.run(
            ["blkid", "-o", "value", "-s", "LABEL", devpath],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def format_bytes(n: int) -> str:
    """Return human-readable byte size (e.g. '42.3 GB')."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_duration(seconds: float) -> str:
    """Return human-readable duration (e.g. '10m 32s')."""
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
