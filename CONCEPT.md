# Happy Hare for OctoPrint — plugin concept

**Plugin id:** `happyhare` · **Name:** Happy Hare MMU · **Targets:** OctoPrint 1.11 + 2.0, Python ≥ 3.9 · **Supports:** Happy Hare v3.4x and v4.x

A single OctoPrint plugin that gives a Happy Hare MMU the live view, the controls and the print-safety behaviour that Mainsail, Fluidd and KlipperScreen users already get — none of which exist for OctoPrint today, because every one of those UIs requires Moonraker.

---

## 1. Why this plugin exists

Verified read-only against the target machine (Voron V2.4, `10.147.17.100`, 2026-09-22):

| Fact | Consequence |
|---|---|
| OctoPrint 1.11.8 talks to Klipper over `~/printer_data/comms/klippy.serial` | No MMU status is available to OctoPrint at all — the MMU is invisible |
| Klipper runs with `-a /home/pi/printer_data/comms/klippy.sock` | A **full, structured status feed is already available** on the same Pi; nothing needs installing on the printer |
| No Moonraker (port 7125 dead) | Happy Hare's `mmu_server.py` never runs → no slicer pre-processing, no Spoolman bridge |
| `serial.disconnectOnErrors = False`, `serial.ignoreErrorsFromFirmware = False` | **Any Happy Hare `!!` error during a print cancels the print** |
| `plugins.action_command_prompt.enable = "detected"`, Klipper reports no `PROMPT_SUPPORT` | Happy Hare's error dialog (`show_error_dialog: 1`) is silently discarded |
| `scripts.gcode.afterPrintCancelled` is empty | Cancelling from OctoPrint skips `CANCEL_PRINT` → no `MMU_PRINT_END`, no unload, HH print state left stale |
| `PRINT_START` calls `MMU_PRINT_START` directly, not `MMU_START_SETUP` | Slicer colours / materials / temps / purge volumes never reach Happy Hare |
| Printer profile: 12 extruders, `sharedNozzle: true` | Works around `serial.sanityCheckTools` so `T0`–`T11` are not suppressed — keep this |

Hardware on that machine: **ERCF 2.0, 12 gates**, encoder gate homing, extruder + toolhead switches, selector servo, blobifier purge, gantry-servo tip cutting, EndlessSpool on, flowguard on, sync off, Spoolman off, Happy Hare **v3.42**.

### The four problems, ranked

1. **Print-killing errors.** `mmu_logger.log_error()` emits `!! <reason>` *before* Happy Hare parks and pauses. OctoPrint's `comm.py::_handle_errors` sees `!!`, and with the above settings calls `cancelPrint()`. The MMU's entire designed recovery flow (pause → fix → `MMU_UNLOCK` → `RESUME`) is unreachable from OctoPrint.
2. **No recovery UI.** Even when Happy Hare does pause, its `_MMU_ERROR_DIALOG` prompt (`action:prompt_begin … prompt_button UNLOCK|MMU_UNLOCK|secondary …`) is dropped, so the reason for the pause and the recovery buttons never appear.
3. **No visibility.** Tool, gate, filament position, gate map, sensors, encoder health, statistics — all present in Klipper's `printer.mmu` object, none of it shown.
4. **No slicer integration.** Tool→gate mapping, colour/material checks, purge volumes and automap all depend on data Happy Hare only receives through the Moonraker preprocessor.

---

## 2. Architecture

```
          Klipper (klippy.sock)                      OctoPrint serial (klippy.serial)
                   │  objects/subscribe                        │  // and !! lines
                   ▼                                           ▼
        ┌──────────────────────┐                   ┌──────────────────────────┐
        │  KlippyClient thread │                   │  gcode.received / action │
        │  reconnect + framing │                   │  gcode.error (swallow)   │
        └──────────┬───────────┘                   └────────────┬─────────────┘
                   └──────────────┬─────────────────────────────┘
                                  ▼
                      ┌───────────────────────┐   version adapter: v3.42 ⇄ v4.x
                      │   MmuState (model)    │   hardware adapter: vendor/selector
                      └───────────┬───────────┘
              send_plugin_message │ ≤4 Hz coalesced deltas      ┌──────────────────┐
                                  ▼                              │ file preprocessor│
                    Knockout view models (tab / sidebar /        │ + metadata store │
                     navbar / prompt modal / settings)           └──────────────────┘
                                  │ SimpleApi commands
                                  ▼
                    self._printer.commands("MMU_…")  → OctoPrint queue → Klipper
```

### 2.1 Transport ladder (auto-selected, overridable in settings)

1. **`klippy.sock`** — primary. Path default `~/printer_data/comms/klippy.sock`, then `/tmp/klippy_uds`; the running path can be read from the `Args:` line in `klippy.log`. Requires OctoPrint to run as a user that can access the socket (same Pi, same user on this machine).
2. **`moonraker.sock` / websocket** — used if Moonraker turns up later (same object model, no auth needed on the unix socket).
3. **Serial-only fallback** — for Klipper on another host. A config snippet points `_MMU_STATE_VARS`'s `user_action_changed_extension` / `user_print_state_changed_extension` / `user_mmu_event_extension` at a macro that emits
   `{action_respond_info("action:hh_state " ~ (printer.mmu|tojson))}`,
   caught by `octoprint.comm.protocol.action`. Klipper pins Jinja 2.11, so `|tojson` exists. Caveats: every line is echoed to OctoPrint's terminal and to `klippy.log`, Klipper cuts G-code lines at `;` or `#`, and pty writes are non-blocking and can be truncated — so this mode sends a **reduced field set**, not the whole status dict.

### 2.2 KlippyClient (modelled on Moonraker's `klippy_connection.py`)

- JSON messages framed by a single `0x03` byte; requests `{"id", "method", "params"}`; responses may arrive out of order.
- Startup: wait for the socket file → connect → send `info` every 0.25 s until `state != "startup"` → subscribe.
- Subscribe (one call; a second `objects/subscribe` on the same connection **replaces** the first):
  `mmu`, `mmu_machine`, `mmu_encoder mmu_encoder`, `print_stats`, `pause_resume`, `extruder`, `save_variables`, `webhooks`.
- One-shot `objects/query` of `configfile.settings.mmu` (+ `configfile.config.mmu_machine`) for static config: bowden length, `show_error_dialog`, `t_macro_color`, sensor set, vendor/version.
- Deltas are **per top-level field**, not deep: any change inside `gate_status` resends the whole list. Merge, don't patch.
- **Re-query if the first snapshot lacks the gate arrays** (`gate`, `gate_color`, `gate_status`, `filament_pos`, `ttg_map`) — a race KlipperScreen's Happy Hare edition hit in practice.
- Klipper's `RESTART`/`FIRMWARE_RESTART` closes the socket but *not* the pty: OctoPrint stays connected while this client must reconnect. Watch `webhooks.state` for `shutdown`/`error`.
- Never block: if a client stops reading, Klipper drops it after ~5 s.

### 2.3 Version + hardware adapter

The plugin normalises both Happy Hare generations into one internal model. Differences that matter:

| Concern | v3.42 (this machine) | v4.x (upstream since 2026-08-30) |
|---|---|---|
| Servo / grip / bypass | top level: `servo`, `grip`, `has_bypass` | `selector.{servo,grip,has_bypass}`; `mmu.has_bypass` is always `True` |
| Units | single unit, `mmu_machine` minimal | `mmu_machine.{happy_hare_version,num_units,unit_N{vendor,version,selector_type,first_gate,num_gates,…}}` |
| Sensor keys | `mmu_pre_gate`, `mmu_gear`, `mmu_gate`, `extruder`, `toolhead` | `mmu_entry`, `mmu_exit`, `mmu_shared_exit`, `encoder`, `extruder`, `toolhead` |
| Gate map | as below | adds `gate_vendor`, `gate_spool_rfid`, `drying_state`, `nfc` |
| Commands | `MMU_CALIBRATE_GATES`, `DETAIL=` | `MMU_CALIBRATE_GATE`, `DETAILS=`, `UNIT=` on many commands |

Shared model (the fields the UI binds to): `enabled, num_gates, is_homed, print_state, tool, gate, last_tool, next_tool, filament, filament_pos, filament_position, filament_direction, action, operation, sync_drive, reason_for_pause, bowden_progress, num_toolchanges, active_filament, ttg_map, endless_spool_groups, endless_spool_enabled, gate_status, gate_color, gate_color_rgb, gate_material, gate_filament_name, gate_temperature, gate_spool_id, gate_speed_override, slicer_tool_map, slicer_color_rgb, sensors{}, encoder{}, selector{}`.

Key enums the UI depends on:

- `filament_pos`: `-1` unknown, `0` unloaded/parked, `1` homed gate, `2` start bowden, `3` in bowden, `4` end bowden, `5` homed entry, `6` homed extruder, `7` extruder entry, `8` homed toolhead sensor, `9` in extruder, `10` loaded.
- `gate_status`: `-1` unknown, `0` empty, `1` available, `2` available from buffer.
- `tool`/`gate`: `-1` unknown, `-2` bypass.
- `action`: `Idle, Loading, Loading Ext, Unloading, Exiting Ext, Forming Tip, Heating, Checking, Homing, Selecting, Cutting Tip, Cutting Filament, Purging` (+ `Preload` in v4).
- `print_state`: `initialized, ready, started, printing, complete, cancelled, error, pause_locked, paused, standby, idle`.

Hardware layout comes from vendor + selector type: a **linear selector** (ERCF, Tradrack) draws a rail with a moving carriage; a **virtual selector** (Box Turtle, Angry Beaver, 3MS, QuattroBox, Night Owl…) draws independent lanes with no carriage; rotary/servo/indexed selectors draw a dial. Gate numbering is continuous across units in v4.

### 2.4 OctoPrint hooks

| Hook | Use |
|---|---|
| `octoprint.comm.protocol.gcode.error` | **Print protection.** Return `True` for Happy Hare's own errors so OctoPrint neither cancels nor disconnects and Happy Hare's pause/recovery runs. Match conservatively: only while an MMU operation/pause is in flight or the text matches known HH patterns; let Klipper shutdowns (`MCU 'mcu' shutdown`, `Lost communication`, `Heater … not heating`) through untouched. Setting: *Let Happy Hare handle MMU errors* (default on), with every swallowed line logged and shown in the plugin console. |
| `octoprint.comm.protocol.action` | Render Klipper/Mainsail `prompt_begin / prompt_text / prompt_button LABEL\|GCODE\|style / prompt_button_group_* / prompt_footer_button / prompt_show / prompt_end` as a real modal. Buttons whose G-code is `RESUME` / `CANCEL_PRINT` route to `self._printer.resume_print()` / `cancel_print()` so OctoPrint's streaming state stays in sync; everything else is sent as G-code. Also consumes `action:hh_state` in fallback mode, and the built-ins `paused`/`resumed`/`cancel`. |
| `octoprint.comm.protocol.scripts` | Append `MMU_PRINT_END STATE=cancelled` to `afterPrintCancelled` (option: send the user's `CANCEL_PRINT` instead); optional `MMU_PRINT_START` safety net on `beforePrintStarted` for configs that don't call it. |
| `octoprint.filemanager.preprocessor` | Port of Happy Hare's `mmu_server` gcode preprocessing (below). |
| `octoprint.comm.protocol.gcode.queuing` | Optional hygiene: strip the tool suffix from `M104 T<n>` / `M109 T<n>` (Klipper raises "Extruder not configured" → another `!!`). Never rewrites `T<n>` itself — Happy Hare's own `T<n>` macros handle tool changes, and rewriting would bypass OctoPrint's tool tracking. |
| `octoprint.comm.protocol.gcode.received` | Cheap parsing of Happy Hare console lines for the plugin console panel, stripping `<span style=…>` colour markup that `console_show_colored_text` can emit. |
| `octoprint.events.register_custom_events` | `tool_changed`, `mmu_paused`, `mmu_error`, `runout`, `endless_spool_swap`, `preflight_warning`, `gate_map_changed` → usable from OctoPrint's event/system-command config and by other plugins. |
| `octoprint.access.permissions` | `HAPPYHARE_VIEW`, `HAPPYHARE_CONTROL`, `HAPPYHARE_CALIBRATE` (admin by default). |

All comm hooks run on the communication thread: no I/O, no locks held, nothing slow.

### 2.5 Command path

Frontend → `SimpleApiPlugin.on_api_command` → whitelist builder → `self._printer.commands([...])`.

Sending through OctoPrint (rather than the socket's `gcode/script`) keeps commands in OctoPrint's terminal, its queue and its job state, and avoids a second, invisible command stream. Raw G-code is accepted only from the console panel, under `HAPPYHARE_CONTROL`.

Gating by `print_state` / `action`:

- **Always:** status, gate map edits, TTG/EndlessSpool edits (with a warning while printing), `MMU_STATUS`, `MMU_STATS`.
- **Not while printing:** `MMU_HOME`, `MMU_SELECT`, `MMU_LOAD/UNLOAD/EJECT`, `MMU_PRELOAD`, `MMU_CHECK_GATE`, `MMU_SERVO`, `MMU_MOTORS_OFF`, calibration (Happy Hare refuses most of these itself; the UI shouldn't offer them).
- **Only while paused/`pause_locked`:** `MMU_UNLOCK`, `MMU_RECOVER`, recovery `RESUME`.
- Confirmation dialogs for anything that moves filament while a job is loaded.

### 2.6 G-code preprocessing (replacing the Moonraker component)

Two passes (scan, then rewrite) into a temp file returned as a `DiskFileWrapper` — `LineProcessorStream` is single-pass and can't do this. Skips files already carrying `; processed by HappyHare`, and writes that fingerprint itself.

| Placeholder | Source comment(s) |
|---|---|
| `!referenced_tools!`, `!total_toolchanges!` | lines matching `((^MMU_CHANGE_TOOL(_STANDALONE)? .*?TOOL=)|(^T))(?P<tool>\d{1,2})` |
| `!colors!` | `extruder_colour` / `filament_colour` (PS/SS), `filament_colour` (Orca/Bambu) |
| `!temperatures!` | `(nozzle_)?temperature` |
| `!materials!` | `filament_type` |
| `!purge_volumes!` | `wiping_volumes_matrix` (PS/SS) or `flush_volumes_matrix` (Orca/Bambu), × `flush_multiplier` except Orca ≥ 2.3.2 |
| `!filament_names!` | `filament_settings_id` |

Optional `NEXT_POS` rewriting (`T<n>` → `MMU_CHANGE_TOOL TOOL=n NEXT_POS="x,y"` using the next `G0/G1` with X and Y) is **off by default** here, since it changes what OctoPrint streams; the placeholder substitution alone is what `MMU_START_SETUP` needs.

Everything parsed is also stored via `set_additional_metadata(location, path, "happyhare", {...})` so the file list and the pre-flight check can use it without re-reading the file. Source: Happy Hare `components/mmu_server.py` (GPLv3 — keep attribution; compatible with an AGPLv3 plugin).

Because `MMU_START_SETUP` must actually be called for any of this to matter, the plugin's wizard shows the exact slicer start-G-code line to paste:

```gcode
MMU_START_SETUP INITIAL_TOOL={initial_tool} REFERENCED_TOOLS=!referenced_tools! TOOL_COLORS=!colors! TOOL_TEMPS=!temperatures! TOOL_MATERIALS=!materials! FILAMENT_NAMES=!filament_names! PURGE_VOLUMES=!purge_volumes! TOTAL_TOOLCHANGES=!total_toolchanges!
```

---

## 3. The UI

### Navbar
Active tool chip (colour from `gate_color_rgb`, label `T2→G2`), action text while busy, red pulse when `print_state` is `pause_locked`.

### Sidebar panel
Tool/gate, filament state, action, homed/sync icons, encoder headroom bar, and two buttons that are always contextually right: *Unload* when idle, *Recover* when paused.

### Main tab — sub-tabs

**Operate**
- Status strip: print state, action, tool→gate, filament position name, bowden progress, encoder flow/headroom, sync, servo.
- **Selector rail** (SVG, adapts to selector type): gates at their real calibrated offsets, each a spool disc in its gate colour with material, tool badges, EndlessSpool group letter, and a status ring (available / from buffer / empty / unknown). The carriage sits under the selected gate and animates during `Selecting`; the bypass slot sits past the last gate. Click a gate → Select, Check, Preload, Eject, Edit.
- **Filament path** (SVG): gate → encoder → bowden (progress-filled, real length) → extruder entry sensor → extruder gears → toolhead sensor → nozzle. Filament is drawn in the active gate's colour and filled to `filament_pos`; sensor dots light from `sensors{}`; the direction arrow follows `filament_direction`; tip-forming/cutting/purging get their own state art.
- Tool buttons `T0…Tn` + bypass.

**Gates & TTG**
- Gate map table, inline-editable: name, material, colour, temperature, spool id, availability, speed override → `MMU_GATE_MAP GATE=n NAME= MATERIAL= COLOR= TEMP= SPOOLID= AVAILABLE= [SPEED= VENDOR=]`.
- TTG map editor (tool → gate) → `MMU_TTG_MAP MAP=…`; EndlessSpool groups (letters A, B, C…) → `MMU_ENDLESS_SPOOL GROUPS=… ENABLE=`.
- Reset buttons mirror `MMU_TTG_MAP RESET=1` / `MMU_ENDLESS_SPOOL RESET=1`.

**Pre-flight** (per file, from preprocessor metadata + live gate map)
- Tools the file uses, their slicer colour/material/temperature against the mapped gate's, with mismatch, empty-gate and unknown-gate warnings, plus "gate not calibrated" flags.
- One-click automap (`MMU_SLICER_TOOL_MAP AUTOMAP=material|closest_color|filament_name|spool_id`) or manual TTG fix, then re-check.
- Purge-volume matrix heatmap when the slicer provided one.
- Offered from the file list and before a print starts.

**Health**
- Per-gate quality bar with loads/unloads, failures and pauses (`save_variables.mmu_statistics_gate_N`).
- Maintenance counters with limits and warnings (`mmu_statistics_counters`, e.g. servo_down 1048/5000, cutter_blade 0/3000).
- Swap time breakdown (pre-unload / form tip / unload / pre-load / load / purge / post-load) and totals; pause count and time.
- Encoder calibration values and clog/flowguard headroom history.

**Console**
- Happy Hare lines only, colour markup stripped, `!!` lines flagged with a badge showing they were swallowed rather than allowed to cancel the print. Raw toggle; command entry under permission.

### Recovery wizard (while paused)
Shows `reason_for_pause`, the filament position Happy Hare believes it is in, and the ordered recovery: *Unlock* (`MMU_UNLOCK`, restores temperature) → *Tell Happy Hare where the filament is* (`MMU_RECOVER LOADED=0|1`, or `TOOL=`/`GATE=`) → *Resume* (OctoPrint resume, which runs `RESUME`) → *Cancel*. This is the flow that is currently unreachable on this machine.

### Settings & wizard
Transport + socket path (auto-detected, with a test button), error protection, prompt rendering, preprocessor, cancel hook, confirmation level, polling rate. First-run wizard: detect socket → detect Happy Hare version and hardware → check whether start G-code uses `MMU_START_SETUP` → offer the snippet → check printer-profile extruder count ≥ gates.

---

## 4. Roadmap

| Phase | Contents | Why this order |
|---|---|---|
| **P1 — See it, stop breaking prints** | KlippyClient + adapter, status strip, selector rail, filament path, sidebar, navbar, console, `gcode.error` protection, prompt modal, cancel → `MMU_PRINT_END` | The two safety fixes need no UI state, and the read-only view carries no risk |
| **P2 — Drive it** | Tool/gate/load/unload/home/check/preload actions with gating, gate map editor, TTG + EndlessSpool editors, recovery wizard | Everything here depends on P1's state model |
| **P3 — Know before you print** | File preprocessor, file-list swatches + metadata, pre-flight dialog, automap, slicer snippet wizard, Continuous Print queue pre-flight | Needs P2's map editing to be useful when a check fails |
| **P4 — Extras** | Spoolman bridge (register the `spoolman_*` remote methods on the socket, since no Moonraker does), LED mirror, v4 multi-unit layouts, type-B lane layouts, per-gate drying/NFC (v4) | Optional, hardware- or setup-specific |

---

## 5. Risks and open questions

- **Error filtering must be tight.** Swallowing too much hides a genuine Klipper shutdown. Mitigation: only filter while an MMU operation or pause is active, keep an explicit pattern list, log everything swallowed, and make the whole feature switchable.
- **Comm timeouts.** OctoPrint's serial timeout is 30 s and `T` is not in `serial.longRunningCommands`; a slow tool change that emits nothing could trip a timeout (5 while printing → disconnect). Happy Hare's progress messages normally prevent this; the wizard can suggest adding `T`.
- **Socket permissions.** OctoPrint must be able to open `klippy.sock`. Same user on this machine; other setups may need a group.
- **v4 migration.** Field names, sensor names and a few command parameters change. The adapter is written from both sources; a v4 machine is needed to verify.
- **OctoPrint 2.0.** The serial hooks moved into the bundled Serial Connector and blueprints are CSRF-protected by default; the Moonraker Connector fires only the `action` hook, which is why the fallback channel is worth keeping. Import comm bits with a fallback, as OctoKlipper does.
- **Other plugins.** OctoPrint-Spoolman tracks usage per tool index, which is wrong when TTG maps a tool to a different gate — worth a warning or a bridge in P4.

## 6. References

- Happy Hare: `moggieuk/Happy-Hare` — v3 branch (`extras/mmu/mmu.py`, `VERSION 3.42`), `main` (v4: `extras/mmu/mmu_controller.py`, `mmu_constants.py`, `mmu_gate_maps.py`), `components/mmu_server.py`; docs at `moggieuk.github.io/Happy-Hare-Doc`.
- Klipper: `docs/API_Server.md`, `klippy/webhooks.py`, `klippy/extras/respond.py`; Moonraker's `klippy_connection.py` as the reference client.
- OctoPrint: plugin hooks/mixins/viewmodels docs, `util/comm.py` (`_handle_errors`, `_validate_tool`), bundled `action_command_prompt`.
- Closest prior art: `jukebox42/Octoprint-PrusaMMU` (navbar indicator, tool remap modal, custom events, spool-plugin view-model injection). No OctoPrint plugin exists for Happy Hare, ERCF, Tradrack or Box Turtle/AFC.
- Local copies used while writing this: `B:\Downloads\Claude\VORON V2.4\Happy-Hare\` (commit eba8343a) and `printer_data\config\mmu\mmu_vars.cfg`.

## 7. Density and theme compatibility (UI Customizer)

This printer's OctoPrint is heavily customised by **UI Customizer 0.1.10.0**: theme `red-night`, `fluidLayout`, `responsiveMode`, `filesFullHeight`, `compressTempControls`, icon-only main tabs with per-tab colours, a user-ordered sidebar row layout, a navbar icon sort order, and a block of user `customCSS`. A plugin that assumes stock OctoPrint looks broken there, so the panel ships in two densities and takes its colours from the theme.

What UI Customizer actually exposes (read from the running instance and its assets):

- Body classes `UICResponsiveMode`, `UICfixedHeader`, `UICfixedFooter`, `UICPreviewON`; html classes `UICDefaultTheme` / `UICCustomTheme`.
- Its own root variables `--background` and `--uicmainwidth`.
- Themes are served locally at `/plugin/uicustomizer/theme/<name>.css` and define a full palette. `red-night` is: `--accent #d32f2f`, `--accent-darker #ab2424`, `--background #1e1e20`, `--background-darker #121213`, `--grey1 #19191b`, `--grey2 #242424`, `--grey3 #2d2d2f`, `--grey4 #3e3e3e`, `--red #f44336`, `--green #4caf50`, `--quiteWhite #f7f7f7`, `--quiteWhite-dark #dedede`, `--grey #999`.
- Layout config keyed by element id: sidebar widgets as `#sidebar_plugin_<id>_wrapper` (placed in rows), tabs as `#tab_plugin_<id>_link` (icon, colour, icon-only/text-only), navbar items by id in `topIconSort`.

Rules the plugin follows:

1. **Inherit the theme, don't restate it.** Every colour is `var(--accent, #2d6a9f)`, `var(--quiteWhite, #14202b)`, `var(--grey2, #ffffff)` and so on, so red-night, Discorded or stock all skin the panel for free. Set `color` explicitly on the panel root — `body`'s inherited colour is computed in the host's palette and will otherwise leak into a re-skinned subtree.
2. **Two densities, chosen automatically.** Compact when `body.UICResponsiveMode` is present or the panel's own container is under ~900px, watched with a `ResizeObserver` — `fluidLayout` changes the container width, not the viewport, so media queries are the wrong instrument. A settings override forces either density.
   Compact drops the status tiles to a single chip row, shortens the rail (no material captions, tooltips instead), slims the filament path to one label row, and tightens the tool grid, so the whole panel fits one screen on a tablet.
3. **Use the conventional ids** — `tab_plugin_happyhare`, `sidebar_plugin_happyhare`, `navbar_plugin_happyhare` — so UI Customizer's tab, row and icon-sort configuration picks the plugin up with no special support, and ship a Font Awesome icon so icon-only tab mode isn't blank.
4. **Gate colour is data, not decoration.** Black and dark filament (four gates here are black ABS+) disappears against a dark theme, so each spool gets a light halo ring plus the status ring, and never relies on the panel background for contrast.
5. **Lose gracefully to user CSS.** All selectors are prefixed `hh-`, with no `!important` and no bare element selectors, so a `customCSS` block still wins.
6. **Keep the sidebar widget short and self-bounded**, since users stack several widgets per row.

## 8. Mockup

`mockup/index.html` is a self-contained, clickable mockup of the tab, sidebar and navbar described above, running on a simulated `printer.mmu` seeded with the real gate map, selector offsets and statistics from this machine. Scenarios: a full T0→T2 tool change stepping through every `action`, a load failure with the error dialog and recovery wizard, an EndlessSpool runout swap, and the pre-flight check. A hardware toggle switches between the 12-gate ERCF rail and a 4-lane type-B MMU, and a layout toggle switches between stock OctoPrint and the compact red-night UI Customizer skin described above.
