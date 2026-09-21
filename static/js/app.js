/**
 * OpenFOAM 2D Web GUI — Main Application Controller
 */

const App = (() => {
    let currentCase = null;
    let solverPolling = null;
    let logLineIndex = 0;

    function init() {
        Canvas2D.init("fabric-canvas");

        // Toolbar buttons
        document.getElementById("btn-new").addEventListener("click", showNewCaseModal);
        document.getElementById("btn-open").addEventListener("click", showOpenCaseModal);
        document.getElementById("btn-save").addEventListener("click", saveCase);
        document.getElementById("btn-mesh").addEventListener("click", generateMesh);
        document.getElementById("btn-setup").addEventListener("click", applySetup);
        document.getElementById("btn-simulate").addEventListener("click", startSolver);
        document.getElementById("btn-stop").addEventListener("click", stopSolver);
        document.getElementById("btn-residuals").addEventListener("click", showResiduals);
        document.getElementById("btn-paraview").addEventListener("click", launchParaView);
        document.getElementById("btn-apply").addEventListener("click", applySetup);

        // Canvas tool buttons
        document.querySelectorAll(".ct-btn[data-tool]").forEach(btn => {
            btn.addEventListener("click", () => Canvas2D.setTool(btn.dataset.tool));
        });
        document.getElementById("btn-clear-canvas").addEventListener("click", () => {
            if (confirm("Cancellare tutta la geometria?")) Canvas2D.clearCanvas();
        });
        document.getElementById("btn-close-shape").addEventListener("click", () => {
            // Trigger close shape by simulating Enter
            document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
        });

        // Grid / Snap toggles
        document.getElementById("chk-grid").addEventListener("change", (e) => Canvas2D.setGrid(e.target.checked));
        document.getElementById("chk-snap").addEventListener("change", (e) => Canvas2D.setSnap(e.target.checked));
        document.getElementById("canvas-scale").addEventListener("change", () => Canvas2D.updateScale());

        // Domain bounds — draw on load + live update
        updateDomainBounds();
        ["set-domain-xmin", "set-domain-xmax", "set-domain-ymin", "set-domain-ymax"].forEach(id => {
            document.getElementById(id).addEventListener("input", updateDomainBounds);
        });

        // ParaView animation helper — live recompute + apply button
        const animInputs = ["set-video-duration", "set-video-fps", "set-video-saves", "set-endTime"];
        animInputs.forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener("input", updateAnimCalc);
        });
        document.getElementById("btn-apply-anim").addEventListener("click", applyAnimWriteInterval);
        updateAnimCalc();

        // Refinement slider
        const refSlider = document.getElementById("set-refinement");
        const refVal = document.getElementById("refinement-val");
        refSlider.addEventListener("input", () => { refVal.textContent = refSlider.value; });

        // Solver preset: when user picks a different solver, auto-fill
        // endTime / deltaT / writeInterval with sensible defaults and update
        // the inline hints so they describe the right regime.
        const solverSel = document.getElementById("set-solver");
        solverSel.addEventListener("change", () => applySolverPreset(solverSel.value));

        // 2D / 3D mode switch
        document.querySelectorAll(".mode-btn").forEach(btn => {
            btn.addEventListener("click", () => setMode(btn.dataset.mode));
        });

        // STL upload (click-to-pick + drag&drop)
        const dropzone = document.getElementById("stl-dropzone");
        const fileInput = document.getElementById("stl-file-input");
        dropzone.addEventListener("click", () => fileInput.click());
        fileInput.addEventListener("change", () => {
            if (fileInput.files.length > 0) uploadStl(fileInput.files[0]);
        });
        dropzone.addEventListener("dragover", (e) => {
            e.preventDefault(); dropzone.classList.add("dragover");
        });
        dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
        dropzone.addEventListener("drop", (e) => {
            e.preventDefault(); dropzone.classList.remove("dragover");
            const f = e.dataTransfer.files[0];
            if (f) uploadStl(f);
        });

        // Section collapsing
        document.querySelectorAll(".section-header").forEach(header => {
            header.addEventListener("click", () => {
                const target = document.getElementById(header.dataset.target);
                header.classList.toggle("collapsed");
                target.classList.toggle("hidden");
            });
        });

        // Tabs
        document.querySelectorAll(".tab-btn").forEach(btn => {
            btn.addEventListener("click", () => switchTab(btn.dataset.tab));
        });

        // Output
        document.getElementById("btn-clear-output").addEventListener("click", clearOutput);

        // Modal
        document.getElementById("modal-close").addEventListener("click", closeModal);
        document.getElementById("modal-overlay").addEventListener("click", (e) => {
            if (e.target === e.currentTarget) closeModal();
        });

        log("OpenFOAM 2D Web GUI avviata. Crea un nuovo caso per iniziare.", "ok");
    }

    // ===== LOGGING =====
    // Keep the rendered log capped so the DOM doesn't grow unbounded —
    // rhoCentralFoam at dt=1e-6 prints tens of thousands of lines per
    // minute and the browser becomes unresponsive after a few minutes if
    // every line keeps its own <span> in the DOM.
    const MAX_LOG_LINES = 2000;
    let logLineCount = 0;

    function log(msg, type) {
        const el = document.getElementById("log-output");
        const span = document.createElement("span");
        if (type === "error") span.className = "log-error";
        else if (type === "warn") span.className = "log-warn";
        else if (type === "ok") span.className = "log-ok";
        span.textContent = msg + "\n";
        el.appendChild(span);
        logLineCount++;
        if (logLineCount > MAX_LOG_LINES) {
            // Trim a chunk at a time (cheaper than removing one node per line)
            const toRemove = logLineCount - MAX_LOG_LINES;
            for (let i = 0; i < toRemove && el.firstChild; i++) {
                el.removeChild(el.firstChild);
            }
            logLineCount = MAX_LOG_LINES;
        }
        el.scrollTop = el.scrollHeight;
    }

    // Batch many lines into a single DOM operation. Used by the solver
    // polling loop, which can receive hundreds of lines per tick.
    function logBatch(lines) {
        if (!lines || lines.length === 0) return;
        const el = document.getElementById("log-output");
        const frag = document.createDocumentFragment();
        for (const line of lines) {
            const span = document.createElement("span");
            span.textContent = line + "\n";
            frag.appendChild(span);
        }
        el.appendChild(frag);
        logLineCount += lines.length;
        if (logLineCount > MAX_LOG_LINES) {
            const toRemove = logLineCount - MAX_LOG_LINES;
            for (let i = 0; i < toRemove && el.firstChild; i++) {
                el.removeChild(el.firstChild);
            }
            logLineCount = MAX_LOG_LINES;
        }
        el.scrollTop = el.scrollHeight;
    }

    function clearOutput() {
        document.getElementById("log-output").innerHTML = "";
        logLineCount = 0;
    }

    // ===== TABS =====
    function switchTab(tabId) {
        document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
        document.querySelector(`.tab-btn[data-tab="${tabId}"]`).classList.add("active");

        // Show/hide tab content
        document.querySelectorAll(".tab-content").forEach(tc => tc.style.display = "none");
        const tabEl = document.getElementById(tabId);
        if (tabEl) tabEl.style.display = "block";

        // Show/hide canvas
        const canvasContainer = document.getElementById("canvas-container");
        const canvasToolbar = document.getElementById("canvas-toolbar");
        if (tabId === "tab-canvas") {
            canvasContainer.style.display = "block";
            canvasToolbar.style.display = "flex";
        } else {
            canvasContainer.style.display = "none";
            canvasToolbar.style.display = "none";
        }

        if (tabId === "tab-residuals") {
            fetchResiduals();
        }
    }

    // ===== MODAL =====
    function showModal(title, bodyHtml) {
        document.getElementById("modal-title").textContent = title;
        document.getElementById("modal-body").innerHTML = bodyHtml;
        document.getElementById("modal-overlay").style.display = "flex";
    }

    function closeModal() {
        document.getElementById("modal-overlay").style.display = "none";
    }

    // ===== CASE MANAGEMENT =====
    function showNewCaseModal() {
        showModal("Nuovo Caso", `
            <label style="font-size:13px;display:block;margin-bottom:4px;">Nome del caso:</label>
            <input type="text" id="new-case-name" placeholder="es. airfoil2D" value="caso_${Date.now().toString(36)}">
            <button onclick="App.createCase()">Crea</button>
        `);
    }

    async function createCase() {
        const name = document.getElementById("new-case-name").value.trim();
        if (!name) return;

        try {
            const resp = await fetch("/api/cases", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            const data = await resp.json();
            if (resp.ok) {
                currentCase = data.name;
                document.getElementById("case-name-display").textContent = currentCase;
                log(`Caso "${currentCase}" creato.`, "ok");
                closeModal();
            } else {
                log(`Errore: ${data.error}`, "error");
            }
        } catch (e) {
            log(`Errore di rete: ${e.message}`, "error");
        }
    }

    async function showOpenCaseModal() {
        try {
            const resp = await fetch("/api/cases");
            const cases = await resp.json();

            if (cases.length === 0) {
                showModal("Apri Caso", "<p>Nessun caso disponibile. Crea un nuovo caso.</p>");
                return;
            }

            let html = "";
            for (const c of cases) {
                html += `
                    <div class="case-item" onclick="App.openCase('${c.name}')">
                        <div>
                            <div class="case-label">${c.name}</div>
                            <div class="case-meta">${c.hasMesh ? "Mesh presente" : "Senza mesh"}</div>
                        </div>
                        <span class="case-delete" onclick="event.stopPropagation(); App.deleteCase('${c.name}')">&times;</span>
                    </div>
                `;
            }
            showModal("Apri Caso", html);
        } catch (e) {
            log(`Errore: ${e.message}`, "error");
        }
    }

    function openCase(name) {
        currentCase = name;
        document.getElementById("case-name-display").textContent = name;
        log(`Caso "${name}" aperto.`, "ok");
        closeModal();
    }

    async function deleteCase(name) {
        if (!confirm(`Eliminare il caso "${name}"?`)) return;
        await fetch(`/api/cases/${name}`, { method: "DELETE" });
        log(`Caso "${name}" eliminato.`, "warn");
        if (currentCase === name) {
            currentCase = null;
            document.getElementById("case-name-display").textContent = "Nessun caso";
        }
        showOpenCaseModal();
    }

    function saveCase() {
        if (!currentCase) {
            log("Nessun caso aperto da salvare.", "warn");
            return;
        }
        applySetup();
    }

    // ===== PARAVIEW ANIMATION =====
    // writeInterval = endTime / numero_timestep_salvati. ParaView interpola
    // linearmente tra i timestep salvati, quindi 30-60 timestep bastano per
    // un video fluido a qualunque durata/fps.
    function updateAnimCalc() {
        const endTime = pf("set-endTime");
        const saves = pf("set-video-saves");
        const dur = pf("set-video-duration");
        const fps = pf("set-video-fps");
        const out = document.getElementById("anim-writeInterval-calc");
        const note = document.getElementById("anim-explanation");
        if (!out || !note) return;
        if (!isFinite(endTime) || !isFinite(saves) || saves <= 0 || endTime <= 0) {
            out.textContent = "—";
            note.textContent = "Imposta endTime e timestep da salvare.";
            return;
        }
        const wi = endTime / saves;
        // Formatting: if endTime is an integer (steady-state iter count),
        // round writeInterval to integer; otherwise keep scientific notation.
        const isSteady = Number.isInteger(endTime) && endTime >= 10;
        const wiStr = isSteady ? Math.max(1, Math.round(wi)).toString()
                               : wi.toExponential(3);
        out.textContent = wiStr;
        const totalFrames = Math.round(dur * fps);
        const ratio = totalFrames / saves;
        note.textContent =
            `Video ${dur}s × ${fps}fps = ${totalFrames} frame. ` +
            `ParaView interpolera' ~${ratio.toFixed(1)} frame per ogni timestep salvato.`;
    }

    function applyAnimWriteInterval() {
        const endTime = pf("set-endTime");
        const saves = pf("set-video-saves");
        if (!isFinite(endTime) || !isFinite(saves) || saves <= 0) {
            log("Imposta endTime e timestep validi prima di applicare.", "warn");
            return;
        }
        const wi = endTime / saves;
        const isSteady = Number.isInteger(endTime) && endTime >= 10;
        const value = isSteady ? Math.max(1, Math.round(wi)) : wi;
        document.getElementById("set-writeInterval").value = value;
        log(`writeInterval impostato a ${value} (per ${saves} timestep su endTime=${endTime}).`, "ok");
    }

    // ===== DOMAIN BOUNDS =====
    function updateDomainBounds() {
        Canvas2D.drawDomainBounds(
            pf("set-domain-xmin"), pf("set-domain-xmax"),
            pf("set-domain-ymin"), pf("set-domain-ymax"),
        );
    }

    // ===== SETTINGS =====
    // Parse a number from an input id, accepting both `,` and `.` as the
    // decimal separator. JS parseFloat does NOT accept comma, so users who
    // type "0,71" would silently get 0. Italian keyboards default to comma
    // on the numpad — make it forgiving.
    function pf(id) {
        const raw = (document.getElementById(id).value || "").replace(",", ".");
        return parseFloat(raw);
    }

    // ----- Solver presets ------------------------------------------------
    // Each preset describes what the controlDict should look like and what
    // hints should appear next to the most regime-sensitive fields.
    const SOLVER_PRESETS = {
        rhoSimpleFoam: {
            endTime: 2000, deltaT: 1, writeInterval: 200,
            hints: {
                solver: "Steady-state, compressibile subsonico (M < 0.8). Cerca lo stato stazionario.",
                endTime: "Iterazioni: 1000–3000 (è uno steady, non secondi)",
                deltaT: "Passo iterazione: lascia 1",
                writeInterval: "Ogni N iterazioni: 100–500",
                inletUx: "30–300 m/s (subsonico)",
                outletP: "Atm: 101325 Pa",
            },
        },
        rhoPimpleFoam: {
            endTime: 1.0, deltaT: 1e-4, writeInterval: 0.05,
            hints: {
                solver: "Transient compressibile subsonico (M < 0.8).",
                endTime: "Secondi fisici: 0.5–10 s",
                deltaT: "Passo iniziale: 1e-4 — 1e-3 s (adjustTimeStep lo regola)",
                writeInterval: "Ogni X secondi: 0.01–0.1",
                inletUx: "30–300 m/s (subsonico)",
                outletP: "Atm: 101325 Pa",
            },
        },
        rhoCentralFoam: {
            endTime: 0.1, deltaT: 1e-6, writeInterval: 0.01,
            hints: {
                solver: "Supersonico/ipersonico (M > 1). Density-based esplicito.",
                endTime: "Secondi fisici: 0.05–0.5 s (basta per attraversare il dominio)",
                deltaT: "Iniziale: 1e-6 s (forzato dal backend, poi adjustTimeStep)",
                writeInterval: "Ogni X secondi: 0.005–0.02",
                inletUx: "350–2000 m/s (M > 1 a T=300 K)",
                outletP: "Atm: 101325 Pa (in supersonico viene estrapolato)",
            },
        },
        sonicFoam: {
            endTime: 0.1, deltaT: 1e-5, writeInterval: 0.01,
            hints: {
                solver: "Transonico/supersonico (M ~ 1+). Density-based implicito.",
                endTime: "Secondi fisici: 0.05–0.5 s",
                deltaT: "Iniziale: 1e-5 s",
                writeInterval: "Ogni X secondi: 0.005–0.02",
                inletUx: "300–1500 m/s",
                outletP: "Atm: 101325 Pa",
            },
        },
        simpleFoam: {
            endTime: 2000, deltaT: 1, writeInterval: 200,
            hints: {
                solver: "Steady-state, incompressibile (M < 0.3, no temperatura).",
                endTime: "Iterazioni: 1000–5000",
                deltaT: "Passo iterazione: lascia 1",
                writeInterval: "Ogni N iterazioni: 100–500",
                inletUx: "0.1–50 m/s",
                outletP: "Lascia 0 (è pressione cinematica, p/ρ)",
            },
        },
    };

    function setHint(id, text) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function applySolverPreset(solverName) {
        const preset = SOLVER_PRESETS[solverName];
        if (!preset) return;
        document.getElementById("set-endTime").value = preset.endTime;
        document.getElementById("set-deltaT").value = preset.deltaT;
        document.getElementById("set-writeInterval").value = preset.writeInterval;
        const h = preset.hints;
        setHint("hint-solver", h.solver);
        setHint("hint-endTime", h.endTime);
        setHint("hint-deltaT", h.deltaT);
        setHint("hint-writeInterval", h.writeInterval);
        setHint("hint-inletUx", h.inletUx);
        setHint("hint-outletP", h.outletP);
        log(`Preset "${solverName}" applicato: endTime=${preset.endTime}, deltaT=${preset.deltaT}, writeInterval=${preset.writeInterval}`, "ok");
    }

    let currentMode = "2d";

    function getSettings() {
        return {
            mode: currentMode,
            solver: document.getElementById("set-solver").value,
            endTime: pf("set-endTime"),
            deltaT: pf("set-deltaT"),
            writeInterval: pf("set-writeInterval"),
            parallel: document.getElementById("set-parallel").checked,
            nProcs: parseInt(document.getElementById("set-nProcs").value, 10) || 1,
            turbulenceModel: document.getElementById("set-turbModel").value,
            inlet: {
                U: [
                    pf("set-inlet-Ux"),
                    pf("set-inlet-Uy"),
                    0,
                ],
                T: pf("set-inlet-T"),
            },
            outlet: {
                p: pf("set-outlet-p"),
            },
            fluidProperties: {
                mu: pf("set-mu"),
                Cp: pf("set-Cp"),
                Pr: pf("set-Pr"),
                molWeight: pf("set-molWeight"),
            },
        };
    }

    async function applySetup() {
        if (!currentCase) {
            log("Crea o apri un caso prima.", "warn");
            return;
        }

        const settings = getSettings();
        log(`Applicando impostazioni al caso "${currentCase}"...`);

        try {
            const resp = await fetch("/api/setup", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ caseName: currentCase, settings }),
            });
            const data = await resp.json();
            if (resp.ok) {
                log("Impostazioni applicate con successo.", "ok");
            } else {
                log(`Errore: ${data.error}`, "error");
            }
        } catch (e) {
            log(`Errore di rete: ${e.message}`, "error");
        }
    }

    // ===== MESH =====
    // Suggested MPI process count based on cell count, tuned for the
    // i9-14900K (8 P-cores + 16 E-cores). The breakpoints come from the
    // ~50k cells/proc CFD rule of thumb and from local benchmarks: with
    // a 24k-cell mesh, N=8 wins by ~5% over N=4 and N=24 is 27% slower.
    // Once the mesh is large enough that comms overhead is negligible
    // (>500k cells), E-cores start paying off.
    function recommendNProcs(cells) {
        if (cells < 30000)   return { n: 4,  note: "mesh piccola, 4 procs evita l'overhead di comunicazione MPI." };
        if (cells < 100000)  return { n: 6,  note: "6 procs sui P-core, sweet spot per mesh medio-piccole." };
        if (cells < 300000)  return { n: 8,  note: "8 procs (un P-core ciascuno) — ottimale per il 14900K." };
        if (cells < 700000)  return { n: 8,  note: "8 procs (P-core). Oltre servirebbero E-core, meno efficienti." };
        if (cells < 1500000) return { n: 12, note: "12 procs (8 P-core + 4 E-core) — la mesh giustifica gli E-core." };
        if (cells < 3000000) return { n: 16, note: "16 procs (8 P + 8 E) — parallelo massivo paga." };
        return                     { n: 24, note: "24 procs (tutti i core fisici) — mesh molto grande." };
    }

    // ===== MODE & STL =====
    function setMode(mode) {
        currentMode = mode;
        document.querySelectorAll(".mode-btn").forEach(b => {
            b.classList.toggle("active", b.dataset.mode === mode);
        });
        document.querySelectorAll(".section-3d-only").forEach(el => {
            el.style.display = (mode === "3d") ? "" : "none";
        });
        const ct = document.getElementById("canvas-toolbar");
        const cc = document.getElementById("canvas-container");
        if (mode === "3d") {
            if (ct) ct.style.display = "none";
            if (cc) cc.style.opacity = "0.3";
            log("Modalità 3D: carica un .stl e premi 'Mesh' (snappyHexMesh).", "ok");
        } else {
            if (ct) ct.style.display = "";
            if (cc) cc.style.opacity = "1";
            log("Modalità 2D attiva.", "ok");
        }
    }

    async function uploadStl(file) {
        if (!currentCase) {
            log("Crea o apri un caso prima di caricare un modello.", "warn");
            return;
        }
        const status = document.getElementById("stl-status");
        status.textContent = `Caricamento "${file.name}" (${(file.size/1024).toFixed(1)} KB)...`;
        status.style.color = "#888";
        const fd = new FormData();
        fd.append("file", file);
        try {
            const resp = await fetch(`/api/upload-stl/${currentCase}`, { method: "POST", body: fd });
            const data = await resp.json();
            if (resp.ok) {
                status.textContent = `✓ ${file.name} (${(data.size/1024).toFixed(1)} KB)`;
                status.style.color = "#388e3c";
                log(`Modello STL "${file.name}" caricato nel caso "${currentCase}".`, "ok");
            } else {
                status.textContent = `Errore: ${data.error}`;
                status.style.color = "#d32f2f";
                log(`Errore upload STL: ${data.error}`, "error");
            }
        } catch (e) {
            status.textContent = `Errore di rete: ${e.message}`;
            status.style.color = "#d32f2f";
        }
    }

    async function generateMesh() {
        if (!currentCase) {
            log("Crea o apri un caso prima.", "warn");
            return;
        }

        const meshSize = pf("set-meshSize");
        const refinement = pf("set-refinement");
        const domain = {
            xmin: pf("set-domain-xmin"),
            xmax: pf("set-domain-xmax"),
            ymin: pf("set-domain-ymin"),
            ymax: pf("set-domain-ymax"),
        };
        if (currentMode === "3d") {
            domain.zmin = pf("set-domain-zmin");
            domain.zmax = pf("set-domain-zmax");
        }

        // ----- 3D branch: snappyHexMesh on uploaded STL -----
        if (currentMode === "3d") {
            log(`Generando mesh 3D per "${currentCase}" (snappyHexMesh)...`);
            log(`  Cella background: ${meshSize}m, Raffinamento STL: lvl ${Math.round(refinement*2)+1}`);
            try {
                const resp = await fetch("/api/mesh", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        caseName: currentCase, mode: "3d",
                        domain, meshSize, refinement,
                    }),
                });
                const data = await resp.json();
                if (resp.ok) {
                    log(`Mesh 3D generata: ${data.cells} celle.`, "ok");
                    if (data.cells > 0) {
                        const rec = recommendNProcs(data.cells);
                        log(`Suggerimento: ${rec.n} processi. ${rec.note}`, "ok");
                    }
                    if (data.checkMesh) {
                        document.getElementById("mesh-info-text").textContent = data.checkMesh;
                    }
                    switchTab("tab-meshinfo");
                } else {
                    log(`Errore mesh 3D: ${data.error}`, "error");
                    if (data.log) log(data.log.slice(-3000), "error");
                }
            } catch (e) {
                log(`Errore di rete: ${e.message}`, "error");
            }
            return;
        }

        // ----- 2D branch (canvas geometry → gmsh) -----
        const geometry = Canvas2D.exportGeometry();
        if (!geometry || geometry.shape_points.length < 3) {
            log("Disegna una geometria chiusa prima di generare la mesh (almeno 3 punti).", "warn");
            return;
        }
        geometry.domain = domain;
        geometry.scale = 1.0;
        log(`Generando mesh 2D per "${currentCase}"...`);
        log(`  Punti: ${geometry.shape_points.length}, Celle: ~${meshSize}m, Raffinamento: ${refinement}`);

        try {
            const resp = await fetch("/api/mesh", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    caseName: currentCase, mode: "2d",
                    geometry, meshSize, refinement,
                }),
            });
            const data = await resp.json();
            if (resp.ok) {
                const cells = data.cells || 0;
                log(`Mesh generata: ${data.nodes} nodi, ${cells} celle.`, "ok");
                if (cells > 0) {
                    const rec = recommendNProcs(cells);
                    log(`Suggerimento: ${rec.n} processi per la simulazione parallela. ${rec.note}`, "ok");
                }
                if (data.checkMesh) {
                    document.getElementById("mesh-info-text").textContent = data.checkMesh;
                }
                switchTab("tab-meshinfo");
            } else {
                log(`Errore mesh: ${data.error}`, "error");
                if (data.log) log(data.log, "error");
            }
        } catch (e) {
            log(`Errore di rete: ${e.message}`, "error");
        }
    }

    // ===== SOLVER =====
    let solverStartTime = 0;
    let solverEndTime = 1;   // user-set endTime from the case
    let lastSolverTime = 0;  // last "Time = X" parsed from the log

    function setProgress(pct, label) {
        const cont = document.getElementById("progress-container");
        const fill = document.getElementById("progress-fill");
        const lbl  = document.getElementById("progress-label");
        if (pct === null) { cont.style.display = "none"; return; }
        cont.style.display = "block";
        fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
        if (label) lbl.textContent = label;
    }

    function setSolverStatus(text, cls) {
        const el = document.getElementById("solver-status");
        el.textContent = text;
        el.className = "solver-status " + (cls || "");
    }

    function fmtDuration(ms) {
        const s = Math.floor(ms / 1000);
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        if (h > 0) return `${h}h ${m}m ${sec}s`;
        if (m > 0) return `${m}m ${sec}s`;
        return `${sec}s`;
    }

    async function startSolver() {
        if (!currentCase) {
            log("Crea o apri un caso prima.", "warn");
            return;
        }

        const solver = document.getElementById("set-solver").value;
        const parallel = document.getElementById("set-parallel").checked;
        const nProcs = parseInt(document.getElementById("set-nProcs").value, 10) || 1;
        const modeLabel = (parallel && nProcs > 1) ? ` [parallelo ×${nProcs}]` : "";
        log(`Avviando ${solver}${modeLabel} sul caso "${currentCase}"...`, "ok");
        logLineIndex = 0;
        solverStartTime = Date.now();
        solverEndTime = pf("set-endTime") || 1;
        lastSolverTime = 0;
        setProgress(0, `Avvio ${solver}${modeLabel}...`);
        setSolverStatus("In esecuzione" + modeLabel, "running");

        try {
            const resp = await fetch("/api/solve/start", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ caseName: currentCase, solver, parallel, nProcs }),
            });
            const data = await resp.json();
            if (resp.ok) {
                startLogPolling();
            } else {
                log(`Errore: ${data.error}`, "error");
                setSolverStatus("Errore", "error");
                setProgress(null);
            }
        } catch (e) {
            log(`Errore di rete: ${e.message}`, "error");
            setSolverStatus("Errore", "error");
            setProgress(null);
        }
    }

    // Match "Time = 0.012345" but NOT "ExecutionTime = ..." or "deltaT = ...".
    const TIME_RE = /^\s*Time\s*=\s*([0-9.eE+-]+)\s*$/;

    function parseProgressFromLines(lines) {
        // Walk backwards to grab the most recent Time = X
        for (let i = lines.length - 1; i >= 0; i--) {
            const m = TIME_RE.exec(lines[i]);
            if (m) {
                const v = parseFloat(m[1]);
                if (!isNaN(v)) lastSolverTime = v;
                return;
            }
        }
    }

    function startLogPolling() {
        if (solverPolling) clearInterval(solverPolling);

        solverPolling = setInterval(async () => {
            try {
                const resp = await fetch(`/api/solve/status?after=${logLineIndex}`);
                const data = await resp.json();

                if (data.lines && data.lines.length > 0) {
                    logBatch(data.lines);
                    parseProgressFromLines(data.lines);
                }
                logLineIndex = data.lineCount;

                // Progress: based on Time / endTime when available
                const elapsed = Date.now() - solverStartTime;
                const pct = solverEndTime > 0 ? (lastSolverTime / solverEndTime) * 100 : 0;
                // ETA: linear extrapolation from simulated-time progress.
                // Only meaningful once a small amount of time has actually
                // been simulated, otherwise the ratio is noisy / infinite.
                let etaStr = "—";
                if (lastSolverTime > 0 && solverEndTime > lastSolverTime) {
                    const etaMs = elapsed * (solverEndTime - lastSolverTime) / lastSolverTime;
                    etaStr = fmtDuration(etaMs);
                }
                setProgress(pct,
                    `Time = ${lastSolverTime.toExponential(3)} / ${solverEndTime}  ·  ` +
                    `${pct.toFixed(1)}%  ·  trascorso ${fmtDuration(elapsed)}  ·  ` +
                    `rimanente ~${etaStr}  ·  righe ${data.lineCount}`
                );

                if (!data.running) {
                    clearInterval(solverPolling);
                    solverPolling = null;
                    const ok = data.returnCode === 0;
                    log(`\nSimulazione terminata (codice: ${data.returnCode}).`, ok ? "ok" : "error");
                    if (ok) {
                        setProgress(100,
                            `Completato in ${fmtDuration(elapsed)}  ·  Time finale ${lastSolverTime}`);
                        setSolverStatus("Completato", "done");
                        log("Premi il pulsante ParaView per visualizzare i risultati.", "ok");
                        const wasParallel = document.getElementById("set-parallel").checked;
                        const np = parseInt(document.getElementById("set-nProcs").value, 10) || 1;
                        if (wasParallel && np > 1) {
                            log("Modalita' parallela: in ParaView seleziona 'Case Type: Decomposed Case' e premi Apply.", "warn");
                        }
                    } else {
                        setSolverStatus(`Errore (codice ${data.returnCode})`, "error");
                    }
                }
            } catch (e) { /* ignore */ }
        }, 500);
    }

    async function stopSolver() {
        log("Arrestando il solver...", "warn");
        setSolverStatus("Arresto in corso...", "error");
        try {
            await fetch("/api/solve/stop", { method: "POST" });
        } catch (e) { /* ignore */ }
    }

    // ===== PARAVIEW =====
    async function launchParaView() {
        if (!currentCase) {
            log("Crea o apri un caso prima di avviare ParaView.", "warn");
            return;
        }

        log(`Avviando ParaView per il caso "${currentCase}"...`, "ok");

        try {
            const resp = await fetch("/api/solve/paraview", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ caseName: currentCase }),
            });
            const data = await resp.json();
            if (resp.ok) {
                log("ParaView avviato con successo.", "ok");
            } else {
                log(`Errore: ${data.error}`, "error");
            }
        } catch (e) {
            log(`Errore di rete: ${e.message}`, "error");
        }
    }

    // ===== RESIDUALS =====
    async function fetchResiduals() {
        if (!currentCase) return;

        try {
            const resp = await fetch(`/api/residuals/${currentCase}`);
            const data = await resp.json();
            drawResidualChart(data);
        } catch (e) {
            log(`Errore caricamento residui: ${e.message}`, "error");
        }
    }

    function showResiduals() {
        switchTab("tab-residuals");
    }

    function drawResidualChart(data) {
        const canvas = document.getElementById("residuals-chart");
        const ctx = canvas.getContext("2d");
        const W = canvas.width;
        const H = canvas.height;
        const pad = { top: 30, right: 30, bottom: 40, left: 60 };

        ctx.clearRect(0, 0, W, H);

        // Background
        ctx.fillStyle = "#fff";
        ctx.fillRect(0, 0, W, H);

        const fields = Object.keys(data);
        if (fields.length === 0) {
            ctx.fillStyle = "#888";
            ctx.font = "14px sans-serif";
            ctx.textAlign = "center";
            ctx.fillText("Nessun dato residuo disponibile", W / 2, H / 2);
            return;
        }

        // Find data range
        let maxIter = 0, minVal = 1, maxVal = 0;
        for (const f of fields) {
            for (const d of data[f]) {
                maxIter = Math.max(maxIter, d.iter);
                if (d.value > 0) {
                    minVal = Math.min(minVal, d.value);
                    maxVal = Math.max(maxVal, d.value);
                }
            }
        }

        if (maxIter === 0 || maxVal === 0) return;

        // Log scale for Y
        const logMin = Math.floor(Math.log10(Math.max(minVal, 1e-15)));
        const logMax = Math.ceil(Math.log10(maxVal));
        const xScale = (W - pad.left - pad.right) / maxIter;
        const yScale = (H - pad.top - pad.bottom) / (logMax - logMin);

        // Grid
        ctx.strokeStyle = "#eee";
        ctx.lineWidth = 0.5;
        for (let p = logMin; p <= logMax; p++) {
            const y = pad.top + (logMax - p) * yScale;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(W - pad.right, y);
            ctx.stroke();

            ctx.fillStyle = "#888";
            ctx.font = "11px monospace";
            ctx.textAlign = "right";
            ctx.fillText(`1e${p}`, pad.left - 6, y + 4);
        }

        // Axes
        ctx.strokeStyle = "#333";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, pad.top);
        ctx.lineTo(pad.left, H - pad.bottom);
        ctx.lineTo(W - pad.right, H - pad.bottom);
        ctx.stroke();

        // X axis labels
        ctx.fillStyle = "#888";
        ctx.textAlign = "center";
        const xStep = Math.max(1, Math.floor(maxIter / 10));
        for (let i = 0; i <= maxIter; i += xStep) {
            const x = pad.left + i * xScale;
            ctx.fillText(i.toString(), x, H - pad.bottom + 16);
        }

        // Title
        ctx.fillStyle = "#333";
        ctx.font = "bold 13px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("Residui", W / 2, 18);

        // Plot lines
        const colors = { U: "#1565c0", p: "#d32f2f", k: "#388e3c", omega: "#f57c00", epsilon: "#7b1fa2", e: "#00838f", h: "#ad1457" };

        for (const field of fields) {
            const pts = data[field];
            if (pts.length < 2) continue;

            ctx.strokeStyle = colors[field] || "#666";
            ctx.lineWidth = 1.5;
            ctx.beginPath();

            let started = false;
            for (const d of pts) {
                if (d.value <= 0) continue;
                const x = pad.left + d.iter * xScale;
                const y = pad.top + (logMax - Math.log10(d.value)) * yScale;
                if (!started) { ctx.moveTo(x, y); started = true; }
                else ctx.lineTo(x, y);
            }
            ctx.stroke();
        }

        // Legend
        let lx = pad.left + 10;
        const ly = pad.top + 10;
        ctx.font = "12px sans-serif";
        for (const field of fields) {
            ctx.fillStyle = colors[field] || "#666";
            ctx.fillRect(lx, ly, 14, 10);
            ctx.fillStyle = "#333";
            ctx.textAlign = "left";
            ctx.fillText(field, lx + 18, ly + 9);
            lx += 60;
        }
    }

    // Public API
    return {
        init,
        log,
        createCase,
        openCase,
        deleteCase,
    };
})();

// Make App accessible globally for onclick handlers
window.App = App;
document.addEventListener("DOMContentLoaded", App.init);
