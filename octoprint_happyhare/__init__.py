# -*- coding: utf-8 -*-
"""OctoPrint plugin for the Happy Hare MMU.

Reads live MMU state from Klipper's API socket, renders it, sends the MMU's
commands back through OctoPrint's own queue, and fixes the three things that make
Happy Hare unusable from OctoPrint out of the box:

* an MMU error cancels the print, because Happy Hare reports errors as ``!!``
  lines and OctoPrint treats those as firmware errors,
* Happy Hare's pause dialog is never rendered, so the recovery flow is unreachable,
* cancelling from OctoPrint never runs ``CANCEL_PRINT``/``MMU_PRINT_END``, leaving
  the MMU's print state stale.
"""

from __future__ import absolute_import, unicode_literals

import collections
import json
import os
import re
import shutil
import tempfile
import threading
import time

import octoprint.plugin
from octoprint.util import RepeatedTimer

from . import klippy, model, preprocessor, prompts

__plugin_pythoncompat__ = ">=3.7,<4"

# Lines that must always reach OctoPrint's error handling, whatever else matches
FATAL_PATTERNS = re.compile(
    r"(mcu\s+'|shutdown|lost communication|thermistor|adc out of range|"
    r"heater\s+\w+\s+not heating|timer too close|move exceeds maximum|emergency)",
    re.IGNORECASE)

# Lines that are Happy Hare's own
MMU_PATTERNS = re.compile(r"(happy hare|\bmmu\b|mmu_)", re.IGNORECASE)

HTML_TAG = re.compile(r"<[^>]+>")

MOTION_COMMANDS = {
    "home", "select_gate", "select_tool", "select_bypass", "change_tool", "load",
    "unload", "eject", "preload", "check_gate", "check_all", "servo", "motors_off",
}
RECOVERY_COMMANDS = {"unlock", "recover", "resume", "cancel"}


class HappyHarePlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
):

    def __init__(self):
        self._client = None
        self._status = {}                      # merged Klipper status cache
        self._config_mmu = {}                  # configfile.settings.mmu, queried once
        self._model = {}
        self._model_lock = threading.RLock()
        self._prompts = prompts.PromptParser()
        self._console = collections.deque(maxlen=500)
        self._errors_blocked = 0
        self._dirty = False
        self._timer = None
        self._link = {"connected": False, "message": "not started", "socket": None}
        self._file_metadata_lock = threading.RLock()
        self._pending_metadata = {}

    # ------------------------------------------------------------------
    # Settings / assets / templates
    # ------------------------------------------------------------------
    def get_settings_defaults(self):
        return {
            "socket_path": "",              # empty = auto discover
            "protect_errors": True,         # swallow Happy Hare's !! lines
            "render_prompts": True,         # draw action:prompt_* dialogs
            "preprocess_uploads": True,     # substitute slicer placeholders
            "print_end_on_cancel": True,    # MMU_PRINT_END STATE=cancelled after a cancel
            "cancel_command": "MMU_PRINT_END STATE=cancelled",
            "strip_tool_temps": True,       # M104/M109 T<n> -> M104/M109
            "confirm_moves": True,          # confirm anything that moves filament
            "density": "auto",              # auto | cozy | compact
            "show_navbar": True,            # the indicator in OctoPrint's top bar
            "show_sensors": True,           # sensor panel in the sidebar
            "push_interval": 0.25,
            "console_lines": 200,
        }

    def get_settings_restricted_paths(self):
        return {"admin": [["socket_path"]]}

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        # reconnect if the socket path changed
        path = self._settings.get(["socket_path"])
        if self._client is not None and path and path != self._client.socket_path:
            self._restart_client()

    def get_assets(self):
        return {"js": ["js/happyhare.js"], "css": ["css/happyhare.css"]}

    def get_template_configs(self):
        return [
            {"type": "tab", "name": "MMU", "custom_bindings": True},
            {"type": "sidebar", "name": "Happy Hare MMU", "icon": "circle-notch",
             "custom_bindings": True},
            {"type": "navbar", "custom_bindings": True},
            {"type": "settings", "name": "Happy Hare MMU", "custom_bindings": True},
        ]

    def get_template_vars(self):
        return {"version": self._plugin_version}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def on_after_startup(self):
        if not klippy.supported():
            self._logger.warning(
                "Unix sockets are unavailable on this platform; Happy Hare state "
                "can only come from the serial fallback channel.")
        else:
            self._start_client()
        interval = float(self._settings.get_float(["push_interval"]) or 0.25)
        self._timer = RepeatedTimer(max(0.1, interval), self._push_if_dirty, run_first=False)
        self._timer.start()

    def on_shutdown(self):
        if self._timer is not None:
            self._timer.cancel()
        if self._client is not None:
            self._client.stop()

    def _start_client(self):
        configured = self._settings.get(["socket_path"]) or ""
        path = klippy.discover_socket(configured or None)
        if not path:
            self._link = {"connected": False, "socket": None,
                          "message": "no Klipper API socket found - is Klipper started with -a?"}
            self._logger.warning(self._link["message"])
            return
        self._client = klippy.KlippyClient(
            path,
            model.SUBSCRIBE_OBJECTS,
            on_status=self._on_status,
            on_gcode=self._on_klippy_gcode,
            on_link=self._on_link,
            logger=self._logger,
        )
        self._client.start()

    def _restart_client(self):
        if self._client is not None:
            self._client.stop()
            self._client = None
        self._status = {}
        self._config_mmu = {}
        self._start_client()

    # ------------------------------------------------------------------
    # Klipper callbacks
    # ------------------------------------------------------------------
    def _on_status(self, status, is_snapshot):
        with self._model_lock:
            model.merge_status(self._status, status)
            mmu = self._status.get("mmu") or {}
            if is_snapshot and mmu and not model.is_ready(mmu):
                # Happy Hare can answer the first subscription before the gate
                # arrays exist; ask again rather than rendering an empty map.
                self._spawn(self._requery, "happyhare-requery")
            self._model = model.normalize(self._status, self._config_mmu,
                                          self._status.get("save_variables"))
            self._dirty = True
        if is_snapshot:
            # queries must not run on the socket thread: request() waits for that
            # same thread to pump the reply
            self._spawn(self._load_config, "happyhare-config")

    @staticmethod
    def _spawn(target, name):
        thread = threading.Thread(target=target, name=name)
        thread.daemon = True
        thread.start()

    def _requery(self):
        time.sleep(1.0)
        try:
            result = self._client.query(["mmu", "mmu_machine"])
            self._on_status(result.get("status", {}), False)
        except klippy.KlippyError as error:
            self._logger.debug("Re-query failed: %s" % error)

    def _load_config(self):
        if self._config_mmu or self._client is None:
            return
        try:
            result = self._client.query(["configfile"])
        except klippy.KlippyError as error:
            self._logger.debug("Could not read configfile: %s" % error)
            return
        configfile = (result.get("status") or {}).get("configfile") or {}
        settings = configfile.get("settings") or {}
        config = configfile.get("config") or {}
        merged = dict(settings.get("mmu") or {})
        for key, value in (config.get("mmu_machine") or {}).items():
            merged.setdefault(key, value)
        with self._model_lock:
            self._config_mmu = merged
            self._model = model.normalize(self._status, self._config_mmu,
                                          self._status.get("save_variables"))
            self._dirty = True

    def _on_klippy_gcode(self, line):
        """Console output from Klipper's socket (includes lines OctoPrint sent)."""
        self._record_console(line)

    def _on_link(self, connected, message):
        self._link = {
            "connected": bool(connected),
            "message": message,
            "socket": self._client.socket_path if self._client else None,
        }
        self._dirty = True
        self._push({"type": "link", "link": self._link})

    # ------------------------------------------------------------------
    # Console handling
    # ------------------------------------------------------------------
    def _record_console(self, line, kind=None):
        text = (line or "").rstrip("\r\n")
        if not text:
            return
        if text.startswith("!!"):
            kind = kind or "error"
            text = text[2:].strip()
        elif text.startswith("//"):
            text = text[2:].strip()
        if text.startswith("action:"):
            return
        if not MMU_PATTERNS.search(text) and kind is None:
            return
        entry = {"text": HTML_TAG.sub("", text), "kind": kind or "info", "ts": time.time()}
        self._console.append(entry)
        self._push({"type": "console", "line": entry})

    # ------------------------------------------------------------------
    # OctoPrint hooks
    # ------------------------------------------------------------------
    def hook_gcode_error(self, comm_instance, error_message, *args, **kwargs):
        """Keep a Happy Hare error from cancelling the print.

        OctoPrint treats every ``!!`` line as a firmware error. With the settings
        Klipper's own docs recommend that cancels the running print, and Happy
        Hare emits its error *before* it pauses - so the MMU's recovery flow never
        gets a chance. Returning True here swallows only Happy Hare's own errors.
        """
        if not self._settings.get_boolean(["protect_errors"]):
            return None
        message = error_message or ""
        if FATAL_PATTERNS.search(message):
            return None

        with self._model_lock:
            busy = bool(self._model.get("busy") or self._model.get("paused"))
        if not (MMU_PATTERNS.search(message) or busy):
            return None

        self._errors_blocked += 1
        self._logger.warning("Intercepted MMU error (print protected): %s" % message)
        self._record_console(message, kind="error")
        self._push({"type": "protected", "count": self._errors_blocked, "message": message})
        self._fire("mmu_error", {"message": message})
        return True

    def hook_action(self, comm_instance, line, action, name=None, params=None, *args, **kwargs):
        """Render Klipper prompts, and accept the serial fallback state channel."""
        action = action or ""
        params = params or ""

        if action.startswith("hh_state"):
            self._handle_fallback_state(params)
            return

        if not action.startswith("prompt_"):
            return
        if not self._settings.get_boolean(["render_prompts"]):
            return
        result = self._prompts.handle(action, params)
        if result == "show":
            self._push({"type": "prompt", "prompt": self._prompts.dialog})
        elif result == "close":
            self._push({"type": "prompt", "prompt": None})

    def hook_gcode_received(self, comm_instance, line, *args, **kwargs):
        """Collect Happy Hare's console output (works even with no socket)."""
        if line and (line.startswith("//") or line.startswith("!!")):
            self._record_console(line)
        return line

    def hook_gcode_queuing(self, comm_instance, phase, cmd, cmd_type, gcode,
                           subcode=None, tags=None, *args, **kwargs):
        """Strip the tool suffix from M104/M109.

        Klipper raises "Extruder not configured" for ``M104 T1 S…`` on a single
        extruder machine, which becomes another ``!!`` line. Slicers set up for an
        MMU emit these from time to time.
        """
        if not self._settings.get_boolean(["strip_tool_temps"]):
            return None
        if gcode not in ("M104", "M109") or not cmd:
            return None
        if not re.search(r"\bT\d+", cmd):
            return None
        stripped = re.sub(r"\s*\bT\d+", "", cmd).strip()
        return [(stripped, cmd_type)]

    def hook_scripts(self, comm_instance, script_type, script_name, *args, **kwargs):
        """Tell Happy Hare the print ended when OctoPrint cancels it."""
        if script_type != "gcode" or script_name != "afterPrintCancelled":
            return None
        if not self._settings.get_boolean(["print_end_on_cancel"]):
            return None
        command = (self._settings.get(["cancel_command"]) or "").strip()
        if not command:
            return None
        with self._model_lock:
            available = bool(self._model.get("available"))
        if not available:
            return None
        return None, [command]

    def hook_file_preprocessor(self, path, file_object, links=None, printer_profile=None,
                               allow_overwrite=True, *args, **kwargs):
        """Substitute Happy Hare's slicer placeholders at upload time."""
        if not self._settings.get_boolean(["preprocess_uploads"]):
            return file_object
        name = getattr(file_object, "filename", path) or ""
        if not name.lower().endswith((".gcode", ".gco", ".g")):
            return file_object
        try:
            return _PreprocessedFile(file_object, self)
        except Exception as error:  # noqa: BLE001 - never block an upload
            self._logger.exception("Pre-processing failed, storing file unchanged: %s" % error)
            return file_object

    def hook_permissions(self, *args, **kwargs):
        return [
            {"key": "VIEW", "name": "View MMU state",
             "description": "Allows viewing Happy Hare MMU status",
             "roles": ["view"], "dangerous": False, "default_groups": ["users", "guests"]},
            {"key": "CONTROL", "name": "Control the MMU",
             "description": "Allows tool changes, loading, unloading and map edits",
             "roles": ["control"], "dangerous": True, "default_groups": ["users"]},
            {"key": "CALIBRATE", "name": "Calibrate the MMU",
             "description": "Allows running Happy Hare calibration commands",
             "roles": ["calibrate"], "dangerous": True, "default_groups": ["admins"]},
        ]

    def register_custom_events(self, *args, **kwargs):
        return ["mmu_error", "mmu_paused", "tool_changed", "gate_map_changed",
                "runout", "preflight_warning"]

    def get_update_information(self):
        return {
            "happyhare": {
                "displayName": "Happy Hare MMU",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "Luix333",
                "repo": "OctoPrint-HappyHareMMU",
                "current": self._plugin_version,
                "pip": "https://github.com/Luix333/OctoPrint-HappyHareMMU/archive/{target_version}.zip",
            }
        }

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def on_event(self, event, payload):
        if event in ("PrintStarted", "PrintDone", "PrintFailed", "PrintCancelled"):
            self._dirty = True
        elif event == "FileAdded":
            self._attach_metadata(payload or {})

    # ------------------------------------------------------------------
    # Serial fallback channel
    # ------------------------------------------------------------------
    def _handle_fallback_state(self, params):
        try:
            data = json.loads(params)
        except ValueError:
            self._logger.debug("Ignoring malformed hh_state payload")
            return
        if not isinstance(data, dict):
            return
        with self._model_lock:
            model.merge_status(self._status, {"mmu": data})
            self._model = model.normalize(self._status, self._config_mmu,
                                          self._status.get("save_variables"))
            self._dirty = True

    # ------------------------------------------------------------------
    # Pushing state to the browser
    # ------------------------------------------------------------------
    def _push(self, payload):
        try:
            self._plugin_manager.send_plugin_message(self._identifier, payload)
        except Exception:  # noqa: BLE001
            pass

    def _push_if_dirty(self):
        if not self._dirty:
            return
        with self._model_lock:
            self._dirty = False
            snapshot = dict(self._model)
        snapshot["link"] = self._link
        snapshot["errors_blocked"] = self._errors_blocked
        self._push({"type": "state", "state": snapshot})

    def _fire(self, event, payload):
        try:
            self._event_bus.fire("plugin_happyhare_%s" % event, payload)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {
            "refresh": [],
            "command": ["id"],
            "gate_map": ["gate"],
            "ttg_map": ["map"],
            "endless_spool": ["groups"],
            "prompt_button": ["gcode"],
            "dismiss_prompt": [],
            "refresh_sensors": [],
            "preflight": ["origin", "path"],
        }

    def on_api_get(self, request):
        with self._model_lock:
            snapshot = dict(self._model)
        snapshot["link"] = self._link
        snapshot["errors_blocked"] = self._errors_blocked
        snapshot["console"] = list(self._console)[-int(self._settings.get_int(["console_lines"]) or 200):]
        snapshot["prompt"] = self._prompts.dialog
        snapshot["settings"] = {
            "density": self._settings.get(["density"]),
            "confirm_moves": self._settings.get_boolean(["confirm_moves"]),
            "protect_errors": self._settings.get_boolean(["protect_errors"]),
        }
        return _json(snapshot)

    def on_api_command(self, command, data):
        if command == "refresh":
            self._dirty = True
            self._push_if_dirty()
            return _json({"ok": True})

        if command == "command":
            return self._run_command(data.get("id"), data)

        if command == "gate_map":
            return self._set_gate_map(data)

        if command == "ttg_map":
            mapping = data.get("map") or []
            if not isinstance(mapping, list) or not all(isinstance(v, int) for v in mapping):
                return _json({"ok": False, "error": "invalid map"}, status=400)
            self._send(["MMU_TTG_MAP MAP=%s" % ",".join(str(v) for v in mapping)])
            self._fire("gate_map_changed", {"ttg_map": mapping})
            return _json({"ok": True})

        if command == "endless_spool":
            groups = data.get("groups") or []
            if not isinstance(groups, list) or not all(isinstance(v, int) for v in groups):
                return _json({"ok": False, "error": "invalid groups"}, status=400)
            enable = 1 if data.get("enable", True) else 0
            self._send(["MMU_ENDLESS_SPOOL ENABLE=%d GROUPS=%s"
                        % (enable, ",".join(str(v) for v in groups))])
            return _json({"ok": True})

        if command == "prompt_button":
            return self._prompt_button(data.get("gcode") or "")

        if command == "dismiss_prompt":
            self._prompts.reset()
            self._push({"type": "prompt", "prompt": None})
            return _json({"ok": True})

        if command == "refresh_sensors":
            return self._refresh_sensors()

        if command == "preflight":
            return _json(self.preflight(data.get("origin"), data.get("path")))

        return _json({"ok": False, "error": "unknown command"}, status=400)

    def _refresh_sensors(self):
        """Ask Klipper to re-read the endstops and the probe.

        The filament switches publish continuously, but an endstop (the selector
        home switch) and the probe only update their status when queried, so this
        sends the two query commands and lets the subscription carry the result
        back. Both move nothing, but they still go through the G-code queue, so
        they wait for a print.
        """
        with self._model_lock:
            printing = bool(self._model.get("printing"))
            paused = bool(self._model.get("paused"))
        if printing and not paused:
            return _json({"ok": False, "error": "not while printing"}, status=409)

        available = getattr(self._client, "available_objects", []) if self._client else []
        commands = ["QUERY_ENDSTOPS"]
        if "probe" in available or (self._status or {}).get("probe") is not None:
            commands.append("QUERY_PROBE")
        self._send(commands)
        # The queries go out through OctoPrint's queue, and these two objects are
        # not always part of the subscription, so read them back explicitly once
        # the commands have had time to run.
        self._spawn(self._reread_sensors, "happyhare-sensors")
        return _json({"ok": True, "commands": commands})

    def _reread_sensors(self):
        if self._client is None:
            return
        time.sleep(1.5)
        # asking for an object Klipper does not have fails the whole query
        available = getattr(self._client, "available_objects", [])
        wanted = [name for name in ("query_endstops", "probe")
                  if not available or name in available]
        if not wanted:
            return
        try:
            result = self._client.query(wanted)
        except klippy.KlippyError as error:
            self._logger.debug("Sensor re-read failed: %s" % error)
            return
        self._on_status(result.get("status", {}), False)

    # -- command whitelist -------------------------------------------------
    def _run_command(self, command_id, data):
        builders = {
            "home": lambda d: "MMU_HOME",
            "select_gate": lambda d: "MMU_SELECT GATE=%d" % int(d["gate"]),
            "select_tool": lambda d: "MMU_SELECT TOOL=%d" % int(d["tool"]),
            "select_bypass": lambda d: "MMU_SELECT BYPASS=1",
            "change_tool": lambda d: "MMU_CHANGE_TOOL TOOL=%d" % int(d["tool"]),
            "load": lambda d: "MMU_LOAD",
            "unload": lambda d: "MMU_UNLOAD",
            "eject": lambda d: ("MMU_EJECT GATE=%d" % int(d["gate"])) if "gate" in d else "MMU_EJECT",
            "preload": lambda d: ("MMU_PRELOAD GATE=%d" % int(d["gate"])) if "gate" in d else "MMU_PRELOAD",
            "check_gate": lambda d: "MMU_CHECK_GATE GATE=%d" % int(d["gate"]),
            "check_all": lambda d: "MMU_CHECK_GATE ALL=1",
            "motors_off": lambda d: "MMU_MOTORS_OFF",
            "servo": lambda d: "MMU_SERVO POS=%s" % _word(d.get("pos", "up")),
            "sync": lambda d: "MMU_SYNC_GEAR_MOTOR SYNC=%d" % (1 if d.get("sync") else 0),
            "status": lambda d: "MMU_STATUS DETAIL=1",
            "stats": lambda d: "MMU_STATS",
            "unlock": lambda d: "MMU_UNLOCK",
            "recover": lambda d: _recover_command(d),
            "enable": lambda d: "MMU ENABLE=%d" % (1 if d.get("enable", True) else 0),
        }

        if command_id in ("resume", "cancel"):
            # route through OctoPrint so its own job state follows along
            if command_id == "resume":
                self._printer.resume_print()
            else:
                self._printer.cancel_print()
            return _json({"ok": True, "routed": "octoprint"})

        builder = builders.get(command_id)
        if builder is None:
            return _json({"ok": False, "error": "unknown command id"}, status=400)

        with self._model_lock:
            printing = bool(self._model.get("printing"))
            paused = bool(self._model.get("paused"))
        if command_id in MOTION_COMMANDS and printing and not paused:
            return _json({"ok": False, "error": "not while printing"}, status=409)

        try:
            gcode = builder(data)
        except (KeyError, TypeError, ValueError):
            return _json({"ok": False, "error": "missing or invalid parameter"}, status=400)

        self._send([gcode])
        if command_id == "change_tool":
            self._fire("tool_changed", {"tool": data.get("tool")})
        return _json({"ok": True, "gcode": gcode})

    def _set_gate_map(self, data):
        try:
            gate = int(data["gate"])
        except (KeyError, TypeError, ValueError):
            return _json({"ok": False, "error": "invalid gate"}, status=400)

        parts = ["MMU_GATE_MAP GATE=%d" % gate]
        if "material" in data:
            parts.append("MATERIAL='%s'" % _quote(data["material"]))
        if "color" in data:
            parts.append("COLOR=%s" % _word(str(data["color"]).lstrip("#")))
        if "name" in data:
            parts.append("NAME='%s'" % _quote(data["name"]))
        if "temperature" in data:
            parts.append("TEMP=%d" % int(data["temperature"]))
        if "spool_id" in data:
            parts.append("SPOOLID=%d" % int(data["spool_id"]))
        if "speed" in data:
            parts.append("SPEED=%d" % max(10, min(150, int(data["speed"]))))
        if "status" in data:
            parts.append("AVAILABLE=%d" % int(data["status"]))
        parts.append("QUIET=1")
        self._send([" ".join(parts)])
        self._fire("gate_map_changed", {"gate": gate})
        return _json({"ok": True, "gcode": " ".join(parts)})

    def _prompt_button(self, gcode):
        gcode = (gcode or "").strip()
        if not gcode:
            self._prompts.reset()
            self._push({"type": "prompt", "prompt": None})
            return _json({"ok": True})
        # RESUME and CANCEL_PRINT must go through OctoPrint, otherwise the job
        # stays paused on OctoPrint's side while Klipper carries on.
        upper = gcode.upper()
        if upper == "RESUME":
            self._printer.resume_print()
        elif upper in ("CANCEL_PRINT", "CANCEL"):
            self._printer.cancel_print()
        else:
            self._send([gcode])
        self._prompts.reset()
        self._push({"type": "prompt", "prompt": None})
        return _json({"ok": True, "gcode": gcode})

    def _send(self, commands):
        self._printer.commands(commands, tags={"plugin:happyhare"})

    # ------------------------------------------------------------------
    # Pre-flight check
    # ------------------------------------------------------------------
    def preflight(self, origin, path):
        """Compare a file's slicer data with what is actually loaded."""
        origin = origin or "local"
        metadata = {}
        try:
            metadata = self._file_manager.get_metadata(origin, path).get("happyhare") or {}
        except Exception:  # noqa: BLE001
            metadata = {}

        with self._model_lock:
            state = dict(self._model)

        if not metadata:
            return {"ok": False, "reason": "no Happy Hare metadata for this file",
                    "file": path, "issues": [], "tools": []}

        gates = {gate["index"]: gate for gate in state.get("gates", [])}
        ttg = state.get("ttg_map") or []
        rows, issues = [], []

        for position, tool in enumerate(metadata.get("tools") or []):
            gate_index = ttg[tool] if tool < len(ttg) else None
            gate = gates.get(gate_index, {})
            slicer_color = _at(metadata.get("colors"), position, "")
            slicer_material = _at(metadata.get("materials"), position, "")
            slicer_temp = _at(metadata.get("temps"), position, "")
            row_issues = []

            if gate_index is None:
                row_issues.append(("critical", "tool %d is not mapped to a gate" % tool))
            else:
                if gate.get("status") == model.GATE_EMPTY:
                    row_issues.append(("critical", "gate %d is empty" % gate_index))
                elif gate.get("status") == model.GATE_UNKNOWN:
                    row_issues.append(("warning", "gate %d status unknown" % gate_index))
                gate_material = (gate.get("material") or "").upper()
                if slicer_material and gate_material and gate_material != slicer_material.upper():
                    row_issues.append(("critical", "material %s loaded, file wants %s"
                                       % (gate_material, slicer_material)))
                gate_color = (gate.get("color") or "").lower()[:6]
                if slicer_color and gate_color and gate_color != slicer_color.lower()[:6]:
                    row_issues.append(("warning", "colour differs"))

            rows.append({
                "tool": tool,
                "gate": gate_index,
                "slicer": {"color": slicer_color, "material": slicer_material, "temp": slicer_temp},
                "loaded": {"color": gate.get("color", ""), "material": gate.get("material", ""),
                           "status": gate.get("status_text", "")},
                "issues": [{"severity": severity, "text": text} for severity, text in row_issues],
            })
            issues.extend(row_issues)

        worst = "ok"
        if any(severity == "warning" for severity, _ in issues):
            worst = "warning"
        if any(severity == "critical" for severity, _ in issues):
            worst = "critical"
        if worst != "ok":
            self._fire("preflight_warning", {"file": path, "severity": worst})

        return {"ok": True, "file": path, "severity": worst, "tools": rows,
                "total_toolchanges": metadata.get("total_toolchanges", 0),
                "slicer": metadata.get("slicer"),
                "purge_volumes": metadata.get("purge_volumes") or []}

    def store_file_metadata(self, filename, metadata):
        """Called by the upload wrapper once a file has been scanned.

        The wrapper only knows the temporary path, so the findings wait here until
        OctoPrint announces the stored file with FileAdded.
        """
        with self._file_metadata_lock:
            self._pending_metadata[os.path.basename(filename)] = metadata

    def _attach_metadata(self, payload):
        path = payload.get("path")
        storage = payload.get("storage", "local")
        if not path:
            return
        with self._file_metadata_lock:
            metadata = self._pending_metadata.pop(os.path.basename(path), None)
        if metadata is None:
            return
        try:
            self._file_manager.set_additional_metadata(storage, path, "happyhare", metadata,
                                                       overwrite=True)
            self._logger.debug("Stored Happy Hare metadata for %s" % path)
        except Exception as error:  # noqa: BLE001
            self._logger.debug("Could not store metadata: %s" % error)


class _PreprocessedFile(object):
    """File wrapper that runs the placeholder substitution during upload."""

    def __init__(self, wrapped, plugin):
        self.wrapped = wrapped
        self.plugin = plugin
        self.filename = getattr(wrapped, "filename", None)

    def save(self, path, permissions=None):
        source = tempfile.NamedTemporaryFile(suffix=".gcode", delete=False)
        source.close()
        try:
            self.wrapped.save(source.name)
            target = source.name + ".hh"
            changed, metadata = preprocessor.process_file(source.name, target)
            if changed:
                shutil.move(target, path)
                self.plugin._logger.info(
                    "Substituted Happy Hare placeholders in %s (%s, tools %s)"
                    % (self.filename, metadata.get("slicer"), metadata.get("tools")))
            else:
                shutil.move(source.name, path)
                if os.path.exists(target):
                    os.remove(target)
            self.plugin.store_file_metadata(self.filename or path, metadata)
        finally:
            if os.path.exists(source.name):
                try:
                    os.remove(source.name)
                except OSError:
                    pass
        if permissions is not None:
            try:
                os.chmod(path, permissions)
            except OSError:
                pass

    def __getattr__(self, item):
        return getattr(self.wrapped, item)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _json(payload, status=200):
    from flask import jsonify, make_response
    response = make_response(jsonify(payload))
    response.status_code = status
    return response


def _word(value):
    """Allow only a bare word in a G-code parameter."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "", str(value))[:32]


def _quote(value):
    return re.sub(r"['\";#\n\r]", "", str(value))[:64]


def _at(seq, index, default=None):
    try:
        return seq[index]
    except (TypeError, IndexError, KeyError):
        return default


def _recover_command(data):
    parts = ["MMU_RECOVER"]
    if "loaded" in data:
        parts.append("LOADED=%d" % (1 if data["loaded"] else 0))
    if "tool" in data:
        parts.append("TOOL=%d" % int(data["tool"]))
    if "gate" in data:
        parts.append("GATE=%d" % int(data["gate"]))
    return " ".join(parts)


__plugin_name__ = "Happy Hare MMU"
__plugin_description__ = ("Live view and control for a Happy Hare MMU on Klipper, "
                          "with print-safe error handling for OctoPrint.")


def __plugin_load__():
    plugin = HappyHarePlugin()
    global __plugin_implementation__
    __plugin_implementation__ = plugin

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.error": plugin.hook_gcode_error,
        "octoprint.comm.protocol.action": plugin.hook_action,
        "octoprint.comm.protocol.gcode.received": plugin.hook_gcode_received,
        "octoprint.comm.protocol.gcode.queuing": plugin.hook_gcode_queuing,
        "octoprint.comm.protocol.scripts": plugin.hook_scripts,
        "octoprint.filemanager.preprocessor": plugin.hook_file_preprocessor,
        "octoprint.access.permissions": plugin.hook_permissions,
        "octoprint.events.register_custom_events": plugin.register_custom_events,
        "octoprint.plugin.softwareupdate.check_config": plugin.get_update_information,
    }
