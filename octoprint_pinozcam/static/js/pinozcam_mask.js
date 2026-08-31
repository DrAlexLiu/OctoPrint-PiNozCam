(function () {
    "use strict";

    function MaskCanvas(canvas, options) {
        this.canvas = canvas;
        this.ctx = canvas.getContext("2d");
        this.grid = options.gridSize || 128;
        this.onChange = options.onChange || function () {};
        this.snapshotUrl = options.snapshotUrl || "plugin/pinozcam/snapshot_raw";
        this.status = options.status || function () {};

        this.MAX_W = 854;
        this.MAX_H = 480;
        this.MIN_W = 640;

        this.cells = this.decode(options.mask);
        this.background = null;
        this.undoStack = [];
        this.redoStack = [];
        this.tool = "brush";
        this.brushCells = 3;
        this.drawing = false;
        this.rectStart = null;
        this.rectNow = null;

        this._onDown = this.onDown.bind(this);
        this._onMove = this.onMove.bind(this);
        this._onUp = this.onUp.bind(this);
        canvas.addEventListener("pointerdown", this._onDown);
        canvas.addEventListener("pointermove", this._onMove);
        canvas.addEventListener("pointerup", this._onUp);
        canvas.addEventListener("pointercancel", this._onUp);
        canvas.addEventListener("pointerleave", this._onUp);
        canvas.style.touchAction = "none";
    }

    MaskCanvas.prototype.destroy = function () {
        var c = this.canvas;
        c.removeEventListener("pointerdown", this._onDown);
        c.removeEventListener("pointermove", this._onMove);
        c.removeEventListener("pointerup", this._onUp);
        c.removeEventListener("pointercancel", this._onUp);
        c.removeEventListener("pointerleave", this._onUp);
        this.background = null;
    };

    MaskCanvas.prototype.blank = function () {
        return new Uint8Array(this.grid * this.grid);
    };

    MaskCanvas.prototype.decode = function (text) {
        var out = this.blank();
        if (!text) return out;
        var size = Math.round(Math.sqrt(text.length));
        if (size * size !== text.length || size < 1) return out;
        for (var r = 0; r < this.grid; r++) {
            var sr = Math.floor(r * size / this.grid);
            for (var c = 0; c < this.grid; c++) {
                var sc = Math.floor(c * size / this.grid);
                if (text.charAt(sr * size + sc) === "1") out[r * this.grid + c] = 1;
            }
        }
        return out;
    };

    MaskCanvas.prototype.encode = function () {
        var parts = new Array(this.cells.length);
        for (var i = 0; i < this.cells.length; i++) {
            parts[i] = this.cells[i] ? "1" : "0";
        }
        return parts.join("");
    };

    MaskCanvas.prototype.setMask = function (text) {
        if (text === this.encode()) return;
        this.cells = this.decode(text);
        this.redraw();
    };

    MaskCanvas.prototype.coverage = function () {
        var on = 0;
        for (var i = 0; i < this.cells.length; i++) on += this.cells[i];
        return 100 * on / this.cells.length;
    };

    MaskCanvas.prototype.cellX = function (c) {
        return Math.round(this.canvas.width * c / this.grid);
    };
    MaskCanvas.prototype.cellY = function (r) {
        return Math.round(this.canvas.height * r / this.grid);
    };

    MaskCanvas.prototype.redraw = function () {
        var ctx = this.ctx, g = this.grid;
        ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        if (this.background) {
            ctx.drawImage(this.background, 0, 0,
                          this.canvas.width, this.canvas.height);
        }
        ctx.fillStyle = "rgba(0, 0, 0, 0.72)";
        for (var r = 0; r < g; r++) {
            var c = 0;
            while (c < g) {
                if (!this.cells[r * g + c]) { c++; continue; }
                var start = c;
                while (c < g && this.cells[r * g + c]) c++;
                ctx.fillRect(this.cellX(start), this.cellY(r),
                             this.cellX(c) - this.cellX(start),
                             this.cellY(r + 1) - this.cellY(r));
            }
        }
        if (this.rectStart && this.rectNow) {
            ctx.strokeStyle = this.tool === "rectErase" ? "#4caf50" : "#e53935";
            ctx.lineWidth = 2;
            ctx.setLineDash([6, 4]);
            var x1 = this.cellX(Math.min(this.rectStart.c, this.rectNow.c));
            var y1 = this.cellY(Math.min(this.rectStart.r, this.rectNow.r));
            var x2 = this.cellX(Math.max(this.rectStart.c, this.rectNow.c) + 1);
            var y2 = this.cellY(Math.max(this.rectStart.r, this.rectNow.r) + 1);
            ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
            ctx.setLineDash([]);
        }
        this.status(this);
    };

    function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

    MaskCanvas.prototype.cellAt = function (event) {
        var rect = this.canvas.getBoundingClientRect();
        var x = (event.clientX - rect.left) / rect.width;
        var y = (event.clientY - rect.top) / rect.height;
        return { c: clamp(Math.floor(x * this.grid), 0, this.grid - 1),
                 r: clamp(Math.floor(y * this.grid), 0, this.grid - 1) };
    };

    MaskCanvas.prototype.pushUndo = function () {
        this.undoStack.push(this.cells.slice());
        if (this.undoStack.length > 40) this.undoStack.shift();
        this.redoStack.length = 0;
    };

    MaskCanvas.prototype.paintAt = function (pos, value) {
        var radius = (this.brushCells - 1) / 2;
        var lo = Math.ceil(-radius), hi = Math.floor(radius);
        for (var dr = lo; dr <= hi; dr++) {
            for (var dc = lo; dc <= hi; dc++) {
                if (this.brushCells > 2 &&
                    dr * dr + dc * dc > radius * radius + 0.25) continue;
                var r = pos.r + dr, c = pos.c + dc;
                if (r < 0 || c < 0 || r >= this.grid || c >= this.grid) continue;
                this.cells[r * this.grid + c] = value;
            }
        }
    };

    MaskCanvas.prototype.commitRect = function (value) {
        var r1 = Math.min(this.rectStart.r, this.rectNow.r);
        var r2 = Math.max(this.rectStart.r, this.rectNow.r);
        var c1 = Math.min(this.rectStart.c, this.rectNow.c);
        var c2 = Math.max(this.rectStart.c, this.rectNow.c);
        for (var r = r1; r <= r2; r++) {
            for (var c = c1; c <= c2; c++) this.cells[r * this.grid + c] = value;
        }
    };

    MaskCanvas.prototype.onDown = function (event) {
        event.preventDefault();
        this.drawing = true;
        this.pushUndo();
        var pos = this.cellAt(event);
        if (this.tool === "rect" || this.tool === "rectErase") {
            this.rectStart = this.rectNow = pos;
        } else {
            this.paintAt(pos, this.tool === "brush" ? 1 : 0);
        }
        this.redraw();
    };

    MaskCanvas.prototype.onMove = function (event) {
        if (!this.drawing) return;
        event.preventDefault();
        var pos = this.cellAt(event);
        if (this.tool === "rect" || this.tool === "rectErase") {
            this.rectNow = pos;
        } else {
            this.paintAt(pos, this.tool === "brush" ? 1 : 0);
        }
        this.redraw();
    };

    MaskCanvas.prototype.onUp = function () {
        if (!this.drawing) return;
        this.drawing = false;
        if (this.rectStart && this.rectNow) {
            this.commitRect(this.tool === "rectErase" ? 0 : 1);
            this.rectStart = this.rectNow = null;
        }
        this.redraw();
        this.onChange(this.encode());
    };

    MaskCanvas.prototype.setTool = function (tool) { this.tool = tool; };
    MaskCanvas.prototype.setBrush = function (n) {
        this.brushCells = clamp(n, 1, 31);
    };
    MaskCanvas.prototype.undo = function () {
        if (!this.undoStack.length) return;
        this.redoStack.push(this.cells.slice());
        this.cells = this.undoStack.pop();
        this.redraw();
        this.onChange(this.encode());
    };
    MaskCanvas.prototype.redo = function () {
        if (!this.redoStack.length) return;
        this.undoStack.push(this.cells.slice());
        this.cells = this.redoStack.pop();
        this.redraw();
        this.onChange(this.encode());
    };
    MaskCanvas.prototype.clear = function () {
        this.pushUndo();
        this.cells = this.blank();
        this.redraw();
        this.onChange(this.encode());
    };

    MaskCanvas.prototype.sizeFor = function (image) {
        var w = image.width || this.MIN_W;
        var h = image.height || Math.round(this.MIN_W * 0.6);
        var scale = Math.min(this.MAX_W / w, this.MAX_H / h);
        if (w * scale < this.MIN_W) scale = this.MIN_W / w;
        this.canvas.width = Math.max(1, Math.round(w * scale));
        this.canvas.height = Math.max(1, Math.round(h * scale));
    };

    MaskCanvas.prototype.loadBackground = function (done) {
        var self = this;
        $.ajax({ url: this.snapshotUrl, type: "GET", dataType: "json" })
            .done(function (response) {
                if (response.maskGrid && response.maskGrid !== self.grid) {
                    self.grid = response.maskGrid;
                }
                var image = new Image();
                image.onload = function () {
                    self.background = image;
                    self.sizeFor(image);
                    self.hasCamera = response.hasCamera;
                    if (done) done();
                    self.redraw();
                };
                image.onerror = function () {
                    self.background = null;
                    self.sizeFor({ width: self.MIN_W, height: 384 });
                    self.hasCamera = false;
                    if (done) done();
                    self.redraw();
                };
                image.src = response.image;
            })
            .fail(function () {
                self.background = null;
                self.sizeFor({ width: self.MIN_W, height: 384 });
                self.hasCamera = false;
                if (done) done();
                self.redraw();
            });
    };

    // Teardown and setup follow KO binding lifetime to avoid listener leaks.
    ko.bindingHandlers.maskCanvas = {
        init: function (element, valueAccessor) {
            var options = ko.unwrap(valueAccessor());
            var editor = new MaskCanvas(element, {
                gridSize: ko.unwrap(options.gridSize) || 128,
                mask: ko.unwrap(options.mask),
                status: options.status,
                onChange: function (data) { options.mask(data); }
            });
            element._maskEditor = editor;
            if (options.editor) options.editor(editor);
            ko.utils.domNodeDisposal.addDisposeCallback(element, function () {
                editor.destroy();
                delete element._maskEditor;
                if (options.editor) options.editor(null);
            });
        },
        update: function (element, valueAccessor) {
            var options = ko.unwrap(valueAccessor());
            if (element._maskEditor) {
                element._maskEditor.setMask(ko.unwrap(options.mask));
            }
        }
    };

    window.PiNozCamMaskCanvas = MaskCanvas;
}());
