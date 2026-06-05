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
    is_system_mount_point,
)

logger = logging.getLogger(__name__)

# Accept whole-disk (e.g. /dev/sdc) and partition (e.g. /dev/sdc1) devices.
# Portable SSDs formatted without a partition table appear as DEVTYPE=disk.
_ACCEPTED_DEVTYPES = {"disk", "partition"}


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
        # Filter by subsystem only; DEVTYPE filtering is done in the handler
        # because portable SSDs without partition tables appear as DEVTYPE=disk.
        self._monitor.filter_by(subsystem="block")
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
        props = device.properties

        if props.get("DEVTYPE") not in _ACCEPTED_DEVTYPES:
            return

        # Must carry filesystem metadata to be a data device
        if not props.get("ID_FS_UUID") and not props.get("ID_FS_LABEL"):
            logger.debug(f"{device.device_node}: ファイルシステム情報なし、スキップ")
            return

        if not self._is_external_udev_device(device):
            logger.debug(f"{device.device_node}: 外付けデバイスではないためスキップ")
            return

        devpath = device.device_node
        if not devpath:
            return

        mount_point = get_mount_point(devpath)
        if mount_point is None:
            logger.warning(f"{devpath} のマウントポイント取得がタイムアウトしました。スキップします。")
            return

        if is_system_mount_point(str(mount_point)):
            logger.debug(f"{mount_point} はシステムマウントポイントのためスキップ")
            return

        # Prefer udev properties (already resolved by udevd, no blkid subprocess needed)
        uuid = props.get("ID_FS_UUID") or get_device_uuid(devpath)
        label = props.get("ID_FS_LABEL") or get_device_label(devpath)
        devname = Path(devpath).name
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

    def _is_external_udev_device(self, device: pyudev.Device) -> bool:
        """Return True if this udev device is an external (USB/Firewire) drive.

        USB-to-SATA adapters report ID_BUS=ata but their sys_path and ID_PATH
        both contain 'usb', which is the reliable check for this hardware.
        """
        # sys_path is the canonical /sys/devices/... path; contains 'usb' for USB-attached
        sys_path = device.sys_path or ""
        if "usb" in sys_path or "ieee1394" in sys_path:
            return True

        # ID_PATH set by udevd; also contains 'usb' for USB-SATA adapters
        id_path = device.properties.get("ID_PATH", "")
        if "usb" in id_path or "ieee1394" in id_path:
            return True

        # Fallback: /sys/block/<dev>/removable flag
        devname = Path(device.device_node or "").name.rstrip("0123456789")
        removable = Path(f"/sys/block/{devname}/removable")
        try:
            if removable.read_text().strip() == "1":
                return True
        except OSError:
            pass

        return False

    def _scan_existing_devices(self):
        """Scan already-mounted external block devices at startup."""
        logger.info("起動時スキャン: マウント済みデバイスを確認中...")
        mounted = {p.device: Path(p.mountpoint) for p in psutil.disk_partitions(all=True)}

        for devtype in _ACCEPTED_DEVTYPES:
            for device in self._context.list_devices(subsystem="block", DEVTYPE=devtype):
                devpath = device.device_node
                if not devpath or devpath not in mounted:
                    continue

                props = device.properties
                if not props.get("ID_FS_UUID") and not props.get("ID_FS_LABEL"):
                    continue

                if not self._is_external_udev_device(device):
                    continue

                mount_point = mounted[devpath]
                if is_system_mount_point(str(mount_point)):
                    continue

                uuid = props.get("ID_FS_UUID") or get_device_uuid(devpath, retries=1)
                label = props.get("ID_FS_LABEL") or get_device_label(devpath)
                devname = Path(devpath).name
                display_name = label if label else (f"dev_{uuid[:8]}" if uuid else devname)

                logger.info(f"起動時スキャン: 既存デバイス {display_name} ({devpath}) → {mount_point}")
                self._on_device_added(
                    devpath=devpath,
                    mount_point=mount_point,
                    uuid=uuid,
                    label=label,
                    display_name=display_name,
                )
