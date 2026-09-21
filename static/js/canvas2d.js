/**
 * OpenFOAM 2D Web GUI — Canvas Drawing Module
 *
 * Tools: Select, Line (polyline), Spline (Catmull-Rom), Delete
 *
 * The canvas builds a list of "shapes". Each shape is one continuous
 * polyline or spline curve made up of consecutive points. Multiple shapes
 * can coexist (e.g. an airfoil drawn as one closed spline + a deflector
 * drawn as a separate closed polyline). Export iterates over the shapes,
 * not over canvas objects, so we never mix up points from different shapes.
 */

const Canvas2D = (() => {
    let fc;                       // fabric.Canvas
    let currentTool = "select";
    let gridEnabled = true;
    let snapEnabled = true;
    let gridSize = 20;            // px
    let scale = 100;              // px per metre

    // Active drawing (resets when the shape is finished/closed)
    let drawingPoints = [];       // array of {x,y} canvas-px
    let pointMarkers = [];        // fabric.Circle objects for the active draw
    let segmentObjects = [];      // line segments / preview spline for active draw
    let tempLine = null;          // rubber-band preview line

    // All committed shapes
    // Each shape: {type:"polyline"|"spline", closed:bool, points:[{x,y}], objects:[fabric...]}
    let shapes = [];

    const COLORS = {
        shape: "#1565c0",
        point: "#d32f2f",
        grid: "#e8e8e8",
        gridMajor: "#cccccc",
        temp: "#90caf9",
        domain: "#00897b",
    };

    let domainObjects = [];
    let lastDomain = null;

    function init(canvasId) {
        const container = document.getElementById("canvas-container");
        fc = new fabric.Canvas(canvasId, {
            width: container.clientWidth,
            height: container.clientHeight,
            backgroundColor: "#fafafa",
            selection: false,
        });

        drawGrid();
        setupEvents();
        setupKeyboard();
        updateScale();

        window.addEventListener("resize", () => {
            fc.setWidth(container.clientWidth);
            fc.setHeight(container.clientHeight);
            drawGrid();
            if (lastDomain) drawDomainBounds(lastDomain.xmin, lastDomain.xmax, lastDomain.ymin, lastDomain.ymax);
            fc.renderAll();
        });

        return fc;
    }

    // ---------------------------------------------------------- grid
    function drawGrid() {
        fc.getObjects("line").forEach(o => { if (o._isGrid) fc.remove(o); });
        if (!gridEnabled) return;
        const w = fc.getWidth(), h = fc.getHeight(), step = gridSize;
        for (let x = 0; x <= w; x += step) {
            const major = Math.round(x / step) % 5 === 0;
            const ln = new fabric.Line([x, 0, x, h], {
                stroke: major ? COLORS.gridMajor : COLORS.grid,
                strokeWidth: major ? 0.8 : 0.4,
                selectable: false, evented: false, excludeFromExport: true,
            });
            ln._isGrid = true; fc.add(ln); fc.sendToBack(ln);
        }
        for (let y = 0; y <= h; y += step) {
            const major = Math.round(y / step) % 5 === 0;
            const ln = new fabric.Line([0, y, fc.getWidth(), y], {
                stroke: major ? COLORS.gridMajor : COLORS.grid,
                strokeWidth: major ? 0.8 : 0.4,
                selectable: false, evented: false, excludeFromExport: true,
            });
            ln._isGrid = true; fc.add(ln); fc.sendToBack(ln);
        }
    }

    // ---------------------------------------------------------- coords
    function snap(x, y) {
        if (!snapEnabled) return { x, y };
        return {
            x: Math.round(x / gridSize) * gridSize,
            y: Math.round(y / gridSize) * gridSize,
        };
    }

    function canvasToMeters(px, py) {
        const cx = fc.getWidth() / 2, cy = fc.getHeight() / 2;
        return { x: (px - cx) / scale, y: -(py - cy) / scale };
    }

    function metersToCanvas(mx, my) {
        const cx = fc.getWidth() / 2, cy = fc.getHeight() / 2;
        return { x: cx + mx * scale, y: cy - my * scale };
    }

    function drawDomainBounds(xmin, xmax, ymin, ymax) {
        lastDomain = { xmin, xmax, ymin, ymax };
        domainObjects.forEach(o => fc.remove(o));
        domainObjects = [];

        const tl = metersToCanvas(xmin, ymax);
        const br = metersToCanvas(xmax, ymin);
        const w = br.x - tl.x;
        const h = br.y - tl.y;

        const rect = new fabric.Rect({
            left: tl.x, top: tl.y, width: w, height: h,
            fill: "transparent",
            stroke: COLORS.domain, strokeWidth: 1.8,
            strokeDashArray: [8, 4],
            selectable: false, evented: false,
        });
        rect._isDomain = true;
        domainObjects.push(rect);
        fc.add(rect);

        const labels = [
            { text: "inlet",  x: tl.x,      y: (tl.y + br.y) / 2, angle: -90 },
            { text: "outlet", x: br.x,      y: (tl.y + br.y) / 2, angle: 90 },
            { text: "top",    x: (tl.x + br.x) / 2, y: tl.y, angle: 0 },
            { text: "bottom", x: (tl.x + br.x) / 2, y: br.y, angle: 0 },
        ];
        const offsets = [
            { dx: -14, dy: 0 },
            { dx: 14, dy: 0 },
            { dx: 0, dy: -12 },
            { dx: 0, dy: 12 },
        ];
        labels.forEach((lb, i) => {
            const txt = new fabric.Text(lb.text, {
                left: lb.x + offsets[i].dx,
                top: lb.y + offsets[i].dy,
                fontSize: 11,
                fill: COLORS.domain,
                fontFamily: "monospace",
                originX: "center", originY: "center",
                angle: lb.angle,
                selectable: false, evented: false,
            });
            txt._isDomain = true;
            domainObjects.push(txt);
            fc.add(txt);
        });

        fc.renderAll();
    }

    // ---------------------------------------------------------- events
    // ----- Pan & zoom state (Ctrl+drag to pan, wheel to zoom) -----
    let isPanning = false;
    let lastPanX = 0, lastPanY = 0;

    function setupEvents() {
        fc.on("mouse:move", (opt) => {
            // Panning takes precedence over everything else
            if (isPanning) {
                const e = opt.e;
                const dx = e.clientX - lastPanX;
                const dy = e.clientY - lastPanY;
                lastPanX = e.clientX;
                lastPanY = e.clientY;
                fc.relativePan(new fabric.Point(dx, dy));
                return;
            }

            const p = fc.getPointer(opt.e);
            const s = snap(p.x, p.y);
            const m = canvasToMeters(s.x, s.y);
            document.getElementById("canvas-coords").textContent =
                `X: ${m.x.toFixed(4)}  Y: ${m.y.toFixed(4)} m`;

            // Hint the user that Ctrl puts the canvas in pan mode
            if (opt.e.ctrlKey) {
                fc.defaultCursor = "grab";
                fc.hoverCursor   = "grab";
            } else {
                fc.defaultCursor = currentTool === "select" ? "default" : "crosshair";
                fc.hoverCursor   = currentTool === "select" ? "move"    : "crosshair";
            }

            if ((currentTool === "line" || currentTool === "spline") && drawingPoints.length > 0) {
                if (tempLine) fc.remove(tempLine);
                const last = drawingPoints[drawingPoints.length - 1];
                tempLine = new fabric.Line([last.x, last.y, s.x, s.y], {
                    stroke: COLORS.temp, strokeWidth: 1.5,
                    strokeDashArray: [5, 3],
                    selectable: false, evented: false,
                });
                fc.add(tempLine);
                fc.renderAll();
            }
        });

        fc.on("mouse:down", (opt) => {
            if (opt.e.button !== 0) return;

            // Ctrl + left-click starts a pan, regardless of the active tool.
            // We must NOT call any tool handler in this branch so the user
            // can pan freely while in line/spline/delete mode.
            if (opt.e.ctrlKey) {
                isPanning = true;
                lastPanX = opt.e.clientX;
                lastPanY = opt.e.clientY;
                fc.defaultCursor = "grabbing";
                fc.hoverCursor   = "grabbing";
                fc.selection = false;
                return;
            }

            const p = fc.getPointer(opt.e);
            const s = snap(p.x, p.y);
            if (currentTool === "line")        handleLineClick(s);
            else if (currentTool === "spline") handleSplineClick(s);
            else if (currentTool === "delete") handleDeleteClick(opt);
        });

        fc.on("mouse:up", () => {
            if (isPanning) {
                isPanning = false;
                // Restore tool cursor
                fc.defaultCursor = currentTool === "select" ? "default" : "crosshair";
                fc.hoverCursor   = currentTool === "select" ? "move"    : "crosshair";
            }
        });

        fc.on("mouse:dblclick", () => {
            if (currentTool === "line" || currentTool === "spline") {
                finishDrawing();
            }
        });

        // Mouse wheel: zoom centered on the cursor. Ctrl+wheel is a more
        // common convention but Ctrl is already taken by panning here, so
        // we use the plain wheel.
        fc.on("mouse:wheel", (opt) => {
            const delta = opt.e.deltaY;
            let zoom = fc.getZoom();
            zoom *= 0.999 ** delta;
            zoom = Math.max(0.1, Math.min(zoom, 20));
            fc.zoomToPoint(new fabric.Point(opt.e.offsetX, opt.e.offsetY), zoom);
            opt.e.preventDefault();
            opt.e.stopPropagation();
        });
    }

    function setupKeyboard() {
        document.addEventListener("keydown", (e) => {
            const t = e.target.tagName;
            if (t === "INPUT" || t === "SELECT" || t === "TEXTAREA") return;
            switch (e.key.toLowerCase()) {
                case "v": setTool("select"); break;
                case "l": setTool("line"); break;
                case "s": setTool("spline"); break;
                case "d": setTool("delete"); break;
                case "escape": cancelDrawing(); break;
                case "enter": finishDrawing(); break;
                case "0":
                case "home":
                    // Reset pan & zoom to identity
                    fc.setViewportTransform([1, 0, 0, 1, 0, 0]);
                    fc.renderAll();
                    break;
            }
        });
    }

    // ---------------------------------------------------------- markers
    function addPointMarker(pt) {
        const c = new fabric.Circle({
            radius: 4,
            fill: COLORS.point, stroke: "#fff", strokeWidth: 1.5,
            originX: "center", originY: "center",
            left: pt.x, top: pt.y,
            selectable: false, evented: false,
        });
        c._isControlPoint = true;
        fc.add(c);
        pointMarkers.push(c);
    }

    // ---------------------------------------------------------- LINE tool
    function handleLineClick(pt) {
        if (drawingPoints.length >= 3) {
            const first = drawingPoints[0];
            if (Math.hypot(pt.x - first.x, pt.y - first.y) < 10) {
                closeShape();
                return;
            }
        }
        drawingPoints.push(pt);
        addPointMarker(pt);

        if (drawingPoints.length > 1) {
            const prev = drawingPoints[drawingPoints.length - 2];
            const seg = new fabric.Line([prev.x, prev.y, pt.x, pt.y], {
                stroke: COLORS.shape, strokeWidth: 2,
                selectable: false, evented: false,
            });
            seg._isShape = true;
            fc.add(seg);
            segmentObjects.push(seg);
        }
        fc.renderAll();
    }

    // ---------------------------------------------------------- SPLINE tool
    function handleSplineClick(pt) {
        if (drawingPoints.length >= 3) {
            const first = drawingPoints[0];
            if (Math.hypot(pt.x - first.x, pt.y - first.y) < 10) {
                closeShape();
                return;
            }
        }
        drawingPoints.push(pt);
        addPointMarker(pt);
        if (drawingPoints.length >= 2) redrawSplinePreview();
        fc.renderAll();
    }

    function catmullRomToBezier(points, closed) {
        const n = points.length;
        if (n < 2) return "";
        let path = `M ${points[0].x} ${points[0].y}`;
        const pts = closed
            ? [points[n - 1], ...points, points[0], points[1]]
            : [points[0], ...points, points[n - 1]];
        for (let i = 1; i < pts.length - 2; i++) {
            const p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2];
            const cp1x = p1.x + (p2.x - p0.x) / 6;
            const cp1y = p1.y + (p2.y - p0.y) / 6;
            const cp2x = p2.x - (p3.x - p1.x) / 6;
            const cp2y = p2.y - (p3.y - p1.y) / 6;
            path += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${p2.x} ${p2.y}`;
        }
        if (closed) path += " Z";
        return path;
    }

    function redrawSplinePreview() {
        segmentObjects.forEach(o => fc.remove(o));
        segmentObjects = [];
        if (drawingPoints.length < 2) return;

        const pathStr = catmullRomToBezier(drawingPoints, false);
        const p = new fabric.Path(pathStr, {
            fill: "", stroke: COLORS.shape, strokeWidth: 2,
            selectable: false, evented: false,
        });
        p._isShape = true;
        fc.add(p);
        segmentObjects.push(p);
    }

    // ---------------------------------------------------------- commit shapes
    function commitShape(type, closed) {
        const shape = {
            type, closed,
            points: drawingPoints.slice(),    // copy
            objects: segmentObjects.slice().concat(pointMarkers.slice()),
        };
        shapes.push(shape);
        // Don't remove the objects from canvas — they stay as the
        // visual representation of the shape. Just clear the active
        // drawing buffers.
        drawingPoints = [];
        pointMarkers = [];
        segmentObjects = [];
        if (tempLine) { fc.remove(tempLine); tempLine = null; }
    }

    function closeShape() {
        if (drawingPoints.length < 3) return;

        if (currentTool === "line") {
            const last = drawingPoints[drawingPoints.length - 1];
            const first = drawingPoints[0];
            const seg = new fabric.Line([last.x, last.y, first.x, first.y], {
                stroke: COLORS.shape, strokeWidth: 2,
                selectable: false, evented: false,
            });
            seg._isShape = true;
            fc.add(seg);
            segmentObjects.push(seg);
            commitShape("polyline", true);
            logMessage(`Forma chiusa: polilinea con ${shapes[shapes.length-1].points.length} punti.`, "ok");
        } else if (currentTool === "spline") {
            segmentObjects.forEach(o => fc.remove(o));
            segmentObjects = [];
            const pathStr = catmullRomToBezier(drawingPoints, true);
            const p = new fabric.Path(pathStr, {
                fill: "rgba(21,101,192,0.08)",
                stroke: COLORS.shape, strokeWidth: 2,
                selectable: false, evented: false,
            });
            p._isShape = true;
            fc.add(p);
            segmentObjects.push(p);
            commitShape("spline", true);
            logMessage(`Forma chiusa: spline con ${shapes[shapes.length-1].points.length} punti.`, "ok");
        }

        fc.renderAll();
    }

    function finishDrawing() {
        if (drawingPoints.length < 2) { cancelDrawing(); return; }
        const type = currentTool === "spline" ? "spline" : "polyline";
        commitShape(type, false);
        logMessage(`Forma aperta salvata (${type}). Clicca vicino al primo punto per chiuderla.`, "warn");
        fc.renderAll();
    }

    function cancelDrawing() {
        if (tempLine) { fc.remove(tempLine); tempLine = null; }
        pointMarkers.forEach(m => fc.remove(m));
        segmentObjects.forEach(o => fc.remove(o));
        pointMarkers = []; segmentObjects = []; drawingPoints = [];
        fc.renderAll();
    }

    // ---------------------------------------------------------- delete tool
    // Distance from point (px,py) to the segment (x1,y1)-(x2,y2)
    function pointToSegmentDist(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1, dy = y2 - y1;
        const len2 = dx * dx + dy * dy;
        if (len2 === 0) return Math.hypot(px - x1, py - y1);
        let t = ((px - x1) * dx + (py - y1) * dy) / len2;
        t = Math.max(0, Math.min(1, t));
        const ix = x1 + t * dx, iy = y1 + t * dy;
        return Math.hypot(px - ix, py - iy);
    }

    function rebuildPolylineObjects(sh) {
        // Remove old canvas objects belonging to this shape, then recreate
        // markers + segments from the (possibly modified) points list.
        sh.objects.forEach(o => fc.remove(o));
        sh.objects = [];
        const pts = sh.points;
        // Point markers first (drawn under segments? same z order, ok)
        pts.forEach(pt => {
            const c = new fabric.Circle({
                radius: 4,
                fill: COLORS.point, stroke: "#fff", strokeWidth: 1.5,
                originX: "center", originY: "center",
                left: pt.x, top: pt.y,
                selectable: false, evented: false,
            });
            c._isControlPoint = true;
            fc.add(c);
            sh.objects.push(c);
        });
        // Segments
        const n = pts.length;
        for (let i = 0; i < n - 1; i++) {
            const seg = new fabric.Line([pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y], {
                stroke: COLORS.shape, strokeWidth: 2,
                selectable: false, evented: false,
            });
            seg._isShape = true;
            fc.add(seg);
            sh.objects.push(seg);
        }
        // Closing segment if still closed
        if (sh.closed && n >= 3) {
            const seg = new fabric.Line([pts[n - 1].x, pts[n - 1].y, pts[0].x, pts[0].y], {
                stroke: COLORS.shape, strokeWidth: 2,
                selectable: false, evented: false,
            });
            seg._isShape = true;
            fc.add(seg);
            sh.objects.push(seg);
        }
    }

    function deleteSegmentFromPolyline(sh, shapeIdx, segIdx) {
        const N = sh.points.length;
        if (sh.closed) {
            // Reorder so the deleted segment becomes the wrap-around;
            // the shape becomes an open polyline with all N points kept.
            const newPoints = [];
            for (let k = 0; k < N; k++) {
                newPoints.push(sh.points[(segIdx + 1 + k) % N]);
            }
            sh.points = newPoints;
            sh.closed = false;
            rebuildPolylineObjects(sh);
            logMessage(`Tratto eliminato. La forma ora è aperta (${N} punti).`, "warn");
        } else {
            // Open polyline → splits into two
            const first = sh.points.slice(0, segIdx + 1);
            const second = sh.points.slice(segIdx + 1);
            sh.points = first;
            rebuildPolylineObjects(sh);
            if (second.length >= 1) {
                const newShape = { type: "polyline", closed: false, points: second, objects: [] };
                shapes.push(newShape);
                rebuildPolylineObjects(newShape);
            }
            // Clean up degenerate shapes (≤1 point)
            if (sh.points.length <= 1) {
                sh.objects.forEach(o => fc.remove(o));
                shapes.splice(shapeIdx, 1);
            }
            logMessage(`Tratto eliminato. Forma divisa in due (${first.length} + ${second.length} punti).`, "warn");
        }
    }

    function handleDeleteClick(opt) {
        const pointer = fc.getPointer(opt.e);
        const threshold = 10;  // px (in canvas coords)

        // 1) Closest polyline segment within threshold wins
        let bestDist = threshold;
        let hit = null;
        for (let si = 0; si < shapes.length; si++) {
            const sh = shapes[si];
            if (sh.type !== "polyline") continue;
            const segs = sh.objects.filter(o => o instanceof fabric.Line);
            for (let i = 0; i < segs.length; i++) {
                const s = segs[i];
                const d = pointToSegmentDist(pointer.x, pointer.y, s.x1, s.y1, s.x2, s.y2);
                if (d < bestDist) {
                    bestDist = d;
                    hit = { shape: sh, shapeIdx: si, segIdx: i };
                }
            }
        }
        if (hit) {
            deleteSegmentFromPolyline(hit.shape, hit.shapeIdx, hit.segIdx);
            fc.renderAll();
            return;
        }

        // 2) Fallback: spline (fabric.Path) — delete the whole shape if the
        //    click is inside its bounding box. Splines are single Path
        //    objects, you can't remove just one piece.
        for (let i = shapes.length - 1; i >= 0; i--) {
            const sh = shapes[i];
            if (sh.type !== "spline") continue;
            const path = sh.objects.find(o => o instanceof fabric.Path);
            if (path && path.containsPoint(pointer)) {
                sh.objects.forEach(o => fc.remove(o));
                shapes.splice(i, 1);
                fc.renderAll();
                logMessage("Spline eliminata.", "warn");
                return;
            }
        }
    }

    // ---------------------------------------------------------- tool
    function setTool(tool) {
        if (drawingPoints.length > 0) finishDrawing();
        currentTool = tool;
        document.querySelectorAll(".ct-btn[data-tool]").forEach(b => {
            b.classList.toggle("active", b.dataset.tool === tool);
        });
        fc.defaultCursor = tool === "select" ? "default" : "crosshair";
        fc.hoverCursor   = tool === "select" ? "move"    : "crosshair";
    }

    function clearCanvas() {
        cancelDrawing();
        shapes.forEach(s => s.objects.forEach(o => fc.remove(o)));
        shapes = [];
        fc.renderAll();
    }

    function updateScale() {
        const el = document.getElementById("canvas-scale");
        if (!el) return;
        scale = Math.max(1, parseInt(el.value, 10) || 100);
        gridSize = Math.max(10, Math.round(scale / 5));
        drawGrid();
        if (lastDomain) drawDomainBounds(lastDomain.xmin, lastDomain.xmax, lastDomain.ymin, lastDomain.ymax);
        fc.renderAll();
    }

    // ---------------------------------------------------------- export
    /**
     * Build the geometry payload sent to /api/mesh.
     *
     * Strategy: walk each committed shape, deduplicate its points (so two
     * shapes sharing a vertex use the same point index), and emit one curve
     * entry per line segment / spline.
     *
     * For a closed POLYLINE with N points P0..P(N-1):
     *   emit N "line" curves: (P0,P1), (P1,P2), ..., (P(N-1),P0)
     * For a closed SPLINE with N points:
     *   emit ONE "spline" curve listing all N points, with P0 appended
     *   at the end so gmsh closes it.
     * For an open polyline/spline: same as above without the closing piece.
     */
    function exportGeometry() {
        // Commit any unsaved drawing
        if (drawingPoints.length >= 2) finishDrawing();

        if (shapes.length === 0) return null;

        const points = [];                  // [[x,y]] in metres
        const indexMap = new Map();         // "x,y" canvas-px -> point index

        function addPoint(pt) {
            const key = `${Math.round(pt.x)},${Math.round(pt.y)}`;
            if (indexMap.has(key)) return indexMap.get(key);
            const m = canvasToMeters(pt.x, pt.y);
            indexMap.set(key, points.length);
            points.push([m.x, m.y]);
            return points.length - 1;
        }

        const curves = [];
        // Track which curves belong to which closed shape so the backend
        // can build one gmsh curve loop per shape (multiple obstacles in
        // the same domain). Without this, all curves from all shapes get
        // dumped into a single loop and gmsh fails because they don't
        // form one continuous closed boundary.
        const shape_loops = [];

        for (const sh of shapes) {
            const idxs = sh.points.map(addPoint);
            const startIdx = curves.length;
            if (sh.type === "polyline") {
                for (let i = 0; i < idxs.length - 1; i++) {
                    curves.push({ type: "line", points: [idxs[i], idxs[i + 1]] });
                }
                if (sh.closed && idxs.length >= 3) {
                    curves.push({ type: "line", points: [idxs[idxs.length - 1], idxs[0]] });
                }
            } else if (sh.type === "spline") {
                // gmsh addSpline needs >=3 points. We pass all points,
                // and for a closed spline append the first point again so
                // the curve closes back on itself.
                const splinePts = sh.closed ? idxs.concat([idxs[0]]) : idxs;
                if (splinePts.length < 3) {
                    // Degenerate: fall back to a line
                    for (let i = 0; i < splinePts.length - 1; i++) {
                        curves.push({ type: "line", points: [splinePts[i], splinePts[i + 1]] });
                    }
                } else {
                    curves.push({ type: "spline", points: splinePts });
                }
            }
            if (sh.closed && curves.length > startIdx) {
                shape_loops.push({
                    curves: Array.from(
                        { length: curves.length - startIdx },
                        (_, i) => startIdx + i,
                    ),
                });
            }
        }

        return {
            shape_points: points,
            shape_curves: curves,
            shape_loops: shape_loops,
        };
    }

    function logMessage(msg, type) {
        if (window.App && window.App.log) window.App.log(msg, type);
    }

    return {
        init,
        setTool,
        clearCanvas,
        updateScale,
        exportGeometry,
        drawDomainBounds,
        setGrid: (on) => { gridEnabled = on; drawGrid(); fc.renderAll(); },
        setSnap: (on) => { snapEnabled = on; },
        getCanvas: () => fc,
        getShapeCount: () => shapes.length,
    };
})();
