"""Entry point: CLI parsing, startup validation, glue."""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .monitor import DeviceMonitor
from .progress import ProgressDisplay
from .transfer import TransferJob, read_complete_marker, read_started_marker
from .utils import format_bytes

logger = logging.getLogger(__name__)


def _setup_logging():
    log_dir = Path.home() / ".local" / "share" / "ssd-transfer"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "transfers.log"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    # Suppress noisy pyudev debug output
    logging.getLogger("pyudev").setLevel(logging.WARNING)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ssd-transfer",
        description="CLI daemon that auto-detects external SSDs and copies files to a destination folder",
    )
    parser.add_argument("--dest", required=True, type=Path, metavar="DIR", help="destination folder")
    parser.add_argument(
        "--mode",
        choices=["sequential", "parallel"],
        default="sequential",
        help="multi-SSD processing mode (default: sequential)",
    )
    parser.add_argument(
        "--filter-ext",
        nargs="+",
        metavar="EXT",
        help="copy only these extensions (e.g. --filter-ext .jpg .mp4)",
    )
    parser.add_argument(
        "--filter-dir",
        nargs="+",
        metavar="DIR",
        help="copy only from these directories (e.g. --filter-dir DCIM Pictures)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=2,
        metavar="N",
        help="max simultaneous transfers in parallel mode (default: 2)",
    )
    return parser.parse_args()


class App:
    def __init__(self, args: argparse.Namespace):
        self._dest = args.dest
        self._mode = args.mode
        self._max_concurrent = args.max_concurrent
        self._filters = {
            "ext": {e.lower() for e in args.filter_ext} if args.filter_ext else None,
            "dir": set(args.filter_dir) if args.filter_dir else None,
        }

        self._progress = ProgressDisplay(self._mode)
        self._active_jobs: dict[str, TransferJob] = {}
        self._jobs_lock = threading.Lock()

        # Sequential mode: queue + single worker thread
        self._job_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None

        # Parallel mode: semaphore limits concurrency
        self._parallel_sem = threading.Semaphore(self._max_concurrent)

        self._monitor: Optional[DeviceMonitor] = None
        self._shutdown = threading.Event()

        # devpath -> cancel_event for devices currently showing a duplicate prompt
        self._pending_prompts: dict[str, threading.Event] = {}
        self._pending_prompts_lock = threading.Lock()

    def run(self):
        self._validate_dest()
        self._progress.start()

        self._monitor = DeviceMonitor(
            dest=self._dest,
            mode=self._mode,
            filters=self._filters,
            on_device_added=self._on_device_added,
            on_device_removed=self._on_device_removed,
        )

        if self._mode == "sequential":
            self._worker_thread = threading.Thread(
                target=self._sequential_worker, daemon=True, name="sequential-worker"
            )
            self._worker_thread.start()

        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)

        self._progress.print(
            f"[bold green][ssd-transfer] Ready. Waiting for SSD... (dest: {self._dest})[/bold green]"
        )
        self._monitor.start()

        # Block main thread until shutdown
        self._shutdown.wait()

    def _validate_dest(self):
        if not self._dest.exists():
            print(f"[error] Destination folder does not exist: {self._dest}", file=sys.stderr)
            sys.exit(1)
        if not os.access(self._dest, os.W_OK):
            print(f"[error] No write permission for destination: {self._dest}", file=sys.stderr)
            sys.exit(1)

    def _on_device_added(
        self,
        devpath: str,
        mount_point: Path,
        uuid: str,
        label: str,
        display_name: str,
    ):
        # Return immediately so the udev observer thread stays free to process
        # remove/add events while a duplicate prompt is being shown.
        if self._shutdown.is_set():
            return
        threading.Thread(
            target=self._handle_device_added,
            kwargs=dict(
                devpath=devpath,
                mount_point=mount_point,
                uuid=uuid,
                label=label,
                display_name=display_name,
            ),
            daemon=True,
            name=f"device-handler-{Path(devpath).name}",
        ).start()

    def _handle_device_added(
        self,
        devpath: str,
        mount_point: Path,
        uuid: str,
        label: str,
        display_name: str,
    ):
        if self._shutdown.is_set():
            return

        # Check for previous transfer of same UUID
        existing_dest = self._find_previous_transfer(uuid) if uuid else None

        if existing_dest:
            cancel_event = threading.Event()
            with self._pending_prompts_lock:
                self._pending_prompts[devpath] = cancel_event

            try:
                choice = self._progress.prompt_duplicate(
                    label=display_name,
                    uuid=uuid,
                    prev_dest=existing_dest,
                    cancel_event=cancel_event,
                )
            finally:
                with self._pending_prompts_lock:
                    if self._pending_prompts.get(devpath) is cancel_event:
                        del self._pending_prompts[devpath]

            if cancel_event.is_set():
                self._progress.print(
                    f"[bold yellow][ssd-transfer] Prompt cancelled: {display_name} was disconnected.[/bold yellow]"
                )
                return

            if choice == "s":
                self._progress.print(f"[ssd-transfer] Skipped: {display_name}")
                return
            elif choice == "o":
                dest_folder = existing_dest
                force_overwrite = True
            elif choice == "r":
                dest_folder = existing_dest
                force_overwrite = False
            else:  # 'c'
                dest_folder = self._make_dest_folder(display_name)
                force_overwrite = False
        else:
            dest_folder = self._make_dest_folder(display_name)
            force_overwrite = False

        dest_folder.mkdir(parents=True, exist_ok=True)

        job_id = f"{devpath}_{datetime.now().strftime('%H%M%S')}"
        job = TransferJob(
            src=mount_point,
            dest=dest_folder,
            filters=self._filters,
            uuid=uuid,
            label=label,
            display_name=display_name,
            job_id=job_id,
            on_progress=self._on_progress,
            on_complete=self._on_complete,
            on_error=self._on_error,
            force_overwrite=force_overwrite,
        )

        with self._jobs_lock:
            self._active_jobs[devpath] = job

        self._progress.print(
            f"[bold cyan][ssd-transfer] Detected: {display_name} ({devpath}) → {dest_folder}[/bold cyan]"
        )

        if self._mode == "sequential":
            self._job_queue.put(job)
        else:
            threading.Thread(
                target=self._run_parallel_job, args=(job,), daemon=True
            ).start()

    def _on_device_removed(self, devpath: str):
        # Cancel a pending duplicate prompt for this device (if any).
        with self._pending_prompts_lock:
            cancel_event = self._pending_prompts.get(devpath)
        if cancel_event is not None:
            cancel_event.set()

        with self._jobs_lock:
            job = self._active_jobs.get(devpath)
        if job:
            job.cancel()
            self._progress.print(
                f'[bold yellow][ssd-transfer] SSD "{job.display_name}" disconnected. Transfer cancelled.\n'
                f"  Waiting for reconnect...[/bold yellow]"
            )

    def _on_progress(self, job_id: str, copied_bytes: int, current_file: str, total_bytes: int, phase: str):
        if phase == "start":
            self._progress.add_job(job_id, job_id.split("_")[0], total_bytes)
        else:
            self._progress.update(job_id, copied_bytes, current_file)

    def _on_complete(self, job_id: str, summary: dict):
        self._progress.complete(job_id, summary)

    def _on_error(self, job_id: str, message: str):
        self._progress.error(job_id, message)

    def _sequential_worker(self):
        while not self._shutdown.is_set():
            try:
                job: TransferJob = self._job_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            job.start()
            job.join()
            self._job_queue.task_done()

    def _run_parallel_job(self, job: TransferJob):
        self._parallel_sem.acquire()
        try:
            job.start()
            job.join()
        finally:
            self._parallel_sem.release()

    def _make_dest_folder(self, display_name: str) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self._dest / ts / display_name

    def _find_previous_transfer(self, uuid: str) -> Optional[Path]:
        """Scan dest for transfer markers matching UUID; return most recent folder."""
        candidates = []
        seen: set[Path] = set()

        for marker_file in self._dest.rglob(".transfer_complete"):
            parent = marker_file.parent
            data = read_complete_marker(parent)
            if data and data.get("uuid") == uuid:
                candidates.append((data.get("completed_at", ""), parent))
                seen.add(parent)

        # Also find incomplete (started but not completed) transfers
        for marker_file in self._dest.rglob(".transfer_started"):
            parent = marker_file.parent
            if parent in seen:
                continue
            data = read_started_marker(parent)
            if data and data.get("uuid") == uuid:
                candidates.append((data.get("started_at", ""), parent))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _handle_sigint(self, signum, frame):
        self._progress.print("\n[bold red][ssd-transfer] Shutting down...[/bold red]")
        if self._monitor:
            self._monitor.stop()
        with self._jobs_lock:
            jobs = list(self._active_jobs.values())
        for job in jobs:
            job.cancel()
        for job in jobs:
            job.join()
        self._progress.stop()
        self._shutdown.set()


def main():
    _setup_logging()
    args = _parse_args()
    App(args).run()


if __name__ == "__main__":
    main()
