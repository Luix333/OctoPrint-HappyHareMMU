# Happy Hare MMU for OctoPrint

Live view, control and print-safe error handling for a [Happy Hare](https://github.com/moggieuk/Happy-Hare)
MMU on Klipper — for people who run **OctoPrint instead of Mainsail/Fluidd**, and therefore have no
MMU interface at all.

Every existing Happy Hare UI (Mainsail, Fluidd, KlipperScreen) talks to Moonraker. This plugin talks
to Klipper's own API socket, so it needs nothing installed on the printer beyond Happy Hare itself.

> **Status: alpha.** The logic is covered by 45 unit tests, but it has not yet run a print on real
> hardware. Treat the first run as a test: watch it, and keep the printer within reach.

---

## Why

Out of the box, an MMU on OctoPrint is not just invisible — it is actively worse than no MMU:

| What happens today | What this plugin does |
|---|---|
| **An MMU error cancels the print.** Happy Hare logs `!! MMU issue: …` *before* it pauses. OctoPrint treats any `!!` line as a firmware error and, with the settings Klipper's own docs recommend, cancels the job. | Intercepts Happy Hare's own errors in the `gcode.error` hook, so Happy Hare pauses and you can recover. Klipper shutdowns, heater faults, MCU errors and ADC errors are never intercepted. |
| **The pause dialog never appears.** Happy Hare raises a Mainsail-style `action:prompt_*` dialog; OctoPrint's bundled prompt support is gated on a `PROMPT_SUPPORT` capability Klipper never reports, ignores `prompt_text`, and answers with `M876`, which Klipper does not implement. | Renders the dialog itself. `RESUME` and `CANCEL_PRINT` buttons are routed through OctoPrint so the job state stays in sync. |
| **Cancelling from OctoPrint leaves the MMU out of step**, because OctoPrint never sends `CANCEL_PRINT`, so `MMU_PRINT_END` never runs. | Appends `MMU_PRINT_END STATE=cancelled` to the *after print cancelled* script. |
| **Slicer data never reaches the MMU**, because Happy Hare's placeholder substitution lives in a Moonraker component. | Does the same substitution when a file is uploaded, and remembers what it found for the pre-flight check. |

## What you get

- **Selector rail / lane view** — every gate with its filament colour, material, status ring, mapped
  tools and EndlessSpool group; the carriage animates to the selected gate. Gates are drawn at their
  calibrated selector offsets when Happy Hare has them.
- **Filament path** — gate → encoder → bowden (with live progress) → extruder → toolhead sensor →
  nozzle, filled to Happy Hare's `filament_pos`, with live sensor states.
- **Controls** — tool change, select, load, unload, eject, preload, check gates, home, motors off,
  bypass. Anything that moves filament is blocked while printing and offered while paused.
- **Gate map, tool-to-gate map and EndlessSpool groups**, editable from the table.
- **Recovery wizard** when the MMU pauses: unlock → tell Happy Hare where the filament is → resume.
- **Pre-flight check** comparing a file's slicer colours and materials against what is actually
  loaded, before you start the print.
- **Health** — per-gate reliability from Happy Hare's own statistics, maintenance counters
  (servo, cutter blade) against their limits, and where swap time goes.
- **Sidebar panel, navbar chip and filtered Happy Hare console.**

It adapts to the hardware Happy Hare reports: a linear selector (ERCF, Tradrack) draws a rail with a
carriage, a virtual selector (Box Turtle, Angry Beaver, Night Owl, 3MS, QuattroBox …) draws lanes.

## Requirements

- OctoPrint 1.11 or 2.0, Python 3.7+
- Klipper started with its API socket: `-a /home/pi/printer_data/comms/klippy.sock`
  (KIAUH installs do this; Klipper's own `install-octopi.sh` does not).
  OctoPrint must run as a user that can read and write that socket — normally it already does,
  since both run as the same user on the same Pi.
- Happy Hare v3.4x or v4.x
- A printer profile with at least as many extruders as you have gates, with *shared nozzle* ticked —
  otherwise OctoPrint suppresses `T1`…`Tn` before Klipper ever sees them.

Without the socket (Klipper on another host), the plugin still renders prompts, protects prints and
reads the console, and can take state over a serial fallback channel — see `CONCEPT.md`.

## Install

While the repository is private, install it from a local clone:

```bash
git clone https://github.com/Luix333/OctoPrint-HappyHareMMU.git
~/oprint/bin/pip install ./OctoPrint-HappyHareMMU
sudo service octoprint restart
```

Once it is public, the usual one-liner works from OctoPrint's *Plugin Manager → Get More → from URL*:

```
https://github.com/Luix333/OctoPrint-HappyHareMMU/archive/main.zip
```

## After installing

1. Open **Settings → Happy Hare MMU** and check the connection line reads *connected*.
2. Leave **Let Happy Hare handle MMU errors** on. This is the setting that stops an MMU fault from
   cancelling your print.
3. For the pre-flight check and correct purge volumes, your slicer's start G-code has to hand the
   placeholders to Happy Hare:

```gcode
MMU_START_SETUP INITIAL_TOOL={initial_tool} REFERENCED_TOOLS=!referenced_tools! TOOL_COLORS=!colors! TOOL_TEMPS=!temperatures! TOOL_MATERIALS=!materials! FILAMENT_NAMES=!filament_names! PURGE_VOLUMES=!purge_volumes! TOTAL_TOOLCHANGES=!total_toolchanges!
```

   The plugin fills those in at upload time. Supported slicers: PrusaSlicer, SuperSlicer, OrcaSlicer,
   BambuStudio.

## Interface density and themes

The panel takes its colours from the host theme's CSS variables (`--accent`, `--grey2`…, as published
by UI Customizer's themes) and falls back to its own palette on stock OctoPrint. It switches to a
compact layout automatically when `body.UICResponsiveMode` is present or its own container is narrow —
container width, not viewport width, because UI Customizer's fluid layout changes the former.
`Settings → Density` forces either mode.

## Development

```bash
python -m unittest discover -s tests     # 45 tests, no OctoPrint needed
```

- `octoprint_happyhare/klippy.py` — Klipper API socket client (framing, handshake, reconnect)
- `octoprint_happyhare/model.py` — normalises Happy Hare v3 and v4 status into one model
- `octoprint_happyhare/prompts.py` — `action:prompt_*` dialog parser
- `octoprint_happyhare/preprocessor.py` — slicer placeholder substitution
- `octoprint_happyhare/__init__.py` — the plugin: hooks, API, safety gating

Those four modules are deliberately free of OctoPrint imports so they can be tested directly.

`CONCEPT.md` is the design document, including everything that was verified against a real Happy Hare
install while writing this. `mockup/index.html` is a self-contained, clickable mockup of the interface
running on simulated state — useful for working on the layout without a printer.

## Credits and licence

- [Happy Hare](https://github.com/moggieuk/Happy-Hare) by moggieuk — `preprocessor.py` is a port of the
  parsing in its Moonraker component (GPLv3).
- Licensed under the [AGPL-3.0](LICENSE), like OctoPrint itself.
