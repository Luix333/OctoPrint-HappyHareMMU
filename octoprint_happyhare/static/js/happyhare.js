/*
 * Happy Hare MMU for OctoPrint — front end.
 *
 * The plugin pushes a normalised model (see model.py) over the socket; everything
 * here renders that model. SVG is drawn imperatively rather than through Knockout
 * bindings because the gate rail and filament path are geometry, not lists.
 */
$(function () {
    "use strict";

    var SVG_NS = "http://www.w3.org/2000/svg";

    function el(tag, attrs, kids) {
        var node = document.createElement(tag);
        attrs = attrs || {};
        Object.keys(attrs).forEach(function (key) {
            if (attrs[key] === null || attrs[key] === undefined) return;
            if (key === "class") node.className = attrs[key];
            else if (key === "text") node.textContent = attrs[key];
            else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), attrs[key]);
            else node.setAttribute(key, attrs[key]);
        });
        (kids || []).forEach(function (kid) {
            node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
        });
        return node;
    }

    function svg(tag, attrs, kids) {
        var node = document.createElementNS(SVG_NS, tag);
        attrs = attrs || {};
        Object.keys(attrs).forEach(function (key) {
            if (attrs[key] === null || attrs[key] === undefined) return;
            node.setAttribute(key, attrs[key]);
        });
        (kids || []).forEach(function (kid) { node.appendChild(kid); });
        return node;
    }

    function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
    function byId(id) { return document.getElementById(id); }

    /*
     * Write-if-different setters. The server pushes the model several times a
     * second while a print runs (the encoder fields change on every Klipper
     * tick), and a DOM write queues a mutation and a restyle even when it stores
     * the value already there. Everything that runs per push goes through these,
     * so an unchanged value costs a read and nothing else.
     */
    function text(node, value) {
        if (!node) return;
        value = value === undefined || value === null ? "" : String(value);
        if (node.textContent !== value) node.textContent = value;
    }

    function attr(node, name, value) {
        if (!node) return;
        if (value === null || value === undefined) {
            if (node.hasAttribute(name)) node.removeAttribute(name);
        } else if (node.getAttribute(name) !== String(value)) {
            node.setAttribute(name, value);
        }
    }

    // properties such as hidden, disabled, className and title
    function prop(node, name, value) {
        if (node && node[name] !== value) node[name] = value;
    }

    // the browser normalises what style stores, so compare with what was written
    function css(node, name, value) {
        if (!node) return;
        var written = node.hhStyle || (node.hhStyle = {});
        if (written[name] === value) return;
        written[name] = value;
        node.style[name] = value;
    }

    function show(node, visible) { attr(node, "display", visible ? null : "none"); }

    /*
     * Keep a container's children in step with a list. The node at each position
     * is built once by `make` and refilled by `fill` through the setters above,
     * so a list whose items did not change is left alone and only a change in
     * length adds or removes nodes.
     */
    function syncList(box, items, make, fill) {
        if (!box) return;
        if (!box.hhList) {
            clear(box);
            box.hhList = true;
        }
        while (box.children.length > items.length) box.removeChild(box.lastElementChild);
        items.forEach(function (item, index) {
            var node = box.children[index];
            if (!node) {
                node = make();
                box.appendChild(node);
            }
            fill(node, item, index);
        });
    }

    function esLetter(group) {
        return String.fromCharCode(64 + (group || 0));
    }

    function statusColor(status) {
        if (status === 2) return "var(--hh-accent)";
        if (status === 1) return "var(--hh-ok)";
        if (status === 0) return "var(--hh-crit)";
        return "var(--hh-warn)";
    }

    // Where the filament sits along the drawn path, per Happy Hare's filament_pos
    var PATH_X = {gate: 56, encoder: 150, bstart: 205, bend: 600, entry: 672,
                  gears: 740, ts: 826, nozzle: 944};

    function HappyHareViewModel(parameters) {
        var self = this;
        self.settings = parameters[0];
        self.loginState = parameters[1];
        self.access = parameters[2];

        self.state = {};
        self.consoleLines = [];
        self.prompt = null;
        self.link = {connected: false, message: "starting"};
        self.errorsBlocked = 0;
        self.view = "operate";
        self.recoveryStep = 0;
        self.preflightFiles = [];

        self.linkText = ko.observable("not connected");
        self.startGcodeSnippet = ko.observable(
            "MMU_START_SETUP INITIAL_TOOL={initial_tool} REFERENCED_TOOLS=!referenced_tools!" +
            " TOOL_COLORS=!colors! TOOL_TEMPS=!temperatures! TOOL_MATERIALS=!materials!" +
            " FILAMENT_NAMES=!filament_names! PURGE_VOLUMES=!purge_volumes!" +
            " TOTAL_TOOLCHANGES=!total_toolchanges!");

        // ------------------------------------------------------------------
        // permissions
        // ------------------------------------------------------------------
        self.canControl = function () {
            try {
                return self.loginState.hasPermission(self.access.permissions.PLUGIN_HAPPYHARE_CONTROL);
            } catch (error) {
                return self.loginState.isUser();
            }
        };

        // ------------------------------------------------------------------
        // talking to the plugin
        // ------------------------------------------------------------------
        self.command = function (id, payload, description) {
            if (!self.canControl()) return;
            var data = $.extend({id: id}, payload || {});
            var send = function () {
                OctoPrint.simpleApiCommand("happyhare", "command", data).fail(function (response) {
                    var message = (response && response.responseJSON && response.responseJSON.error)
                        || "command refused";
                    new PNotify({title: "Happy Hare", text: message, type: "error"});
                });
            };
            var needsConfirm = description && self.settingValue("confirm_moves", true);
            if (needsConfirm) {
                showConfirmationDialog({
                    message: description,
                    onproceed: send,
                    proceed: "Run it",
                    title: "Happy Hare MMU"
                });
            } else {
                send();
            }
        };

        self.settingValue = function (key, fallback) {
            try {
                var value = self.settings.settings.plugins.happyhare[key]();
                return value === undefined ? fallback : value;
            } catch (error) {
                return fallback;
            }
        };

        // ------------------------------------------------------------------
        // socket messages
        // ------------------------------------------------------------------
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "happyhare" || !data) return;
            switch (data.type) {
                case "state":
                    self.applyState(data.state);
                    break;
                case "console":
                    self.appendConsole(data.line);
                    break;
                case "prompt":
                    self.prompt = data.prompt;
                    self.renderPrompt();
                    break;
                case "protected":
                    self.errorsBlocked = data.count;
                    self.renderProtected();
                    break;
                case "link":
                    self.link = data.link;
                    self.renderLink();
                    break;
            }
        };

        self.applyState = function (state) {
            if (!state) return;
            var previous = self.state || {};
            self.state = state;
            if (state.link) self.link = state.link;
            if (state.errors_blocked !== undefined) self.errorsBlocked = state.errors_blocked;

            if (state.paused && !previous.paused) {
                self.recoveryStep = state.locked ? 0 : 1;
            }
            if (!state.paused) self.recoveryStep = 0;

            self.render();
        };

        self.refresh = function () {
            OctoPrint.simpleApiGet("happyhare").done(function (data) {
                if (data && data.console) {
                    self.consoleLines = data.console;
                    self.renderConsole();
                }
                if (data && data.prompt) {
                    self.prompt = data.prompt;
                    self.renderPrompt();
                }
                self.applyState(data);
            });
        };

        // ------------------------------------------------------------------
        // density: follow UI Customizer and the panel's own width, not the viewport
        // ------------------------------------------------------------------
        self.applyDensity = function () {
            var setting = self.settingValue("density", "auto");
            var compact;
            if (setting === "compact") compact = true;
            else if (setting === "cozy") compact = false;
            else {
                var root = byId("hh-root");
                var width = root ? root.clientWidth : 0;
                compact = document.body.classList.contains("UICResponsiveMode")
                    || (width > 0 && width < 900);
            }
            ["hh-root", "hh-side"].forEach(function (id) {
                var node = byId(id);
                if (node) node.classList.toggle("hh-compact", !!compact);
            });
            self.compact = !!compact;
        };

        // ------------------------------------------------------------------
        // rendering
        // ------------------------------------------------------------------
        self.render = function () {
            self.applyDensity();
            self.renderOffline();
            self.renderStrip();
            self.renderRail();
            self.renderPath();
            self.renderTools();
            self.renderSide();
            self.renderSensors();
            self.renderNavbar();
            self.renderRecovery();
            self.renderGateTable();
            self.renderTtgTable();
            self.renderHealth();
            self.renderLink();
            self.renderProtected();
        };

        self.renderOffline = function () {
            var box = byId("hh-offline");
            if (!box) return;
            var available = self.state && self.state.available;
            prop(box, "hidden", !!available);
            if (!available) {
                text(byId("hh-offline-reason"),
                    self.link && self.link.connected
                        ? "Connected to Klipper, but no [mmu] object — is Happy Hare installed?"
                        : (self.link && self.link.message) || "Waiting for Klipper…");
            }
        };

        self.renderStrip = function () {
            var box = byId("hh-strip");
            if (!box || !self.state.available) return;
            var state = self.state;
            var tiles = [
                ["Print state", state.print_state || "—", state.locked ? "crit" : state.printing ? "ok" : ""],
                ["Action", state.action || "—", state.busy ? "act" : ""],
                ["Tool → gate", (state.tool < 0 ? "—" : "T" + state.tool) + " → "
                    + (state.gate < 0 ? "—" : "G" + state.gate), ""],
                ["Filament", state.filament || "—", state.filament === "Loaded" ? "ok"
                    : state.filament === "Unknown" ? "warn" : ""],
                ["Position", state.filament_pos_name || "—", ""],
                ["Toolchanges", String(state.num_toolchanges || 0), ""]
            ];
            if (state.encoder && state.encoder.flow_rate !== undefined) {
                tiles.push(["Encoder flow", state.encoder.flow_rate + " %",
                    state.encoder.flow_rate < 80 ? "warn" : ""]);
            }
            if (state.has_selector) {
                tiles.push(["Selector", state.servo ? "servo " + String(state.servo).toLowerCase()
                    : (state.grip || "—"), ""]);
            }
            syncList(box, tiles, function () {
                return el("div", {class: "hh-tile"}, [el("span", {class: "hh-label"}), el("b")]);
            }, function (node, tile) {
                prop(node, "className", "hh-tile" + (tile[2] ? " sev-" + tile[2] : ""));
                text(node.firstChild, tile[0]);
                text(node.lastChild, tile[1]);
            });
        };

        // -- selector rail / lanes ----------------------------------------
        // everything buildRail draws; the rest (selection, carriage) is updateRail's
        self.railSignature = function () {
            var state = self.state;
            return JSON.stringify([self.compact, state.num_gates, state.selector_type, state.vendor,
                state.hw_version, state.has_selector, state.has_bypass, state.selector_offsets,
                (state.gates || []).map(function (gate) {
                    return [gate.rgb, gate.status, gate.status_text, gate.dark, gate.material,
                        gate.temperature, gate.group, gate.tools];
                })]);
        };

        self.renderRail = function () {
            var box = byId("hh-rail");
            if (!box || !self.state.available) return;
            // the cache lives on the box, so every instance of this view model
            // updates the one drawing instead of rebuilding it in turn
            var cache = box.hhRail;
            var signature = self.railSignature();
            if (!cache || cache.signature !== signature) self.buildRail(signature);
            self.updateRail();
        };

        self.updateRail = function () {
            var cache = byId("hh-rail").hhRail;
            if (!cache) return;
            var state = self.state;
            text(byId("hh-rail-hint"), state.has_selector
                ? (state.servo ? "servo " + String(state.servo).toLowerCase() + " · " : "")
                    + (state.is_homed ? "homed" : "not homed")
                : "one gear per lane · no selector");
            cache.plates.forEach(function (plate, index) {
                var selected = index === state.gate;
                attr(plate, "fill", selected ? "var(--hh-accent-soft)" : "var(--hh-surface-2)");
                attr(plate, "stroke", selected ? "var(--hh-accent)" : "var(--hh-line)");
                attr(plate, "stroke-width", selected ? 2 : 1);
            });
            if (cache.carriage) {
                var target = state.gate >= 0 ? cache.gx(state.gate)
                    : (state.gate === -2 && cache.bypassX ? cache.bypassX : cache.gx(0));
                css(cache.carriage, "transform", "translateX(" + (target - cache.baseX) + "px)");
                text(cache.servoLabel, state.servo === "Down" ? "GRIP"
                    : state.servo === "Move" ? "MOVE" : state.servo === "Up" ? "UP"
                    : (state.grip === "Gripped" ? "GRIP" : "—"));
            }
        };

        self.buildRail = function (signature) {
            var box = byId("hh-rail");
            var state = self.state;
            var gates = state.gates || [];
            var count = gates.length;
            clear(box);
            text(byId("hh-rail-title"), "Selector — " + (state.vendor || "MMU") + " "
                + (state.hw_version || "") + ", " + count
                + (state.has_selector ? " gates" : " lanes"));
            if (!count) return;

            var compact = self.compact;
            var M = compact
                ? {yDisc: 42, plateY: -24, plateH: 64, plateW: 42, r: 14, inner: 5, num: -15,
                   tool: 24, mat: null, railY: 28, carApex: 36, carRectY: 46, carH: 19,
                   carText: 59, cut: 10, gnum: 10, tnum: 9.5}
                : {yDisc: 58, plateY: -34, plateH: 96, plateW: 54, r: 20, inner: 6.5, num: -24,
                   tool: 40, mat: 55, railY: 44, carApex: 58, carRectY: 72, carH: 24,
                   carText: 88, cut: 14, gnum: 11.5, tnum: 11};

            var hasRail = !!state.has_selector;
            var offsets = state.selector_offsets;
            var useOffsets = hasRail && offsets && offsets.gates && offsets.gates.length === count;
            var width = 1000;
            var height = M.yDisc + (hasRail ? M.carText + 14 : M.tool + 34);
            var root = svg("svg", {viewBox: "0 0 " + width + " " + height, role: "img",
                "aria-label": "Gate status"});
            var x0 = 62;
            var x1 = width - (hasRail ? 56 : 30);
            var span = useOffsets
                ? ((offsets.bypass || offsets.gates[count - 1]) - offsets.gates[0]) || 1
                : 1;
            var gx = function (index) {
                if (useOffsets) {
                    return x0 + ((offsets.gates[index] - offsets.gates[0]) / span) * (x1 - x0);
                }
                return x0 + (index + 0.5) * ((x1 - x0) / count);
            };
            var bypassX = hasRail ? x1 : null;
            var yDisc = M.yDisc;

            if (hasRail) {
                root.appendChild(svg("rect", {x: x0 - 28, y: yDisc + M.railY,
                    width: (x1 - x0) + 56, height: 7, rx: 3.5, fill: "var(--hh-rail)"}));
            }

            var plates = [];
            gates.forEach(function (gate, index) {
                var x = gx(index);
                var group = svg("g", {class: "hh-gate", tabindex: "0", role: "button",
                    "aria-label": "Gate " + index + " " + gate.status_text});
                group.addEventListener("click", function () { self.openGate(index); });
                group.addEventListener("keydown", function (event) {
                    if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        self.openGate(index);
                    }
                });

                var plate = svg("rect", {class: "hh-gate-plate", x: x - M.plateW / 2,
                    y: yDisc + M.plateY, width: M.plateW, height: M.plateH, rx: 9,
                    fill: "var(--hh-surface-2)", stroke: "var(--hh-line)", "stroke-width": 1});
                plates.push(plate);
                group.appendChild(plate);

                // halo so black filament stays visible on a dark theme
                group.appendChild(svg("circle", {cx: x, cy: yDisc, r: M.r + 1.5, fill: "none",
                    stroke: "var(--hh-line)", "stroke-width": 1}));
                group.appendChild(svg("circle", {cx: x, cy: yDisc, r: M.r,
                    fill: gate.rgb || "var(--hh-surface-3)",
                    stroke: statusColor(gate.status), "stroke-width": 3}));
                group.appendChild(svg("circle", {cx: x, cy: yDisc, r: M.inner,
                    fill: "var(--hh-surface)", stroke: "var(--hh-line)", "stroke-width": 1}));

                if (gate.status === 0) {
                    group.appendChild(svg("line", {x1: x - M.cut, y1: yDisc + M.cut,
                        x2: x + M.cut, y2: yDisc - M.cut, stroke: "var(--hh-crit)",
                        "stroke-width": 2.5, "stroke-linecap": "round"}));
                } else if (gate.status === -1) {
                    var mark = svg("text", {x: x, y: yDisc + M.r * 0.3, "text-anchor": "middle",
                        "font-size": M.r * 0.85, "font-weight": "600",
                        fill: gate.dark ? "#ffffff" : "var(--hh-warn)"});
                    mark.textContent = "?";
                    group.appendChild(mark);
                }

                var number = svg("text", {x: x, y: yDisc + M.num, "text-anchor": "middle",
                    "font-size": M.gnum, "font-weight": "600", fill: "var(--hh-ink)"});
                number.textContent = (hasRail ? "G" : "L") + index;
                group.appendChild(number);

                if (gate.group) {
                    var groupLabel = svg("text", {x: x + M.r, y: yDisc + M.num,
                        "text-anchor": "middle", "font-size": 10, "font-weight": "600",
                        fill: "var(--hh-accent)"});
                    groupLabel.textContent = esLetter(gate.group);
                    group.appendChild(groupLabel);
                }

                var tools = svg("text", {x: x, y: yDisc + M.tool, "text-anchor": "middle",
                    "font-size": M.tnum, fill: "var(--hh-muted)"});
                tools.textContent = gate.tools.length
                    ? gate.tools.map(function (tool) { return "T" + tool; }).join(",") : "—";
                group.appendChild(tools);

                if (M.mat !== null && gate.material) {
                    var material = svg("text", {x: x, y: yDisc + M.mat, "text-anchor": "middle",
                        "font-size": 9.5, fill: "var(--hh-muted)"});
                    material.textContent = gate.material;
                    group.appendChild(material);
                }

                var title = svg("title", {});
                title.textContent = "Gate " + index + " · " + (gate.material || "?") + " · "
                    + gate.status_text + (gate.temperature ? " · " + gate.temperature + "°C" : "");
                group.appendChild(title);
                root.appendChild(group);
            });

            var cache = {signature: signature, plates: plates, gx: gx, bypassX: bypassX,
                baseX: 0, carriage: null, servoLabel: null};

            if (hasRail) {
                if (state.has_bypass) {
                    root.appendChild(svg("rect", {x: bypassX - M.plateW / 2.4, y: yDisc + M.plateY,
                        width: M.plateW / 1.2, height: M.plateH, rx: 9, fill: "var(--hh-surface-2)",
                        stroke: "var(--hh-line)", "stroke-dasharray": "4 3"}));
                    var bypassLabel = svg("text", {x: bypassX, y: yDisc + 4, "text-anchor": "middle",
                        "font-size": 10.5, fill: "var(--hh-muted)"});
                    bypassLabel.textContent = "BYPASS";
                    root.appendChild(bypassLabel);
                }
                var cx = gx(0);
                var carriage = svg("g", {});
                carriage.appendChild(svg("path", {d: "M" + (cx - 15) + " " + (yDisc + M.carRectY)
                    + " L" + cx + " " + (yDisc + M.carApex) + " L" + (cx + 15) + " "
                    + (yDisc + M.carRectY) + " Z", fill: "var(--hh-accent)"}));
                carriage.appendChild(svg("rect", {x: cx - 26, y: yDisc + M.carRectY, width: 52,
                    height: M.carH, rx: 6, fill: "var(--hh-accent)", opacity: "0.92"}));
                var servoLabel = svg("text", {x: cx, y: yDisc + M.carText, "text-anchor": "middle",
                    "font-size": 11, fill: "#ffffff", "font-weight": "600"});
                carriage.appendChild(servoLabel);
                if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
                    carriage.style.transition = "transform .5s cubic-bezier(.4,0,.2,1)";
                }
                root.appendChild(carriage);
                cache.baseX = cx;
                cache.carriage = carriage;
                cache.servoLabel = servoLabel;
            }

            box.appendChild(root);
            box.hhRail = cache;
        };

        // -- filament path -------------------------------------------------
        self.pathFillX = function () {
            var pos = self.state.filament_pos;
            var X = PATH_X;
            if (pos <= 0) return X.gate;
            if (pos === 1) return X.encoder;
            if (pos === 2) return X.bstart;
            if (pos === 3) {
                var progress = self.state.bowden_progress >= 0 ? self.state.bowden_progress / 100 : 0.5;
                return X.bstart + (X.bend - X.bstart) * progress;
            }
            if (pos === 4) return X.bend;
            if (pos === 5) return X.entry;
            if (pos === 6) return X.gears - 14;
            if (pos === 7) return X.gears + 18;
            if (pos === 8) return X.ts;
            if (pos === 9) return X.ts + 52;
            return X.nozzle;
        };

        // what buildPath draws; the values along the path are updatePath's
        self.pathSignature = function () {
            return JSON.stringify([self.compact, (self.state.sensors || []).map(function (sensor) {
                return sensor.id;
            })]);
        };

        self.renderPath = function () {
            var box = byId("hh-path");
            if (!box || !self.state.available) return;
            var cache = box.hhPath;
            var signature = self.pathSignature();
            if (!cache || cache.signature !== signature) self.buildPath(signature);
            self.updatePath();
        };

        self.buildPath = function (signature) {
            var box = byId("hh-path");
            var compact = self.compact;
            var X = PATH_X;
            // headroom above the path for the sensor labels, below it for the stops
            var y = compact ? 58 : 82;
            var height = compact ? 120 : 168;
            var labelSize = compact ? 14 : 15;
            var subSize = compact ? 12 : 13;
            var root = svg("svg", {viewBox: "0 0 1000 " + height, role: "img",
                "aria-label": "Filament path"});
            var add = function (node) { root.appendChild(node); return node; };
            var cache = {signature: signature, y: y, dots: []};
            var sensorIds = (self.state.sensors || []).map(function (sensor) { return sensor.id; });

            add(svg("rect", {x: X.gate - 14, y: y - 13,
                width: (X.nozzle - X.gate) + 30, height: 26, rx: 13,
                fill: "var(--hh-surface-2)", stroke: "var(--hh-line)"}));
            add(svg("rect", {x: X.bstart, y: y - 13, width: X.bend - X.bstart,
                height: 26, fill: "var(--hh-surface-3)", opacity: "0.75"}));

            // drawn once and shown or hidden per push, so a push only edits attributes
            cache.fill = add(svg("line", {x1: X.gate, y1: y, y2: y, "stroke-width": 13,
                "stroke-linecap": "round"}));
            cache.arrow = add(svg("path", {fill: "var(--hh-accent)"}));
            cache.unknown = add(svg("text", {x: (X.gate + X.bend) / 2, y: y + 5,
                "text-anchor": "middle", "font-size": 13, "font-weight": "600",
                fill: "var(--hh-warn)"}));
            cache.unknown.textContent = "filament position unknown";

            // a stop along the path; returns the node for its second line, if it has one
            var mark = function (x, label, withSub) {
                add(svg("line", {x1: x, y1: y + 16, x2: x, y2: y + 27, stroke: "var(--hh-line)"}));
                add(svg("text", {x: x, y: y + 44, "text-anchor": "middle",
                    "font-size": labelSize, "font-weight": "500", fill: "var(--hh-ink)"}))
                    .textContent = label;
                if (!withSub || compact) return null;
                return add(svg("text", {x: x, y: y + 62, "text-anchor": "middle",
                    "font-size": subSize, fill: "var(--hh-muted)"}));
            };

            // the first of `ids` that this machine has
            var sensorDot = function (x, ids) {
                if (!ids.some(function (id) { return sensorIds.indexOf(id) >= 0; })) return;
                cache.dots.push({ids: ids,
                    circle: add(svg("circle", {cx: x, cy: y - 30, r: 8, "stroke-width": 2})),
                    label: add(svg("text", {x: x, y: y - 46, "text-anchor": "middle",
                        "font-size": subSize, "font-weight": "500"}))});
            };

            add(svg("rect", {x: X.gate - 32, y: y - 30, width: 34, height: 60, rx: 7,
                fill: "var(--hh-surface-2)", stroke: "var(--hh-line)"}));
            cache.gateLabel = add(svg("text", {x: X.gate - 15, y: y + 4, "text-anchor": "middle",
                "font-size": 11, "font-weight": "600", fill: "var(--hh-ink)"}));
            var park = mark(X.gate, "gate", true);
            if (park) park.textContent = "park";
            sensorDot(X.gate + 34, ["gate_entry", "gate_shared"]);

            add(svg("circle", {cx: X.encoder, cy: y, r: 16, fill: "none",
                stroke: "var(--hh-accent)", "stroke-width": 2.5, "stroke-dasharray": "5 4"}));
            cache.flow = mark(X.encoder, "encoder", true);

            cache.bowden = mark((X.bstart + X.bend) / 2, "bowden", true);
            cache.progressTrack = add(svg("rect", {x: X.bstart, y: y + 20, width: X.bend - X.bstart,
                height: 5, rx: 2.5, fill: "var(--hh-surface-3)"}));
            cache.progressBar = add(svg("rect", {x: X.bstart, y: y + 20, height: 5, rx: 2.5,
                fill: "var(--hh-accent)"}));

            sensorDot(X.entry, ["extruder"]);
            mark(X.entry, "entry", false);

            [-1, 1].forEach(function (side) {
                add(svg("circle", {cx: X.gears, cy: y + side * 21, r: 13,
                    fill: "var(--hh-surface-2)", stroke: "var(--hh-ink-2)", "stroke-width": 2}));
                add(svg("circle", {cx: X.gears, cy: y + side * 21, r: 5, fill: "var(--hh-ink-2)"}));
            });
            cache.synced = mark(X.gears, "extruder", true);

            sensorDot(X.ts, ["toolhead"]);
            mark(X.ts, "toolhead", false);

            add(svg("path", {d: "M" + (X.nozzle - 16) + " " + (y - 20)
                + " h32 l-10 26 h-12 Z", fill: "var(--hh-surface-2)", stroke: "var(--hh-ink-2)",
                "stroke-width": 2}));
            mark(X.nozzle, "nozzle", false);

            clear(box);
            box.appendChild(root);
            box.hhPath = cache;
        };

        self.updatePath = function () {
            var cache = byId("hh-path").hhPath;
            var state = self.state;
            var X = PATH_X;
            var y = cache.y;

            text(byId("hh-path-hint"), (state.bowden_length ? "bowden "
                + Math.round(state.bowden_length) + " mm" : "")
                + (state.bowden_progress >= 0 ? " · " + state.bowden_progress + "%" : ""));

            var known = state.filament_pos >= 0;
            var gate = (state.gates || [])[state.gate];
            var fill = known ? self.pathFillX() : X.gate;
            show(cache.fill, known && fill > X.gate);
            if (known && fill > X.gate) {
                attr(cache.fill, "x2", fill);
                attr(cache.fill, "stroke", (gate && gate.rgb) || "var(--hh-muted)");
            }
            var dir = known ? state.filament_direction : 0;
            show(cache.arrow, !!dir);
            if (dir) {
                var ax = Math.min(Math.max(fill, X.gate + 30), X.nozzle - 10);
                attr(cache.arrow, "d", "M" + ax + " " + (y - 26) + " l" + (14 * dir)
                    + " 9 l" + (-14 * dir) + " 9 Z");
            }
            show(cache.unknown, !known);

            text(cache.gateLabel, state.gate >= 0 ? "G" + state.gate
                : state.gate === -2 ? "BP" : "—");
            var sub = function (node, value) {
                if (!node) return;
                show(node, !!value);
                text(node, value);
            };
            sub(cache.flow, state.encoder && state.encoder.flow_rate !== undefined
                ? state.encoder.flow_rate + "% flow" : null);
            sub(cache.bowden, state.bowden_length ? Math.round(state.bowden_length) + " mm" : null);
            sub(cache.synced, state.sync_drive ? "synced" : null);

            var progress = state.bowden_progress >= 0;
            show(cache.progressTrack, progress);
            show(cache.progressBar, progress);
            if (progress) {
                attr(cache.progressBar, "width", (X.bend - X.bstart) * (state.bowden_progress / 100));
            }

            var sensors = {};
            (state.sensors || []).forEach(function (sensor) { sensors[sensor.id] = sensor; });
            cache.dots.forEach(function (dot) {
                var sensor = null;
                dot.ids.some(function (id) { sensor = sensors[id]; return !!sensor; });
                if (!sensor) return;
                var on = sensor.state === true;
                var off = sensor.state === false;
                attr(dot.circle, "fill", on ? "var(--hh-ok)" : "var(--hh-surface)");
                attr(dot.circle, "stroke", on ? "var(--hh-ok)" : off ? "var(--hh-line)" : "var(--hh-muted)");
                attr(dot.circle, "stroke-dasharray", sensor.state === null ? "2 2" : null);
                attr(dot.label, "fill", on ? "var(--hh-ok)" : "var(--hh-muted)");
                text(dot.label, sensor.label);
            });
        };

        // -- tools and actions --------------------------------------------
        self.renderTools = function () {
            var box = byId("hh-tools");
            if (!box || !self.state.available) return;
            var state = self.state;
            var blocked = (state.printing && !state.paused) || state.busy || !self.canControl();
            syncList(box, state.ttg_map || [], function () {
                // reads its tool from the node, which the next fill keeps current
                return el("button", {class: "hh-tool", onclick: function () {
                    var tool = this.hhTool;
                    self.command("change_tool", {tool: tool},
                        "Change to tool T" + tool + " (gate " + this.hhGate + ")?");
                }}, [el("span", {class: "hh-swatch"}), el("span")]);
            }, function (button, gateIndex, tool) {
                var gate = (state.gates || [])[gateIndex] || {};
                button.hhTool = tool;
                button.hhGate = gateIndex;
                prop(button, "className", "hh-tool" + (tool === state.tool ? " on" : ""));
                prop(button, "title", "MMU_CHANGE_TOOL TOOL=" + tool + " (gate " + gateIndex + ")");
                css(button.firstChild, "background", gate.rgb || "transparent");
                text(button.lastChild, "T" + tool);
                prop(button, "disabled", !!blocked);
            });

            var actions = byId("hh-actions");
            if (!actions) return;
            var busy = (state.printing && !state.paused) || !self.canControl();
            var buttons = [
                ["Home", "home", "Home the MMU selector?", busy],
                ["Unload", "unload", "Unload the filament from the extruder?", busy],
                ["Load", "load", "Load filament from the selected gate?", busy],
                ["Check all gates", "check_all", "Check every gate for filament?", busy],
                ["Motors off", "motors_off", null, busy],
                ["Status to console", "status", null, !self.canControl()]
            ];
            if (state.has_bypass) {
                buttons.splice(3, 0, ["Select bypass", "select_bypass", "Select the bypass?", busy]);
            }
            syncList(actions, buttons, function () {
                return el("button", {class: "btn btn-small", onclick: function () {
                    self.command(this.hhSpec[1], {}, this.hhSpec[2]);
                }});
            }, function (button, spec) {
                button.hhSpec = spec;
                text(button, spec[0]);
                prop(button, "disabled", !!spec[3]);
            });
        };

        self.openGate = function (index) {
            var state = self.state;
            var gate = (state.gates || [])[index];
            if (!gate) return;
            var busy = (state.printing && !state.paused) || state.busy || !self.canControl();
            var body = el("div", {}, [
                el("p", {text: (gate.material || "unknown material") + " · " + gate.status_text
                    + (gate.temperature ? " · " + gate.temperature + " °C" : "")
                    + " · EndlessSpool " + esLetter(gate.group)}),
                el("p", {class: "hh-hint", text: "Tools mapped here: "
                    + (gate.tools.length ? gate.tools.map(function (t) { return "T" + t; }).join(", ")
                        : "none")})
            ]);
            var actions = el("div", {class: "hh-row"}, [
                ["Select", "select_gate", "Select gate " + index + "?"],
                ["Check", "check_gate", null],
                ["Preload", "preload", "Preload filament into gate " + index + "?"],
                ["Eject", "eject", "Eject the filament in gate " + index + "?"]
            ].map(function (spec) {
                var button = el("button", {class: "btn btn-small", text: spec[0],
                    onclick: function () {
                        self.command(spec[1], {gate: index}, spec[2]);
                        self.closeDialog();
                    }});
                button.disabled = busy;
                return button;
            }));
            self.showDialog("Gate " + index, [body, actions], false);
        };

        // -- sidebar and navbar -------------------------------------------
        self.renderSide = function () {
            var state = self.state;
            var spool = byId("hh-side-spool");
            if (!spool) return;
            var gate = (state.gates || [])[state.gate];
            css(spool, "background", (gate && gate.rgb) || "var(--hh-surface-3)");
            text(byId("hh-side-tool"), state.available
                ? "Tool " + (state.tool < 0 ? "?" : state.tool) + " · Gate "
                    + (state.gate < 0 ? "?" : state.gate)
                : "Happy Hare");
            text(byId("hh-side-material"), gate
                ? (gate.material || "unknown") + (gate.temperature ? " · " + gate.temperature + " °C" : "")
                : (state.available ? "no gate selected" : ((self.link && self.link.message) || "")));
            text(byId("hh-side-filament"), state.filament || "—");
            text(byId("hh-side-position"), state.filament_pos_name || "—");
            text(byId("hh-side-action"), state.action || "—");

            var badge = byId("hh-side-state");
            if (badge) {
                text(badge, state.print_state || "");
                prop(badge, "className", "hh-badge " + (state.locked ? "crit" : state.paused ? "warn"
                    : state.printing ? "ok" : "mute"));
            }

            // the one genuinely live value: headroom moves on every tick while printing
            var encoderBox = byId("hh-side-encoder");
            if (encoderBox) {
                var encoder = state.encoder || {};
                var has = encoder.headroom !== undefined;
                prop(encoderBox, "hidden", !has);
                if (has) {
                    text(byId("hh-side-headroom"), encoder.headroom.toFixed
                        ? encoder.headroom.toFixed(1) + " mm" : encoder.headroom + " mm");
                    var bar = byId("hh-side-headroom-bar");
                    var reference = encoder.detection_length || (encoder.desired_headroom * 2) || 10;
                    // whole percent: finer steps are sub-pixel on a sidebar-wide meter
                    var pct = Math.round(Math.max(0, Math.min(100, (encoder.headroom / reference) * 100)));
                    css(bar, "width", pct + "%");
                    css(bar, "background", encoder.headroom < encoder.desired_headroom
                        ? "var(--hh-warn)" : "var(--hh-ok)");
                }
            }

            var unload = byId("hh-side-unload");
            var recover = byId("hh-side-recover");
            prop(unload, "disabled", !!(!state.available || (state.printing && !state.paused)
                || !self.canControl()));
            prop(recover, "disabled", !!(!state.paused || !self.canControl()));
        };

        self.renderNavbar = function () {
            var state = self.state;
            var swatch = byId("hh-nav-swatch");
            var label = byId("hh-nav-text");
            var item = byId("hh-navbar-item");
            // OctoPrint wraps navbar templates in its own <li>. Hide both it and
            // our own element: which one actually carries the chip depends on how
            // the wrapper and the template markup nest.
            var show = self.settingValue("show_navbar", true);
            [byId("navbar_plugin_happyhare"), item].forEach(function (node) {
                if (!node) return;
                prop(node, "hidden", !show);
                css(node, "display", show ? "" : "none");
            });
            if (!swatch || !label) return;
            var gate = (state.gates || [])[state.gate];
            css(swatch, "background", (gate && gate.rgb) || "transparent");
            if (!state.available) {
                text(label, "MMU");
            } else if (state.paused) {
                text(label, "MMU paused");
            } else if (state.busy) {
                text(label, state.action);
            } else {
                text(label, "T" + (state.tool < 0 ? "?" : state.tool)
                    + " → G" + (state.gate < 0 ? "?" : state.gate));
            }
            if (item) item.classList.toggle("hh-alarm", !!state.paused);
        };

        // -- sensors -------------------------------------------------------
        /*
         * Two kinds of thing live here. The filament switches and the encoder
         * publish their state continuously through the subscription. An endstop
         * (the selector home switch) and the probe only update when queried, so
         * they read "not queried" until the refresh button runs QUERY_ENDSTOPS /
         * QUERY_PROBE.
         */
        self.renderSensors = function () {
            var card = byId("hh-sensors");
            var box = byId("hh-sensors-list");
            if (!card || !box) return;
            if (!self.settingValue("show_sensors", true)) {
                css(card, "display", "none");
                return;
            }
            css(card, "display", "");

            var state = self.state;
            var rows = [];
            var row = function (label, badgeClass, badgeText, title) {
                rows.push([label, badgeClass, badgeText, title || ""]);
            };

            (state.sensors || []).forEach(function (sensor) {
                if (sensor.state === null || sensor.state === undefined) {
                    row(sensor.label, "mute", "disabled", sensor.raw);
                } else {
                    row(sensor.label, sensor.state ? "ok" : "mute",
                        sensor.state ? "triggered" : "open", sensor.raw);
                }
            });

            var encoder = state.encoder || {};
            if (encoder.flow_rate !== undefined || encoder.headroom !== undefined) {
                var flow = encoder.enabled === false ? "disabled"
                    : (encoder.flow_rate !== undefined ? encoder.flow_rate + "% flow" : "active");
                row("Encoder", encoder.enabled === false ? "mute" : "ok", flow,
                    encoder.headroom !== undefined ? "headroom " + encoder.headroom + " mm" : "");
            }

            var endstops = state.endstops || [];
            if (endstops.length) {
                endstops.forEach(function (endstop) {
                    row(endstop.label, endstop.state ? "ok" : "mute",
                        endstop.state ? "triggered" : "open", endstop.id);
                });
            } else {
                row("Selector home", "warn", "not queried", "press Refresh to run QUERY_ENDSTOPS");
            }

            if (state.probe) {
                row("Probe", state.probe.triggered ? "ok" : "mute",
                    state.probe.triggered ? "triggered" : "open",
                    state.probe.last_z_result !== undefined && state.probe.last_z_result !== null
                        ? "last Z result " + state.probe.last_z_result : "");
            }

            syncList(box, rows, function () {
                return el("div", {class: "hh-sensor-row"}, [
                    el("span", {class: "hh-sensor-name"}),
                    el("span", {class: "hh-badge"})
                ]);
            }, function (node, spec) {
                prop(node, "title", spec[3]);
                text(node.firstChild, spec[0]);
                prop(node.lastChild, "className", "hh-badge " + spec[1]);
                text(node.lastChild, spec[2]);
            });

            var button = byId("hh-sensors-refresh");
            if (button) {
                var blocked = (state.printing && !state.paused) || !self.canControl();
                prop(button, "disabled", !!blocked);
                prop(button, "title", blocked
                    ? "Not while printing — the queries go through the G-code queue"
                    : "Runs QUERY_ENDSTOPS and QUERY_PROBE");
            }
        };

        self.refreshSensors = function () {
            OctoPrint.simpleApiCommand("happyhare", "refresh_sensors", {})
                .fail(function (response) {
                    var message = (response && response.responseJSON && response.responseJSON.error)
                        || "could not query the sensors";
                    new PNotify({title: "Happy Hare", text: message, type: "error"});
                });
        };

        // -- recovery ------------------------------------------------------
        self.renderRecovery = function () {
            var panel = byId("hh-recovery");
            if (!panel) return;
            var state = self.state;
            prop(panel, "hidden", !state.paused);
            if (!state.paused) return;
            text(byId("hh-recovery-reason"), state.reason_for_pause || "paused");
            var box = byId("hh-recovery-steps");
            if (!box) return;
            if (!box.hhSteps) box.hhSteps = self.buildRecovery(box);

            var allowed = self.canControl();
            box.hhSteps.forEach(function (step, index) {
                var stateClass = index < self.recoveryStep ? "done"
                    : index === self.recoveryStep ? "now" : "";
                prop(step.node, "className", "hh-step " + stateClass);
                step.buttons.forEach(function (button) {
                    if (step.single) {
                        prop(button, "className", "btn btn-small" + (stateClass === "now"
                            ? " btn-primary" : ""));
                    }
                    prop(button, "disabled", index !== self.recoveryStep || !allowed);
                });
            });
        };

        // the three steps never change shape, so they are built once and restyled
        self.buildRecovery = function (box) {
            var advance = function (step) {
                self.recoveryStep = step;
                self.renderRecovery();
            };
            var steps = [
                {title: "Unlock", hint: "MMU_UNLOCK restores the hotend temperature and idle timeout",
                 buttons: [["Unlock", function () { self.command("unlock", {}); advance(1); }]]},
                {title: "Tell Happy Hare where the filament is",
                 hint: "MMU_RECOVER LOADED=0/1 — pick what is actually true",
                 buttons: [
                     ["Unloaded", function () { self.command("recover", {loaded: false}); advance(2); }],
                     ["Loaded", function () { self.command("recover", {loaded: true}); advance(2); }]
                 ]},
                {title: "Resume the print", hint: "Runs RESUME through OctoPrint so the job follows",
                 buttons: [["Resume", function () { self.command("resume", {}); }]]}
            ];

            clear(box);
            return steps.map(function (step, index) {
                var buttons = step.buttons.map(function (spec) {
                    return el("button", {class: "btn btn-small", text: spec[0], onclick: spec[1]});
                });
                var single = buttons.length === 1;
                var node = el("div", {class: "hh-step"}, [
                    el("span", {class: "hh-step-no", text: String(index + 1)}),
                    el("div", {}, [
                        el("p", {text: step.title}),
                        el("small", {class: "hh-hint", text: step.hint})
                    ]),
                    single ? buttons[0] : el("span", {class: "hh-row"}, buttons)
                ]);
                box.appendChild(node);
                return {node: node, buttons: buttons, single: single};
            });
        };

        // -- gate map / TTG tables ----------------------------------------
        self.renderGateTable = function () {
            var table = byId("hh-gate-table");
            if (!table || !self.state.available) return;
            var editable = self.canControl();
            // rebuilt only when what it shows changes, which also keeps an
            // unapplied edit in place across the pushes in between
            var signature = JSON.stringify([editable, self.state.gate,
                (self.state.gates || []).map(function (gate) {
                    return [gate.index, gate.status, gate.rgb, gate.material, gate.temperature,
                        gate.spool_id, gate.speed, gate.tools, gate.group];
                })]);
            if (table.hhSignature === signature) return;
            if (table.contains(document.activeElement)) return;   // do not fight an edit
            table.hhSignature = signature;
            clear(table);
            var head = el("tr", {}, ["Gate", "Status", "Colour", "Material", "Temp", "Spool", "Speed",
                "Tools", "ES", ""].map(function (label) { return el("th", {text: label}); }));
            table.appendChild(el("thead", {}, [head]));
            var body = el("tbody", {});

            (self.state.gates || []).forEach(function (gate) {
                var statusSelect = el("select", {}, [[1, "On spool"], [2, "Buffered"], [0, "Empty"],
                    [-1, "Unknown"]].map(function (option) {
                    return el("option", {value: option[0], text: option[1],
                        selected: gate.status === option[0] ? "selected" : null});
                }));
                var color = el("input", {type: "color", value: gate.rgb || "#cccccc"});
                var material = el("input", {type: "text", class: "hh-input-sm", value: gate.material || ""});
                var temperature = el("input", {type: "number", class: "hh-input-sm",
                    value: gate.temperature || 0});
                var spool = el("input", {type: "number", class: "hh-input-sm", value: gate.spool_id});
                var speed = el("input", {type: "number", class: "hh-input-sm", value: gate.speed});
                [statusSelect, color, material, temperature, spool, speed].forEach(function (input) {
                    input.disabled = !editable;
                });

                var apply = el("button", {class: "btn btn-small", text: "Apply",
                    onclick: function () {
                        OctoPrint.simpleApiCommand("happyhare", "gate_map", {
                            gate: gate.index,
                            status: parseInt(statusSelect.value, 10),
                            color: color.value.replace("#", ""),
                            material: material.value,
                            temperature: parseInt(temperature.value, 10),
                            spool_id: parseInt(spool.value, 10),
                            speed: parseInt(speed.value, 10)
                        });
                    }});
                apply.disabled = !editable;

                body.appendChild(el("tr", {class: gate.index === self.state.gate ? "hh-selected" : ""}, [
                    el("td", {text: "G" + gate.index}),
                    el("td", {}, [statusSelect]),
                    el("td", {}, [color]),
                    el("td", {}, [material]),
                    el("td", {}, [temperature]),
                    el("td", {}, [spool]),
                    el("td", {}, [speed]),
                    el("td", {text: gate.tools.length
                        ? gate.tools.map(function (t) { return "T" + t; }).join(" ") : "—"}),
                    el("td", {text: esLetter(gate.group)}),
                    el("td", {}, [apply])
                ]));
            });
            table.appendChild(body);
        };

        self.renderTtgTable = function () {
            var table = byId("hh-ttg-table");
            if (!table || !self.state.available) return;
            var state = self.state;
            var editable = self.canControl();
            var signature = JSON.stringify([editable, state.tool, state.ttg_map,
                (state.gates || []).map(function (gate) {
                    return [gate.index, gate.rgb, gate.material, gate.status_text, gate.group];
                })]);
            if (table.hhSignature === signature) return;
            if (table.contains(document.activeElement)) return;
            table.hhSignature = signature;
            clear(table);
            table.appendChild(el("thead", {}, [el("tr", {}, ["Tool", "Gate", "Loaded",
                "EndlessSpool group"].map(function (label) { return el("th", {text: label}); }))]));
            var body = el("tbody", {});

            (state.ttg_map || []).forEach(function (gateIndex, tool) {
                var gate = (state.gates || [])[gateIndex] || {};
                // the handlers read the state current when they run, not at build time
                var gateSelect = el("select", {onchange: function (event) {
                    var mapping = (self.state.ttg_map || []).slice();
                    mapping[tool] = parseInt(event.target.value, 10);
                    OctoPrint.simpleApiCommand("happyhare", "ttg_map", {map: mapping});
                }}, (state.gates || []).map(function (candidate) {
                    return el("option", {value: candidate.index, text: "Gate " + candidate.index,
                        selected: candidate.index === gateIndex ? "selected" : null});
                }));
                var groupSelect = el("select", {onchange: function (event) {
                    var groups = (self.state.endless_spool_groups || []).slice();
                    groups[gateIndex] = parseInt(event.target.value, 10);
                    OctoPrint.simpleApiCommand("happyhare", "endless_spool",
                        {groups: groups, enable: true});
                }}, (state.gates || []).map(function (_, index) {
                    return el("option", {value: index + 1, text: esLetter(index + 1),
                        selected: (gate.group || 0) === index + 1 ? "selected" : null});
                }));
                gateSelect.disabled = !editable;
                groupSelect.disabled = !editable;

                body.appendChild(el("tr", {class: tool === state.tool ? "hh-selected" : ""}, [
                    el("td", {text: "T" + tool}),
                    el("td", {}, [gateSelect]),
                    el("td", {}, [el("span", {class: "hh-chips"}, [
                        el("span", {class: "hh-swatch", style: "background:" + (gate.rgb || "transparent")}),
                        el("span", {text: gate.material || "—"}),
                        el("span", {class: "hh-hint", text: gate.status_text || ""})
                    ])]),
                    el("td", {}, [groupSelect])
                ]));
            });
            table.appendChild(body);
        };

        // -- health --------------------------------------------------------
        self.renderHealth = function () {
            var state = self.state;
            var quality = byId("hh-quality");
            if (!quality || !state.available) return;

            // label, bar width, bar colour, figure, tooltip
            var fillBar = function (node, bar) {
                prop(node, "title", bar[4]);
                text(node.children[0], bar[0]);
                css(node.children[1].firstChild, "width", bar[1] + "%");
                css(node.children[1].firstChild, "background", bar[2]);
                text(node.children[2], bar[3]);
            };
            var makeBar = function () {
                return el("div", {class: "hh-bar"}, [
                    el("span"),
                    el("span", {class: "hh-track"}, [el("i")]),
                    el("span", {class: "hh-hint"})
                ]);
            };

            syncList(quality, (state.gates || []).map(function (gate) {
                var stats = gate.stats || {};
                var value = stats.quality;
                var known = value !== undefined && value >= 0;
                var pct = known ? Math.max(4, Math.min(100, value * 100)) : 0;
                var color = !known ? "var(--hh-line)" : value >= 0.95 ? "var(--hh-ok)"
                    : value >= 0.85 ? "var(--hh-warn)" : "var(--hh-crit)";
                return ["G" + gate.index, pct, color, known
                    ? Math.round(value * 100) + "% · " + (stats.loads || 0) + "L/"
                        + (stats.load_failures || 0) + "F"
                    : "no data", ""];
            }), makeBar, fillBar);

            syncList(byId("hh-counters"), (state.counters || []).map(function (counter) {
                var limited = counter.limit > 0;
                var pct = limited ? Math.min(100, (counter.count / counter.limit) * 100) : 0;
                var color = pct > 85 ? "var(--hh-crit)" : pct > 60 ? "var(--hh-warn)" : "var(--hh-ok)";
                return [counter.name, pct, color, limited
                    ? counter.count + " / " + counter.limit : String(counter.count),
                    counter.warning || ""];
            }), makeBar, fillBar);

            var swaps = state.swap_stats || {};
            var parts = [["form_tip", "Form/cut tip", "#7f6bd4"], ["unload", "Unload", "#2d6a9f"],
                ["load", "Load", "#2f7d5b"], ["purge", "Purge", "#a96908"],
                ["post_load", "Post-load", "#5d748a"], ["pre_unload", "Pre-unload", "#bc3b2d"]];
            var total = parts.reduce(function (sum, part) { return sum + (swaps[part[0]] || 0); }, 0);
            var shown = total > 0 ? parts : [];
            syncList(byId("hh-swap-stack"), shown, function () { return el("i"); },
                function (node, part) {
                    css(node, "width", ((swaps[part[0]] || 0) / total * 100) + "%");
                    css(node, "background", part[2]);
                    prop(node, "title", part[1]);
                });
            syncList(byId("hh-swap-legend"), shown, function () {
                return el("span", {}, [el("i"), el("span")]);
            }, function (node, part) {
                css(node.firstChild, "background", part[2]);
                text(node.lastChild, part[1] + " " + Math.round((swaps[part[0]] || 0) / 60) + " min");
            });
            text(byId("hh-swap-total"), total > 0
                ? (swaps.total_swaps || 0) + " swaps · " + ((swaps.total || 0) / 3600).toFixed(1)
                    + " h · " + (swaps.total_pauses || 0) + " pauses"
                : "no statistics yet");
        };

        // -- console -------------------------------------------------------
        self.consoleNode = function (line) {
            var node = el("div", {class: "hh-line" + (line.kind === "error" ? " err" : "")},
                [line.text]);
            if (line.kind === "error") {
                node.appendChild(el("span", {class: "hh-flag",
                    text: "intercepted — print protected"}));
            }
            return node;
        };

        // full redraw: startup, a refresh, opening the view
        self.renderConsole = function () {
            var box = byId("hh-console");
            if (!box) return;
            clear(box);
            self.consoleLines.forEach(function (line) { box.appendChild(self.consoleNode(line)); });
            box.scrollTop = box.scrollHeight;
        };

        // a pushed line adds one node and drops the oldest, instead of redrawing 300
        self.appendConsole = function (line) {
            var box = byId("hh-console");
            self.consoleLines.push(line);
            if (box) box.appendChild(self.consoleNode(line));
            while (self.consoleLines.length > 300) {
                self.consoleLines.shift();
                if (box && box.firstChild) box.removeChild(box.firstChild);
            }
            if (box) box.scrollTop = box.scrollHeight;
        };

        self.renderProtected = function () {
            text(byId("hh-protected-count"), self.errorsBlocked
                ? self.errorsBlocked + " MMU error(s) intercepted this session" : "");
        };

        self.renderLink = function () {
            var message = self.link && self.link.connected
                ? "connected · " + (self.link.socket || "klippy.sock")
                : (self.link && self.link.message) || "not connected";
            self.linkText(message);
            text(byId("hh-side-link"), message);
        };

        // -- pre-flight ----------------------------------------------------
        self.loadFileList = function () {
            OctoPrint.files.listForLocation("local", true).done(function (response) {
                var files = [];
                var walk = function (entries) {
                    (entries || []).forEach(function (entry) {
                        if (entry.type === "folder") walk(entry.children);
                        else if (entry.type === "machinecode") files.push(entry);
                    });
                };
                walk(response.files);
                files.sort(function (a, b) { return (b.date || 0) - (a.date || 0); });
                self.preflightFiles = files.slice(0, 50);
                var select = byId("hh-preflight-file");
                if (!select) return;
                clear(select);
                self.preflightFiles.forEach(function (file) {
                    select.appendChild(el("option", {value: file.path, text: file.path}));
                });
            });
        };

        self.runPreflight = function () {
            var select = byId("hh-preflight-file");
            if (!select || !select.value) return;
            OctoPrint.simpleApiCommand("happyhare", "preflight",
                {origin: "local", path: select.value}).done(function (result) {
                self.renderPreflight(result);
            });
        };

        self.renderPreflight = function (result) {
            var box = byId("hh-preflight-out");
            if (!box) return;
            clear(box);
            if (!result || !result.ok) {
                box.appendChild(el("p", {class: "hh-hint", text: (result && result.reason)
                    || "No Happy Hare data for this file. Upload it with pre-processing enabled and"
                    + " a slicer start G-code that calls MMU_START_SETUP."}));
                return;
            }
            var severity = result.severity || "ok";
            box.appendChild(el("p", {}, [el("span", {
                class: "hh-badge " + (severity === "ok" ? "ok" : severity === "warning" ? "warn" : "crit"),
                text: severity === "ok" ? "ready to print" : severity === "warning"
                    ? "check before printing" : "blocking issues"})]));

            var table = el("table", {class: "table table-condensed hh-table"});
            table.appendChild(el("thead", {}, [el("tr", {}, ["Tool", "Gate", "Slicer", "Loaded",
                "Check"].map(function (label) { return el("th", {text: label}); }))]));
            var body = el("tbody", {});
            (result.tools || []).forEach(function (row) {
                body.appendChild(el("tr", {}, [
                    el("td", {text: "T" + row.tool}),
                    el("td", {text: row.gate === null ? "—" : "G" + row.gate}),
                    el("td", {}, [el("span", {class: "hh-chips"}, [
                        el("span", {class: "hh-swatch",
                            style: "background:#" + (row.slicer.color || "888888")}),
                        el("span", {text: row.slicer.material || "?"})
                    ])]),
                    el("td", {}, [el("span", {class: "hh-chips"}, [
                        el("span", {class: "hh-swatch",
                            style: "background:#" + (row.loaded.color || "888888")}),
                        el("span", {text: (row.loaded.material || "?") + " · " + row.loaded.status})
                    ])]),
                    el("td", {}, row.issues.length
                        ? row.issues.map(function (issue) {
                            return el("span", {class: "hh-badge "
                                + (issue.severity === "critical" ? "crit" : "warn"),
                                text: issue.text});
                        })
                        : [el("span", {class: "hh-badge ok", text: "match"})])
                ]));
            });
            table.appendChild(body);
            box.appendChild(el("div", {class: "hh-tablewrap"}, [table]));
        };

        // -- dialogs -------------------------------------------------------
        self.ensureDialog = function () {
            var dialog = byId("hh-dialog");
            if (dialog) return dialog;
            dialog = el("div", {id: "hh-dialog", class: "modal hide fade hh-dialog"}, [
                el("div", {class: "modal-header"}, [
                    el("a", {class: "close", "data-dismiss": "modal", text: "×"}),
                    el("h3", {id: "hh-dialog-title", text: "Happy Hare"})
                ]),
                el("div", {class: "modal-body", id: "hh-dialog-body"}),
                el("div", {class: "modal-footer", id: "hh-dialog-footer"})
            ]);
            document.body.appendChild(dialog);
            return dialog;
        };

        self.showDialog = function (title, nodes, isError) {
            var dialog = self.ensureDialog();
            text(byId("hh-dialog-title"), title);
            var body = byId("hh-dialog-body");
            clear(body);
            nodes.forEach(function (node) { body.appendChild(node); });
            var footer = byId("hh-dialog-footer");
            clear(footer);
            if (!isError) {
                footer.appendChild(el("button", {class: "btn", text: "Close",
                    onclick: self.closeDialog}));
            }
            $(dialog).modal(isError ? {backdrop: "static", keyboard: false} : {});
            $(dialog).modal("show");
        };

        self.closeDialog = function () {
            var dialog = byId("hh-dialog");
            if (dialog) $(dialog).modal("hide");
        };

        self.renderPrompt = function () {
            if (!self.prompt) {
                if (self.promptOpen) {
                    self.closeDialog();
                    self.promptOpen = false;
                }
                return;
            }
            var prompt = self.prompt;
            var body = el("div", {}, (prompt.text || []).map(function (line) {
                return el("p", {text: line});
            }));
            var footer = el("div", {class: "hh-row hh-prompt-buttons"},
                (prompt.buttons || []).concat(prompt.footer || []).map(function (button) {
                    var style = button.style === "error" || button.style === "danger" ? "btn-danger"
                        : button.style === "warning" ? "btn-warning"
                        : button.style === "primary" ? "btn-primary" : "";
                    return el("button", {class: "btn " + style, text: button.label,
                        onclick: function () {
                            OctoPrint.simpleApiCommand("happyhare", "prompt_button",
                                {gcode: button.gcode || ""});
                            self.promptOpen = false;
                        }});
                }));
            self.showDialog(prompt.title || "Printer prompt", [body, footer], true);
            self.promptOpen = true;
        };

        // ------------------------------------------------------------------
        // wiring
        // ------------------------------------------------------------------
        self.selectView = function (view) {
            if (!view) return;
            self.view = view;
            ["operate", "gates", "preflight", "health", "console"].forEach(function (name) {
                var node = byId("hh-view-" + name);
                if (!node) return;
                var active = name === view;
                // both, because a theme that gives `section` a display rule beats
                // the browser's own [hidden] rule and would show every view at once
                node.hidden = !active;
                node.style.display = active ? "" : "none";
            });
            $("#hh-subtabs li").each(function () {
                $(this).toggleClass("active", $(this).find("a").data("view") === view);
            });
            if (view === "console") self.renderConsole();
            if (view === "preflight") self.loadFileList();
        };

        /*
         * Handlers are delegated from the document and namespaced, so they survive
         * anything that moves or re-renders the panel (UI Customizer rearranges
         * tabs and sidebar rows), and binding twice is harmless.
         */
        self.bindHandlers = function () {
            var doc = $(document);
            doc.off(".hh");
            doc.on("click.hh", "#hh-subtabs a", function (event) {
                event.preventDefault();
                self.selectView($(this).data("view"));
            });
            doc.on("click.hh", "#hh-side-unload", function () {
                self.command("unload", {}, "Unload the filament?");
            });
            doc.on("click.hh", "#hh-side-recover", function () {
                $("#tab_plugin_happyhare_link a").click();
                self.selectView("operate");
            });
            doc.on("click.hh", "#hh-preflight-run", function () { self.runPreflight(); });
            doc.on("click.hh", "#hh-navbar-item", function (event) {
                event.preventDefault();
                $("#tab_plugin_happyhare_link a").click();
            });
            doc.on("click.hh", "#hh-sensors-refresh", function () { self.refreshSensors(); });
        };

        self.watchSettings = function () {
            var plugin;
            try {
                plugin = self.settings.settings.plugins.happyhare;
            } catch (error) {
                return;
            }
            ["show_navbar", "show_sensors", "density", "confirm_moves"].forEach(function (key) {
                var observable = plugin ? plugin[key] : null;
                if (observable && observable.subscribe && !observable.hhWatched) {
                    observable.subscribe(function () { self.render(); });
                    observable.hhWatched = true;
                }
            });
        };

        self.onAfterBinding = function () {
            self.bindHandlers();
            self.watchSettings();
        };

        self.onSettingsHidden = function () {
            self.watchSettings();
            self.render();
        };

        self.onStartupComplete = function () {
            self.bindHandlers();
            self.watchSettings();
            if (window.ResizeObserver) {
                var root = byId("hh-root");
                if (root) {
                    new ResizeObserver(function () { self.applyDensity(); }).observe(root);
                }
            }
            self.refresh();
        };

        self.onTabChange = function (current) {
            if (current === "#tab_plugin_happyhare") self.render();
        };

        self.onSettingsShown = function () {
            self.watchSettings();
            self.renderLink();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: HappyHareViewModel,
        dependencies: ["settingsViewModel", "loginStateViewModel", "accessViewModel"],
        elements: ["#tab_plugin_happyhare", "#sidebar_plugin_happyhare_wrapper",
                   "#navbar_plugin_happyhare", "#settings_plugin_happyhare"]
    });
});
