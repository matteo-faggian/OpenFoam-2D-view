"""
benchmark_procs.py - Sweep MPI process counts to find the sweet spot.

Usage:
    python benchmark_procs.py <case_name> [--n 4,8,12,16,20,24] [--seconds 60]

For each N in the list:
  1. Clones the case to cases/<case>__bench_N (so the original is untouched)
  2. Rewrites decomposeParDict for N processes
  3. Runs decomposePar + mpiexec for at most --seconds wall-clock seconds
  4. Counts iterations completed (number of "Time = ..." lines)
  5. Removes the clone

Prints a table at the end with iter/s and relative speedup. Highest
iter/s wins. Run with the Flask GUI closed (or at least not solving)
to avoid CPU contention skewing the numbers.

The case must already be meshed and have a valid system/controlDict
(do Mesh + Imposta in the GUI first, then close the simulation).
"""

import os
import re
import sys
import shutil
import time
import signal
import subprocess
import threading
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from of_runner import MSYS_BASH, OF_BASHRC, MPIEXEC_MSYS, _win_to_msys
from mesher import _decompose_par_dict, _write

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.path.join(BASE_DIR, "cases")

TIME_LINE = re.compile(rb"^Time\s*=\s*[0-9]")


def read_solver(case_dir):
    cd = os.path.join(case_dir, "system", "controlDict")
    try:
        with open(cd, "r", encoding="utf-8", errors="replace") as f:
            m = re.search(r"application\s+(\w+)\s*;", f.read())
        return m.group(1) if m else "rhoSimpleFoam"
    except FileNotFoundError:
        return "rhoSimpleFoam"


def bash_run(case_dir, command):
    """Run a one-shot bash command in the OF environment."""
    msys = _win_to_msys(case_dir)
    full = f"source {OF_BASHRC} 2>/dev/null && cd '{msys}' && {command}"
    return subprocess.run(
        [MSYS_BASH, "--login", "-c", full],
        capture_output=True, text=True,
    )


def kill_tree(pid):
    """Kill a process tree on Windows."""
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
    )


def run_bench(case_dir, n, max_seconds):
    """
    Returns (iters, elapsed_seconds, error_msg or None).
    """
    # Remove any leftover processor* dirs
    for d in os.listdir(case_dir):
        if d.startswith("processor"):
            shutil.rmtree(os.path.join(case_dir, d), ignore_errors=True)

    _write(case_dir, "system/decomposeParDict", _decompose_par_dict(n))

    r = bash_run(case_dir, "decomposePar -force")
    if r.returncode != 0:
        return 0, 0, f"decomposePar failed:\n{r.stdout[-500:]}\n{r.stderr[-500:]}"

    solver = read_solver(case_dir)
    msys = _win_to_msys(case_dir)
    cmd = (
        f"source {OF_BASHRC} 2>/dev/null && cd '{msys}' && "
        f"{MPIEXEC_MSYS} -n {n} {solver} -parallel"
    )

    start = time.time()
    proc = subprocess.Popen(
        [MSYS_BASH, "--login", "-c", cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )

    # Hard kill the whole process tree after max_seconds. taskkill /T also
    # gets mpiexec and its solver children — sending a signal to bash alone
    # leaves orphaned solver.exe processes that keep eating CPU.
    timer = threading.Timer(max_seconds, lambda: kill_tree(proc.pid))
    timer.daemon = True
    timer.start()

    iters = 0
    try:
        for line in proc.stdout:
            if TIME_LINE.match(line):
                iters += 1
    except Exception:
        pass

    timer.cancel()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)

    elapsed = time.time() - start
    # Belt and braces: nuke any leftover solver processes by name.
    subprocess.run(
        ["taskkill", "/F", "/IM", f"{solver}.exe"],
        capture_output=True,
    )
    return iters, elapsed, None


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("case", help="case name in webapp/cases/")
    p.add_argument("--n", default="4,8,12,16,20,24",
                   help="comma-separated process counts (default: 4,8,12,16,20,24)")
    p.add_argument("--seconds", type=float, default=60,
                   help="wall-clock seconds per test (default: 60)")
    args = p.parse_args()

    src = os.path.join(CASES_DIR, args.case)
    if not os.path.isdir(src):
        print(f"Case '{args.case}' non trovato in {CASES_DIR}")
        sys.exit(1)
    if not os.path.isfile(os.path.join(src, "constant", "polyMesh", "points")):
        print("La mesh non esiste per questo caso. Premi 'Mesh' nella GUI prima.")
        sys.exit(1)
    if not os.path.isfile(os.path.join(src, "system", "controlDict")):
        print("Manca system/controlDict. Premi 'Imposta' nella GUI prima.")
        sys.exit(1)

    try:
        n_list = [int(x) for x in args.n.split(",")]
    except ValueError:
        print(f"--n deve essere lista di interi separati da virgola, dato: {args.n}")
        sys.exit(1)

    print(f"\nBenchmark caso '{args.case}' - {args.seconds}s per N")
    print(f"Lista N: {n_list}")
    print(f"Solver: {read_solver(src)}\n")
    print(f"{'N':>4}  {'iters':>8}  {'iter/s':>10}  {'vs first':>10}")
    print("-" * 40)

    results = []
    baseline = None
    for n in n_list:
        work = os.path.join(CASES_DIR, f"{args.case}__bench_{n}")
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
        # Don't copy any processor* leftovers from the source
        shutil.copytree(src, work, ignore=shutil.ignore_patterns("processor*"))

        try:
            iters, elapsed, err = run_bench(work, n, args.seconds)
            if err:
                print(f"{n:>4}  FALLITO: {err[:120]}")
                continue
            rate = iters / elapsed if elapsed > 0 else 0
            if baseline is None or baseline == 0:
                baseline = rate
            speedup = rate / baseline if baseline > 0 else 1.0
            results.append((n, iters, rate, speedup))
            print(f"{n:>4}  {iters:>8}  {rate:>10.2f}  {speedup:>9.2f}x")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print()
    if results:
        best = max(results, key=lambda r: r[2])
        print(f"Migliore: N={best[0]}  ({best[2]:.2f} iter/s, {best[3]:.2f}x rispetto a N={results[0][0]})")
        worst_at_high = results[-1]
        if worst_at_high[2] < results[0][2]:
            print(f"Nota: N={worst_at_high[0]} e' piu' lento di N={results[0][0]}: troppi processi per la mesh.")
    else:
        print("Nessun risultato valido.")


if __name__ == "__main__":
    main()
