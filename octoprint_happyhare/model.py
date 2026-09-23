# -*- coding: utf-8 -*-
"""Normalisation of Happy Hare's Klipper status into one model.

Happy Hare v3.4x and v4.x publish different shapes of ``printer.mmu``:

* v3 keeps ``servo``, ``grip`` and ``has_bypass`` at the top level, has a minimal
  ``mmu_machine`` object and uses the sensor names ``mmu_pre_gate`` / ``mmu_gear`` /
  ``mmu_gate``.
* v4 moves those under ``selector``, describes every unit in ``mmu_machine.unit_N``
  and renames the sensors to ``mmu_entry`` / ``mmu_exit`` / ``mmu_shared_exit``.

Everything in this module is plain Python with no OctoPrint imports so it can be
unit tested on its own.
"""

from __future__ import absolute_import, unicode_literals

import re

# ---------------------------------------------------------------------------
# Enumerations (values come from Happy Hare's mmu_constants.py / mmu.py)
# ---------------------------------------------------------------------------

TOOL_UNKNOWN = -1
TOOL_BYPASS = -2

GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1
GATE_AVAILABLE_FROM_BUFFER = 2

GATE_STATUS_TEXT = {
    GATE_UNKNOWN: "Unknown",
    GATE_EMPTY: "Empty",
    GATE_AVAILABLE: "On spool",
    GATE_AVAILABLE_FROM_BUFFER: "Buffered",
}

FILAMENT_POS_NAME = {
    -1: "Unknown",
    0: "Unloaded and parked",
    1: "Homed at gate",
    2: "Start of bowden",
    3: "In bowden",
    4: "End of bowden",
    5: "Homed at extruder sensor",
    6: "At extruder gear",
    7: "Past extruder gear",
    8: "Homed at toolhead sensor",
    9: "In extruder",
    10: "Loaded",
}

FILAMENT_POS_LOADED = 10
FILAMENT_POS_UNLOADED = 0

# print_state values Happy Hare can report
PRINT_STATES = (
    "initialized", "ready", "started", "printing", "complete",
    "cancelled", "error", "pause_locked", "paused", "standby", "idle",
)
PAUSED_STATES = ("pause_locked", "paused")

# Actions that mean the MMU is doing something right now
IDLE_ACTION = "Idle"

# Sensors: map both generations onto one set of ids, ordered along the filament path.
# (canonical id, display label)
SENSOR_ORDER = (
    ("gate_entry", "Gate entry"),
    ("gate_exit", "Gate exit"),
    ("gate_shared", "Gate"),
    ("encoder", "Encoder"),
    ("extruder", "Extruder entry"),
    ("toolhead", "Toolhead"),
    ("compression", "Compression"),
    ("tension", "Tension"),
)

SENSOR_ALIASES = {
    # v3 name -> canonical
    "mmu_pre_gate": "gate_entry",
    "mmu_gear": "gate_exit",
    "mmu_gate": "gate_shared",
    # v4 name -> canonical
    "mmu_entry": "gate_entry",
    "mmu_exit": "gate_exit",
    "mmu_shared_exit": "gate_shared",
    # same in both
    "encoder": "encoder",
    "extruder": "extruder",
    "toolhead": "toolhead",
    "filament_compression": "compression",
    "filament_tension": "tension",
}

# Klipper objects worth subscribing to
SUBSCRIBE_OBJECTS = (
    "mmu",
    "mmu_machine",
    "mmu_encoder mmu_encoder",
    "print_stats",
    "pause_resume",
    "extruder",
    "save_variables",
    "webhooks",
)

# Fields that must be present before the UI can draw a gate map. Happy Hare can
# answer the first subscription before these are populated (KlipperScreen hit the
# same race), in which case the caller should re-query.
REQUIRED_FIELDS = ("gate", "gate_color", "gate_status", "filament_pos", "ttg_map")


def is_ready(mmu):
    """True when a status dict carries enough to render the panel."""
    if not mmu:
        return False
    return all(field in mmu for field in REQUIRED_FIELDS)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Happy Hare accepts a W3C colour name as well as hex. These are the ones people
# actually put on a filament spool; anything else falls through to `css`, where the
# browser resolves the name for us.
_W3C = {
    "black": "000000", "white": "ffffff", "red": "ff0000", "green": "008000",
    "blue": "0000ff", "yellow": "ffff00", "orange": "ffa500", "purple": "800080",
    "grey": "808080", "gray": "808080", "silver": "c0c0c0", "brown": "a52a2a",
    "pink": "ffc0cb", "cyan": "00ffff", "aqua": "00ffff", "magenta": "ff00ff",
    "fuchsia": "ff00ff", "lime": "00ff00", "navy": "000080", "teal": "008080",
    "olive": "808000", "maroon": "800000", "gold": "ffd700", "beige": "f5f5dc",
    "ivory": "fffff0", "khaki": "f0e68c", "salmon": "fa8072", "tan": "d2b48c",
    "violet": "ee82ee", "indigo": "4b0082", "turquoise": "40e0d0", "coral": "ff7f50",
    "crimson": "dc143c", "orchid": "da70d6", "plum": "dda0dd", "wheat": "f5deb3",
    "darkred": "8b0000", "darkgreen": "006400", "darkblue": "00008b",
    "darkgrey": "a9a9a9", "darkgray": "a9a9a9", "darkorange": "ff8c00",
    "lightblue": "add8e6", "lightgreen": "90ee90", "lightgrey": "d3d3d3",
    "lightgray": "d3d3d3", "lightyellow": "ffffe0", "lightpink": "ffb6c1",
    "skyblue": "87ceeb", "steelblue": "4682b4", "seagreen": "2e8b57",
    "forestgreen": "228b22", "limegreen": "32cd32", "royalblue": "4169e1",
    "midnightblue": "191970", "chocolate": "d2691e", "sienna": "a0522d",
    "transparent": None,
}


def color_to_hex(color):
    """Happy Hare stores 'RRGGBB', 'RRGGBBAA', a W3C name or ''. Return '#rrggbb' or None."""
    if not color:
        return None
    value = str(color).strip().lstrip("#").lower()
    if value in _W3C:
        value = _W3C[value]
    if not value:
        return None
    if len(value) in (6, 8):
        try:
            int(value[:6], 16)
        except ValueError:
            return None
        return "#" + value[:6]
    return None


def color_to_css(color):
    """A value the browser can paint: hex where we know it, else the name itself."""
    resolved = color_to_hex(color)
    if resolved:
        return resolved
    name = re.sub(r"[^a-zA-Z]", "", str(color or ""))
    return name.lower() or None


def is_dark(hex_color):
    """Rough luminance test, used by the UI to outline dark filament."""
    if not hex_color:
        return False
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
    except (ValueError, IndexError):
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) < 110


def _at(seq, index, default=None):
    try:
        return seq[index]
    except (TypeError, IndexError, KeyError):
        return default


def merge_status(cache, delta):
    """Merge one ``objects/subscribe`` delta into the cached status.

    Klipper's deltas are per top level field of each object (a change to one gate
    resends the whole ``gate_status`` list), so a one level deep merge is correct
    and a deep merge would be wrong.
    """
    for obj, fields in (delta or {}).items():
        if not isinstance(fields, dict):
            cache[obj] = fields
            continue
        target = cache.setdefault(obj, {})
        if isinstance(target, dict):
            target.update(fields)
        else:
            cache[obj] = dict(fields)
    return cache


# ---------------------------------------------------------------------------
# Version / hardware detection
# ---------------------------------------------------------------------------

def detect(machine, config_mmu=None):
    """Work out the Happy Hare generation and the hardware description.

    ``machine`` is ``printer.mmu_machine``; ``config_mmu`` is
    ``configfile.settings.mmu`` (v3 keeps ``happy_hare_version`` there).
    """
    machine = machine or {}
    config_mmu = config_mmu or {}

    version = machine.get("happy_hare_version") or config_mmu.get("happy_hare_version") or ""
    version = str(version)
    try:
        generation = int(float(version.split(".")[0]))
    except (ValueError, IndexError):
        generation = 4 if "unit_0" in machine else 3

    units = []
    for index in range(int(machine.get("num_units", 0) or 0)):
        unit = machine.get("unit_%d" % index)
        if isinstance(unit, dict):
            units.append(unit)

    if units:
        first = units[0]
        hardware = {
            "vendor": first.get("vendor", "Unknown"),
            "hw_version": str(first.get("version", "")),
            "selector_type": first.get("selector_type", "LinearSelector"),
            "num_units": len(units),
            "units": units,
        }
    else:  # v3, or a v4 machine that has not published its units yet
        hardware = {
            "vendor": machine.get("mmu_vendor") or config_mmu.get("mmu_vendor") or "Unknown",
            "hw_version": str(machine.get("mmu_version") or config_mmu.get("mmu_version") or ""),
            "selector_type": machine.get("selector_type") or "",
            "num_units": 1,
            "units": [],
        }

    hardware["version"] = version
    hardware["generation"] = generation
    if not hardware["selector_type"]:
        hardware["selector_type"] = _guess_selector(hardware["vendor"])
    hardware["has_selector"] = hardware["selector_type"] not in ("VirtualSelector", "")
    return hardware


# Vendors whose gates each have their own gear motor: drawn as lanes, not a rail
_TYPE_B_VENDORS = (
    "boxturtle", "angrybeaver", "nightowl", "3ms", "quattrobox", "kms", "emu", "qidi",
)


def _guess_selector(vendor):
    name = (vendor or "").replace(" ", "").replace("-", "").lower()
    if name in _TYPE_B_VENDORS:
        return "VirtualSelector"
    if name in ("3dchameleon", "mmx6", "lowrider"):
        return "RotarySelector"
    if name in ("picommu", "mmx"):
        return "ServoSelector"
    if name == "vvd":
        return "IndexedSelector"
    return "LinearSelector"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize(status, config_mmu=None, save_variables=None):
    """Turn a cached Klipper status into the model the UI renders.

    ``status`` is the merged cache: ``{"mmu": {...}, "mmu_machine": {...}, ...}``.
    """
    status = status or {}
    mmu = status.get("mmu") or {}
    machine = status.get("mmu_machine") or {}
    config_mmu = config_mmu or {}

    hardware = detect(machine, config_mmu)
    selector = mmu.get("selector") if isinstance(mmu.get("selector"), dict) else {}

    num_gates = int(mmu.get("num_gates") or machine.get("num_gates") or 0)
    ttg_map = list(mmu.get("ttg_map") or [])
    groups = list(mmu.get("endless_spool_groups") or [])

    gates = []
    for index in range(num_gates):
        color = _at(mmu.get("gate_color") or [], index, "")
        rgb = color_to_hex(color)
        css = color_to_css(color)
        status_value = _at(mmu.get("gate_status") or [], index, GATE_UNKNOWN)
        gates.append({
            "index": index,
            "status": status_value,
            "status_text": GATE_STATUS_TEXT.get(status_value, "Unknown"),
            "color": color or "",
            "rgb": rgb,
            "css": css,
            "dark": is_dark(rgb),
            "material": _at(mmu.get("gate_material") or [], index, "") or "",
            "name": _at(mmu.get("gate_filament_name") or [], index, "") or "",
            "vendor": _at(mmu.get("gate_vendor") or [], index, "") or "",
            "temperature": _at(mmu.get("gate_temperature") or [], index, 0),
            "spool_id": _at(mmu.get("gate_spool_id") or [], index, -1),
            "speed": _at(mmu.get("gate_speed_override") or [], index, 100),
            "group": _at(groups, index, 0),
            "tools": [tool for tool, gate in enumerate(ttg_map) if gate == index],
            "stats": _gate_stats(save_variables, index),
        })

    filament_pos = mmu.get("filament_pos", -1)
    action = mmu.get("action", IDLE_ACTION)
    print_state = mmu.get("print_state", "")

    model = {
        "available": bool(mmu),
        "enabled": bool(mmu.get("enabled", False)),
        "version": hardware["version"],
        "generation": hardware["generation"],
        "vendor": hardware["vendor"],
        "hw_version": hardware["hw_version"],
        "selector_type": hardware["selector_type"],
        "has_selector": hardware["has_selector"],
        "num_units": hardware["num_units"],

        "print_state": print_state,
        "paused": print_state in PAUSED_STATES,
        "locked": print_state == "pause_locked",
        "printing": print_state in ("printing", "started"),
        "action": action,
        "busy": action != IDLE_ACTION,
        "operation": mmu.get("operation", "") or "",
        "reason_for_pause": mmu.get("reason_for_pause", "") or "",

        "tool": mmu.get("tool", TOOL_UNKNOWN),
        "gate": mmu.get("gate", TOOL_UNKNOWN),
        "last_tool": mmu.get("last_tool", TOOL_UNKNOWN),
        "next_tool": mmu.get("next_tool", TOOL_UNKNOWN),
        "num_toolchanges": mmu.get("num_toolchanges", 0),
        "last_toolchange": mmu.get("last_toolchange", "") or "",
        "active_filament": mmu.get("active_filament") or {},

        "filament": mmu.get("filament", "Unknown"),
        "filament_pos": filament_pos,
        "filament_pos_name": FILAMENT_POS_NAME.get(filament_pos, "Unknown"),
        "filament_position": mmu.get("filament_position", 0),
        "filament_direction": mmu.get("filament_direction", 0),
        "bowden_progress": mmu.get("bowden_progress", -1),
        "bowden_length": _bowden_length(config_mmu, save_variables),

        "is_homed": bool(mmu.get("is_homed", False)),
        "sync_drive": bool(mmu.get("sync_drive", False)),
        "servo": selector.get("servo", mmu.get("servo", "")) or "",
        "grip": selector.get("grip", mmu.get("grip", "")) or "",
        "has_bypass": bool(selector.get("has_bypass", mmu.get("has_bypass", False))),

        "num_gates": num_gates,
        "gates": gates,
        "ttg_map": ttg_map,
        "endless_spool_groups": groups,
        "endless_spool_enabled": bool(
            mmu.get("endless_spool_enabled", mmu.get("endless_spool", False))
        ),

        "sensors": normalize_sensors(mmu.get("sensors")),
        "encoder": mmu.get("encoder") or {},
        "slicer_tool_map": mmu.get("slicer_tool_map") or {},
        "spoolman_support": mmu.get("spoolman_support", "off"),
        "selector_offsets": _selector_offsets(save_variables),
        "counters": _counters(save_variables),
        "swap_stats": _swap_stats(save_variables),
    }
    return model


def normalize_sensors(sensors):
    """Map either generation's sensor names onto the canonical ids, in path order."""
    sensors = sensors or {}
    labels = dict(SENSOR_ORDER)
    out = []
    seen = set()
    for raw_name, value in sensors.items():
        # v4 appends the gate number when the selected gate is unknown (mmu_entry_3)
        base = raw_name
        canonical = SENSOR_ALIASES.get(base)
        if canonical is None and "_" in base:
            trimmed = base.rsplit("_", 1)[0]
            canonical = SENSOR_ALIASES.get(trimmed)
        if canonical is None:
            canonical = base
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append({
            "id": canonical,
            "raw": raw_name,
            "label": labels.get(canonical, raw_name.replace("_", " ")),
            "state": value,          # True / False / None (disabled)
        })
    order = [name for name, _ in SENSOR_ORDER]
    out.sort(key=lambda s: order.index(s["id"]) if s["id"] in order else len(order))
    return out


def _bowden_length(config_mmu, save_variables):
    variables = (save_variables or {}).get("variables") or {}
    lengths = variables.get("mmu_calibration_bowden_lengths")
    if isinstance(lengths, (list, tuple)) and lengths:
        try:
            return float(lengths[0])
        except (TypeError, ValueError):
            pass
    for key in ("mmu_calibration_bowden_length", "bowden_length"):
        if key in variables:
            try:
                return float(variables[key])
            except (TypeError, ValueError):
                pass
    try:
        return float(config_mmu.get("bowden_length", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _selector_offsets(save_variables):
    """Calibrated selector positions, so the UI can draw gates where they really are."""
    variables = (save_variables or {}).get("variables") or {}
    offsets = variables.get("mmu_selector_offsets")
    bypass = variables.get("mmu_selector_bypass")
    if not isinstance(offsets, (list, tuple)) or not offsets:
        return None
    try:
        return {"gates": [float(value) for value in offsets],
                "bypass": float(bypass) if bypass is not None else None}
    except (TypeError, ValueError):
        return None


def _gate_stats(save_variables, index):
    variables = (save_variables or {}).get("variables") or {}
    # v4 renames per unit keys to mmu_<unit>_statistics_gate_<n>
    for key in variables:
        if key.startswith("mmu_") and key.endswith("_statistics_gate_%d" % index) \
                or key == "mmu_statistics_gate_%d" % index:
            value = variables[key]
            if isinstance(value, dict):
                return value
    return {}


def _counters(save_variables):
    variables = (save_variables or {}).get("variables") or {}
    counters = variables.get("mmu_statistics_counters")
    if not isinstance(counters, dict):
        return []
    out = []
    for name, data in sorted(counters.items()):
        if not isinstance(data, dict):
            continue
        out.append({
            "name": name,
            "count": data.get("count", 0),
            "limit": data.get("limit", -1),
            "warning": data.get("warning", ""),
        })
    return out


def _swap_stats(save_variables):
    variables = (save_variables or {}).get("variables") or {}
    swaps = variables.get("mmu_statistics_swaps")
    return swaps if isinstance(swaps, dict) else {}
