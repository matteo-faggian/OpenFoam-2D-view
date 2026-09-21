"""OpenFOAM 2D Web GUI - Flask Application."""

import os
import sys
import json
import time
import shutil
import subprocess
import re
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from mesher import setup_openfoam_case, fix_boundary_types
from of_runner import (
    SolverProcess, run_of_command_sync, build_solver_command,
    _win_to_msys, MSYS_BASH, OF_BASHRC,
)

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.path.join(BASE_DIR, "cases")
MESH_WORKER = os.path.join(BASE_DIR, "mesh_worker.py")

solver_process = SolverProcess()


# ============================================================ pages
@app.route("/")
def index():
    return render_template("index.html")


# ============================================================ cases
@app.route("/api/cases", methods=["GET"])
def list_cases():
    cases = []
    if os.path.isdir(CASES_DIR):
        for name in sorted(os.listdir(CASES_DIR)):
            case_path = os.path.join(CASES_DIR, name)
            if os.path.isdir(case_path):
                has_mesh = os.path.isdir(os.path.join(case_path, "constant", "polyMesh"))
                cases.append({"name": name, "hasMesh": has_mesh})
    return jsonify(cases)


@app.route("/api/cases", methods=["POST"])
def create_case():
    data = request.get_json() or {}
    name = (data.get("name", "newCase") or "newCase").strip().replace(" ", "_")
    case_path = os.path.join(CASES_DIR, name)
    if os.path.exists(case_path):
        return jsonify({"error": f"Case '{name}' already exists"}), 409
    os.makedirs(case_path, exist_ok=True)
    return jsonify({"name": name, "path": case_path})


@app.route("/api/cases/<name>", methods=["DELETE"])
def delete_case(name):
    case_path = os.path.join(CASES_DIR, name)
    if os.path.isdir(case_path):
        shutil.rmtree(case_path, ignore_errors=True)
    return jsonify({"ok": True})


# ============================================================ STL upload (3D)
@app.route("/api/upload-stl/<case_name>", methods=["POST"])
def upload_stl(case_name):
    """Receive an .stl file and store it under constant/triSurface/."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".stl"):
        return jsonify({"error": "Only .stl files are accepted"}), 400
    case_path = os.path.join(CASES_DIR, case_name)
    tri_dir = os.path.join(case_path, "constant", "triSurface")
    os.makedirs(tri_dir, exist_ok=True)
    # Always save with the same name so snappyHexMeshDict can reference it
    saved_path = os.path.join(tri_dir, "model.stl")
    f.save(saved_path)
    size = os.path.getsize(saved_path)
    return jsonify({"ok": True, "path": saved_path, "size": size,
                    "filename": "model.stl"})


# ============================================================ mesh
@app.route("/api/mesh", methods=["POST"])
def generate_mesh_route():
    """Generate mesh from canvas geometry (2D) or STL (3D), then convert to OpenFOAM."""
    data = request.get_json() or {}
    case_name = data.get("caseName", "default")
    mode = data.get("mode", "2d")
    geometry = data.get("geometry")
    mesh_size = float(data.get("meshSize", 0.05))
    refinement = float(data.get("refinement", 1.0))
    domain = data.get("domain") or {}

    case_path = os.path.join(CASES_DIR, case_name)
    os.makedirs(case_path, exist_ok=True)

    # ---------- 3D branch: blockMesh + snappyHexMesh on the uploaded STL ----
    if mode == "3d":
        stl_path = os.path.join(case_path, "constant", "triSurface", "model.stl")
        if not os.path.isfile(stl_path):
            return jsonify({
                "error": "Nessun file STL caricato per questo caso. "
                         "Carica un modello .stl prima di generare la mesh."
            }), 400
        # Bootstrap a minimal case for snappy to run
        if not os.path.isfile(os.path.join(case_path, "system", "controlDict")):
            try:
                setup_openfoam_case(case_path, {"mode": "3d"})
            except Exception as exc:
                return jsonify({"error": f"Could not bootstrap case: {exc}"}), 500
        # Write 3D mesh dicts
        try:
            from mesher import write_3d_mesh_dicts
            write_3d_mesh_dicts(case_path, domain, mesh_size, "model.stl",
                                refinement_level=int(refinement * 2) + 1)
        except Exception as exc:
            return jsonify({"error": f"Could not write 3D dicts: {exc}"}), 500

        # 1. blockMesh
        rc1, out1 = run_of_command_sync("blockMesh", case_path)
        if rc1 != 0:
            return jsonify({"error": "blockMesh failed", "log": out1}), 500
        # 2. surfaceFeatures (extract .eMesh from the STL)
        rc2, out2 = run_of_command_sync("surfaceFeatures", case_path)
        # 3. snappyHexMesh (overwrite into constant/polyMesh)
        rc3, out3 = run_of_command_sync("snappyHexMesh -overwrite", case_path, timeout=900)
        if rc3 != 0:
            return jsonify({"error": "snappyHexMesh failed",
                            "log": out1 + "\n" + out2 + "\n" + out3}), 500
        # 4. checkMesh
        rc4, check_output = run_of_command_sync("checkMesh -allTopology", case_path)
        cells_match = re.search(r"cells:\s+(\d+)", check_output)
        cells = int(cells_match.group(1)) if cells_match else 0
        return jsonify({
            "cells": cells,
            "nodes": 0,
            "elements": cells,
            "snappyHexMesh": out1 + "\n" + out2 + "\n" + out3,
            "checkMesh": check_output,
        })

    # ---------- 2D branch (canvas geometry → gmsh) ------------------------
    if not geometry:
        return jsonify({"error": "No geometry provided"}), 400

    # gmshToFoam and checkMesh need a complete case skeleton (controlDict,
    # fvSchemes, fvSolution). If the user hasn't applied settings yet, write
    # defaults so the conversion + check can proceed. They can still hit
    # "Imposta" afterwards to overwrite with their own values.
    if not os.path.isfile(os.path.join(case_path, "system", "controlDict")):
        try:
            setup_openfoam_case(case_path, {})
        except Exception as exc:
            return jsonify({"error": f"Could not bootstrap case: {exc}"}), 500

    # 1. Run gmsh in a subprocess (avoids signal-thread issue)
    worker_input = json.dumps({
        "case_dir": case_path,
        "mesh_size": mesh_size,
        "refinement": refinement,
        "geometry": geometry,
    })
    try:
        proc = subprocess.run(
            [sys.executable, MESH_WORKER],
            input=worker_input,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Mesh generation timed out (>5 min)"}), 500
    except Exception as exc:
        return jsonify({"error": f"Failed to launch mesh worker: {exc}"}), 500

    # The worker writes a JSON object to stdout.
    stdout = proc.stdout.strip()
    if not stdout:
        return jsonify({
            "error": "Mesh worker produced no output",
            "stderr": proc.stderr,
        }), 500
    try:
        mesh_result = json.loads(stdout)
    except json.JSONDecodeError:
        return jsonify({
            "error": "Mesh worker returned invalid JSON",
            "stdout": stdout[:2000],
            "stderr": proc.stderr[:2000],
        }), 500

    if not mesh_result.get("ok"):
        return jsonify({
            "error": mesh_result.get("error", "Mesh generation failed"),
            "traceback": mesh_result.get("traceback", ""),
        }), 500

    # 2. Convert msh to OpenFOAM polyMesh
    rc, output = run_of_command_sync("gmshToFoam mesh.msh", case_path)
    if rc != 0:
        return jsonify({
            "error": "gmshToFoam failed",
            "log": output,
        }), 500

    # 3. Fix patch types in constant/polyMesh/boundary
    try:
        fix_boundary_types(case_path, {
            "frontAndBack": "empty",
            "wall": "wall",
            "inlet": "patch",
            "outlet": "patch",
            "top": "patch",
            "bottom": "patch",
        })
    except Exception as exc:
        return jsonify({"error": f"Could not fix boundary types: {exc}"}), 500

    # 4. Run checkMesh
    rc3, check_output = run_of_command_sync("checkMesh -allTopology", case_path)

    # Parse actual cell count from checkMesh — `elements` from gmsh includes
    # points/edges/faces too and is misleading for sizing a parallel run.
    cells_match = re.search(r"cells:\s+(\d+)", check_output)
    cells = int(cells_match.group(1)) if cells_match else 0

    return jsonify({
        "nodes": mesh_result.get("nodes", 0),
        "elements": mesh_result.get("elements", 0),
        "cells": cells,
        "gmshToFoam": output,
        "checkMesh": check_output,
    })


# ============================================================ setup
@app.route("/api/setup", methods=["POST"])
def setup_case():
    data = request.get_json() or {}
    case_name = data.get("caseName", "default")
    settings = data.get("settings", {})

    case_path = os.path.join(CASES_DIR, case_name)
    os.makedirs(case_path, exist_ok=True)

    try:
        setup_openfoam_case(case_path, settings)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ============================================================ solver
@app.route("/api/solve/start", methods=["POST"])
def start_solver():
    if solver_process.running:
        return jsonify({"error": "Solver already running"}), 409

    data = request.get_json() or {}
    case_name = data.get("caseName", "default")
    solver_name = data.get("solver", "rhoSimpleFoam")
    parallel = bool(data.get("parallel", False))
    n_procs = int(data.get("nProcs", 1) or 1)

    case_path = os.path.join(CASES_DIR, case_name)
    if not os.path.isdir(case_path):
        return jsonify({"error": "Case not found"}), 404

    # Catch the most common user mistake: trying to solve before meshing.
    # Without polyMesh/points decomposePar (or the solver in serial) crash
    # with a cryptic "Cannot find file points" — much friendlier to say so
    # before launching anything.
    points_file = os.path.join(case_path, "constant", "polyMesh", "points")
    if not os.path.isfile(points_file):
        return jsonify({
            "error": (
                f"Mesh non trovata per il caso '{case_name}'. "
                "Devi prima premere 'Mesh' (e poi 'Imposta') prima di avviare la simulazione."
            )
        }), 400

    # When the user picked parallel mode, make sure decomposeParDict exists
    # — applySetup writes it, but if the user changed nProcs after Imposta
    # we re-write it here so decomposePar always sees an up-to-date file.
    if parallel and n_procs > 1:
        from mesher import _decompose_par_dict, _write
        _write(case_path, "system/decomposeParDict", _decompose_par_dict(n_procs))

    cmd = build_solver_command(solver_name, parallel=parallel, n_procs=n_procs)
    solver_process.run_async(cmd, case_path)
    return jsonify({"ok": True, "command": cmd})


@app.route("/api/solve/stop", methods=["POST"])
def stop_solver():
    solver_process.stop()
    return jsonify({"ok": True})


@app.route("/api/solve/paraview", methods=["POST"])
def launch_paraview():
    data = request.get_json() or {}
    case_name = data.get("caseName", "default")
    case_path = os.path.join(CASES_DIR, case_name)

    if not os.path.isdir(case_path):
        return jsonify({"error": "Case not found"}), 404

    # create .foam file
    foam_file = os.path.join(case_path, f"{case_name}.foam")
    try:
        open(foam_file, "a").close()
    except Exception:
        pass

    try:
        if sys.platform == "win32":
            os.startfile(foam_file)
        else:
            subprocess.Popen(["paraview", foam_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": f"Impossibile avviare ParaView: {exc}. Assicurati di averlo installato."}), 500


@app.route("/api/solve/status")
def solver_status():
    after = int(request.args.get("after", 0))
    lines = solver_process.get_new_lines(after)
    return jsonify({
        "running": solver_process.running,
        "returnCode": solver_process.return_code,
        "lineCount": solver_process.line_count,
        "lines": lines,
    })


@app.route("/api/solve/stream")
def solver_stream():
    def generate():
        idx = 0
        while True:
            lines = solver_process.get_new_lines(idx)
            if lines:
                for line in lines:
                    yield f"data: {json.dumps({'line': line})}\n\n"
                idx += len(lines)
            if not solver_process.running and idx >= solver_process.line_count:
                yield f"data: {json.dumps({'done': True, 'returnCode': solver_process.return_code})}\n\n"
                break
            time.sleep(0.1)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ============================================================ residuals
# Regex to parse OpenFOAM solver-info lines, e.g.:
#   smoothSolver:  Solving for Ux, Initial residual = 0.001234, Final residual = ..., No Iterations 3
#   GAMG:  Solving for p, Initial residual = 0.01, ...
_RES_RE = re.compile(
    r"Solving for\s+(\w+),\s*Initial residual\s*=\s*([0-9.eE+-]+)"
)
# Iteration markers
_TIME_RE = re.compile(r"^\s*Time\s*=\s*([0-9.eE+-]+)")
_STEP_RE = re.compile(r"-->\s*time step\s*(\d+)")


@app.route("/api/residuals/<case_name>")
def get_residuals(case_name):
    """Parse Initial residuals from the solver log."""
    lines = solver_process.log_lines[:]  # snapshot

    residuals = {}        # field -> list of {iter, value}
    iteration = 0
    pending = {}          # field -> last seen residual for current iteration

    def flush():
        for fname, val in pending.items():
            # Group Ux,Uy,Uz under "U" using the largest residual
            key = "U" if fname in ("Ux", "Uy", "Uz") else fname
            existing = residuals.setdefault(key, [])
            # Only one entry per iteration per key — keep max value
            if existing and existing[-1]["iter"] == iteration:
                existing[-1]["value"] = max(existing[-1]["value"], val)
            else:
                existing.append({"iter": iteration, "value": val})
        pending.clear()

    for line in lines:
        # Detect new iteration / time step
        if _STEP_RE.search(line):
            flush()
            iteration += 1
            continue
        m_time = _TIME_RE.match(line)
        if m_time and "ExecutionTime" not in line:
            flush()
            iteration += 1
            continue

        m = _RES_RE.search(line)
        if m:
            fname = m.group(1)
            try:
                val = float(m.group(2))
            except ValueError:
                continue
            # Keep the FIRST residual seen for this field in this iteration
            # (subsequent corrections give smaller numbers, not useful as
            # convergence indicator)
            if fname not in pending:
                pending[fname] = val

    flush()
    return jsonify(residuals)


# ============================================================ entry point
if __name__ == "__main__":
    os.makedirs(CASES_DIR, exist_ok=True)
    print("=" * 50)
    print("  OpenFOAM 2D Web GUI")
    print("  Apri nel browser: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(debug=True, port=5000, threaded=True)
