#!/usr/bin/env python3
"""Find Candela lamps, blink each one so you can tell them apart, and write config.json.

Both lamps advertise the same name, so the only way to work out which physical
lamp is which address is to make one of them blink.

    python discover.py            # scan, blink each, name them, write config
    python discover.py --list     # just scan and print
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

from bleak import BleakScanner

from candela import CandelaLamp, looks_like_candela

CONFIG = Path(__file__).with_name("config.json")
SCAN_SECONDS = 12.0


def _random_pincode() -> str:
    """HomeKit setup code, in the required NNN-NN-NNN form."""
    digits = "".join(str(secrets.randbelow(10)) for _ in range(8))
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


async def scan() -> list[tuple[str, str, int]]:
    found: dict[str, tuple[str, int]] = {}

    def on_detect(device, adv) -> None:
        name = adv.local_name or device.name or ""
        if looks_like_candela(name):
            found[device.address] = (name, adv.rssi)

    print(f"Scanning {SCAN_SECONDS:.0f}s for Candela lamps...")
    async with BleakScanner(detection_callback=on_detect):
        await asyncio.sleep(SCAN_SECONDS)

    return sorted(
        ((addr, n, r) for addr, (n, r) in found.items()),
        key=lambda t: -t[2],
    )


async def main() -> None:
    lamps = await scan()
    if not lamps:
        sys.exit(
            "No lamps found.\n"
            "  - Make sure each lamp is powered on (press the top).\n"
            "  - Quit the Yeelight app: a lamp connected to the phone stops\n"
            "    advertising and becomes invisible to everything else."
        )

    print(f"\nFound {len(lamps)} lamp(s):")
    for addr, name, rssi in lamps:
        print(f"  {name:<14} {addr}  rssi={rssi}")

    if "--list" in sys.argv:
        return

    entries = []
    for i, (addr, _name, _rssi) in enumerate(lamps, 1):
        print(f"\n--- lamp {i} of {len(lamps)} ({addr}) ---")
        lamp = CandelaLamp(f"lamp{i}", addr)
        print("Blinking it 3 times, watch your lamps...")
        try:
            await lamp.blink()
        except Exception as exc:  # noqa: BLE001
            print(f"  could not blink it: {exc}")
        finally:
            await lamp._disconnect()

        label = input("Name for the lamp that just blinked [Candela {}]: ".format(i))
        entries.append({"name": label.strip() or f"Candela {i}", "address": addr})

    # Rediscovery must only replace the lamp list. Everything else is settings
    # the user chose -- and changing the pincode would break an existing pairing.
    config = {"pincode": _random_pincode(), "port": 51826, "idle_disconnect": 0}
    if CONFIG.exists():
        config.update(json.loads(CONFIG.read_text()))
    config["lamps"] = entries

    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nWrote {CONFIG}:")
    print(json.dumps(config, indent=2))


asyncio.run(main())
