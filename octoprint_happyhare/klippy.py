# -*- coding: utf-8 -*-
"""A small client for Klipper's API server (the unix domain socket).

Klipper only opens this socket when it is started with ``-a <path>`` (KIAUH style
installs do, and pass ``~/printer_data/comms/klippy.sock``). The protocol is JSON
messages terminated by a single 0x03 byte; several clients may be connected at
once, so this coexists with Moonraker where both are installed.

Modelled on Moonraker's ``klippy_connection.py``:

1. wait for the socket file to appear,
2. connect and poll ``info`` until the state leaves ``startup``,
3. subscribe to the objects we care about and to console output,
4. reconnect from scratch when Klipper restarts (which closes this socket but
   leaves OctoPrint's serial pty alone).

No OctoPrint imports, so it can be exercised on its own.
"""

from __future__ import absolute_import, unicode_literals

import errno
import json
import os
import re
import select
import socket
import threading
import time

TERM = b"\x03"
RECONNECT_DELAY = 2.0
INFO_POLL = 0.25
SOCKET_TIMEOUT = 20.0

DEFAULT_SOCKET_CANDIDATES = (
    "~/printer_data/comms/klippy.sock",
    "/tmp/klippy_uds",
    "~/klipper_config/klippy.sock",
    "/home/pi/printer_data/comms/klippy.sock",
)

LOG_CANDIDATES = (
    "~/printer_data/logs/klippy.log",
    "/tmp/klippy.log",
)

_ARGS_SOCKET = re.compile(r"'-a',\s*'([^']+)'")


def supported():
    """Unix sockets only; on Windows the plugin runs in a degraded mode."""
    return hasattr(socket, "AF_UNIX")


def discover_socket(extra=None):
    """Find Klipper's API socket: explicit setting, then the log, then the usual paths."""
    candidates = []
    if extra:
        candidates.append(extra)
    for path in LOG_CANDIDATES:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r") as handle:
                for line in handle:
                    if line.startswith("Args:"):
                        match = _ARGS_SOCKET.search(line)
                        if match:
                            candidates.append(match.group(1))
                        break
        except (IOError, OSError):
            pass
    candidates.extend(DEFAULT_SOCKET_CANDIDATES)
    for candidate in candidates:
        candidate = os.path.expanduser(candidate)
        if os.path.exists(candidate) and os.access(candidate, os.R_OK | os.W_OK):
            return candidate
    return None


class KlippyError(Exception):
    pass


class KlippyClient(object):
    """Background connection to Klipper's API server."""

    def __init__(self, socket_path, objects, on_status=None, on_gcode=None,
                 on_link=None, logger=None, client_name="octoprint_happyhare"):
        self.socket_path = socket_path
        self.objects = list(objects)
        self.on_status = on_status or (lambda status, is_snapshot: None)
        self.on_gcode = on_gcode or (lambda line: None)
        self.on_link = on_link or (lambda connected, message: None)
        self.logger = logger
        self.client_name = client_name

        self._thread = None
        self._stop = threading.Event()
        self._sock = None
        self._buffer = b""
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending = {}          # request id -> (event, holder)
        self._outbox = []
        self._outbox_lock = threading.Lock()
        self.connected = False
        self.klippy_state = "disconnected"
        self.klippy_message = ""
        self.info = {}
        self.available_objects = []

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="happyhare-klippy")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._close()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    # -- requests ----------------------------------------------------------
    def _new_id(self):
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
        return request_id

    def request(self, method, params=None, timeout=SOCKET_TIMEOUT):
        """Send a request and wait for its reply. Raises KlippyError on failure."""
        if not self.connected:
            raise KlippyError("not connected to Klipper")
        request_id = self._new_id()
        event = threading.Event()
        holder = {}
        self._pending[request_id] = (event, holder)
        self._write({"id": request_id, "method": method, "params": params or {}})
        if not event.wait(timeout):
            self._pending.pop(request_id, None)
            raise KlippyError("timed out waiting for %s" % method)
        self._pending.pop(request_id, None)
        if "error" in holder:
            raise KlippyError(holder["error"])
        return holder.get("result", {})

    def run_gcode(self, script, timeout=SOCKET_TIMEOUT):
        """Run G-code through Klipper directly.

        Note the plugin normally sends commands through OctoPrint's queue instead,
        so they show up in the terminal; this is here for the cases where a reply
        is needed.
        """
        return self.request("gcode/script", {"script": script}, timeout=timeout)

    def query(self, objects, timeout=SOCKET_TIMEOUT):
        params = {"objects": {name: None for name in objects}}
        return self.request("objects/query", params, timeout=timeout)

    # -- thread ------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._connect()
                self._handshake()
                self._read_loop()
            except KlippyError as error:
                self._log("debug", "Klipper link: %s" % error)
            except Exception as error:  # noqa: BLE001 - background thread must not die
                self._log("debug", "Klipper link error: %s" % error)
            finally:
                was_connected = self.connected
                self._close()
                if was_connected:
                    self.on_link(False, self.klippy_message or "connection closed")
            if self._stop.wait(RECONNECT_DELAY):
                break

    def _connect(self):
        path = self.socket_path
        if not path or not os.path.exists(path):
            path = discover_socket(self.socket_path)
            if not path:
                raise KlippyError("no Klipper API socket found")
            self.socket_path = path
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(path)
        sock.setblocking(False)
        self._sock = sock
        self._buffer = b""
        self._log("info", "Connected to Klipper API socket at %s" % path)

    def _handshake(self):
        # info is answered even while Klipper is still starting up
        deadline = time.time() + 120
        while not self._stop.is_set() and time.time() < deadline:
            self.connected = True                    # allows request() to write
            try:
                info = self._request_blocking("info", {"client_info": {
                    "name": self.client_name, "version": "1"}})
            except KlippyError:
                self.connected = False
                raise
            self.info = info
            self.klippy_state = info.get("state", "startup")
            self.klippy_message = info.get("state_message", "")
            if self.klippy_state != "startup":
                break
            time.sleep(INFO_POLL)

        if self.klippy_state not in ("ready",):
            self._log("info", "Klipper reports state '%s'" % self.klippy_state)

        # Subscribing to an object Klipper does not have fails the whole request,
        # and half of these are optional (no encoder, no MMU at all), so ask first.
        listed = self._request_blocking("objects/list", {}).get("objects", [])
        wanted = [name for name in self.objects if name in listed] or ["webhooks"]
        self.available_objects = list(listed)
        params = {
            "objects": {name: None for name in wanted},
            "response_template": {"method": "hh_status"},
        }
        result = self._request_blocking("objects/subscribe", params)
        self._request_blocking("gcode/subscribe_output", {"response_template": {
            "method": "hh_gcode"}})
        self.on_link(True, self.klippy_state)
        status = (result or {}).get("status", {})
        if status:
            self.on_status(status, True)

    def _request_blocking(self, method, params):
        """Handshake helper: write and pump the socket until the reply lands."""
        request_id = self._new_id()
        self._write_now({"id": request_id, "method": method, "params": params or {}})
        deadline = time.time() + SOCKET_TIMEOUT
        while time.time() < deadline:
            if self._stop.is_set():
                raise KlippyError("stopping")
            for message in self._read_available(0.2):
                if message.get("id") == request_id:
                    if "error" in message:
                        raise KlippyError(_error_text(message["error"]))
                    return message.get("result", {})
                self._dispatch(message)
        raise KlippyError("timed out waiting for %s" % method)

    def _read_loop(self):
        while not self._stop.is_set():
            for message in self._read_available(0.5):
                self._dispatch(message)
            self._flush_outbox()

    def _read_available(self, timeout):
        """Read whatever is on the socket, return decoded messages."""
        sock = self._sock
        if sock is None:
            raise KlippyError("socket closed")
        readable, _, _ = select.select([sock], [], [], timeout)
        messages = []
        if not readable:
            return messages
        try:
            chunk = sock.recv(65536)
        except socket.error as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return messages
            raise KlippyError("recv failed: %s" % error)
        if not chunk:
            raise KlippyError("Klipper closed the connection")
        self._buffer += chunk
        while TERM in self._buffer:
            raw, self._buffer = self._buffer.split(TERM, 1)
            if not raw.strip():
                continue
            try:
                messages.append(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                self._log("debug", "Ignoring malformed frame from Klipper")
        return messages

    def _dispatch(self, message):
        request_id = message.get("id")
        if request_id is not None and request_id in self._pending:
            event, holder = self._pending[request_id]
            if "error" in message:
                holder["error"] = _error_text(message["error"])
            else:
                holder["result"] = message.get("result", {})
            event.set()
            return

        params = message.get("params") or {}
        if message.get("method") == "hh_gcode" or "response" in params:
            line = params.get("response")
            if line:
                self.on_gcode(line)
            return
        if "status" in params:
            self.on_status(params["status"], False)
            webhooks = params["status"].get("webhooks")
            if isinstance(webhooks, dict):
                self.klippy_state = webhooks.get("state", self.klippy_state)
                self.klippy_message = webhooks.get("state_message", "")
                if self.klippy_state in ("shutdown", "error"):
                    self.on_link(False, self.klippy_message or self.klippy_state)

    # -- writing -----------------------------------------------------------
    def _write(self, payload):
        with self._outbox_lock:
            self._outbox.append(payload)

    def _flush_outbox(self):
        with self._outbox_lock:
            pending, self._outbox = self._outbox, []
        for payload in pending:
            self._write_now(payload)

    def _write_now(self, payload):
        sock = self._sock
        if sock is None:
            raise KlippyError("socket closed")
        data = json.dumps(payload).encode("utf-8") + TERM
        while data:
            _, writable, _ = select.select([], [sock], [], 5.0)
            if not writable:
                raise KlippyError("timed out writing to Klipper")
            try:
                sent = sock.send(data)
            except socket.error as error:
                raise KlippyError("send failed: %s" % error)
            data = data[sent:]

    def _close(self):
        self.connected = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
        for event, holder in list(self._pending.values()):
            holder["error"] = "connection closed"
            event.set()
        self._pending.clear()

    def _log(self, level, message):
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message)


def _error_text(error):
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)
    return str(error)
