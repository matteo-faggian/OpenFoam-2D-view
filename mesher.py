"""
OpenFOAM case setup utilities.

The actual mesh generation (gmsh) lives in mesh_worker.py and is invoked
as a subprocess because gmsh.initialize() installs signal handlers, which
in Python is only allowed from the main thread of the main interpreter.
Calling gmsh directly from a Flask worker thread raises
"signal only works in main thread of the main interpreter".
"""

import os
import re
import math


# ---------------------------------------------------------------- boundary
def fix_boundary_types(case_dir, patch_types):
    """
    Edit constant/polyMesh/boundary in place, changing each named patch's
    `type` entry to the value given in patch_types.

    patch_types: {"inlet": "patch", "wall": "wall", "frontAndBack": "empty", ...}

    foamDictionary was unreliable through MSYS for this task (the previous
    implementation silently swallowed all errors with `2>/dev/null`), so we
    rewrite the file ourselves with a regex.
    """
    bfile = os.path.join(case_dir, "constant", "polyMesh", "boundary")
    if not os.path.isfile(bfile):
        raise FileNotFoundError(f"boundary file not found: {bfile}")

    with open(bfile, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    for patch_name, new_type in patch_types.items():
        # Match a patch block:
        #   inlet
        #   {
        #       type            patch;
        #       ...
        #   }
        # and replace the `type` line (and physicalType if present) with
        # the new type.
        pattern = re.compile(
            r"(^[ \t]*" + re.escape(patch_name) + r"\s*\n\s*\{[^}]*?\btype\s+)\w+(\s*;)",
            re.MULTILINE | re.DOTALL,
        )
        new_content, count = pattern.subn(r"\1" + new_type + r"\2", content)
        if count == 0:
            # Patch not found — silently skip; not all patches must exist
            continue
        content = new_content

        # Also remove any inconsistent physicalType entry inside that block
        phys_pattern = re.compile(
            r"(^[ \t]*" + re.escape(patch_name) + r"\s*\n\s*\{[^}]*?)\bphysicalType\s+\w+\s*;\s*\n",
            re.MULTILINE | re.DOTALL,
        )
        content = phys_pattern.sub(r"\1", content)

    with open(bfile, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


# ---------------------------------------------------------------- case files
def setup_openfoam_case(case_dir, settings):
    """
    Create OpenFOAM case directory structure and write configuration files.

    settings = {
        "solver":           "rhoSimpleFoam",
        "endTime":          1000,
        "deltaT":           1,
        "writeInterval":    100,
        "turbulenceModel":  "kOmegaSST",
        "inlet":            {"U": [30,0,0], "T": 300},
        "outlet":           {"p": 101325},
        "fluidProperties":  {"mu":1.7894e-05,"Cp":1005,"Pr":0.71,"molWeight":28.97},
    }
    """
    for sub in ("0", "constant", "system"):
        os.makedirs(os.path.join(case_dir, sub), exist_ok=True)

    solver = settings.get("solver", "rhoSimpleFoam")
    end_time = settings.get("endTime", 1000)
    delta_t = settings.get("deltaT", 1)
    write_interval = settings.get("writeInterval", 100)
    turb_model = settings.get("turbulenceModel", "kOmegaSST")

    inlet = settings.get("inlet", {})
    outlet = settings.get("outlet", {})
    fluid = settings.get("fluidProperties", {})

    Ux, Uy, Uz = inlet.get("U", [30.0, 0.0, 0.0])
    U_mag = math.sqrt(Ux * Ux + Uy * Uy + Uz * Uz) or 1.0
    T_inlet = inlet.get("T", 300)
    p_outlet = outlet.get("p", 101325)

    mu = fluid.get("mu", 1.7894e-05)
    Cp = fluid.get("Cp", 1005)
    Pr = fluid.get("Pr", 0.71)
    molW = fluid.get("molWeight", 28.97)

    # Inlet turbulence quantities (approximate)
    turb_I = 0.05
    turb_L = 0.01
    k_in = max(1e-10, 1.5 * (U_mag * turb_I) ** 2)
    omega_in = max(1e-10, (k_in ** 0.5) / ((0.09 ** 0.25) * turb_L))
    epsilon_in = max(1e-10, 0.09 * (k_in ** 1.5) / turb_L)
    nuTilda_in = 3.0 * mu / 1.225  # rough estimate

    # Parallel run configuration. The actual MPI invocation is in
    # of_runner.run_parallel_solver(); here we only need to write the
    # decomposeParDict so `decomposePar` knows how to split the mesh.
    parallel = bool(settings.get("parallel", False))
    n_procs = int(settings.get("nProcs", 1) or 1)
    if not parallel:
        n_procs = 1
    if parallel and n_procs > 1:
        _write(case_dir, "system/decomposeParDict",
               _decompose_par_dict(n_procs))

    is_steady = solver in ("rhoSimpleFoam", "simpleFoam", "rhoPorousSimpleFoam")
    is_incompressible = solver in ("simpleFoam", "pimpleFoam", "icoFoam")
    # Density-based explicit solvers for compressible / supersonic flow.
    # They need very different controlDict / fvSolution and supersonic-style
    # BCs: all primitive variables (U, p, T) fixed at the inlet and
    # zeroGradient at the outlet, since in supersonic flow information only
    # travels with the stream.
    is_density_based = solver in ("rhoCentralFoam", "sonicFoam")

    # 3D mode: case built from an STL through snappyHexMesh. The patch
    # topology is different (no `frontAndBack empty`, full 6-face box +
    # the STL as walls). All _field_* helpers branch on is_3d.
    is_3d = settings.get("mode", "2d") == "3d"

    # controlDict
    _write(case_dir, "system/controlDict", _control_dict(
        solver, end_time, delta_t, write_interval, is_steady, is_incompressible,
        is_density_based,
    ))

    # fvSchemes
    _write(case_dir, "system/fvSchemes", _fv_schemes(is_steady))

    # fvSolution
    _write(case_dir, "system/fvSolution", _fv_solution(is_steady, is_density_based))

    # fvOptions (for bounding temperature in explicit density-based solvers)
    if is_density_based:
        _write(case_dir, "system/fvOptions", _fv_options())

    # thermophysicalProperties (compressible only)
    if not is_incompressible:
        _write(case_dir, "constant/thermophysicalProperties",
               _thermophysical(mu, Cp, Pr, molW))
    else:
        # For incompressible, write transportProperties
        _write(case_dir, "constant/transportProperties",
               _transport_properties(mu, 1.225))

    # turbulenceProperties
    _write(case_dir, "constant/turbulenceProperties", _turb_properties(turb_model))

    # Field files
    _write(case_dir, "0/U", _field_U(Ux, Uy, Uz, is_density_based, is_3d))
    _write(case_dir, "0/p", _field_p(p_outlet, is_incompressible, is_density_based, is_3d))
    if not is_incompressible:
        _write(case_dir, "0/T", _field_T(T_inlet, is_density_based, is_3d))
        _write(case_dir, "0/alphat", _field_alphat(is_incompressible, is_3d))

    _write(case_dir, "0/nut", _field_nut(is_3d))

    if turb_model == "kOmegaSST":
        _write(case_dir, "0/k", _field_k(k_in, is_3d))
        _write(case_dir, "0/omega", _field_omega(omega_in, is_3d))
    elif turb_model == "kEpsilon":
        _write(case_dir, "0/k", _field_k(k_in, is_3d))
        _write(case_dir, "0/epsilon", _field_epsilon(epsilon_in, is_3d))
    elif turb_model == "SpalartAllmaras":
        _write(case_dir, "0/nuTilda", _field_nuTilda(nuTilda_in, is_3d))


# ---------------------------------------------------------------- helpers
def _write(case_dir, relpath, content):
    fpath = os.path.join(case_dir, relpath.replace("/", os.sep))
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    with open(fpath, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _header(cls, obj, location=None):
    loc = f'    location    "{location}";\n' if location else ""
    return (
        "FoamFile\n"
        "{\n"
        "    version     2.0;\n"
        "    format      ascii;\n"
        f"    class       {cls};\n"
        f"{loc}"
        f"    object      {obj};\n"
        "}\n"
    )


def _bc_block_scalar(value, wall_type="zeroGradient", is_3d=False):
    fb = ("front       { type zeroGradient; }\n    "
          "back        { type zeroGradient; }") if is_3d else \
         "frontAndBack { type empty; }"
    return f"""boundaryField
{{
    inlet       {{ type fixedValue; value uniform {value}; }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type {wall_type}; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""


def _fb_patch(is_3d, content_2d="empty", content_3d="zeroGradient"):
    """
    Render the front/back boundary lines for either 2D (single
    `frontAndBack { type empty; }`) or 3D (two separate `front` and `back`
    patches). The 3D content is something like `zeroGradient` or
    `slip` — whatever a far-field face should do.
    """
    if is_3d:
        return (f"front       {{ type {content_3d}; }}\n"
                f"    back        {{ type {content_3d}; }}")
    return f"frontAndBack {{ type {content_2d}; }}"


def _fb_patch_field(is_3d, value, content_2d="empty"):
    """Same as _fb_patch but with a value-bearing 3D BC (calculated/fixedValue)."""
    if is_3d:
        return (f"front       {{ type calculated; value uniform {value}; }}\n"
                f"    back        {{ type calculated; value uniform {value}; }}")
    return f"frontAndBack {{ type {content_2d}; }}"


# ---------- field files
def _field_U(Ux, Uy, Uz, is_density_based=False, is_3d=False):
    int_Ux = Ux * 0.01 if is_density_based else Ux
    int_Uy = Uy * 0.01 if is_density_based else Uy
    int_Uz = Uz * 0.01 if is_density_based else Uz
    far_field = "zeroGradient" if is_density_based else "slip"
    fb = _fb_patch(is_3d, content_2d="empty", content_3d=far_field)
    return f"""{_header("volVectorField", "U", "0")}
dimensions      [0 1 -1 0 0 0 0];
internalField   uniform ({int_Ux} {int_Uy} {int_Uz});

boundaryField
{{
    inlet       {{ type fixedValue; value uniform ({Ux} {Uy} {Uz}); }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type noSlip; }}
    top         {{ type {far_field}; }}
    bottom      {{ type {far_field}; }}
    {fb}
}}
"""


def _field_p(p_val, is_incompressible, is_density_based=False, is_3d=False):
    if is_incompressible:
        dims = "[0 2 -2 0 0 0 0]"
        ref = 0
    else:
        dims = "[1 -1 -2 0 0 0 0]"
        ref = p_val
    fb = _fb_patch(is_3d)
    if is_density_based:
        bc = f"""boundaryField
{{
    inlet       {{ type fixedValue; value uniform {ref}; }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type zeroGradient; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""
    else:
        bc = f"""boundaryField
{{
    inlet       {{ type zeroGradient; }}
    outlet      {{ type fixedValue; value uniform {ref}; }}
    wall        {{ type zeroGradient; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""
    return f"""{_header("volScalarField", "p", "0")}
dimensions      {dims};
internalField   uniform {ref};

{bc}"""


def _field_T(T_val, is_density_based=False, is_3d=False):
    fb = _fb_patch(is_3d)
    if is_density_based:
        bc = f"""boundaryField
{{
    inlet       {{ type fixedValue; value uniform {T_val}; }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type zeroGradient; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""
    else:
        bc = _bc_block_scalar(T_val, is_3d=is_3d)
    return f"""{_header("volScalarField", "T", "0")}
dimensions      [0 0 0 1 0 0 0];
internalField   uniform {T_val};

{bc}"""


def _field_k(k, is_3d=False):
    fb = _fb_patch(is_3d)
    return f"""{_header("volScalarField", "k", "0")}
dimensions      [0 2 -2 0 0 0 0];
internalField   uniform {k:.6e};

boundaryField
{{
    inlet       {{ type fixedValue; value uniform {k:.6e}; }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type kqRWallFunction; value uniform {k:.6e}; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""


def _field_omega(w, is_3d=False):
    fb = _fb_patch(is_3d)
    return f"""{_header("volScalarField", "omega", "0")}
dimensions      [0 0 -1 0 0 0 0];
internalField   uniform {w:.6e};

boundaryField
{{
    inlet       {{ type fixedValue; value uniform {w:.6e}; }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type omegaWallFunction; value uniform {w:.6e}; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""


def _field_epsilon(e, is_3d=False):
    fb = _fb_patch(is_3d)
    return f"""{_header("volScalarField", "epsilon", "0")}
dimensions      [0 2 -3 0 0 0 0];
internalField   uniform {e:.6e};

boundaryField
{{
    inlet       {{ type fixedValue; value uniform {e:.6e}; }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type epsilonWallFunction; value uniform {e:.6e}; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""


def _field_nuTilda(nt, is_3d=False):
    fb = _fb_patch(is_3d)
    return f"""{_header("volScalarField", "nuTilda", "0")}
dimensions      [0 2 -1 0 0 0 0];
internalField   uniform {nt:.6e};

boundaryField
{{
    inlet       {{ type fixedValue; value uniform {nt:.6e}; }}
    outlet      {{ type zeroGradient; }}
    wall        {{ type fixedValue; value uniform 0; }}
    top         {{ type zeroGradient; }}
    bottom      {{ type zeroGradient; }}
    {fb}
}}
"""


def _field_nut(is_3d=False):
    fb = _fb_patch_field(is_3d, value=0)
    return f"""{_header("volScalarField", "nut", "0")}
dimensions      [0 2 -1 0 0 0 0];
internalField   uniform 0;

boundaryField
{{
    inlet       {{ type calculated; value uniform 0; }}
    outlet      {{ type calculated; value uniform 0; }}
    wall        {{ type nutkWallFunction; value uniform 0; }}
    top         {{ type calculated; value uniform 0; }}
    bottom      {{ type calculated; value uniform 0; }}
    {fb}
}}
"""


def _field_alphat(is_incompressible, is_3d=False):
    dims = "[0 2 -1 0 0 0 0]" if is_incompressible else "[1 -1 -1 0 0 0 0]"
    wall_type = "alphatJayatillekeWallFunction" if is_incompressible else "compressible::alphatWallFunction"
    fb = _fb_patch_field(is_3d, value=0)
    return f"""{_header("volScalarField", "alphat", "0")}
dimensions      {dims};
internalField   uniform 0;

boundaryField
{{
    inlet       {{ type calculated; value uniform 0; }}
    outlet      {{ type calculated; value uniform 0; }}
    wall        {{ type {wall_type}; value uniform 0; }}
    top         {{ type calculated; value uniform 0; }}
    bottom      {{ type calculated; value uniform 0; }}
    {fb}
}}
"""



# ---------- system files
def _control_dict(solver, end_time, delta_t, write_interval, is_steady, is_incompressible,
                  is_density_based=False):
    energy_field = "" if is_incompressible else " T"
    if is_steady:
        write_control = "timeStep"
        adjust_settings = ""
    else:
        write_control = "adjustable"
        # adjustableRunTime is the documented name; "adjustable" works too in v2312
        adjust_settings = "adjustTimeStep  yes;\nmaxCo           0.2;\nmaxDeltaT       1;\n"

    if is_density_based:
        # rhoCentralFoam is an explicit density-based solver. The user-set
        # deltaT (often 1 s, suitable for steady SIMPLE) is catastrophic as
        # an initial step. Force a tiny seed deltaT and let adjustTimeStep
        # ramp it up via the Courant number.
        delta_t = min(float(delta_t), 1e-6)
        # writeControl adjustable + adjustTimeStep yes is mandatory
        write_control = "adjustable"

    return f"""{_header("dictionary", "controlDict", "system")}
application     {solver};
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {end_time};
deltaT          {delta_t};
writeControl    {write_control};
writeInterval   {write_interval};
purgeWrite      0;
writeFormat     ascii;
writePrecision  8;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;

{adjust_settings}
functions
{{
    residuals
    {{
        type            solverInfo;
        libs            ("libutilityFunctionObjects.so");
        writeResidualFields no;
        fields          (U p{energy_field});
    }}
}}
"""


def _fv_schemes(is_steady):
    ddt = "steadyState" if is_steady else "Euler"
    return f"""{_header("dictionary", "fvSchemes", "system")}
fluxScheme          Kurganov;

ddtSchemes
{{
    default         {ddt};
}}
gradSchemes
{{
    default         Gauss linear;
    grad(p)         Gauss linear;
    grad(U)         Gauss linear;
}}
divSchemes
{{
    default         none;
    div(phi,U)      bounded Gauss linearUpwind default;
    div(phi,T)      bounded Gauss linearUpwind default;
    div(phi,h)      bounded Gauss linearUpwind default;
    div(phi,e)      bounded Gauss linearUpwind default;
    div(phi,K)      bounded Gauss linearUpwind default;
    div(phi,Ekp)    bounded Gauss linearUpwind default;
    div(phi,k)      bounded Gauss upwind;
    div(phi,omega)  bounded Gauss upwind;
    div(phi,epsilon) bounded Gauss upwind;
    div(phi,nuTilda) bounded Gauss upwind;
    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;
    div(phid,p)     Gauss upwind;
    div(meshPhi,p)  Gauss linear;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
    div(tauMC)      Gauss linear;
}}
laplacianSchemes
{{
    default         Gauss linear corrected;
}}
interpolationSchemes
{{
    default         linear;
    reconstruct(rho) vanLeer;
    reconstruct(U)  vanLeerV;
    reconstruct(T)  vanLeer;
}}
snGradSchemes
{{
    default         corrected;
}}
wallDist
{{
    method          meshWave;
}}
"""


def _fv_solution(is_steady, is_density_based=False):
    if is_density_based:
        # rhoCentralFoam is explicit. The conservative variables (rho, rhoU,
        # rhoE) are solved with `diagonal` (literally just division by the
        # mass). The remaining transported quantities (U, T/e, turbulence)
        # get a smoothSolver. There is NO SIMPLE/PIMPLE block and NO
        # relaxationFactors — both would corrupt the explicit update.
        return f"""{_header("dictionary", "fvSolution", "system")}
solvers
{{
    "(rho|rhoU|rhoE)"
    {{
        solver          diagonal;
    }}
    "(U|e|h|k|omega|epsilon|nuTilda|T)"
    {{
        solver          smoothSolver;
        smoother        GaussSeidel;
        nSweeps         2;
        tolerance       1e-09;
        relTol          0.01;
    }}
}}
"""

    if is_steady:
        algo = """SIMPLE
{
    nNonOrthogonalCorrectors 1;
    consistent      yes;
    residualControl
    {
        p   1e-4;
        U   1e-4;
        "(k|omega|epsilon|nuTilda)" 1e-4;
    }
}
"""
    else:
        algo = """PIMPLE
{
    nNonOrthogonalCorrectors 1;
    nOuterCorrectors    2;
    nCorrectors         1;
    residualControl
    {
        p   1e-4;
        U   1e-4;
        "(k|omega|epsilon|nuTilda)" 1e-4;
    }
}
"""
    return f"""{_header("dictionary", "fvSolution", "system")}
solvers
{{
    "(rho|rhoU|rhoE)"
    {{
        solver          diagonal;
    }}
    p
    {{
        solver          GAMG;
        tolerance       1e-06;
        relTol          0.01;
        smoother        GaussSeidel;
        nPreSweeps      0;
        nPostSweeps     2;
        cacheAgglomeration on;
        agglomerator    faceAreaPair;
        nCellsInCoarsestLevel 10;
        mergeLevels     1;
    }}
    "(U|e|h|k|omega|epsilon|nuTilda|T)"
    {{
        solver          smoothSolver;
        smoother        GaussSeidel;
        tolerance       1e-07;
        relTol          0.01;
    }}
}}

{algo}
relaxationFactors
{{
    fields  {{ p 0.3; rho 0.5; }}
    equations
    {{
        U       0.7;
        "(k|omega|epsilon|nuTilda)" 0.7;
        e       0.7;
        h       0.7;
        T       0.7;
    }}
}}
"""


def _fv_options():
    return f"""{_header("dictionary", "fvOptions", "system")}
limitT
{{
    type            limitTemperature;
    active          yes;
    selectionMode   all;
    min             10;
    max             5000;
}}
"""


def _thermophysical(mu, Cp, Pr, molW):
    return f"""{_header("dictionary", "thermophysicalProperties", "constant")}
thermoType
{{
    type            hePsiThermo;
    mixture         pureMixture;
    transport       const;
    thermo          hConst;
    equationOfState perfectGas;
    specie          specie;
    energy          sensibleInternalEnergy;
}}

mixture
{{
    specie        {{ molWeight {molW}; }}
    thermodynamics {{ Cp {Cp}; Hf 0; }}
    transport     {{ mu {mu}; Pr {Pr}; }}
}}
"""


def _transport_properties(mu, rho):
    nu = mu / rho
    return f"""{_header("dictionary", "transportProperties", "constant")}
transportModel  Newtonian;
nu              {nu:.6e};
"""


def _decompose_par_dict(n_procs):
    """
    Simple decomposition for 2D cases. We split the domain along X only
    (the streamwise direction) because the mesh is one cell thick in Z
    (front/back are empty) and most geometries are wider than tall in 2D.
    The user can edit this manually if they want a 2D decomposition.
    """
    return f"""{_header("dictionary", "decomposeParDict", "system")}
numberOfSubdomains  {n_procs};
method              simple;
simpleCoeffs
{{
    n               ({n_procs} 1 1);
    delta           0.001;
}}
"""


def _turb_properties(turb_model):
    if turb_model == "laminar":
        return f"""{_header("dictionary", "turbulenceProperties", "constant")}
simulationType  laminar;
"""
    return f"""{_header("dictionary", "turbulenceProperties", "constant")}
simulationType  RAS;
RAS
{{
    RASModel        {turb_model};
    turbulence      on;
    printCoeffs     on;
}}
"""


# ============================================================================
# 3D pipeline: blockMesh + snappyHexMesh + surfaceFeatures
# ============================================================================

def write_3d_mesh_dicts(case_dir, domain, mesh_size, stl_filename,
                        refinement_level=2, stl_bbox=None):
    """
    Write all the system/ dicts needed for the 3D meshing pipeline:
        blockMeshDict           — background hex grid
        surfaceFeaturesDict     — extract feature edges from the STL
        snappyHexMeshDict       — cut/snap around the STL

    `domain`  is {"xmin":..,"xmax":..,"ymin":..,"ymax":..,"zmin":..,"zmax":..}
    `mesh_size` is the background cell size (uniform hexes)
    `stl_filename` is the name of the STL inside constant/triSurface/
    `stl_bbox` (optional) is {"xmin":..,...} of the STL itself, used to
        place locationInMesh safely outside the model.
    """
    _write(case_dir, "system/blockMeshDict",
           _block_mesh_dict(domain, mesh_size))
    _write(case_dir, "system/surfaceFeaturesDict",
           _surface_features_dict(stl_filename))
    _write(case_dir, "system/snappyHexMeshDict",
           _snappy_hex_mesh_dict(stl_filename, domain, refinement_level,
                                 stl_bbox))
    # snappy needs meshQualityDict and a dummy fvSchemes/fvSolution to run
    _write(case_dir, "system/meshQualityDict", _mesh_quality_dict())


def _block_mesh_dict(domain, cell_size):
    xmin, xmax = float(domain["xmin"]), float(domain["xmax"])
    ymin, ymax = float(domain["ymin"]), float(domain["ymax"])
    zmin, zmax = float(domain["zmin"]), float(domain["zmax"])
    nx = max(4, int(round((xmax - xmin) / cell_size)))
    ny = max(4, int(round((ymax - ymin) / cell_size)))
    nz = max(4, int(round((zmax - zmin) / cell_size)))
    return f"""{_header("dictionary", "blockMeshDict", "system")}
scale   1.0;

vertices
(
    ({xmin} {ymin} {zmin})
    ({xmax} {ymin} {zmin})
    ({xmax} {ymax} {zmin})
    ({xmin} {ymax} {zmin})
    ({xmin} {ymin} {zmax})
    ({xmax} {ymin} {zmax})
    ({xmax} {ymax} {zmax})
    ({xmin} {ymax} {zmax})
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)
);

edges ();

boundary
(
    inlet  {{ type patch; faces ((0 4 7 3)); }}
    outlet {{ type patch; faces ((1 2 6 5)); }}
    bottom {{ type patch; faces ((0 1 5 4)); }}
    top    {{ type patch; faces ((3 7 6 2)); }}
    front  {{ type patch; faces ((0 3 2 1)); }}
    back   {{ type patch; faces ((4 5 6 7)); }}
);

mergePatchPairs ();
"""


def _surface_features_dict(stl_filename):
    return f"""{_header("dictionary", "surfaceFeaturesDict", "system")}
surfaces ("{stl_filename}");
includedAngle   150;
writeObj        no;
"""


def _snappy_hex_mesh_dict(stl_filename, domain, refinement_level=2, stl_bbox=None):
    # locationInMesh must be a point inside the fluid region (outside the
    # solid). Default: 5% from the inlet/bottom/front corner.
    xmin, xmax = float(domain["xmin"]), float(domain["xmax"])
    ymin, ymax = float(domain["ymin"]), float(domain["ymax"])
    zmin, zmax = float(domain["zmin"]), float(domain["zmax"])
    lx = xmin + 0.05 * (xmax - xmin)
    ly = ymin + 0.05 * (ymax - ymin)
    lz = zmin + 0.05 * (zmax - zmin)
    # If the STL bbox is known, make sure the point is outside it
    if stl_bbox:
        if (stl_bbox["xmin"] <= lx <= stl_bbox["xmax"] and
            stl_bbox["ymin"] <= ly <= stl_bbox["ymax"] and
            stl_bbox["zmin"] <= lz <= stl_bbox["zmax"]):
            # Push past the STL
            lx = stl_bbox["xmax"] + 0.1 * (xmax - stl_bbox["xmax"]) if xmax > stl_bbox["xmax"] else xmin + 0.01
    base = os.path.splitext(stl_filename)[0]
    rl = int(refinement_level)
    return f"""{_header("dictionary", "snappyHexMeshDict", "system")}
castellatedMesh true;
snap            true;
addLayers       false;

geometry
{{
    {stl_filename}
    {{
        type triSurfaceMesh;
        name {base};
    }}
}}

castellatedMeshControls
{{
    maxLocalCells       1000000;
    maxGlobalCells      4000000;
    minRefinementCells  0;
    nCellsBetweenLevels 2;
    features
    (
        {{
            file "{base}.eMesh";
            level {rl};
        }}
    );
    refinementSurfaces
    {{
        {base}
        {{
            level ({rl} {rl});
            patchInfo {{ type wall; }}
        }}
    }}
    resolveFeatureAngle 30;
    refinementRegions {{}}
    locationInMesh ({lx} {ly} {lz});
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch    3;
    tolerance       2.0;
    nSolveIter      30;
    nRelaxIter      5;
    nFeatureSnapIter 10;
    implicitFeatureSnap     false;
    explicitFeatureSnap     true;
    multiRegionFeatureSnap  false;
}}

addLayersControls
{{
    relativeSizes       true;
    layers              {{}}
    expansionRatio      1.0;
    finalLayerThickness 0.3;
    minThickness        0.1;
    nGrow               0;
    featureAngle        60;
    nRelaxIter          3;
    nSmoothSurfaceNormals 1;
    nSmoothNormals      3;
    nSmoothThickness    10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle  90;
    nBufferCellsNoExtrude 0;
    nLayerIter          50;
}}

meshQualityControls
{{
    #include "meshQualityDict"
}}

debug 0;
mergeTolerance 1e-6;
"""


def _mesh_quality_dict():
    return f"""{_header("dictionary", "meshQualityDict", "system")}
maxNonOrtho             65;
maxBoundarySkewness     20;
maxInternalSkewness     4;
maxConcave              80;
minVol                  1e-13;
minTetQuality           1e-30;
minArea                 -1;
minTwist                0.05;
minDeterminant          0.001;
minFaceWeight           0.05;
minVolRatio             0.01;
minTriangleTwist        -1;
nSmoothScale            4;
errorReduction          0.75;
"""
