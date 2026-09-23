# -*- coding: utf-8 -*-
"""Tests for the parts of the plugin that do not need OctoPrint.

Run with:  python -m unittest discover -s tests
"""

import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load(name):
    """Load one plugin module by path.

    Importing the package would pull in octoprint_happyhare/__init__.py, which
    needs OctoPrint itself; these three modules are deliberately stdlib only.
    """
    path = os.path.join(ROOT, "octoprint_happyhare", name + ".py")
    spec = importlib.util.spec_from_file_location("hh_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


model = _load("model")
preprocessor = _load("preprocessor")
prompts = _load("prompts")


# --------------------------------------------------------------------------
# Status normalisation
# --------------------------------------------------------------------------

V3_STATUS = {
    "mmu": {
        "enabled": True,
        "num_gates": 4,
        "is_homed": True,
        "print_state": "printing",
        "action": "Loading",
        "tool": 2,
        "gate": 2,
        "last_tool": 0,
        "next_tool": 2,
        "filament": "Unloaded",
        "filament_pos": 3,
        "filament_direction": 1,
        "bowden_progress": 42,
        "num_toolchanges": 17,
        "reason_for_pause": "",
        "servo": "Down",
        "has_bypass": True,
        "sync_drive": False,
        "ttg_map": [0, 1, 2, 3],
        "endless_spool_groups": [1, 1, 2, 2],
        "gate_status": [1, 0, 2, -1],
        "gate_color": ["000000", "ff0000", "ffffff", ""],
        "gate_material": ["ABS+", "PLA", "PETG", "ABS"],
        "gate_temperature": [260, 215, 240, 255],
        "gate_spool_id": [-1, -1, 5, -1],
        "gate_speed_override": [100, 100, 90, 100],
        "gate_filament_name": ["", "", "Prusament", ""],
        "sensors": {"mmu_gate": True, "extruder": False, "toolhead": None},
        "encoder": {"headroom": 7.4, "desired_headroom": 5.0, "flow_rate": 98},
    },
    "mmu_machine": {"mmu_vendor": "ERCF", "mmu_version": "2.0", "num_gates": 4},
    "save_variables": {"variables": {
        "mmu_calibration_bowden_lengths": [1016.4, 1016.4, 1016.4, 1016.4],
        "mmu_selector_offsets": [3.2, 26.0, 48.7, 71.5],
        "mmu_selector_bypass": 267.27,
        "mmu_statistics_gate_0": {"loads": 108, "load_failures": 26, "quality": 0.864},
        "mmu_statistics_counters": {"servo_down": {"count": 1048, "limit": 5000,
                                                   "warning": "Inspect servo arm"}},
        "mmu_statistics_swaps": {"total_swaps": 156, "load": 100.0, "unload": 50.0},
    }},
}

V4_STATUS = {
    "mmu": {
        "enabled": True,
        "num_gates": 2,
        "print_state": "pause_locked",
        "action": "Idle",
        "tool": -1,
        "gate": 1,
        "filament": "Unknown",
        "filament_pos": -1,
        "reason_for_pause": "No movement detected by encoder",
        "selector": {"servo": "Up", "grip": "Released", "has_bypass": False},
        "ttg_map": [0, 1],
        "endless_spool_groups": [1, 1],
        "gate_status": [2, 1],
        "gate_color": ["darkgreen", "112233ff"],
        "gate_material": ["PLA", "PLA"],
        "gate_temperature": [215, 215],
        "gate_vendor": ["Polymaker", ""],
        "sensors": {"mmu_entry": True, "mmu_shared_exit": False, "toolhead": True},
    },
    "mmu_machine": {
        "happy_hare_version": "4.0.0",
        "num_units": 1,
        "num_gates": 2,
        "unit_0": {"name": "unit0", "vendor": "BoxTurtle", "version": "1.0",
                   "selector_type": "VirtualSelector", "num_gates": 2, "first_gate": 0,
                   "has_bypass": False},
    },
}


class TestModel(unittest.TestCase):
    def test_v3_shape(self):
        state = model.normalize(V3_STATUS, {}, V3_STATUS["save_variables"])
        self.assertEqual(state["generation"], 3)
        self.assertEqual(state["vendor"], "ERCF")
        self.assertTrue(state["has_selector"])
        self.assertEqual(state["servo"], "Down")           # v3 keeps it top level
        self.assertEqual(state["num_gates"], 4)
        self.assertEqual(state["filament_pos_name"], "In bowden")
        self.assertTrue(state["printing"])
        self.assertFalse(state["paused"])
        self.assertAlmostEqual(state["bowden_length"], 1016.4)
        self.assertEqual(state["selector_offsets"]["bypass"], 267.27)

    def test_v3_gates(self):
        state = model.normalize(V3_STATUS, {}, V3_STATUS["save_variables"])
        gates = state["gates"]
        self.assertEqual(len(gates), 4)
        self.assertEqual(gates[0]["rgb"], "#000000")
        self.assertTrue(gates[0]["dark"])                  # black needs the halo
        self.assertEqual(gates[0]["status_text"], "On spool")
        self.assertEqual(gates[1]["status_text"], "Empty")
        self.assertEqual(gates[3]["rgb"], None)            # no colour set
        self.assertEqual(gates[2]["tools"], [2])
        self.assertEqual(gates[0]["stats"]["loads"], 108)

    def test_v4_shape(self):
        state = model.normalize(V4_STATUS)
        self.assertEqual(state["generation"], 4)
        self.assertEqual(state["vendor"], "BoxTurtle")
        self.assertFalse(state["has_selector"])            # type B draws lanes
        self.assertEqual(state["servo"], "Up")             # v4 nests it under selector
        self.assertTrue(state["locked"])
        self.assertTrue(state["paused"])
        self.assertEqual(state["gates"][0]["rgb"], "#006400")   # W3C name
        self.assertEqual(state["gates"][1]["rgb"], "#112233")   # RRGGBBAA

    def test_sensor_names_are_unified(self):
        v3 = {sensor["id"]: sensor["state"]
              for sensor in model.normalize(V3_STATUS)["sensors"]}
        v4 = {sensor["id"]: sensor["state"]
              for sensor in model.normalize(V4_STATUS)["sensors"]}
        self.assertEqual(v3["gate_shared"], True)          # v3 mmu_gate
        self.assertEqual(v4["gate_shared"], False)         # v4 mmu_shared_exit
        self.assertEqual(v4["gate_entry"], True)           # v4 mmu_entry
        self.assertIsNone(v3["toolhead"])                  # disabled sensor stays None

    def test_gate_suffixed_sensor(self):
        status = {"mmu": {"sensors": {"mmu_entry_3": True}}}
        sensors = model.normalize(status)["sensors"]
        self.assertEqual(sensors[0]["id"], "gate_entry")
        self.assertEqual(sensors[0]["raw"], "mmu_entry_3")

    def test_merge_is_one_level_deep(self):
        cache = {"mmu": {"gate": 0, "gate_status": [1, 1]}}
        model.merge_status(cache, {"mmu": {"gate_status": [1, 0]}})
        self.assertEqual(cache["mmu"]["gate"], 0)          # untouched field survives
        self.assertEqual(cache["mmu"]["gate_status"], [1, 0])

    def test_is_ready_guards_the_first_snapshot(self):
        self.assertFalse(model.is_ready({"gate": 0}))
        self.assertTrue(model.is_ready(V3_STATUS["mmu"]))

    def test_unknown_mmu_is_not_available(self):
        state = model.normalize({})
        self.assertFalse(state["available"])
        self.assertEqual(state["gates"], [])


# --------------------------------------------------------------------------
# Prompt parsing
# --------------------------------------------------------------------------

HH_DIALOG = [
    ("prompt_begin", "Happy Hare Error Notice"),
    ("prompt_text", "MMU issue: Filament not seen by encoder"),
    ("prompt_text", "Reason: No movement detected"),
    ("prompt_button_group_start", ""),
    ("prompt_button", "UNLOCK|MMU_UNLOCK|secondary"),
    ("prompt_button", "RESUME|RESUME|warning"),
    ("prompt_button_group_end", ""),
    ("prompt_show", ""),
]


class TestPrompts(unittest.TestCase):
    def test_happy_hare_error_dialog(self):
        parser = prompts.PromptParser()
        results = [parser.handle(action, params) for action, params in HH_DIALOG]
        self.assertEqual(results[-1], "show")
        dialog = parser.dialog
        self.assertEqual(dialog["title"], "Happy Hare Error Notice")
        self.assertEqual(len(dialog["text"]), 2)
        self.assertEqual(dialog["buttons"][0],
                         {"label": "UNLOCK", "gcode": "MMU_UNLOCK", "style": "secondary"})
        self.assertEqual(dialog["buttons"][1]["style"], "warning")

    def test_prompt_end_closes(self):
        parser = prompts.PromptParser()
        for action, params in HH_DIALOG:
            parser.handle(action, params)
        self.assertEqual(parser.handle("prompt_end", ""), "close")
        self.assertIsNone(parser.dialog)

    def test_label_only_button(self):
        parser = prompts.PromptParser()
        parser.handle("prompt_begin", "Test")
        parser.handle("prompt_button", "Just a label")
        parser.handle("prompt_show", "")
        self.assertEqual(parser.dialog["buttons"][0]["gcode"], "")

    def test_stray_actions_are_ignored(self):
        parser = prompts.PromptParser()
        self.assertIsNone(parser.handle("prompt_text", "no dialog open"))
        self.assertIsNone(parser.dialog)


# --------------------------------------------------------------------------
# Upload pre-processing
# --------------------------------------------------------------------------

ORCA_FILE = """; generated by OrcaSlicer 2.3.2 on 2026-09-01
; filament_colour = #000000;#FFFFFF;#FF0000
; nozzle_temperature = 260,260,255
; filament_type = ABS+;ABS+;ABS
; filament_settings_id = "Voron ABS";"Generic ABS";"Red ABS"
; flush_volumes_matrix = 0,140,180,150,0,190,190,150,0
; flush_multiplier = 1
MMU_START_SETUP INITIAL_TOOL=0 REFERENCED_TOOLS=!referenced_tools! TOOL_COLORS=!colors! TOOL_TEMPS=!temperatures! TOOL_MATERIALS=!materials! FILAMENT_NAMES=!filament_names! PURGE_VOLUMES=!purge_volumes! TOTAL_TOOLCHANGES=!total_toolchanges!
T0
G1 X10 Y10
T2
G1 X20 Y20
T0
"""


class TestPreprocessor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, content, name="in.gcode"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def test_orca_file_is_substituted(self):
        source = self._write(ORCA_FILE)
        target = os.path.join(self.tmp, "out.gcode")
        changed, metadata = preprocessor.process_file(source, target)
        self.assertTrue(changed)
        self.assertEqual(metadata["slicer"], "OrcaSlicer")
        self.assertEqual(metadata["tools"], [0, 2])
        self.assertEqual(metadata["total_toolchanges"], 3)
        self.assertEqual(metadata["colors"], ["000000", "FFFFFF", "FF0000"])
        self.assertEqual(metadata["materials"], ["ABS+", "ABS+", "ABS"])

        with open(target) as handle:
            result = handle.read()
        self.assertTrue(result.startswith(preprocessor.FINGERPRINT))
        self.assertNotIn("!referenced_tools!", result)
        self.assertIn("REFERENCED_TOOLS=0,2", result)
        self.assertIn("TOOL_COLORS=000000,FFFFFF,FF0000", result)
        self.assertIn("TOOL_TEMPS=260,260,255", result)
        self.assertIn("PURGE_VOLUMES=0,140,180,150,0,190,190,150,0", result)

    def test_already_processed_file_is_left_alone(self):
        source = self._write(preprocessor.FINGERPRINT + "\n" + ORCA_FILE)
        target = os.path.join(self.tmp, "out.gcode")
        changed, metadata = preprocessor.process_file(source, target)
        self.assertFalse(changed)
        self.assertEqual(metadata["skipped"], "already processed")
        self.assertFalse(os.path.exists(target))

    def test_file_without_placeholders_is_left_alone(self):
        source = self._write("; generated by PrusaSlicer 2.9.0\nG28\nT0\n")
        target = os.path.join(self.tmp, "out.gcode")
        changed, metadata = preprocessor.process_file(source, target)
        self.assertFalse(changed)
        self.assertEqual(metadata["skipped"], "no placeholders")
        self.assertEqual(metadata["tools"], [0])

    def test_unknown_slicer_is_left_alone(self):
        source = self._write("; sliced by something else\nT0\n!colors!\n")
        target = os.path.join(self.tmp, "out.gcode")
        changed, metadata = preprocessor.process_file(source, target)
        self.assertFalse(changed)
        self.assertEqual(metadata["skipped"], "unsupported slicer")

    def test_flush_multiplier_applies_below_orca_232(self):
        # matrix first, multiplier after: the order this file happens to use
        content = ORCA_FILE.replace("OrcaSlicer 2.3.2", "OrcaSlicer 2.3.0") \
                           .replace("; flush_multiplier = 1", "; flush_multiplier = 2")
        found = preprocessor.scan(iter(content.splitlines(True)))
        self.assertEqual(found["purge_volumes"][1], "280")

    def test_flush_multiplier_is_applied_once_whatever_the_order(self):
        # real Orca files list flush_multiplier *before* flush_volumes_matrix
        content = ORCA_FILE.replace("OrcaSlicer 2.3.2", "OrcaSlicer 2.3.0") \
                           .replace("; flush_multiplier = 1\n", "")
        content = content.replace("; flush_volumes_matrix",
                                  "; flush_multiplier = 2\n; flush_volumes_matrix")
        found = preprocessor.scan(iter(content.splitlines(True)))
        self.assertEqual(found["purge_volumes"][1], "280")   # not 560

    def test_flush_multiplier_is_not_double_applied_on_new_orca(self):
        content = ORCA_FILE.replace("; flush_multiplier = 1", "; flush_multiplier = 2")
        found = preprocessor.scan(iter(content.splitlines(True)))
        self.assertEqual(found["purge_volumes"][1], "140")

    def test_placeholders_in_comments_are_not_substituted(self):
        content = ORCA_FILE + "; a comment mentioning !colors!\n"
        source = self._write(content)
        target = os.path.join(self.tmp, "out.gcode")
        preprocessor.process_file(source, target)
        with open(target) as handle:
            lines = handle.read().splitlines()
        self.assertIn("; a comment mentioning !colors!", lines)


if __name__ == "__main__":
    unittest.main()
