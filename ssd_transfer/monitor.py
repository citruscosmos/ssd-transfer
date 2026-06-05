"""udev device monitor: detects SSD plug/unplug events."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

import psutil
import pyudev

from .utils import (
    get_device_label,
    get_device_uuid,
    get_mount_point,
    is_external_device,
    is_system_mount_point,
)

logger = logging.getLogger(__name__)


class DeviceMonitor:
    def __init__(
        self,
        dest: Path,
        mode: str,
        filters: dict,
        on_device_added: Callable,
        on_device_removed: Callable,
    ):
        self._dest = dest
        self._mode = mode
        self._filters = filters
        self._on_device_added = on_device_added
        self._on_device_removed = on_device_removed

        self._context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._context)
        self._monitor.filter_by(subsystem="block", device_type="partition")
        self._observer: Optional[pyudev.MonitorObserver] = None
        self._stopped = threading.Event()

    def start(self):
        """Start udev monitor and scan already-mounted devices."""
        self._observer = pyudev.MonitorObserver(self._monitor, callback=self._handle_event)
        self._observer.start()
        logger.info("udevモニター開始")
        self._scan_existing_devices()

    def stop(self):
        """Graceful shutdown."""
        self._stopped.set()
        if self._observer:
            self._observer.stop()
        logger.info("udevモニター停止")

    def _handle_event(self, device: pyudev.Device):
        if self._stopped.is_set():
            return
        action = device.action
        if action == "add":
            self._on_device_event_added(device)
        elif action == "remove":
            self._on_device_event_removed(device)

    def _on_device_event_added(self, device: pyudev.Device):
        devpath = device.device_node
        if not devpath:
            return

        devname = Path(devpath).name
        # Strip partition suffix to get base device name (sdb1 → sdb)
        base_devname = devpath.rstrip("0123456789")
        base_devname = Path(base_devname).name

        if not is_external_device(base_devname):
            logger.debug(f"{devpath} は外付けデバイスではないためスキップ")
            return

        mount_point = get_mount_point(devpath)
        if mount_point is None:
            logger.warning(f"{devpath} のマウントポイント取得がタイムアウトしました。スキップします。")
            return

        if is_system_mount_point(str(mount_point)):
            logger.debug(f"{mount_point} はシステムマウントポイントのためスキップ")
            return

        uuid = get_device_uuid(devpath)
        label = get_device_label(devpath)
        display_name = label if label else (f"dev_{uuid[:8]}" if uuid else devname)

        logger.info(f"外付けデバイス検出: {display_name} ({devpath}) → {mount_point}")
        self._on_device_added(
            devpath=devpath,
            mount_point=mount_point,
            uuid=uuid,
            label=label,
            display_name=display_name,
        )

    def _on_device_event_removed(self, device: pyudev.Device):
        devpath = device.device_node
        if not devpath:
            return
        logger.info(f"デバイス切断検出: {devpath}")
        self._on_device_removed(devpath=devpath)

    def _scan_existing_devices(self):
        """Scan already-mounted external block devices at startup."""
        logger.info("起動時スキャン: マウント済みデバイスを確認中...")
        mounted = {p.device: Path(p.mountpoint) for p in psutil.disk_partitions(all=True)}

        for device in self._context.list_devices(subsystem="block", DEVTYPE="partition"):
            devpath = device.device_node
            if not devpath or devpath not in mounted:
                continue

            mount_point = mounted[devpath]
            if is_system_mount_point(str(mount_point)):
                continue

            devname = Path(devpath).name
            base_devname = devpath.rstrip("0123456789")
            base_devname = Path(base_devname).name

            if not is_external_device(base_devname):
                continue

            uuid = get_device_uuid(devpath, retries=1)
            label = get_device_label(devpath)
            display_name = label if label else (f"dev_{uuid[:8]}" if uuid else devname)

            logger.info(f"起動時スキャン: 既存デバイス {display_name} ({devpath}) → {mount_point}")
            self._on_device_added(
                devpath=devpath,
                mount_point=mount_point,
                uuid=uuid,
                label=label,
                display_name=display_name,
            )
