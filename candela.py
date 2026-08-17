"""BLE control for the Yeelight Candela ambience lamp (YLFW01YL).

Protocol
--------
The lamp exposes a single writable control characteristic. Every command is an
18-byte frame: a 0x43 magic byte, an opcode, its argument, then zero padding.

    43 40 01   power on
    43 40 02   power off
    43 42 NN   brightness, NN = 1..100

The lamp also advertises a "notify" characteristic, but its firmware never
exposes a Client Characteristic Configuration descriptor for it. BlueZ gets
away with writing the CCCD handle blindly; CoreBluetooth refuses, so on macOS
there is no way to subscribe and no way to read state back. Control is
therefore one-way and the state here is optimistic -- see README.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

from bleak import BleakClient, BleakError, BleakScanner
from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

CONTROL_UUID = "aa7d3f34-2d4f-41e0-807f-52fbf8cf7443"

_MAGIC = 0x43
_CMD_POWER = 0x40
_CMD_BRIGHTNESS = 0x42
_POWER_ON = 0x01
_POWER_OFF = 0x02

_FRAME_ON = struct.pack("BBB15x", _MAGIC, _CMD_POWER, _POWER_ON)
_FRAME_OFF = struct.pack("BBB15x", _MAGIC, _CMD_POWER, _POWER_OFF)

SCAN_TIMEOUT = 20.0
CONNECT_TIMEOUT = 25.0
#: The lamp fades between brightness levels and aborts the fade if a second
#: command lands mid-transition, so writes are spaced out.
MIN_COMMAND_GAP = 0.35
DEBOUNCE = 0.3
BACKOFF_MAX = 60.0
WRITE_TIMEOUT = 10.0

#: Advertised/GAP names these lamps are known to use.
NAME_PREFIXES = ("yeelight_ms", "yl_candela")


def _frame_brightness(pct: int) -> bytes:
    return struct.pack("BBB15x", _MAGIC, _CMD_BRIGHTNESS, max(1, min(100, int(pct))))


def looks_like_candela(name: Optional[str]) -> bool:
    return bool(name) and name.lower().startswith(NAME_PREFIXES)


class CandelaLamp:
    """One lamp, with a persistent BLE connection and coalesced writes.

    HomeKit fires On and Brightness as separate rapid updates while a slider
    moves, so callers only ever record a *desired* state; a background worker
    reconciles it onto the lamp.
    """

    def __init__(
        self,
        name: str,
        address: str,
        *,
        idle_disconnect: float = 0.0,
    ) -> None:
        self.name = name
        self.address = address
        #: Seconds of inactivity before releasing the connection so that the
        #: phone app can talk to the lamp. 0 keeps it held (most responsive).
        self.idle_disconnect = idle_disconnect

        self._client: Optional[BleakClient] = None
        self._device: Optional[BLEDevice] = None
        self._conn_lock = asyncio.Lock()
        self._last_write = 0.0

        self._target_power = False
        self._target_brightness = 100
        self._applied_power: Optional[bool] = None
        self._applied_brightness: Optional[int] = None

        self._dirty = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._stopping = False
        self._last_activity = 0.0

    # ---------------------------------------------------------------- state

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def power(self) -> bool:
        return self._target_power

    @property
    def brightness(self) -> int:
        return self._target_brightness

    def request(
        self,
        *,
        power: Optional[bool] = None,
        brightness: Optional[int] = None,
    ) -> None:
        """Record desired state and wake the worker. Safe to call from the
        HAP event loop -- it never blocks and never touches Bluetooth."""
        if power is not None:
            self._target_power = bool(power)
        if brightness is not None:
            brightness = max(0, min(100, int(brightness)))
            if brightness == 0:
                # HomeKit can send 0 rather than On=false.
                self._target_power = False
            else:
                self._target_brightness = brightness
        self._dirty.set()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._stopping = False
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()
        self._worker = loop.create_task(self._run_worker(), name=f"candela-{self.name}")
        self._watchdog = loop.create_task(
            self._run_watchdog(), name=f"candela-wd-{self.name}"
        )
        _LOGGER.info("%s: started (address=%s)", self.name, self.address)

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._worker, self._watchdog):
            if task is not None:
                task.cancel()
        for task in (self._worker, self._watchdog):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._worker = self._watchdog = None
        await self._disconnect()
        _LOGGER.info("%s: stopped", self.name)

    # --------------------------------------------------------------- worker

    async def _run_worker(self) -> None:
        backoff = 1.0
        while not self._stopping:
            await self._dirty.wait()
            # Absorb bursts: keep resetting the timer while updates arrive.
            while True:
                self._dirty.clear()
                await asyncio.sleep(DEBOUNCE)
                if not self._dirty.is_set():
                    break

            power, brightness = self._target_power, self._target_brightness
            try:
                await self._apply(power, brightness)
                backoff = 1.0
            except (BleakError, asyncio.TimeoutError, OSError) as exc:
                _LOGGER.warning("%s: apply failed (%s); retrying in %.0fs",
                                self.name, exc, backoff)
                await self._disconnect()
                self._dirty.set()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _apply(self, power: bool, brightness: int) -> None:
        if power != self._applied_power:
            await self._write(_FRAME_ON if power else _FRAME_OFF)
            self._applied_power = power
            if power:
                # The lamp restores its own last level on power-up, so the
                # brightness we think is applied is no longer trustworthy.
                self._applied_brightness = None
        if power and brightness != self._applied_brightness:
            await self._write(_frame_brightness(brightness))
            self._applied_brightness = brightness

    # ------------------------------------------------------------ watchdog

    async def _run_watchdog(self) -> None:
        """Keep the link warm so the first HomeKit command isn't slow, and
        drop it once idle if the user asked for that."""
        backoff = 1.0
        while not self._stopping:
            await asyncio.sleep(5.0)
            loop = asyncio.get_running_loop()
            idle = loop.time() - self._last_activity

            if self.idle_disconnect and idle > self.idle_disconnect:
                if self.is_connected:
                    _LOGGER.debug("%s: idle %.0fs, releasing link", self.name, idle)
                    await self._disconnect()
                continue

            if self.is_connected:
                backoff = 1.0
                continue
            try:
                await self._ensure_connected()
                backoff = 1.0
            except (BleakError, asyncio.TimeoutError, OSError) as exc:
                _LOGGER.debug("%s: reconnect failed (%s)", self.name, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    # ---------------------------------------------------------------- BLE

    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.info("%s: disconnected", self.name)
        self._client = None
        # Nothing on the lamp is known any more.
        self._applied_power = None
        self._applied_brightness = None

    async def _ensure_connected(self) -> None:
        if self.is_connected:
            return
        async with self._conn_lock:
            if self.is_connected:
                return
            device = self._device
            if device is None:
                _LOGGER.debug("%s: scanning for %s", self.name, self.address)
                device = await BleakScanner.find_device_by_address(
                    self.address, timeout=SCAN_TIMEOUT
                )
                if device is None:
                    raise BleakError(f"{self.name}: not found in scan")
                self._device = device

            client = BleakClient(
                device,
                timeout=CONNECT_TIMEOUT,
                disconnected_callback=self._on_disconnect,
            )
            try:
                await client.connect()
            except Exception:
                # A stale cached handle is the usual cause; force a fresh scan.
                self._device = None
                raise
            self._client = client
            _LOGGER.info("%s: connected", self.name)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        self._applied_power = None
        self._applied_brightness = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("%s: disconnect error (%s)", self.name, exc)

    async def _write(self, payload: bytes, *, _retry: bool = True) -> None:
        await self._ensure_connected()
        loop = asyncio.get_running_loop()

        gap = MIN_COMMAND_GAP - (loop.time() - self._last_write)
        if gap > 0:
            await asyncio.sleep(gap)

        assert self._client is not None
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(CONTROL_UUID, payload, response=True),
                timeout=WRITE_TIMEOUT,
            )
        except (BleakError, asyncio.TimeoutError, OSError):
            await self._disconnect()
            if not _retry:
                raise
            _LOGGER.debug("%s: write failed, reconnecting once", self.name)
            await self._write(payload, _retry=False)
            return

        self._last_write = loop.time()
        self._last_activity = loop.time()
        _LOGGER.debug("%s: wrote %s", self.name, payload[:3].hex(" "))

    # -------------------------------------------------------------- extras

    async def blink(self, times: int = 3) -> None:
        """Physically identify this lamp. Restores the desired state after."""
        for _ in range(times):
            await self._write(_FRAME_OFF)
            await asyncio.sleep(0.5)
            await self._write(_FRAME_ON)
            await asyncio.sleep(0.5)
        self._applied_power = None
        self._applied_brightness = None
        self._dirty.set()
