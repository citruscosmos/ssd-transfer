"""File transfer logic: copy with .tmp dance, resume, disk space checks."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB
DISK_CHECK_INTERVAL = 500 * 1024 * 1024  # 500 MB
DISK_WARN_RATIO = 0.10  # warn when free < 10% of remaining


class TransferJob:
    def __init__(
        self,
        src: Path,
        dest: Path,
        filters: dict,
        uuid: str,
        label: str,
        display_name: str,
        job_id: str,
        on_progress: Optional[Callable] = None,
        on_complete: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        force_overwrite: bool = False,
    ):
        self.src = src
        self.dest = dest
        self.filters = filters
        self.uuid = uuid
        self.label = label
        self.display_name = display_name
        self.job_id = job_id
        self.on_progress = on_progress
        self.on_complete = on_complete
        self.on_error = on_error
        self.force_overwrite = force_overwrite

        self.cancelled = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sudo_synced_dirs: set[Path] = set()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"transfer-{self.job_id}")
        self._thread.start()

    def join(self):
        if self._thread:
            self._thread.join()

    def cancel(self):
        self.cancelled.set()

    def _run(self):
        start_time = time.monotonic()
        total_files = 0
        skipped = 0
        failed = 0
        copied_bytes = 0

        try:
            self._write_started_marker()
            self._cleanup_tmp_files()

            files = list(self._scan_files())
            if not files:
                logger.info(f"{self.job_id}: no files to copy")
                self._write_complete_marker(0, 0, skipped, failed)
                return

            total_size = sum(f.stat().st_size for f in files)
            self._check_disk_space(total_size)

            if self.on_progress:
                self.on_progress(
                    job_id=self.job_id,
                    copied_bytes=0,
                    current_file="",
                    total_bytes=total_size,
                    phase="start",
                )

            bytes_since_last_check = 0

            for src_file in files:
                if self.cancelled.is_set():
                    logger.info(f"{self.job_id}: cancelled")
                    break

                rel = src_file.relative_to(self.src)
                dest_file = self.dest / rel

                # Resume / skip logic
                if not self.force_overwrite and dest_file.exists():
                    src_size = src_file.stat().st_size
                    dest_size = dest_file.stat().st_size
                    if src_size == dest_size:
                        skipped += 1
                        copied_bytes += src_size
                        if self.on_progress:
                            self.on_progress(
                                job_id=self.job_id,
                                copied_bytes=copied_bytes,
                                current_file=str(rel),
                                total_bytes=total_size,
                                phase="skip",
                            )
                        continue

                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    file_copied = self._copy_file(
                        src_file,
                        dest_file,
                        copied_bytes,
                        total_size,
                        rel,
                    )
                    copied_bytes += file_copied
                    bytes_since_last_check += file_copied
                    total_files += 1
                except OSError as exc:
                    logger.warning(f"copy failed {src_file}: {exc}")
                    failed += 1

                # Mid-copy disk space check every 500 MB
                if bytes_since_last_check >= DISK_CHECK_INTERVAL:
                    remaining = total_size - copied_bytes
                    free = shutil.disk_usage(self.dest).free
                    if free < remaining * DISK_WARN_RATIO:
                        logger.warning(
                            f"destination free space is below {DISK_WARN_RATIO*100:.0f}% of remaining transfer"
                        )
                    bytes_since_last_check = 0

        except DiskSpaceError as exc:
            logger.error(str(exc))
            self._cleanup_tmp_files()
            if self.on_error:
                self.on_error(job_id=self.job_id, message=str(exc))
            return
        except IOError as exc:
            # Likely disconnection
            logger.warning(f"IOError (disconnected?): {exc}")
            self._cleanup_tmp_files()
            if self.on_error:
                self.on_error(job_id=self.job_id, message=f"transfer error: {exc}")
            return

        if not self.cancelled.is_set():
            elapsed = time.monotonic() - start_time
            self._write_complete_marker(total_files, copied_bytes, skipped, failed)

            if self.on_complete:
                self.on_complete(
                    job_id=self.job_id,
                    summary={
                        "label": self.display_name,
                        "dest": str(self.dest),
                        "total_files": total_files,
                        "skipped": skipped,
                        "failed": failed,
                        "total_bytes": copied_bytes,
                        "elapsed": elapsed,
                    },
                )

    def _copy_file(
        self,
        src_file: Path,
        dest_file: Path,
        copied_so_far: int,
        total_size: int,
        rel: Path,
    ) -> int:
        tmp_file = dest_file.with_suffix(dest_file.suffix + ".tmp")
        file_bytes = 0
        try:
            try:
                fsrc_cm = open(src_file, "rb")
            except PermissionError:
                return self._copy_file_sudo(src_file, dest_file, copied_so_far, total_size, rel)

            with fsrc_cm as fsrc, open(tmp_file, "wb") as fdst:
                while True:
                    if self.cancelled.is_set():
                        raise TransferCancelledError()
                    chunk = fsrc.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    file_bytes += len(chunk)

                    if self.on_progress:
                        self.on_progress(
                            job_id=self.job_id,
                            copied_bytes=copied_so_far + file_bytes,
                            current_file=str(rel),
                            total_bytes=total_size,
                            phase="copy",
                        )

            # Preserve timestamps (best-effort; may fail on FAT/exFAT)
            try:
                shutil.copystat(src_file, tmp_file)
            except OSError as exc:
                logger.warning(f"failed to preserve timestamps for {dest_file}: {exc}")

            os.rename(tmp_file, dest_file)
        except (TransferCancelledError, BaseException):
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise

        return file_bytes

    def _copy_file_sudo(
        self,
        src_file: Path,
        dest_file: Path,
        copied_so_far: int,
        total_size: int,
        rel: Path,
    ) -> int:
        """Copy via sudo, batching the entire parent directory on first PermissionError.

        Calling sudo rsync per-file has ~100 ms overhead each. Rsyncing the whole
        directory once and reusing the result for sibling files is orders of magnitude
        faster when a directory contains many root-owned files (e.g. coredump/).
        """
        src_dir = src_file.parent
        dest_dir = dest_file.parent

        if src_dir not in self._sudo_synced_dirs:
            logger.info(f"permission denied in {src_dir}, sudo rsync whole directory → {dest_dir}")
            dest_dir.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["sudo", "rsync", "-a", "--", f"{src_dir}/", f"{dest_dir}/"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise OSError(f"sudo rsync failed for {src_dir}: {result.stderr.strip()}")
            try:
                subprocess.run(
                    ["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(dest_dir)],
                    capture_output=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning(f"chown -R failed for {dest_dir}: {exc.stderr.strip() if exc.stderr else exc}")
            self._sudo_synced_dirs.add(src_dir)

        file_bytes = dest_file.stat().st_size if dest_file.exists() else 0
        if self.on_progress:
            self.on_progress(
                job_id=self.job_id,
                copied_bytes=copied_so_far + file_bytes,
                current_file=str(rel),
                total_bytes=total_size,
                phase="copy",
            )
        return file_bytes

    def _scan_files(self):
        """Yield files matching the active filters (generator)."""
        ext_filter = self.filters.get("ext")  # set of lowercase extensions or None
        dir_filter = self.filters.get("dir")  # set of directory names or None

        try:
            entries = list(self.src.rglob("*"))
        except PermissionError:
            entries = list(self._rglob_sudo(self.src))

        for src_file in entries:
            if not src_file.is_file():
                continue
            if src_file.is_symlink():
                continue

            if dir_filter:
                # Any part of the relative path must be in dir_filter
                rel_parts = src_file.relative_to(self.src).parts
                if not any(part in dir_filter for part in rel_parts[:-1]):
                    continue

            if ext_filter:
                if src_file.suffix.lower() not in ext_filter:
                    continue

            yield src_file

    def _rglob_sudo(self, root: Path):
        """List all files under root using sudo find (fallback for root-owned trees)."""
        logger.info(f"permission denied listing {root}, retrying with sudo find")
        result = subprocess.run(
            ["sudo", "find", str(root), "-not", "-type", "d"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PermissionError(f"sudo find failed: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            if line:
                yield Path(line)

    def _check_disk_space(self, required_bytes: int):
        usage = shutil.disk_usage(self.dest)
        free = usage.free
        if free < required_bytes:
            from .utils import format_bytes
            raise DiskSpaceError(
                f"insufficient space at destination\n"
                f"  required: {format_bytes(required_bytes)}\n"
                f"  free:     {format_bytes(free)}\n"
                f"  shortage: {format_bytes(required_bytes - free)}"
            )

    def _cleanup_tmp_files(self):
        if self.dest.exists():
            for tmp in self.dest.rglob("*.tmp"):
                tmp.unlink(missing_ok=True)
                logger.debug(f"removed tmp file: {tmp}")

    def _write_started_marker(self):
        marker_path = self.dest / ".transfer_started"
        data = {
            "uuid": self.uuid,
            "label": self.label,
            "started_at": datetime.now(timezone.utc).astimezone().isoformat(),
        }
        marker_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _write_complete_marker(self, total_files: int, total_bytes: int, skipped: int, failed: int):
        marker_path = self.dest / ".transfer_complete"
        tmp_marker = self.dest / ".transfer_complete.tmp"
        data = {
            "uuid": self.uuid,
            "label": self.label,
            "completed_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "total_files": total_files,
            "skipped": skipped,
            "failed": failed,
            "total_bytes": total_bytes,
        }
        tmp_marker.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.rename(tmp_marker, marker_path)


def read_complete_marker(path: Path) -> Optional[dict]:
    """Return parsed .transfer_complete JSON, or None if absent/invalid."""
    marker = path / ".transfer_complete"
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def read_started_marker(path: Path) -> Optional[dict]:
    """Return parsed .transfer_started JSON, or None if absent/invalid."""
    marker = path / ".transfer_started"
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except (json.JSONDecodeError, OSError):
        return None


class DiskSpaceError(Exception):
    pass


class TransferCancelledError(Exception):
    pass
