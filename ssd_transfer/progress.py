"""Progress display using rich. Handles sequential and parallel modes."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.live import Live
from rich.table import Column

from .utils import format_bytes, format_duration


class _SpeedColumn:
    """5-second moving average speed column."""

    def __init__(self):
        self._histories: dict[TaskID, deque] = {}

    def register(self, task_id: TaskID):
        self._histories[task_id] = deque()

    def record(self, task_id: TaskID, bytes_delta: int, now: float):
        if task_id not in self._histories:
            self._histories[task_id] = deque()
        self._histories[task_id].append((now, bytes_delta))
        # Prune entries older than 5 seconds
        cutoff = now - 5.0
        while self._histories[task_id] and self._histories[task_id][0][0] < cutoff:
            self._histories[task_id].popleft()

    def speed(self, task_id: TaskID) -> float:
        """Return bytes/sec moving average over last 5 seconds."""
        if task_id not in self._histories or not self._histories[task_id]:
            return 0.0
        history = self._histories[task_id]
        if len(history) < 2:
            return 0.0
        elapsed = history[-1][0] - history[0][0]
        if elapsed <= 0:
            return 0.0
        total_bytes = sum(b for _, b in history)
        return total_bytes / elapsed


class ProgressDisplay:
    def __init__(self, mode: str):
        self._mode = mode
        self._lock = threading.Lock()
        self._console = Console()
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=35),
            TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
            TextColumn("•"),
            TextColumn("{task.fields[transferred]} / {task.fields[total_size]}"),
            TextColumn("•"),
            TextColumn("{task.fields[speed]}"),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=self._console,
        )
        self._live = Live(self._progress, console=self._console, refresh_per_second=4)
        self._task_ids: dict[str, TaskID] = {}
        self._speed_tracker = _SpeedColumn()
        self._last_bytes: dict[str, int] = {}
        self._started = False

    def start(self):
        self._live.start()
        self._started = True

    def stop(self):
        if self._started:
            self._live.stop()
            self._started = False

    def add_job(self, job_id: str, label: str, total_bytes: int):
        with self._lock:
            description = f"SSD {label}" if self._mode == "parallel" else label
            task_id = self._progress.add_task(
                description,
                total=total_bytes,
                transferred="0 B",
                total_size=format_bytes(total_bytes),
                speed="-- B/s",
            )
            self._task_ids[job_id] = task_id
            self._speed_tracker.register(task_id)
            self._last_bytes[job_id] = 0

    def update(self, job_id: str, copied_bytes: int, current_file: str = ""):
        with self._lock:
            task_id = self._task_ids.get(job_id)
            if task_id is None:
                return

            now = time.monotonic()
            delta = copied_bytes - self._last_bytes.get(job_id, 0)
            self._last_bytes[job_id] = copied_bytes
            self._speed_tracker.record(task_id, delta, now)

            speed = self._speed_tracker.speed(task_id)
            speed_str = f"{format_bytes(int(speed))}/s" if speed > 0 else "-- B/s"

            self._progress.update(
                task_id,
                completed=copied_bytes,
                transferred=format_bytes(copied_bytes),
                speed=speed_str,
                description=(
                    f"{current_file[:40]}"
                    if self._mode == "sequential" and current_file
                    else self._progress.tasks[task_id].description
                ),
            )

    def complete(self, job_id: str, summary: dict):
        with self._lock:
            task_id = self._task_ids.get(job_id)
            if task_id is not None:
                self._progress.update(task_id, completed=self._progress.tasks[task_id].total)

        label = summary.get("label", job_id)
        dest = summary.get("dest", "")
        total_files = summary.get("total_files", 0)
        skipped = summary.get("skipped", 0)
        failed = summary.get("failed", 0)
        total_bytes = summary.get("total_bytes", 0)
        elapsed = summary.get("elapsed", 0)
        speed = total_bytes / elapsed if elapsed > 0 else 0

        sep = "━" * 55
        self._console.print(f"\n{sep}")
        self._console.print(f'[bold green][done] SSD "{label}" → {dest}[/bold green]')
        self._console.print(f"  Transferred:  {total_files:,} files")
        self._console.print(f"  Skipped:      {skipped:,} files (already transferred)")
        self._console.print(f"  Failed:       {failed:,} files")
        self._console.print(f"  Total size:   {format_bytes(total_bytes)}")
        self._console.print(f"  Duration:     {format_duration(elapsed)}")
        self._console.print(f"  Avg speed:    {format_bytes(int(speed))}/s")
        self._console.print(sep)

    def error(self, job_id: str, message: str):
        with self._lock:
            task_id = self._task_ids.get(job_id)
            if task_id is not None:
                self._progress.update(task_id, description=f"[red]error: {message[:40]}[/red]")
        self._console.print(f"[bold red][error] {message}[/bold red]")

    def prompt_duplicate(
        self,
        label: str,
        uuid: str,
        prev_dest: Path,
        timeout: int = 180,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Show duplicate SSD prompt. Returns 's', 'c', 'r', or 'o'. Stops/restarts Live."""
        with self._lock:
            was_started = self._started
            if was_started:
                self._live.stop()
                self._started = False

        self._console.print(
            f'\n[bold yellow][ssd-transfer] SSD "{label}" (UUID: {uuid}) was previously transferred.[/bold yellow]'
        )
        self._console.print(f"  Destination: {prev_dest}\n")
        self._console.print("  What would you like to do?")
        self._console.print(r"  \[s] Skip (do nothing)")
        self._console.print(r"  \[c] Copy to a new folder (no overwrite)")
        self._console.print(r"  \[r] Resume existing folder (skip same name+size, copy new/changed)")
        self._console.print(r"  \[o] Overwrite copy (re-copy all files)")
        self._console.print(f"  Auto-selecting \\[c] in {timeout}s if no input.")

        choice = _timed_input("  Choice [s/c/r/o]: ", timeout=timeout, default="c", cancel_event=cancel_event)
        valid = {"s", "c", "r", "o"}
        if choice.strip().lower() not in valid:
            if cancel_event is None or not cancel_event.is_set():
                self._console.print(r"  → Auto-selected: \[c] copy to new folder")
            choice = "c"
        else:
            choice = choice.strip().lower()

        with self._lock:
            if was_started:
                self._live.start()
                self._started = True

        return choice

    def print(self, message: str):
        self._console.print(message)


def _timed_input(
    prompt: str, timeout: int, default: str, cancel_event: Optional[threading.Event] = None
) -> str:
    """Read a line from stdin with a timeout. Returns default on timeout or cancellation."""
    import sys
    import select

    print(prompt, end="", flush=True)

    watch_fds = [sys.stdin]
    pipe_fds: Optional[tuple[int, int]] = None

    if cancel_event is not None:
        r_fd, w_fd = os.pipe()
        pipe_fds = (r_fd, w_fd)
        watch_fds.append(r_fd)

        def _pipe_on_cancel() -> None:
            cancel_event.wait()
            try:
                os.write(w_fd, b"\x00")
            except OSError:
                pass

        threading.Thread(target=_pipe_on_cancel, daemon=True).start()

    try:
        ready, _, _ = select.select(watch_fds, [], [], timeout)
        if sys.stdin in ready:
            return sys.stdin.readline().rstrip("\n")
        print()  # newline after timeout or cancellation
        return default
    finally:
        if pipe_fds is not None:
            r_fd, w_fd = pipe_fds
            os.close(r_fd)
            try:
                os.close(w_fd)
            except OSError:
                pass
