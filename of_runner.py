"""Execute OpenFOAM commands through the MSYS2 bash environment."""

import subprocess
import threading
import os
import signal
import time

MSYS_BASH = r"I:\OPENFOAM\v2312\msys64\usr\bin\bash.exe"
OF_BASHRC = "/home/ofuser/OpenFOAM/OpenFOAM-v2312/etc/bashrc"
OF_BIN = r"I:\OPENFOAM\v2312\msys64\home\ofuser\OpenFOAM\OpenFOAM-v2312\platforms\win64MingwDPInt32Opt\bin"
# MS-MPI launcher. Installed under I:\OPENFOAM\bin (non-standard but works).
# Used in MSYS-style path form because we invoke it from bash --login.
MPIEXEC_MSYS = "/i/OPENFOAM/bin/mpiexec.exe"


def _win_to_msys(path):
    """Convert Windows path to MSYS2 path."""
    path = path.replace("\\", "/")
    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        path = f"/{drive}{path[2:]}"
    return path


class SolverProcess:
    """Manages a running OpenFOAM solver process."""

    def __init__(self):
        self.process = None
        self.log_lines = []
        self.running = False
        self.return_code = None
        self._lock = threading.Lock()

    def run_command(self, command, case_dir, callback=None):
        """Run an OpenFOAM command in the case directory."""
        msys_case = _win_to_msys(case_dir)
        full_cmd = f"source {OF_BASHRC} 2>/dev/null; cd '{msys_case}' && {command}"

        env = os.environ.copy()
        env["MSYSTEM"] = "MINGW64"
        env["CHERE_INVOKING"] = "1"

        self.log_lines = []
        self.running = True
        self.return_code = None

        try:
            self.process = subprocess.Popen(
                [MSYS_BASH, "--login", "-c", full_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )

            for raw_line in iter(self.process.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                with self._lock:
                    self.log_lines.append(line)
                if callback:
                    callback(line)

            self.process.wait()
            self.return_code = self.process.returncode
        except Exception as exc:
            with self._lock:
                self.log_lines.append(f"[ERROR] {exc}")
        finally:
            self.running = False
            self.process = None

    def run_async(self, command, case_dir):
        """Run command in a background thread."""
        t = threading.Thread(target=self.run_command, args=(command, case_dir), daemon=True)
        t.start()
        return t

    def stop(self):
        """Stop the running solver."""
        if self.process and self.running:
            try:
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                self.process.kill()

    def get_new_lines(self, after_index=0):
        """Get log lines after the given index."""
        with self._lock:
            return self.log_lines[after_index:]

    @property
    def line_count(self):
        with self._lock:
            return len(self.log_lines)


def run_of_command_sync(command, case_dir, timeout=300):
    """Run an OpenFOAM command synchronously and return output."""
    runner = SolverProcess()
    lines = []
    runner.run_command(command, case_dir, callback=lambda l: lines.append(l))
    return runner.return_code, "\n".join(lines)


def build_solver_command(solver_name, parallel=False, n_procs=1):
    """
    Build the bash command that runs the solver.

    Serial: just `<solver>`
    Parallel: `decomposePar -force && mpiexec -n N <solver> -parallel`

    `decomposePar -force` overwrites any existing processor*/ folders from
    a previous run so the user can re-run without manual cleanup. We do NOT
    auto-reconstruct because for big runs the user often wants to inspect
    processor results in ParaView directly via the `decomposed case` mode.
    """
    if not parallel or n_procs <= 1:
        return solver_name
    # Quoted to make mpiexec interpret the solver name as one token.
    return (
        f"decomposePar -force && "
        f"{MPIEXEC_MSYS} -n {n_procs} {solver_name} -parallel"
    )
