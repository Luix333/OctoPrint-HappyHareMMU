# -*- coding: utf-8 -*-
"""Parser for Klipper/Mainsail style ``action:prompt_*`` dialogs.

Happy Hare raises its pause dialog with ``_MMU_ERROR_DIALOG``, which emits, via
``RESPOND TYPE=command`` (so every line reaches OctoPrint prefixed with ``// ``)::

    action:prompt_begin Happy Hare Error Notice
    action:prompt_text MMU issue: ...
    action:prompt_text Reason: ...
    action:prompt_button_group_start
    action:prompt_button UNLOCK|MMU_UNLOCK|secondary
    action:prompt_button RESUME|RESUME|warning
    action:prompt_button_group_end
    action:prompt_show

OctoPrint's bundled Action Command Prompt plugin cannot show this: it is gated on
a ``PROMPT_SUPPORT`` capability Klipper never reports, it treats the whole
``LABEL|GCODE|style`` string as a label, and it answers with ``M876``, which
Klipper does not implement. So the plugin parses the dialog itself.

Stdlib only, so it can be unit tested without OctoPrint.
"""

from __future__ import absolute_import, unicode_literals

BUTTON_STYLES = ("primary", "secondary", "info", "warning", "error", "danger")


class PromptParser(object):
    """Accumulates ``prompt_*`` actions into a dialog description."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._draft = None
        self.dialog = None      # set once prompt_show arrives

    # -- feeding -----------------------------------------------------------
    def handle(self, action, params=""):
        """Feed one action command. Returns "show", "close" or None."""
        action = (action or "").strip()
        params = (params or "").strip()

        if action == "prompt_begin":
            self._draft = {
                "title": params or "Printer prompt",
                "text": [],
                "buttons": [],
                "footer": [],
                "group": False,
            }
            return None

        if self._draft is None:
            if action == "prompt_end":
                self.dialog = None
                return "close"
            return None

        if action == "prompt_text":
            if params:
                self._draft["text"].append(params)
        elif action in ("prompt_button", "prompt_footer_button"):
            button = _parse_button(params)
            if button:
                key = "footer" if action == "prompt_footer_button" else "buttons"
                self._draft[key].append(button)
        elif action == "prompt_choice":
            button = _parse_button(params)
            if button:
                self._draft["buttons"].append(button)
        elif action == "prompt_button_group_start":
            self._draft["group"] = True
        elif action == "prompt_button_group_end":
            self._draft["group"] = False
        elif action == "prompt_show":
            self.dialog = dict(self._draft)
            self._draft = None
            return "show"
        elif action == "prompt_end":
            self._draft = None
            self.dialog = None
            return "close"
        return None


def _parse_button(params):
    """``LABEL|GCODE|style`` -> dict. Label alone is allowed."""
    if not params:
        return None
    parts = [part.strip() for part in params.split("|")]
    label = parts[0]
    if not label:
        return None
    gcode = parts[1] if len(parts) > 1 else ""
    style = parts[2].lower() if len(parts) > 2 else "secondary"
    if style not in BUTTON_STYLES:
        style = "secondary"
    return {"label": label, "gcode": gcode, "style": style}


def is_prompt_action(action):
    return (action or "").startswith("prompt_")
