$(function () {
    function PiNozCAMViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        function validated(initial, parse, min, max, label, hint) {
            var obs = ko.observable(initial);
            obs.error = ko.observable("");
            obs.subscribe(function (value) {
                if (value === undefined || value === null || value === "") {
                    obs.error(label + " cannot be empty.");
                    return;
                }
                var n = parse(value);
                if (isNaN(n) || n < min || n > max) {
                    obs.error(label + " must be between " + min + " and "
                              + max + (hint ? " (" + hint + ")" : "") + ".");
                } else {
                    obs.error("");
                }
            });
            return obs;
        }

        function validatedOptionalId(initial, pattern, message) {
            var obs = ko.observable(initial);
            obs.error = ko.observable("");
            function validate(value) {
                var clean = value === undefined || value === null
                    ? "" : String(value).trim();
                obs.error(clean === "" || pattern.test(clean) ? "" : message);
            }
            obs.subscribe(validate);
            validate(initial);
            return obs;
        }

        function trimmedString(value) {
            return value === undefined || value === null
                ? "" : String(value).trim();
        }

        function normalizeAction(value) {
            var parsed = parseInt(value, 10);
            if (isNaN(parsed)) return "0";
            return String(Math.max(0, Math.min(2, parsed)));
        }

        self.currentMaskImageData = ko.observable('0'.repeat(4096));
        self.newMaskImageData = ko.observable('0'.repeat(4096));

        self.currentEnableAI = ko.observable(false);
        self.newEnableAI = ko.observable(false);

        self.currentAction = ko.observable();
        self.newAction = ko.observable("");

        self.currentAiBackend = ko.observable();
        self.newAiBackend = ko.observable("");

        self.currentAiStartDelay = ko.observable();
        self.newAiStartDelay = validated(undefined, function (v) { return parseInt(v, 10); }, 0, 60000, "Detection Start Delay (s)");

        self.currentPrintLayoutThreshold = ko.observable();
        self.newPrintLayoutThreshold = validated(undefined, parseFloat, 0, 1, "Display Score Threshold");

        self.currentImgSensitivity = ko.observable();
        self.newImgSensitivity = validated(undefined, parseFloat, 0.0001, 1, "Failure Area Threshold");

        self.currentScoresThreshold = ko.observable();
        self.newScoresThreshold = validated(undefined, parseFloat, 0, 1, "Detection Score Threshold");

        self.currentMaxCount = ko.observable();
        self.newMaxCount = validated(undefined, function (v) { return parseInt(v, 10); }, 1, 100, "Max Failure Count");

        self.currentEnableMaxFailureCountNotification = ko.observable();
        self.newEnableMaxFailureCountNotification = ko.observable();

        self.currentCountTime = ko.observable();
        self.newCountTime = validated(undefined, function (v) { return parseInt(v, 10); }, 10, 3600, "Black Box Length (s)");

        self.currentFailureRatio = ko.observable();
        self.newFailureRatio = validated(undefined, parseFloat, 0.01, 1, "Failure Ratio Threshold");

        var LEVELS = [
            null,
            {scoresThreshold: 0.93, imgSensitivity: 0.06,
             failureRatio: 0.13},
            {scoresThreshold: 0.90, imgSensitivity: 0.05,
             failureRatio: 0.10},
            {scoresThreshold: 0.87, imgSensitivity: 0.04,
             failureRatio: 0.07},
            {scoresThreshold: 0.84, imgSensitivity: 0.03,
             failureRatio: 0.03},
            {scoresThreshold: 0.80, imgSensitivity: 0.02,
             failureRatio: 0.01}
        ];
        var LEVEL_NAMES = [null, "Lowest", "Low", "Medium", "High", "Highest"];
        var PRESET_FIELDS = {
            scoresThreshold: "newScoresThreshold",
            imgSensitivity: "newImgSensitivity",
            failureRatio: "newFailureRatio"
        };

        self.wizardCameraOk = ko.observable(true);

        self.sensitivityLevel = ko.observable(3);
        self.sensitivityIsCustom = ko.observable(false);
        self.sensitivityLabel = ko.computed(function () {
            if (self.sensitivityIsCustom()) return "Custom";
            return LEVEL_NAMES[parseInt(self.sensitivityLevel(), 10) || 3];
        });
        self.showAdvanced = ko.observable(false);
        self.advancedLabel = ko.computed(function () {
            return self.showAdvanced() ? "Hide advanced settings"
                                       : "Show advanced settings";
        });
        self.toggleAdvanced = function () {
            self.showAdvanced(!self.showAdvanced());
        };

        var applyingPreset = false;

        self.matchLevel = function () {
            for (var n = 1; n < LEVELS.length; n++) {
                var level = LEVELS[n], all = true;
                Object.keys(PRESET_FIELDS).forEach(function (key) {
                    var value = parseFloat(self[PRESET_FIELDS[key]]());
                    if (Math.abs(value - level[key]) > 1e-9) all = false;
                });
                if (all) return n;
            }
            return 0;
        };

        self.syncSensitivity = function () {
            var n = self.matchLevel();
            if (n) {
                applyingPreset = true;
                self.sensitivityLevel(n);
                applyingPreset = false;
                self.sensitivityIsCustom(false);
            } else {
                self.sensitivityIsCustom(true);
            }
        };

        function applyLevel(n) {
            if (!LEVELS[n]) return false;
            applyingPreset = true;
            self.sensitivityLevel(n);
            Object.keys(PRESET_FIELDS).forEach(function (key) {
                self[PRESET_FIELDS[key]](LEVELS[n][key]);
            });
            applyingPreset = false;
            self.sensitivityIsCustom(false);
            return true;
        }

        self.sensitivityLevel.subscribe(function (raw) {
            if (applyingPreset) return;
            applyLevel(parseInt(raw, 10));
        });

        Object.keys(PRESET_FIELDS).forEach(function (key) {
            var name = PRESET_FIELDS[key];
            if (self[name] && self[name].subscribe) {
                self[name].subscribe(function () {
                    if (!applyingPreset) {
                        self.syncSensitivity();
                    }
                });
            }
        });

        self.currentCpuSpeedControl = ko.observable();
        self.newCpuSpeedControl = ko.observable("");

        self.currentDetectionInterval = ko.observable();
        self.newDetectionInterval = validated(undefined, function (v) { return parseInt(v, 10); }, 0, 3600, "Minimum Check Interval (s)", "0 = as fast as the hardware allows");

        self.currentFrameSampleInterval = ko.observable();
        self.newFrameSampleInterval = validated(undefined, function (v) { return parseInt(v, 10); }, 10, 1000, "Frame Sampling Interval (ms)", "the sampler is the AI's only frame source, so it cannot be off");

        self.currentFrameBufferMaxAge = ko.observable();
        self.newFrameBufferMaxAge = validated(undefined, function (v) { return parseInt(v, 10); }, 4, 16, "Maximum Candidate Age (s)", "older candidates are discarded after this many seconds");

        self.currentFrameBufferCapacity = ko.observable();
        self.newFrameBufferCapacity = validated(undefined, function (v) { return parseInt(v, 10); }, 4, 16, "Candidate Frame Capacity (frames)");

        self.currentCustomSnapshotURL = ko.observable();
        self.newCustomSnapshotURL = ko.observable();

        self.currentMaxNotification = ko.observable();
        self.newMaxNotification = validated(undefined, function (v) { return parseInt(v, 10); }, 0, 60000, "Maximum Alerts per Print");

        self.currentNotifyInterval = ko.observable();
        self.newNotifyInterval = validated(undefined, function (v) { return parseInt(v, 10); }, 0, 3600, "Minimum Alert Interval (s)");

        self.currentEnableTelegram = ko.observable();
        self.newEnableTelegram = ko.observable();

        self.currentEnableDiscord = ko.observable();
        self.newEnableDiscord = ko.observable();

        self.currentTelegramBotToken = ko.observable();
        self.newTelegramBotToken = ko.observable();

        self.currentTelegramChatId = ko.observable();
        self.newTelegramChatId = validatedOptionalId(
            undefined, /^-?[0-9]{5,}$/,
            "Telegram Chat ID must be at least 5 digits, optionally starting with a minus sign for a group.");

        self.currentDiscordBotToken = ko.observable();
        self.newDiscordBotToken = ko.observable();

        self.currentDiscordChannelID = ko.observable();
        self.newDiscordChannelID = validatedOptionalId(
            undefined, /^[0-9]{17,20}$/,
            "Discord Channel ID must be 17-20 digits.");

        self.aiImage = ko.observable("");
        self.streamInfo = ko.observable(null);
        self.aiFlashActive = ko.observable(false);
        self._flashTimer = null;
        self.pageVisible = ko.observable(true);
        self.liveShowing = ko.computed(function () {
            var s = self.streamInfo();
            return !!(s && s.url) && !self.aiFlashActive();
        });
        self.liveTransform = ko.computed(function () {
            var s = self.streamInfo();
            if (!s || !self.liveShowing()) return "";
            var t = [];
            if (s.flipH) t.push("scaleX(-1)");
            if (s.flipV) t.push("scaleY(-1)");
            return t.join(" ");
        });
        self.displaySrc = ko.computed(function () {
            var s = self.streamInfo();
            if (self.liveShowing() && s && s.url) {
                return self.pageVisible() ? s.url : "";
            }
            return self.aiImage();
        });
        self.frameBoxes = ko.observableArray([]);
        self.frameSeverity = ko.observable(null);
        self._lastFrameId = null;
        self._pendingFrame = null;
        self._displayedFrameId = null;
        self._lastGoodSrc = "";
        self._revertedOnce = false;

        self.drawOverlay = function () {
            var img = document.getElementById("ai-image");
            var canvas = document.getElementById("ai-overlay");
            if (!img || !canvas) return;
            var w = img.clientWidth, h = img.clientHeight;
            if (!w || !h) return;
            canvas.width = w;
            canvas.height = h;
            if (self.liveShowing()) return;
            var boxes = self.frameBoxes();
            if (!boxes || !boxes.length) return;
            var sev = self.frameSeverity();
            var color = sev > 0.66 ? "red" : (sev > 0.33 ? "yellow" : "green");
            var ctx = canvas.getContext("2d");
            ctx.strokeStyle = color;
            ctx.fillStyle = color;
            ctx.lineWidth = 2;
            ctx.font = "14px sans-serif";
            boxes.forEach(function (b) {
                var x1 = b[0] * w, y1 = b[1] * h;
                var x2 = b[2] * w, y2 = b[3] * h;
                ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
                if (typeof b[4] === "number") {
                    ctx.fillText(b[4].toFixed(2), Math.max(0, x1),
                                 y1 > 16 ? y1 - 4 : y2 + 14);
                }
            });
        };
        self.liveShowing.subscribe(self.drawOverlay);

        var AI_FLASH_MS = 3000;
        self._cancelFlash = function () {
            self._flashFrameId = null;
            self.aiFlashActive(false);
            if (self._flashTimer !== null) {
                clearTimeout(self._flashTimer);
                self._flashTimer = null;
            }
        };
        self._flashFrameId = null;
        self._noteAnalysis = function (r) {
            if (r.frameKind !== "analysis" || r.alarming !== true) return;
            // Keep flash suppression bound to the currently loaded frame id.
            if (r.frameId && r.frameId === self._flashFrameId) return;
            self._flashFrameId = r.frameId;
            self.aiFlashActive(true);
            if (self._flashTimer !== null) {
                clearTimeout(self._flashTimer);
            }
            self._flashTimer = setTimeout(function () {
                self._flashTimer = null;
                self.aiFlashActive(false);
            }, AI_FLASH_MS);
        };

        self._eventSrc = function (event) {
            var t = event && (event.currentTarget || event.target);
            return (t && (t.currentSrc || t.src)) || "";
        };
        self._eventIsCurrent = function (event) {
            var expected = self.aiImage() || "";
            var url = self._eventSrc(event);
            return !!expected
                && url.slice(-expected.length) === expected;
        };
        self._onFrameLoad = function (event) {
            if (!self.liveShowing()) {
                if (!self._eventIsCurrent(event)) return;
                var url = self.aiImage() || "";
                var m = url.match(/[?&]fid=([^&#]+)/);
                var fid = m ? decodeURIComponent(m[1]) : null;
                self._displayedFrameId = fid;
                self._lastGoodSrc = url;
                var p = self._pendingFrame;
                if (p && p.id === fid) {
                    self.frameBoxes(p.boxes);
                    self.frameSeverity(p.severity);
                } else {
                    self.frameBoxes([]);
                    self.frameSeverity(null);
                }
            }
            self.drawOverlay();
        };
        self._onFrameError = function (event) {
            if (self.liveShowing() || !self._eventIsCurrent(event)) return;
            var st = self.streamInfo();
            if (self.aiFlashActive() && st && st.url) {
                self._cancelFlash();
                return;
            }
            if (self._lastGoodSrc && !self._revertedOnce
                    && self.aiImage() !== self._lastGoodSrc) {
                self._revertedOnce = true;
                self._lastFrameId = self._displayedFrameId;
                self.aiImage(self._lastGoodSrc);
            } else {
                self._lastFrameId = null;
                self._pendingFrame = null;
                self.aiImage("");
            }
        };

        self.aiOn = ko.observable(false);
        self.aiState = ko.observable("idle");
        self.telegramState = ko.observable("unconfigured");
        self.armed = ko.observable(false);
        self.telegramOn = ko.observable(false);
        self.cpuTemperature = ko.observable(0);
        self.failureCount = ko.observable(0);
        self.windowFrames = ko.observable(0);
        self.countTime = ko.observable(120);
        self.ratio = ko.observable(null);
        self.ratioAct = ko.observable(0.3);
        self.inferenceMs = ko.observable(null);
        self.detectionInterval = ko.observable(0);
        self.cores = ko.observable(null);
        self.totalCores = ko.observable(null);
        self.affinityPoolCores = ko.observable(null);
        self.heterogeneousCpu = ko.observable(false);
        self.backendKind = ko.observable("");
        self.backendError = ko.observable("");
        self.maskWarning = ko.observable("");
        self.cameraWarning = ko.observable("");
        self.evidence = ko.observableArray([]);

        function clamp01(x) { return Math.max(0, Math.min(1, x)); }

        var AI_LABEL = {
            disabled: "AI disabled",
            idle: "AI not started",
            starting: "AI starting",
            warming: "AI warming up",
            watching: "AI watching"
        };
        var AI_CLASS = {
            disabled: "chip-off",
            idle: "chip-off",
            starting: "chip-warm",
            warming: "chip-warm",
            watching: "chip-on"
        };
        self.aiChipText = ko.computed(function () {
            return AI_LABEL[self.aiState()] || "AI not started";
        });
        self.aiChipClass = ko.computed(function () {
            return "chip " + (AI_CLASS[self.aiState()] || "chip-off");
        });
        self.discordStatus = ko.observable("OFF");
        self.discordChipText = ko.computed(function () {
            var s = self.discordStatus();
            if (s === "ON") return "on";
            if (s === "MUTED") return "muted";
            if (s === "FAILED") return "failed";
            if (s === "CONNECTING") return "connecting";
            return "not set";
        });
        self.discordChipClass = ko.computed(function () {
            var s = self.discordStatus();
            if (s === "ON") return "chip chip-on";
            if (s === "FAILED") return "chip chip-hot";
            if (s === "MUTED") return "chip chip-warm";
            return "chip chip-off";
        });

        function tester(url, busy, result, okFlag, badFlag, draft) {
            return function () {
                if (busy()) return;
                busy(true);
                okFlag(false);
                badFlag(false);
                result("");
                var body = {};
                Object.keys(draft || {}).forEach(function (key) {
                    var value = ko.unwrap(draft[key]);
                    if (typeof value === "function") {
                        throw new Error("tester(): '" + key + "' must be a "
                            + "ko observable, not a function returning one");
                    }
                    body[key] = value === undefined || value === null
                        ? "" : String(value);
                });
                $.ajax({url: url, type: "POST", dataType: "json",
                        contentType: "application/json",
                        data: JSON.stringify(body)})
                    .done(function (r) {
                        result(r.message || "");
                        okFlag(r.ok === true);
                        badFlag(r.ok === false);
                    })
                    .fail(function (xhr) {
                        result("Request failed (HTTP " + (xhr.status || 0)
                               + "). The plugin did not answer.");
                        badFlag(true);
                    })
                    .always(function () { busy(false); });
            };
        }

        self.testSnapshotBusy = ko.observable(false);
        self.testSnapshotResult = ko.observable("");
        self.testSnapshotOk = ko.observable(false);
        self.testSnapshotBad = ko.observable(false);
        self.testSnapshot = tester("plugin/pinozcam/test_snapshot",
                                   self.testSnapshotBusy,
                                   self.testSnapshotResult,
                                   self.testSnapshotOk, self.testSnapshotBad,
                                   {customSnapshotURL:
                                        self.newCustomSnapshotURL});

        self.testInferenceBusy = ko.observable(false);
        self.testInferenceResult = ko.observable("");
        self.testInferenceOk = ko.observable(false);
        self.testInferenceBad = ko.observable(false);
        self.testInference = tester("plugin/pinozcam/test_inference",
                                    self.testInferenceBusy,
                                    self.testInferenceResult,
                                    self.testInferenceOk,
                                    self.testInferenceBad,
                                    {cpuSpeedControl:
                                         self.newCpuSpeedControl});

        self.testTelegramBusy = ko.observable(false);
        self.testTelegramResult = ko.observable("");
        self.testTelegramOk = ko.observable(false);
        self.testTelegramBad = ko.observable(false);
        self.testTelegramLabel = ko.observable("Test Telegram connection");
        self.testTelegram = tester(
            "plugin/pinozcam/test_notify?channel=telegram",
            self.testTelegramBusy, self.testTelegramResult,
            self.testTelegramOk, self.testTelegramBad,
            {telegramBotToken: self.newTelegramBotToken,
             telegramChatID: self.newTelegramChatId,
             customSnapshotURL: self.newCustomSnapshotURL});

        self.testDiscordBusy = ko.observable(false);
        self.testDiscordResult = ko.observable("");
        self.testDiscordOk = ko.observable(false);
        self.testDiscordBad = ko.observable(false);
        self.testDiscordLabel = ko.observable("Test Discord connection");
        self.testDiscord = tester(
            "plugin/pinozcam/test_notify?channel=discord",
            self.testDiscordBusy, self.testDiscordResult,
            self.testDiscordOk, self.testDiscordBad,
            {discordBotToken: self.newDiscordBotToken,
             discordChannelID: self.newDiscordChannelID,
             customSnapshotURL: self.newCustomSnapshotURL});

        var TG_LABEL = {unconfigured: "not set",
                        failed: "failed",
                        starting: "connecting",
                        muted: "muted",
                        on: "on"};
        var TG_CLASS = {unconfigured: "chip-off",
                        failed: "chip-hot",
                        starting: "chip-warm",
                        muted: "chip-warm",
                        on: "chip-on"};
        self.telegramChipText = ko.computed(function () {
            return TG_LABEL[self.telegramState()] || "Telegram not enabled";
        });
        self.telegramChipClass = ko.computed(function () {
            return "chip " + (TG_CLASS[self.telegramState()] || "chip-off");
        });
        self.tempChipText = ko.computed(function () {
            var t = self.cpuTemperature();
            return t ? t + "\u00b0C" : "temp n/a";
        });
        self.tempChipClass = ko.computed(function () {
            return "chip" + (self.cpuTemperature() >= 80 ? " chip-hot" : "");
        });

        self.gaugeFraction = ko.computed(function () {
            var act = self.ratioAct() || 0.3;
            return clamp01(self.ratio() === null ? 0 : self.ratio() / act);
        });
        self.gaugeWidth = ko.computed(function () {
            return (self.gaugeFraction() * 100).toFixed(1) + "%";
        });
        self.gaugeClass = ko.computed(function () {
            return self.gaugeFraction() >= 1 ? "gauge-fill gauge-act"
                                             : "gauge-fill";
        });
        self.actMarkStyle = ko.computed(function () {
            return { left: "100%" };
        });
        self.gaugeValue = ko.computed(function () {
            if (self.ratio() === null) {
                return "no data yet";
            }
            return self.ratio().toFixed(2) + " / " + self.ratioAct().toFixed(2);
        });

        self.gaugeDetail = ko.computed(function () {
            if (self.ratio() === null) {
                return self.aiOn()
                    ? "gathering the first frames before it starts judging"
                    : "detection starts when a print does";
            }
            var text = self.failureCount() + " of " + self.windowFrames()
                     + " frames alarming (last " + self.countTime() + "s)";
            if (!self.armed()) text += "  \u00b7  still warming up";
            return text;
        });

        self.gaugeLabel = ko.computed(function () {
            return self.gaugeValue() + "  \u00b7  " + self.gaugeDetail();
        });

        self.rateDetail = ko.computed(function () {
            var ms = self.inferenceMs();
            if (!ms) return "";
            var backendLabels = {
                cpu: "CPU \u00b7 XNNPACK",
                rknn: "NPU \u00b7 RKNN",
                awnn: "NPU \u00b7 VIPLite",
                vulkan: "GPU \u00b7 Vulkan"
            };
            var kind = self.backendKind();
            var parts = [];
            if (backendLabels[kind]) parts.push(backendLabels[kind]);
            parts.push(ms + " ms per check");
            if (self.cores()) {
                var pool = self.affinityPoolCores() || self.totalCores();
                var label = self.heterogeneousCpu()
                    ? " performance cores" : " cores";
                var prefix = kind && kind !== "cpu" ? "CPU helper " : "";
                parts.push(prefix + self.cores() + "/" + pool + label);
            }
            return parts.join("  \u00b7  ");
        });

        self.hasEvidence = ko.computed(function () {
            return self.evidence().length > 0;
        });

        self.openEvidence = function (item) {
            var w = window.open("");
            if (!w) return;
            var doc = w.document;
            var body = doc.body;
            if (!body) {
                body = doc.createElement("body");
                doc.documentElement.appendChild(body);
            }
            body.textContent = "";
            body.style.margin = "0";
            var img = doc.createElement("img");
            img.src = item.image();
            img.style.maxWidth = "100%";
            body.appendChild(img);
        };

        self.maskEditor = ko.observable(null);
        self.maskTool = ko.observable("brush");
        self.brushCells = ko.observable(3);
        self.maskCoverage = ko.observable("0.0%");
        self.maskStatus = ko.observable("");
        self.canUndo = ko.observable(false);
        self.canRedo = ko.observable(false);

        self.maskGridSize = ko.observable(128);
        self.draftMaskImageData = ko.observable("");

        self.onMaskStatus = function (editor) {
            self.maskCoverage(editor.coverage().toFixed(1) + "%");
            self.canUndo(editor.undoStack.length > 0);
            self.canRedo(editor.redoStack.length > 0);
            self.maskStatus(editor.background
                ? editor.canvas.width + "x" + editor.canvas.height
                  + " editing view, " + editor.grid + "x" + editor.grid
                  + " mask grid"
                : "No camera -- drawing on a placeholder");
        };

        self.isTool = function (tool) {
            return self.maskTool() === tool ? "btn btn-small active"
                                            : "btn btn-small";
        };
        self.pickTool = function (tool) {
            self.maskTool(tool);
            if (self.maskEditor()) self.maskEditor().setTool(tool);
        };
        self.brushSmaller = function () {
            self.brushCells(Math.max(1, self.brushCells() - 2));
            if (self.maskEditor()) self.maskEditor().setBrush(self.brushCells());
        };
        self.brushBigger = function () {
            self.brushCells(Math.min(31, self.brushCells() + 2));
            if (self.maskEditor()) self.maskEditor().setBrush(self.brushCells());
        };
        self.maskUndo = function () { if (self.maskEditor()) self.maskEditor().undo(); };
        self.maskRedo = function () { if (self.maskEditor()) self.maskEditor().redo(); };
        self.maskClear = function () { if (self.maskEditor()) self.maskEditor().clear(); };

        self.openSettings = function () {
            if (typeof OctoPrint !== "undefined" && OctoPrint.coreui
                && OctoPrint.coreui.viewmodels
                && OctoPrint.coreui.viewmodels.settingsViewModel) {
                OctoPrint.coreui.viewmodels.settingsViewModel.show(
                    "#settings_plugin_pinozcam");
                return;
            }
            var link = document.querySelector(
                'a[href="#settings_plugin_pinozcam"]');
            if (link) { link.click(); return; }
            var dialog = document.getElementById("settings_dialog");
            if (dialog && window.jQuery) window.jQuery(dialog).modal("show");
        };

        self.openMaskDialog = function () {
            var dialog = document.getElementById("mask-dialog");
            if (!dialog) return;
            self.draftMaskImageData(self.currentMaskImageData());
            self.maskStatus("Loading camera frame...");
            dialog.showModal();
            var editor = self.maskEditor();
            if (editor) {
                editor.undoStack.length = 0;
                editor.redoStack.length = 0;
                editor.setMask(self.currentMaskImageData());
                editor.setTool(self.maskTool());
                editor.setBrush(self.brushCells());
                editor.loadBackground(function () {
                    editor.setMask(self.currentMaskImageData());
                });
            }
        };
        self.saveMask = function () {
            self.newMaskImageData(self.draftMaskImageData());
            self.saveSettings();
            document.getElementById("mask-dialog").close();
        };
        self.cancelMask = function () {
            document.getElementById("mask-dialog").close();
        };

        self.onBeforeBinding = function () {
            var pluginSettings =
                self.settingsViewModel.settings.plugins.pinozcam;

            self.newMaskImageData(pluginSettings.maskImageData());
            self.currentMaskImageData(self.newMaskImageData());
            
            self.newEnableAI(!!pluginSettings.enableAI());
            self.currentEnableAI(self.newEnableAI());    
            
            self.newAction(normalizeAction(pluginSettings.action()));
            self.currentAction(self.newAction());

            self.newAiBackend(pluginSettings.aiBackend());
            self.currentAiBackend(self.newAiBackend());

            self.newAiStartDelay(pluginSettings.aiStartDelay());
            self.currentAiStartDelay(self.newAiStartDelay());

            self.newPrintLayoutThreshold(pluginSettings.printLayoutThreshold());
            self.currentPrintLayoutThreshold(self.newPrintLayoutThreshold());

            self.newImgSensitivity(pluginSettings.imgSensitivity());
            self.currentImgSensitivity(self.newImgSensitivity());

            self.newScoresThreshold(pluginSettings.scoresThreshold());
            self.currentScoresThreshold(self.newScoresThreshold());

            self.newMaxCount(pluginSettings.maxCount());
            self.currentMaxCount(self.newMaxCount());

            self.newEnableMaxFailureCountNotification(pluginSettings.enableMaxFailureCountNotification().toString());
            self.currentEnableMaxFailureCountNotification(self.newEnableMaxFailureCountNotification());

            self.newCountTime(pluginSettings.countTime());
            self.currentCountTime(self.newCountTime());

            self.newFailureRatio(pluginSettings.failureRatio());
            self.currentFailureRatio(self.newFailureRatio());

            self.newFrameBufferMaxAge(pluginSettings.frameBufferMaxAge());
            self.currentFrameBufferMaxAge(self.newFrameBufferMaxAge());

            self.newFrameBufferCapacity(pluginSettings.frameBufferCapacity());
            self.currentFrameBufferCapacity(self.newFrameBufferCapacity());

            self.syncSensitivity();

            self.newCpuSpeedControl(pluginSettings.cpuSpeedControl().toString());
            self.currentCpuSpeedControl(self.newCpuSpeedControl());

            self.newDetectionInterval(pluginSettings.detectionInterval());
            self.currentDetectionInterval(self.newDetectionInterval());

            self.newFrameSampleInterval(pluginSettings.frameSampleInterval());
            self.currentFrameSampleInterval(self.newFrameSampleInterval());

            self.newCustomSnapshotURL(pluginSettings.customSnapshotURL());
            self.currentCustomSnapshotURL(self.newCustomSnapshotURL());

            self.newMaxNotification(pluginSettings.maxNotification());
            self.currentMaxNotification(self.newMaxNotification());

            self.newNotifyInterval(pluginSettings.notifyInterval());
            self.currentNotifyInterval(self.newNotifyInterval());

            self.newEnableTelegram(!!pluginSettings.enableTelegram());
            self.currentEnableTelegram(self.newEnableTelegram());

            self.newEnableDiscord(!!pluginSettings.enableDiscord());
            self.currentEnableDiscord(self.newEnableDiscord());

            self.newTelegramBotToken(pluginSettings.telegramBotToken());
            self.currentTelegramBotToken(self.newTelegramBotToken());

            self.newTelegramChatId(pluginSettings.telegramChatID());
            self.currentTelegramChatId(self.newTelegramChatId());
            
            self.newDiscordBotToken(pluginSettings.discordBotToken());
            self.currentDiscordBotToken(self.newDiscordBotToken());

            self.newDiscordChannelID(pluginSettings.discordChannelID());
            self.currentDiscordChannelID(self.newDiscordChannelID());
        };

        self.settingsPayload = function () {
            return {
                maskImageData: self.newMaskImageData(),
                enableAI: !!self.newEnableAI(),
                action: parseInt(self.newAction(), 10),
                aiBackend: self.newAiBackend(),
                aiStartDelay: parseInt(self.newAiStartDelay(), 10),
                printLayoutThreshold: parseFloat(self.newPrintLayoutThreshold()),
                imgSensitivity: parseFloat(self.newImgSensitivity()),
                scoresThreshold: parseFloat(self.newScoresThreshold()),
                maxCount: parseInt(self.newMaxCount(), 10), 
                enableMaxFailureCountNotification: self.newEnableMaxFailureCountNotification() === "true",
                countTime: parseInt(self.newCountTime(), 10),
                failureRatio: parseFloat(self.newFailureRatio()),
                cpuSpeedControl: parseFloat(self.newCpuSpeedControl()),
                detectionInterval: parseInt(self.newDetectionInterval(), 10),
                frameSampleInterval: parseInt(self.newFrameSampleInterval(), 10),
                frameBufferMaxAge: parseInt(self.newFrameBufferMaxAge(), 10),
                frameBufferCapacity: parseInt(self.newFrameBufferCapacity(), 10),
                customSnapshotURL: self.newCustomSnapshotURL(),
                maxNotification: parseInt(self.newMaxNotification(), 10),
                notifyInterval: parseInt(self.newNotifyInterval(), 10),
                enableTelegram: !!self.newEnableTelegram(),
                enableDiscord: !!self.newEnableDiscord(),
                telegramBotToken: self.newTelegramBotToken(),
                telegramChatID: trimmedString(self.newTelegramChatId()),
                discordBotToken: self.newDiscordBotToken(),
                discordChannelID: trimmedString(self.newDiscordChannelID()),
            };
        };

        self.validationErrors = function () {
            var out = [];
            Object.keys(self).forEach(function (k) {
                if (k.indexOf('new') !== 0 || !self[k]) return;
                if (typeof self[k].error !== 'function') return;
                var e = self[k].error();
                if (e) out.push(e);
            });
            return out;
        };

        self.saveSettings = function () {
            var problems = self.validationErrors();
            if (problems.length) {
                new PNotify({title: 'Not saved',
                             text: problems.join('  '), type: 'error'});
                return;
            }
            var newSettings = self.settingsPayload();
            OctoPrint.settings
                .savePluginSettings("pinozcam", newSettings)
                .done(function () {
                    new PNotify({
                        title: "Success",
                        text: "Settings have been saved.",
                        type: "success",
                    });
                    self.currentMaskImageData(self.newMaskImageData());
                    self.currentEnableAI(self.newEnableAI());
                    self.currentAction(self.newAction());
                    self.currentAiBackend(self.newAiBackend());
                    self.currentAiStartDelay(self.newAiStartDelay());
                    self.currentPrintLayoutThreshold(self.newPrintLayoutThreshold());
                    self.currentImgSensitivity(self.newImgSensitivity());
                    self.currentScoresThreshold(self.newScoresThreshold());
                    self.currentMaxCount(self.newMaxCount());
                    self.currentEnableMaxFailureCountNotification(
                        self.newEnableMaxFailureCountNotification());
                    self.currentCountTime(self.newCountTime());
                    self.currentFailureRatio(self.newFailureRatio());
                    self.currentFrameBufferMaxAge(self.newFrameBufferMaxAge());
                    self.currentFrameBufferCapacity(self.newFrameBufferCapacity());
                    self.currentCpuSpeedControl(self.newCpuSpeedControl());
                    self.currentDetectionInterval(self.newDetectionInterval());
                    self.currentFrameSampleInterval(self.newFrameSampleInterval());
                    self.currentCustomSnapshotURL(self.newCustomSnapshotURL());
                    self.currentMaxNotification(self.newMaxNotification());
            self.currentNotifyInterval(self.newNotifyInterval());
                    self.currentEnableTelegram(self.newEnableTelegram());
                    self.currentEnableDiscord(self.newEnableDiscord());
                    self.currentTelegramBotToken(self.newTelegramBotToken());
                    self.currentTelegramChatId(self.newTelegramChatId());
                    self.currentDiscordBotToken(self.newDiscordBotToken());
                    self.currentDiscordChannelID(self.newDiscordChannelID())
                })
                .fail(function () {
                    new PNotify({
                        title: "Error",
                        text: "Failed to save settings.",
                        type: "error",
                    });
                });
        };


        self.handleHelpButtons = function () {
            ["tab_plugin_pinozcam", "settings_plugin_pinozcam",
             "wizard_plugin_pinozcam"].forEach(
                    function (id) {
                var root = document.getElementById(id);
                if (root) self.bindHelpRoot(root);
            });
        };

        self.bindHelpRoot = function (root) {
            root.addEventListener("click", function (event) {
                var icon = event.target.closest(".pinozcam-help-icon");
                if (!icon) return;
                event.preventDefault();
                var panel = document.getElementById(icon.dataset.help);
                if (!panel) return;
                var open = panel.hasAttribute("hidden");
                if (open) {
                    panel.removeAttribute("hidden");
                } else {
                    panel.setAttribute("hidden", "");
                }
                icon.setAttribute("aria-expanded", open ? "true" : "false");
            });
        };

        self._pollInFlight = false;

        var POLL_MS = 1000;
        var POLL_MAX_MS = 30000;
        self._pollDelay = POLL_MS;
        self._pollTimer = null;
        self._pollStopped = false;

        self.stopPolling = function (why) {
            self._pollStopped = true;
            if (self._pollTimer !== null) {
                clearTimeout(self._pollTimer);
                self._pollTimer = null;
            }
            if (why) console.log("PiNozCam: stopped polling -- " + why);
        };

        self.schedulePoll = function () {
            if (self._pollStopped || self._pollPaused
                    || self._pollTimer !== null) return;
            self._pollTimer = setTimeout(function () {
                self._pollTimer = null;
                self.pollStatus();
            }, self._pollDelay);
        };

        self.pollStatus = function () {
            if (self._pollInFlight) return;
            self._pollInFlight = true;
            var release = function () { self._pollInFlight = false; };
            $.ajax({ url: "plugin/pinozcam/check", type: "GET",
                     dataType: "json" })
                .done(function (r) {
                    release();
                    self._pollDelay = POLL_MS;
                    self.schedulePoll();
                    if (r.frameId && r.frameId !== self._lastFrameId) {
                        self._lastFrameId = r.frameId;
                        self._revertedOnce = false;
                        self._pendingFrame = {
                            id: r.frameId,
                            boxes: (r.frameKind === "analysis"
                                    ? (r.boxes || []) : []),
                            severity: r.severity
                        };
                        self.aiImage("plugin/pinozcam/frame.jpg?fid="
                                     + encodeURIComponent(r.frameId));
                    }
                    self.streamInfo(r.stream || null);
                    self._noteAnalysis(r);
                    self.aiOn(r.aiStatus === "ON");
                    self.aiState(r.aiState
                                 || (r.aiStatus === "ON" ? "watching" : "idle"));
                    self.telegramState(r.telegramState
                                       || (r.telegramStatus === "ON"
                                           ? "on" : "unconfigured"));
                    self.armed(!!r.armed);
                    self.telegramOn(r.telegramStatus === "ON"
                                    || r.telegramState === "muted"
                                    || r.telegramState === "starting");
                    self.discordStatus(r.discordStatus || "OFF");
                    self.cpuTemperature(r.cpuTemperature);
                    self.failureCount(r.failureCount);
                    self.windowFrames(r.windowFrames);
                    self.countTime(r.countTime);
                    self.ratio(r.failureRatio);
                    self.ratioAct(r.failureRatioThreshold);
                    self.inferenceMs(r.inferenceMs);
                    self.detectionInterval(r.detectionInterval);
                    self.cores(r.cores);
                    self.totalCores(r.totalCores);
                    self.affinityPoolCores(r.affinityPoolCores
                                           || r.totalCores);
                    self.heterogeneousCpu(!!r.heterogeneousCpu);
                    self.backendKind(r.backendKind || "");
                    self.backendError(r.backendError || "");
                    self.maskWarning(r.maskWarning || "");
                    self.cameraWarning(r.cameraState === "offline"
                        ? "Camera offline: no frames are reaching the "
                          + "detector. Print failure detection is blind "
                          + "until the camera returns."
                        : "");
                    self.syncEvidence(r.evidence || []);
                })
                .fail(function (xhr) {
                    release();
                    var status = xhr && xhr.status;
                    if (status === 404 || status === 410) {
                        self.stopPolling("plugin/pinozcam/check returned "
                                         + status + "; reload the page");
                        return;
                    }
                    self._pollDelay = Math.min(self._pollDelay * 2,
                                               POLL_MAX_MS);
                    self.schedulePoll();
                });
        };

        self._pollPaused = false;
        self._tabActive = true;

        self.pausePolling = function () {
            self._pollPaused = true;
            if (self._pollTimer !== null) {
                clearTimeout(self._pollTimer);
                self._pollTimer = null;
            }
        };

        self.resumePolling = function () {
            if (self._pollStopped || !self._pollPaused) return;
            self._pollPaused = false;
            self.pollStatus();
        };

        self._syncPolling = function () {
            var visible = self._tabActive && !document.hidden
                    && self._serverConnected !== false;
            if (self.pageVisible) self.pageVisible(visible);
            if (visible) {
                self.resumePolling();
            } else {
                self.pausePolling();
            }
        };

        self.onAfterTabChange = function (current) {
            self._tabActive = (current === "#tab_plugin_pinozcam");
            self._syncPolling();
        };

        self.syncEvidence = function (items) {
            var have = self.evidence(), key = function (list) {
                return list.map(function (i) { return i.id; }).join(",");
            };
            if (key(have) === key(items)) {
                items.forEach(function (fresh, idx) {
                    if (have[idx]) have[idx].age(Math.round(fresh.age));
                });
                return;
            }
            self.evidence(items.map(function (item) {
                var vm = {
                    id: item.id,
                    age: ko.observable(Math.round(item.age)),
                    severity: item.severity.toFixed(2),
                    failureCount: item.failureCount,
                    windowFrames: item.windowFrames,
                    image: ko.observable("")
                };
                $.ajax({ url: "plugin/pinozcam/evidence/" + item.id,
                         dataType: "json" })
                    .done(function (r) { vm.image(r.image); });
                return vm;
            }));
        };

        var FIELD_PANES = {
            newMaxNotification: "notify",
            newNotifyInterval: "notify",
            newTelegramChatId: "notify",
            newDiscordChannelID: "notify",
            newDetectionInterval: "performance"
        };

        self.showTabFor = function (fieldName) {
            var pane = FIELD_PANES[fieldName] || "detection";
            var link = document.querySelector(
                'a[href="#pinozcam_pane_' + pane + '"]');
            if (link && window.jQuery && window.jQuery(link).tab) {
                window.jQuery(link).tab("show");
            }
        };

        self.firstInvalidField = function () {
            var found = null;
            Object.keys(self).forEach(function (k) {
                if (found || k.indexOf("new") !== 0 || !self[k]) return;
                if (typeof self[k].error !== "function") return;
                if (self[k].error()) found = k;
            });
            return found;
        };

        self.onSettingsBeforeSave = function () {
            var problems = self.validationErrors();
            if (problems.length) {
                self.showTabFor(self.firstInvalidField());
                new PNotify({title: "PiNozCam setting not saved",
                             text: problems.join("  "), type: "error"});
                return;
            }
            var target = self.settingsViewModel.settings.plugins.pinozcam;
            var payload = self.settingsPayload();
            Object.keys(payload).forEach(function (key) {
                if (typeof target[key] === "function") target[key](payload[key]);
            });
        };

        self.onServerDisconnect = function () {
            self._serverConnected = false;
            self._syncPolling();
            return true;
        };

        self.onServerReconnect = function () {
            self._serverConnected = true;
            self._pollDelay = POLL_MS;
            self._syncPolling();
        };

        self.onStartupComplete = function() {
            self.handleHelpButtons();
            $("#ai-image").on("load", self._onFrameLoad);
            $("#ai-image").on("error", self._onFrameError);
            $(window).on("resize", self.drawOverlay);
            document.addEventListener("visibilitychange",
                                      self._syncPolling);
            self.pollStatus();
            $.ajax({url: "plugin/pinozcam/snapshot_raw", dataType: "json"})
                .done(function (r) { self.wizardCameraOk(!!r.hasCamera); })
                .fail(function () { self.wizardCameraOk(false); });
        };
    }

    OCTOPRINT_VIEWMODELS.push([
        PiNozCAMViewModel,
        ["settingsViewModel"],
        ["#tab_plugin_pinozcam", "#settings_plugin_pinozcam",
         "#wizard_plugin_pinozcam"],
    ]);
});
