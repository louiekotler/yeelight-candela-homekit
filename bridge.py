#!/usr/bin/env python3
"""A HomeKit bridge that exposes Yeelight Candela lamps as dimmable bulbs.

Runs on the Mac, talks HomeKit over the LAN and BLE to the lamps. Pair it once
from the Home app and the lamps behave like any other HomeKit light: Siri,
scenes, automations, the lot.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
from pathlib import Path

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB

from candela import CandelaLamp

HERE = Path(__file__).parent
CONFIG = HERE / "config.json"
PERSIST = HERE / "accessory.state"

_LOGGER = logging.getLogger("candela.bridge")


class CandelaAccessory(Accessory):
    """One lamp as a HomeKit Lightbulb (On + Brightness)."""

    category = CATEGORY_LIGHTBULB

    def __init__(self, driver: AccessoryDriver, lamp: CandelaLamp) -> None:
        super().__init__(driver, lamp.name)
        self.lamp = lamp

        self.set_info_service(
            manufacturer="Yeelight",
            model="Candela YLFW01YL",
            serial_number=lamp.address,
            firmware_revision="1.0",
        )

        bulb = self.add_preload_service("Lightbulb", chars=["On", "Brightness"])
        self._char_on = bulb.configure_char(
            "On", value=lamp.power, setter_callback=self._set_on
        )
        self._char_brightness = bulb.configure_char(
            "Brightness", value=lamp.brightness, setter_callback=self._set_brightness
        )

        identify = self.get_service("AccessoryInformation").get_characteristic(
            "Identify"
        )
        identify.setter_callback = self._identify

    # These run on the HAP event loop and must not block: they only record the
    # desired state, which the lamp's own worker reconciles over BLE.
    def _set_on(self, value: int) -> None:
        _LOGGER.info("%s: HomeKit set On=%s", self.lamp.name, bool(value))
        self.lamp.request(power=bool(value))

    def _set_brightness(self, value: int) -> None:
        _LOGGER.info("%s: HomeKit set Brightness=%s", self.lamp.name, value)
        self.lamp.request(brightness=int(value))

    def _identify(self, _value: int) -> None:
        self.driver.async_add_job(self.lamp.blink())

    async def run(self) -> None:
        await self.lamp.start()

    async def stop(self) -> None:
        await self.lamp.stop()


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"No {CONFIG}. Run: python discover.py")
    config = json.loads(CONFIG.read_text())
    if not config.get("lamps"):
        sys.exit(f"{CONFIG} lists no lamps. Run: python discover.py")
    return config


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if "-v" in sys.argv else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # bleak is chatty at DEBUG and drowns out everything else.
    logging.getLogger("bleak").setLevel(logging.INFO)

    config = load_config()

    # On a machine with more than one network interface (VPNs, virtual
    # adapters), zeroconf otherwise advertises on all of them and logs errors
    # for the ones with no route.
    bind = config.get("bind_address") or None
    driver = AccessoryDriver(
        address=bind,
        port=config.get("port", 51826),
        persist_file=str(PERSIST),
        pincode=config["pincode"].encode(),
        interface_choice=[bind] if bind else None,
    )

    bridge = Bridge(driver, "Candela Bridge")
    bridge.set_info_service(
        manufacturer="louiekotler",
        model="Candela BLE Bridge",
        serial_number="candela-bridge-1",
        firmware_revision="1.0",
    )

    idle = float(config.get("idle_disconnect", 0))
    for entry in config["lamps"]:
        lamp = CandelaLamp(entry["name"], entry["address"], idle_disconnect=idle)
        bridge.add_accessory(CandelaAccessory(driver, lamp))
        _LOGGER.info("Added %s (%s)", entry["name"], entry["address"])

    driver.add_accessory(accessory=bridge)
    signal.signal(signal.SIGTERM, driver.signal_handler)
    signal.signal(signal.SIGINT, driver.signal_handler)

    _LOGGER.info("HomeKit setup code: %s", config["pincode"])
    driver.start()


if __name__ == "__main__":
    main()
