"""Optional explicit host reset for standalone local evaluation studies."""

from pathlib import Path
import hashlib
import os
import signal
import subprocess
import time
from .storage import write_json


def resolve(value, base):
    if not isinstance(value, dict) or set(value) - {"argv", "cwd", "timeout_seconds"}:
        raise ValueError("Reset accepts argv, cwd and timeout_seconds")
    argv = value.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(s, str) or not s for s in argv)
    ):
        raise ValueError("Reset requires nonempty argv strings")
    seconds = value.get("timeout_seconds", 300)
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("Reset timeout must be 1–3600 seconds")
    cwd = (Path(base) / value.get("cwd", ".")).resolve()
    if not cwd.is_dir():
        raise ValueError("Reset working directory does not exist")
    sources = {}
    for arg in argv:
        path = (cwd / arg).resolve()
        if path.is_file():
            sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(argv=argv, cwd=str(cwd), timeout_seconds=seconds, source_hashes=sources)


def execute(config, directory):
    from .runner import PreparationError

    for path, expected in config["source_hashes"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise PreparationError("Environment reset script changed")
    started = time.monotonic()
    timed_out = False
    with (Path(directory) / "reset.log").open("wb") as stream:
        result = subprocess.Popen(
            config["argv"],
            cwd=config["cwd"],
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            result.wait(timeout=config["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                try:
                    os.killpg(result.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                result.kill()
            result.wait()
    audit = dict(
        passed=result.returncode == 0 and not timed_out,
        method="explicit host reset command",
        argv=config["argv"],
        source_hashes=config["source_hashes"],
        seconds=time.monotonic() - started,
        exit_code=result.returncode,
        timed_out=timed_out,
    )
    write_json(Path(directory) / "reset.json", audit)
    if timed_out:
        raise PreparationError("Environment reset timed out; see reset.log")
    if not audit["passed"]:
        raise PreparationError("Environment reset failed; see reset.log")
    return audit
