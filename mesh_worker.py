"""
Standalone gmsh mesh generation worker.

Run as a subprocess to avoid the Python signal-handler-in-non-main-thread
issue that gmsh.initialize() triggers when called from a Flask worker thread.

Reads a JSON config from stdin, writes a JSON result to stdout.

Input JSON:
{
    "case_dir": "absolute path",
    "mesh_size": 0.05,
    "refinement": 1.0,
    "geometry": {
        "shape_points": [[x,y], ...],
        "shape_curves": [{"type": "line"|"spline", "points": [i, j, ...]}],
        "domain": {"xmin":-5,"xmax":15,"ymin":-5,"ymax":5},
        "scale": 1.0
    }
}

Output JSON (success):
    {"ok": true, "msh_path": "...", "nodes": N, "elements": M}
Output JSON (failure):
    {"ok": false, "error": "...", "traceback": "..."}
"""

import sys
import os
import json
import traceback


def generate(config):
    import gmsh

    geometry = config["geometry"]
    case_dir = config["case_dir"]
    mesh_size = float(config.get("mesh_size", 0.05))
    refinement = float(config.get("refinement", 1.0))

    shape_pts = geometry["shape_points"]
    curves = geometry.get("shape_curves", [])
    domain = geometry.get("domain", {"xmin": -5, "xmax": 15, "ymin": -5, "ymax": 5})
    scale = float(geometry.get("scale", 1.0))

    fine_size = mesh_size * refinement
    xmin_d = float(domain["xmin"]) * scale
    xmax_d = float(domain["xmax"]) * scale
    ymin_d = float(domain["ymin"]) * scale
    ymax_d = float(domain["ymax"]) * scale
    domain_size = max(xmax_d - xmin_d, ymax_d - ymin_d)
    coarse_size = max(mesh_size * 3.0, domain_size / 300.0)

    msh_path = os.path.join(case_dir, "mesh.msh")

    gmsh.initialize()
    try:
        # Quiet — stdout is reserved for the JSON result
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.Verbosity", 0)

        gmsh.model.add("cfd2d")

        # ---------------- 2D geometry ----------------
        shape_gmsh_pts = []
        for pt in shape_pts:
            x = float(pt[0]) * scale
            y = float(pt[1]) * scale
            tag = gmsh.model.geo.addPoint(x, y, 0.0, fine_size)
            shape_gmsh_pts.append(tag)

        shape_curve_tags = []
        if curves:
            for c in curves:
                idxs = c["points"]
                tags = [shape_gmsh_pts[i] for i in idxs]
                ctype = c["type"]
                if ctype == "line":
                    t = gmsh.model.geo.addLine(tags[0], tags[1])
                    shape_curve_tags.append(t)
                elif ctype == "spline":
                    # gmsh splines require at least 3 points
                    if len(tags) < 3:
                        t = gmsh.model.geo.addLine(tags[0], tags[-1])
                    else:
                        t = gmsh.model.geo.addSpline(tags)
                    shape_curve_tags.append(t)
                elif ctype == "bspline":
                    if len(tags) < 4:
                        t = gmsh.model.geo.addSpline(tags)
                    else:
                        t = gmsh.model.geo.addBSpline(tags)
                    shape_curve_tags.append(t)
        else:
            # default: connect points with lines as a closed polygon
            n = len(shape_gmsh_pts)
            for i in range(n):
                t = gmsh.model.geo.addLine(
                    shape_gmsh_pts[i],
                    shape_gmsh_pts[(i + 1) % n],
                )
                shape_curve_tags.append(t)

        # ---------------- Outer domain ----------------
        xmin = float(domain["xmin"]) * scale
        xmax = float(domain["xmax"]) * scale
        ymin = float(domain["ymin"]) * scale
        ymax = float(domain["ymax"]) * scale

        # Ensure the domain completely encloses the shape with a small margin
        # to prevent "Could not find extruded node" errors when points touch the boundary.
        margin = 0.1 * scale
        if shape_pts:
            shape_xs = [float(pt[0]) * scale for pt in shape_pts]
            shape_ys = [float(pt[1]) * scale for pt in shape_pts]
            if min(shape_xs) <= xmin:
                xmin = min(shape_xs) - margin
            if max(shape_xs) >= xmax:
                xmax = max(shape_xs) + margin
            if min(shape_ys) <= ymin:
                ymin = min(shape_ys) - margin
            if max(shape_ys) >= ymax:
                ymax = max(shape_ys) + margin

        d1 = gmsh.model.geo.addPoint(xmin, ymin, 0.0, coarse_size)
        d2 = gmsh.model.geo.addPoint(xmax, ymin, 0.0, coarse_size)
        d3 = gmsh.model.geo.addPoint(xmax, ymax, 0.0, coarse_size)
        d4 = gmsh.model.geo.addPoint(xmin, ymax, 0.0, coarse_size)

        inlet_line = gmsh.model.geo.addLine(d4, d1)
        bottom_line = gmsh.model.geo.addLine(d1, d2)
        outlet_line = gmsh.model.geo.addLine(d2, d3)
        top_line = gmsh.model.geo.addLine(d3, d4)
        domain_lines = [inlet_line, bottom_line, outlet_line, top_line]

        domain_loop = gmsh.model.geo.addCurveLoop(domain_lines)

        # Build one curve loop per closed shape so multiple obstacles in the
        # same domain work (each becomes a hole in the plane surface).
        # The new frontend sends `shape_loops` describing which curves belong
        # to which shape; legacy payloads without it are treated as a single
        # loop containing all curves.
        shape_loops_info = geometry.get("shape_loops")
        gmsh_shape_loops = []
        if shape_loops_info:
            for loop in shape_loops_info:
                tags = [shape_curve_tags[i] for i in loop["curves"]]
                if tags:
                    gmsh_shape_loops.append(gmsh.model.geo.addCurveLoop(tags))
        else:
            gmsh_shape_loops.append(gmsh.model.geo.addCurveLoop(shape_curve_tags))

        surface = gmsh.model.geo.addPlaneSurface([domain_loop] + gmsh_shape_loops)

        gmsh.model.geo.synchronize()

        # ---------------- Extrude 1 cell in Z (2D OpenFOAM) ----------------
        # extrude returns a list of (dim, tag) in this order:
        #   [0]            = top face (dim=2)
        #   [1]            = volume (dim=3)
        #   [2 .. 2+L-1]   = side faces (dim=2), one per boundary line of the
        #                    surface, in the order the loops were given to
        #                    addPlaneSurface, then within each loop the order
        #                    the lines were given to addCurveLoop.
        #
        # `recombine=True` is essential here. Without it, gmsh decomposes the
        # extruded layer into tetrahedra (4-face cells), which produces a
        # mesh that OpenFOAM no longer recognises as 2D ("edges not aligned
        # with non-empty directions" + "faces on empty patches not divisible
        # by nCells"). With recombine, the extrusion stays as prisms/hexes.
        extrusion = gmsh.model.geo.extrude(
            [(2, surface)], 0.0, 0.0, 0.1,
            numElements=[1], recombine=True,
        )
        gmsh.model.geo.synchronize()

        top_face = extrusion[0][1]
        volume = extrusion[1][1]
        side_face_tags = [e[1] for e in extrusion[2:] if e[0] == 2]

        if len(side_face_tags) < 4 + len(shape_curve_tags):
            raise RuntimeError(
                f"Unexpected extrude result: got {len(side_face_tags)} side faces, "
                f"expected at least {4 + len(shape_curve_tags)}"
            )

        inlet_face = side_face_tags[0]
        bottom_face = side_face_tags[1]
        outlet_face = side_face_tags[2]
        topdom_face = side_face_tags[3]
        wall_faces = side_face_tags[4:4 + len(shape_curve_tags)]

        # ---------------- Physical groups (become OpenFOAM patches) ----------------
        gmsh.model.addPhysicalGroup(2, [inlet_face], name="inlet")
        gmsh.model.addPhysicalGroup(2, [outlet_face], name="outlet")
        gmsh.model.addPhysicalGroup(2, [bottom_face], name="bottom")
        gmsh.model.addPhysicalGroup(2, [topdom_face], name="top")
        gmsh.model.addPhysicalGroup(2, wall_faces, name="wall")
        gmsh.model.addPhysicalGroup(2, [surface, top_face], name="frontAndBack")
        gmsh.model.addPhysicalGroup(3, [volume], name="internalField")

        # ---------------- Mesh settings ----------------
        # Algorithm 8 (Frontal-Delaunay for quads) builds a quad-dominant
        # 2D base directly, which extrudes cleanly into hexahedra.
        # RecombinationAlgorithm 1 (Simple) is more tolerant of sharp
        # corners than the default Blossom-quad, avoiding the
        # "Could not find extruded node" error that the Blossom algorithm
        # triggers when the user draws angular shapes (triangles, polygons).
        gmsh.option.setNumber("Mesh.Algorithm", 8)
        gmsh.option.setNumber("Mesh.RecombineAll", 1)
        gmsh.option.setNumber("Mesh.RecombinationAlgorithm", 1)
        gmsh.option.setNumber("Mesh.MeshSizeMin", fine_size * 0.5)
        gmsh.option.setNumber("Mesh.MeshSizeMax", coarse_size * 2)

        # Generate the 2D base first, then the 3D extrusion. Doing them
        # separately gives gmsh a chance to fail with a clearer error on
        # the 2D step alone, and avoids interaction between 2D and 3D
        # algorithms.
        gmsh.model.mesh.generate(2)
        gmsh.model.mesh.generate(3)

        gmsh.write(msh_path)

        node_tags, _, _ = gmsh.model.mesh.getNodes()
        _, elem_tags, _ = gmsh.model.mesh.getElements()

        return {
            "ok": True,
            "msh_path": msh_path,
            "nodes": len(node_tags),
            "elements": sum(len(t) for t in elem_tags),
        }
    finally:
        gmsh.finalize()


def main():
    try:
        raw = sys.stdin.read()
        config = json.loads(raw)
        result = generate(config)
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    except Exception as exc:
        err = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        sys.stdout.write(json.dumps(err))
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
