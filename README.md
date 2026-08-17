# candela-homekit

A HomeKit bridge for the Yeelight Candela ambience lamp (model YLFW01YL). The
Candela communicates only over Bluetooth Low Energy and has no HomeKit
support from the manufacturer. This project runs a bridge process on a Mac:
HomeKit communicates with the bridge over the local network, and the bridge
communicates with each lamp over BLE. Each lamp is exposed to HomeKit as a
standard dimmable light, usable from the Home app, Siri, scenes, and
automations without any additional hub.

## Requirements

- A Mac that remains powered on and awake, within Bluetooth range of the
  lamps (roughly 10 meters, less through walls). If the host sleeps,
  Bluetooth is unavailable and the lamps stop responding to HomeKit until it
  wakes. On a desktop Mac running on AC power, system sleep can be disabled
  entirely (System Settings → Energy, or `pmset`), which removes this
  constraint.
- Python 3.9 or later.
- One or more Yeelight Candela (YLFW01YL) lamps, powered on.
- The Yeelight mobile app must not be connected to the lamp during setup or
  operation. A lamp connected to the app stops advertising over BLE and
  becomes invisible to this bridge.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Setup

### 1. Discover and identify the lamps

All Candela units advertise the same BLE name, so the physical lamp
corresponding to each address must be established by observation:

```bash
.venv/bin/python discover.py
```

This scans for lamps, connects to each in turn, and blinks it three times so
it can be visually identified. It then prompts for a name for each lamp and
writes the result to `config.json`, along with a randomly generated HomeKit
setup code.

If the Mac has more than one active network interface (for example, a VPN in
addition to the LAN), add a `bind_address` field to `config.json` with the
LAN IP address. Without it, the bridge advertises itself over every
interface and logs errors for the ones with no route to a HomeKit client.

### 2. Run the bridge and pair

```bash
.venv/bin/python bridge.py
```

The process prints a HomeKit setup code and a QR code. In the Home app:
**Add Accessory → More options… → Candela Bridge**, then enter the code. All
configured lamps are added together as a single bridge accessory.

### 3. Install as a background service

Once pairing succeeds, install the bridge as a `launchd` LaunchAgent so it
starts automatically at login and restarts if it exits:

```bash
./service.sh install
```

## Service management

```bash
./service.sh install     # load the LaunchAgent and start it at login
./service.sh uninstall   # stop it and remove the LaunchAgent
./service.sh stop        # stop the bridge, releasing its BLE connections
./service.sh start       # start it again
./service.sh restart     # restart the running instance
./service.sh status      # report whether it is loaded and running
./service.sh log         # follow the log file
```

The Candela accepts only one BLE connection at a time. `stop` releases the
bridge's connection to a lamp if another client, such as the Yeelight mobile
application, needs to reach it.

## Protocol

The lamp exposes one writable GATT characteristic
(`aa7d3f34-2d4f-41e0-807f-52fbf8cf7443`). Commands are 18-byte frames
consisting of a fixed leading byte, an opcode, an argument, and zero padding:

| Frame        | Effect                        |
| ------------ | ------------------------------ |
| `43 40 01`   | Power on                       |
| `43 40 02`   | Power off                      |
| `43 42 NN`   | Set brightness, NN = 1–100     |

The lamp also advertises a notify characteristic
(`8f65073d-9f57-4aaa-afea-397d19d5bbeb`), intended to report state changes
back to a client. This project cannot use it; see Limitations.

`candela.py` maintains one persistent connection per lamp. Rather than
issuing a BLE write for every HomeKit callback, it records a desired state
and reconciles it onto the lamp from a background task, because HomeKit
issues `On` and `Brightness` as separate, rapid updates while a brightness
slider is dragged. Writes to the lamp are spaced at least 0.35 seconds apart:
the lamp fades between brightness levels and abandons the fade if a second
command arrives mid-transition.

## Findings

### Connection lifetime

Independent of the traffic sent on the connection, the lamp terminates every
BLE connection after approximately 34 seconds. This was established by
running three trials against a single lamp: idle after connecting, idle
after sending the protocol's pairing command (opcode `0x67`), and idle with
a status request sent every 20 seconds as a keepalive. All three were
terminated by the lamp at the same 34-second mark, including the trial in
which a write was sent 14 seconds before the disconnect. This rules out both
an idle timeout and an incomplete pairing handshake as the cause; the limit
appears to be a fixed value enforced by the lamp's firmware.

The bridge treats this as an expected condition rather than a fault. Each
lamp's connection is monitored, and a lost connection is reestablished
automatically, typically within one to two seconds. In practice this
introduces at most brief added latency to a command that happens to arrive
during a reconnect, and does not require any action from the user.

### BLE mesh advertisement and candle-flicker mode

The lamp's advertised BLE name (`yeelight_ms`) and behavior confirm it
participates in a BLE mesh grouping in addition to responding to direct,
per-device GATT writes. Two lamps within radio range of each other were
observed to synchronize the candle-flicker (fire) effect: setting one lamp's
shade into flicker mode by hand caused a nearby second lamp to begin
flickering as well.

This synchronization occurs over the mesh layer and does not involve, and is
not affected by, the direct GATT control characteristic this project uses
for power and brightness. Power, off, and brightness commands sent to one
lamp's address were confirmed to affect only that lamp, independent of
whatever effect its neighbor was displaying.

Candle-flicker mode itself has no known BLE trigger. It is activated only by
a physical gesture on the lamp — rotating the shade clockwise and then
briefly back — a feature that was never exposed in the official mobile
application and therefore was never available to capture from BLE traffic
during earlier reverse-engineering of this protocol. Direct testing against
this lamp confirmed that flicker mode does not survive a power cycle issued
over BLE, and that none of the four Candela-specific opcodes named but
undocumented in the existing protocol notes (`0x4c`, `0xa2`, `0xa3`, `0xa4`)
produce any visible effect when sent individually. On the evidence
available, candle-flicker mode cannot be triggered or preserved through
software, and is not exposed by this bridge.

## Limitations

**Lamp state is not readable.** The notify characteristic that would report
power and brightness back to a client has no Client Characteristic
Configuration descriptor in this lamp's GATT table, meaning there is no
mechanism through which a client can subscribe to it. The BlueZ Bluetooth
stack on Linux is able to write to the underlying descriptor handle directly
regardless of its absence from the declared GATT table, which is why
Linux-based tools can read state from this lamp; CoreBluetooth on macOS
enforces the declared table and refuses the subscription outright. As a
consequence, HomeKit displays the state it last set and cannot detect
changes made by hand, such as the lamp being switched at the physical
button. The next command issued through HomeKit resynchronizes it.

**Only one BLE connection is permitted at a time.** While the bridge holds a
lamp's connection, no other client, including the Yeelight mobile
application, can connect to it. Set `idle_disconnect` in `config.json` to a
number of seconds to have the bridge release an inactive connection after
that period; the default, `0`, holds the connection continuously for lowest
latency.

## Files

| File                                     | Purpose                                          |
| ----------------------------------------- | ------------------------------------------------ |
| `candela.py`                              | BLE control, connection management, reconnection |
| `bridge.py`                               | The HomeKit bridge                                |
| `discover.py`                             | Locate, identify, and name lamps; write config    |
| `service.sh`                              | LaunchAgent install, start, stop, and log access  |
| `com.louiekotler.candela-homekit.plist`   | LaunchAgent definition used by `service.sh`       |

## Troubleshooting

**No lamps found during discovery.** Confirm each lamp is powered on and
that the Yeelight mobile application is fully closed; a lamp connected to
the app does not advertise over BLE.

**Nothing appears in the Home app after pairing.** The iOS device and the
Mac must be on the same subnet. If `bind_address` is set in `config.json`,
confirm it matches the interface on that subnet. Check that the macOS
firewall permits incoming connections for the Python interpreter running the
bridge.

**Works when run manually but not as a service.** macOS grants Bluetooth
access per executable. Under System Settings → Privacy & Security →
Bluetooth, confirm the interpreter in `.venv/bin` is listed and permitted. A
LaunchAgent that has never been granted access scans indefinitely without
finding any device.

## Related work

- [rytilahti/python-yeelightbt](https://github.com/rytilahti/python-yeelightbt) — protocol structures and opcode table referenced above
- [hcoohb/hass-yeelightbt](https://github.com/hcoohb/hass-yeelightbt) — Home Assistant integration using bleak
- [praschak/candelapy](https://github.com/praschak/candelapy) — the minimal Candela on/off/brightness write sequence
- [Marcocanc/mi-lamp-re](https://github.com/Marcocanc/mi-lamp-re) — original bedside-lamp protocol notes
