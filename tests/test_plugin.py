# -*- coding: utf-8 -*-
"""Tests for the plugin's hooks, with OctoPrint stubbed out.

These cover the behaviour the plugin exists for: not letting a Happy Hare error
cancel the print, rendering the pause dialog, telling Happy Hare about a cancel,
and refusing commands that would move filament mid-print.
"""

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub_octoprint():
    """Minimal stand-ins for the OctoPrint API the plugin imports."""
    if "octoprint" in sys.modules:
        return
    octoprint = types.ModuleType("octoprint")
    plugin = types.ModuleType("octoprint.plugin")
    for name in ("StartupPlugin", "ShutdownPlugin", "SettingsPlugin", "AssetPlugin",
                 "TemplatePlugin", "SimpleApiPlugin", "EventHandlerPlugin"):
        setattr(plugin, name, type(name, (object,), {}))

    def on_settings_save(self, data):
        return None

    plugin.SettingsPlugin.on_settings_save = on_settings_save
    util = types.ModuleType("octoprint.util")
    util.RepeatedTimer = type("RepeatedTimer", (object,), {
        "__init__": lambda self, *a, **k: None,
        "start": lambda self: None,
        "cancel": lambda self: None,
    })
    octoprint.plugin = plugin
    octoprint.util = util
    sys.modules["octoprint"] = octoprint
    sys.modules["octoprint.plugin"] = plugin
    sys.modules["octoprint.util"] = util

    # the plugin only imports flask inside _json(); stub enough for that
    flask = types.ModuleType("flask")
    flask.jsonify = lambda payload: {"payload": payload}
    flask.make_response = lambda payload: types.SimpleNamespace(
        payload=payload, status_code=200)
    sys.modules.setdefault("flask", flask)


_stub_octoprint()

from octoprint_happyhare import HappyHarePlugin  # noqa: E402


class FakeSettings(object):
    def __init__(self, values):
        self.values = values

    def get(self, path):
        return self.values.get(path[0])

    def get_boolean(self, path):
        return bool(self.values.get(path[0]))

    def get_int(self, path):
        return int(self.values.get(path[0], 0))

    def get_float(self, path):
        return float(self.values.get(path[0], 0))


class FakePrinter(object):
    def __init__(self):
        self.sent = []
        self.resumed = False
        self.cancelled = False

    def commands(self, commands, tags=None):
        self.sent.extend(commands)

    def resume_print(self):
        self.resumed = True

    def cancel_print(self):
        self.cancelled = True


class FakeLogger(object):
    def __init__(self):
        self.messages = []

    def __getattr__(self, _name):
        return lambda *args, **kwargs: self.messages.append(args[0] if args else "")


def make_plugin(**overrides):
    plugin = HappyHarePlugin()
    values = {
        "protect_errors": True,
        "render_prompts": True,
        "preprocess_uploads": True,
        "print_end_on_cancel": True,
        "cancel_command": "MMU_PRINT_END STATE=cancelled",
        "strip_tool_temps": True,
        "confirm_moves": True,
        "socket_path": "",
        "density": "auto",
        "console_lines": 200,
        "push_interval": 0.25,
    }
    values.update(overrides)
    plugin._settings = FakeSettings(values)
    plugin._logger = FakeLogger()
    plugin._printer = FakePrinter()
    plugin._plugin_manager = types.SimpleNamespace(send_plugin_message=lambda *a, **k: None)
    plugin._identifier = "happyhare"
    plugin._event_bus = types.SimpleNamespace(fire=lambda *a, **k: None)
    plugin._model = {"available": True, "busy": False, "paused": False, "printing": False}
    return plugin


class TestErrorProtection(unittest.TestCase):
    def test_happy_hare_error_is_swallowed(self):
        plugin = make_plugin()
        result = plugin.hook_gcode_error(None, "MMU issue: Filament not seen by encoder")
        self.assertTrue(result)
        self.assertEqual(plugin._errors_blocked, 1)

    def test_error_while_mmu_busy_is_swallowed(self):
        plugin = make_plugin()
        plugin._model["busy"] = True
        self.assertTrue(plugin.hook_gcode_error(None, "Move out of range: something"))

    def test_klipper_shutdown_is_never_swallowed(self):
        plugin = make_plugin()
        plugin._model["busy"] = True
        for message in ("MCU 'mcu' shutdown: Timer too close",
                        "Lost communication with MCU 'mcu'",
                        "Heater extruder not heating at expected rate",
                        "ADC out of range"):
            self.assertIsNone(plugin.hook_gcode_error(None, message), message)

    def test_unrelated_error_while_idle_passes_through(self):
        plugin = make_plugin()
        self.assertIsNone(plugin.hook_gcode_error(None, "Unknown command: FOO"))

    def test_protection_can_be_turned_off(self):
        plugin = make_plugin(protect_errors=False)
        self.assertIsNone(plugin.hook_gcode_error(None, "MMU issue: anything"))


class TestPrompts(unittest.TestCase):
    def test_dialog_is_collected_and_pushed(self):
        plugin = make_plugin()
        pushed = []
        plugin._push = lambda payload: pushed.append(payload)
        lines = [("prompt_begin", "Happy Hare Error Notice"),
                 ("prompt_text", "MMU issue: something"),
                 ("prompt_button", "UNLOCK|MMU_UNLOCK|secondary"),
                 ("prompt_show", "")]
        for action, params in lines:
            plugin.hook_action(None, "// action:" + action, action, action, params)
        self.assertTrue(pushed)
        dialog = pushed[-1]["prompt"]
        self.assertEqual(dialog["title"], "Happy Hare Error Notice")
        self.assertEqual(dialog["buttons"][0]["gcode"], "MMU_UNLOCK")

    def test_resume_button_goes_through_octoprint(self):
        plugin = make_plugin()
        plugin._push = lambda payload: None
        plugin._prompt_button("RESUME")
        self.assertTrue(plugin._printer.resumed)
        self.assertEqual(plugin._printer.sent, [])

    def test_other_buttons_are_sent_as_gcode(self):
        plugin = make_plugin()
        plugin._push = lambda payload: None
        plugin._prompt_button("MMU_UNLOCK")
        self.assertEqual(plugin._printer.sent, ["MMU_UNLOCK"])

    def test_fallback_state_channel(self):
        plugin = make_plugin()
        plugin.hook_action(None, "", "hh_state",
                           "hh_state", '{"gate": 3, "action": "Loading"}')
        self.assertEqual(plugin._status["mmu"]["gate"], 3)


class TestScriptsAndQueuing(unittest.TestCase):
    def test_cancel_tells_happy_hare(self):
        plugin = make_plugin()
        result = plugin.hook_scripts(None, "gcode", "afterPrintCancelled")
        self.assertEqual(result, (None, ["MMU_PRINT_END STATE=cancelled"]))

    def test_other_scripts_untouched(self):
        plugin = make_plugin()
        self.assertIsNone(plugin.hook_scripts(None, "gcode", "beforePrintStarted"))

    def test_no_mmu_means_no_injection(self):
        plugin = make_plugin()
        plugin._model = {"available": False}
        self.assertIsNone(plugin.hook_scripts(None, "gcode", "afterPrintCancelled"))

    def test_tool_suffix_is_stripped_from_temperature_commands(self):
        plugin = make_plugin()
        result = plugin.hook_gcode_queuing(None, "queuing", "M104 T1 S255", None, "M104")
        self.assertEqual(result, [("M104 S255", None)])

    def test_plain_temperature_commands_are_untouched(self):
        plugin = make_plugin()
        self.assertIsNone(plugin.hook_gcode_queuing(None, "queuing", "M104 S255", None, "M104"))

    def test_tool_changes_are_untouched(self):
        plugin = make_plugin()
        self.assertIsNone(plugin.hook_gcode_queuing(None, "queuing", "T2", None, "T"))


class TestCommandGating(unittest.TestCase):
    def test_motion_is_refused_while_printing(self):
        plugin = make_plugin()
        plugin._model.update({"printing": True, "paused": False})
        response = plugin._run_command("select_gate", {"id": "select_gate", "gate": 2})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(plugin._printer.sent, [])

    def test_motion_is_allowed_while_paused(self):
        plugin = make_plugin()
        plugin._model.update({"printing": True, "paused": True})
        plugin._run_command("select_gate", {"id": "select_gate", "gate": 2})
        self.assertEqual(plugin._printer.sent, ["MMU_SELECT GATE=2"])

    def test_recovery_is_always_allowed(self):
        plugin = make_plugin()
        plugin._model.update({"printing": True, "paused": True})
        plugin._run_command("recover", {"id": "recover", "loaded": False})
        self.assertEqual(plugin._printer.sent, ["MMU_RECOVER LOADED=0"])

    def test_unknown_command_is_refused(self):
        plugin = make_plugin()
        response = plugin._run_command("rm_rf", {"id": "rm_rf"})
        self.assertEqual(response.status_code, 400)

    def test_gate_map_values_are_sanitised(self):
        plugin = make_plugin()
        plugin._set_gate_map({"gate": 1, "material": "ABS'; M112 ;", "color": "#ff0000\nM112",
                              "temperature": 260, "status": 1, "speed": 400})
        sent = plugin._printer.sent[0]
        self.assertEqual(len(plugin._printer.sent), 1)
        # nothing can close the quoted value or start a second command: Klipper
        # splits on quotes, cuts a line at ; or #, and takes one command per line
        for character in ("'", '"', ";", "#", "\n", "\r"):
            self.assertNotIn(character, sent.replace("MATERIAL='", "").replace("' ", " ", 1))
        self.assertIn("MATERIAL='ABS M112 '", sent)   # inert text inside the quotes
        self.assertIn("COLOR=ff0000M112", sent)       # newline stripped, still one token
        self.assertIn("SPEED=150", sent)              # clamped to Happy Hare's range
        self.assertIn("AVAILABLE=1", sent)


class TestPreflight(unittest.TestCase):
    def _plugin_with_file(self, metadata, gates, ttg):
        plugin = make_plugin()
        plugin._file_manager = types.SimpleNamespace(
            get_metadata=lambda origin, path: {"happyhare": metadata})
        plugin._model = {"available": True, "gates": gates, "ttg_map": ttg}
        return plugin

    def test_material_mismatch_is_critical(self):
        gates = [{"index": 0, "status": 1, "status_text": "On spool", "material": "PLA",
                  "color": "00ff00"}]
        plugin = self._plugin_with_file(
            {"tools": [0], "colors": ["000000"], "materials": ["ABS+"], "temps": ["260"]},
            gates, [0])
        result = plugin.preflight("local", "file.gcode")
        self.assertEqual(result["severity"], "critical")
        self.assertIn("material", result["tools"][0]["issues"][0]["text"])

    def test_empty_gate_is_critical(self):
        gates = [{"index": 0, "status": 0, "status_text": "Empty", "material": "ABS+",
                  "color": "000000"}]
        plugin = self._plugin_with_file(
            {"tools": [0], "colors": ["000000"], "materials": ["ABS+"], "temps": ["260"]},
            gates, [0])
        self.assertEqual(plugin.preflight("local", "f.gcode")["severity"], "critical")

    def test_colour_difference_is_only_a_warning(self):
        gates = [{"index": 0, "status": 1, "status_text": "On spool", "material": "ABS+",
                  "color": "ffffff"}]
        plugin = self._plugin_with_file(
            {"tools": [0], "colors": ["000000"], "materials": ["ABS+"], "temps": ["260"]},
            gates, [0])
        self.assertEqual(plugin.preflight("local", "f.gcode")["severity"], "warning")

    def test_matching_file_is_ok(self):
        gates = [{"index": 0, "status": 2, "status_text": "Buffered", "material": "ABS+",
                  "color": "000000"}]
        plugin = self._plugin_with_file(
            {"tools": [0], "colors": ["000000"], "materials": ["ABS+"], "temps": ["260"]},
            gates, [0])
        self.assertEqual(plugin.preflight("local", "f.gcode")["severity"], "ok")

    def test_file_without_metadata(self):
        plugin = make_plugin()
        plugin._file_manager = types.SimpleNamespace(get_metadata=lambda origin, path: {})
        result = plugin.preflight("local", "f.gcode")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
