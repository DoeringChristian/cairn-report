"""What a run records about the machine it ran on: `system/info.json` once, `system/metrics.jsonl` every few seconds,
and the process's output in `logs/stdout.txt` and `logs/stderr.txt`.

- `info()`: host, platform, Python, the command line, the working directory, the pid, the Slurm job (job id, array
  job and task id, node list), and the environment variables matching `SLURM_*` or `CUDA_VISIBLE_DEVICES` (nothing
  else, so no secret of the environment is recorded). The installed packages go to `environment/pip-freeze.txt`
  (cairn/client/code.py), not here.
- `Sampler`: one entry per interval (default 10 s) with CPU, memory, disk, and network use, and per GPU its
  utilization, memory, temperature, and power. psutil is used when installed, else `/proc` and `os`-level
  fallbacks (load average, `resource`, `shutil.disk_usage`); GPUs through pynvml when installed, else `nvidia-smi`
  when it is on the PATH, else none. Keys are flat (`cpu_percent`, `memory_percent`, `gpu0_util_percent`, ...),
  so every key charts over `time`.
- `Capture`: a tee of a file descriptor (1 and 2): the output still reaches the terminal, and is appended to the
  log file as it comes, also what subprocesses and C extensions write. Where the stream has no file descriptor
  (an interactive session, a notebook), the Python stream is wrapped instead.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ENV_PREFIXES = ("SLURM_",)
ENV_NAMES = ("CUDA_VISIBLE_DEVICES",)
INTERVAL = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def info() -> dict:
    """The facts of this process and machine (see the module doc)."""
    environ = {
        key: value for key, value in sorted(os.environ.items()) if key.startswith(ENV_PREFIXES) or key in ENV_NAMES
    }
    slurm = {
        key: os.environ[name]
        for key, name in (
            ("job_id", "SLURM_JOB_ID"),
            ("array_job_id", "SLURM_ARRAY_JOB_ID"),
            ("array_task_id", "SLURM_ARRAY_TASK_ID"),
            ("nodelist", "SLURM_JOB_NODELIST"),
            ("partition", "SLURM_JOB_PARTITION"),
        )
        if os.environ.get(name)
    }
    found = {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpus": os.cpu_count(),
        "python": {
            "version": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "command": [sys.executable, *sys.argv] if sys.argv and sys.argv[0] else [sys.executable],
        "argv": list(sys.argv),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "user": _user(),
        "started": _now(),
        "slurm": slurm or None,
        "environment": environ,
    }
    gpus = _gpu_names()
    if gpus:
        found["gpus"] = gpus
    return found


def _user() -> str | None:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return None


# ------------------------------------------------------------------ sampling


class _Fallback:
    """CPU and memory without psutil: /proc where it exists, else the load average and `resource`."""

    def __init__(self):
        self._cpu = self._proc_stat()

    @staticmethod
    def _proc_stat():
        try:
            fields = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
            values = [int(item) for item in fields]
            return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
        except (OSError, ValueError, IndexError):
            return None

    def sample(self, folder: Path) -> dict:
        found: dict = {}
        current = self._proc_stat()
        if current and self._cpu and current[0] > self._cpu[0]:
            total, idle = current[0] - self._cpu[0], current[1] - self._cpu[1]
            found["cpu_percent"] = round(100.0 * (1 - idle / total), 1)
        self._cpu = current or self._cpu
        try:
            found["load_1m"] = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            pass
        try:
            meminfo = {
                line.split(":")[0]: int(line.split()[1])
                for line in Path("/proc/meminfo").read_text().splitlines()
                if len(line.split()) >= 2
            }
            total, available = meminfo["MemTotal"], meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            found["memory_used_gb"] = round((total - available) / 2**20, 3)
            found["memory_percent"] = round(100.0 * (total - available) / total, 1)
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            pass
        try:
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            found["process_max_rss_gb"] = round(rss / (2**30 if sys.platform == "darwin" else 2**20), 3)
        except (ImportError, OSError):
            pass
        try:
            for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
                name, _, rest = line.partition(":")
                if name.strip() == "lo":
                    continue
                values = rest.split()
                found["net_recv_mb"] = round(found.get("net_recv_mb", 0) + int(values[0]) / 1e6, 3)
                found["net_sent_mb"] = round(found.get("net_sent_mb", 0) + int(values[8]) / 1e6, 3)
        except (OSError, ValueError, IndexError):
            pass
        return found


class _Psutil:
    def __init__(self, psutil):
        self.psutil = psutil
        self.process = psutil.Process()
        psutil.cpu_percent(None)  # the first call only starts the measurement
        self.process.cpu_percent(None)

    def sample(self, folder: Path) -> dict:
        psutil, found = self.psutil, {}
        found["cpu_percent"] = psutil.cpu_percent(None)
        memory = psutil.virtual_memory()
        found["memory_used_gb"] = round(memory.used / 2**30, 3)
        found["memory_percent"] = memory.percent
        try:
            with self.process.oneshot():
                found["process_cpu_percent"] = self.process.cpu_percent(None)
                found["process_rss_gb"] = round(self.process.memory_info().rss / 2**30, 3)
                found["process_threads"] = self.process.num_threads()
        except psutil.Error:
            pass
        try:
            disk = psutil.disk_io_counters()
            if disk is not None:
                found["disk_read_mb"] = round(disk.read_bytes / 1e6, 3)
                found["disk_write_mb"] = round(disk.write_bytes / 1e6, 3)
        except (RuntimeError, OSError):
            pass
        try:
            net = psutil.net_io_counters()
            found["net_sent_mb"] = round(net.bytes_sent / 1e6, 3)
            found["net_recv_mb"] = round(net.bytes_recv / 1e6, 3)
        except (RuntimeError, OSError):
            pass
        return found


def _gpu_names() -> list[str]:
    try:
        import pynvml

        pynvml.nvmlInit()
        return [
            _text(pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(i)))
            for i in range(pynvml.nvmlDeviceGetCount())
        ]
    except Exception:
        pass
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout
            return [line.strip() for line in out.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    return []


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class _Gpus:
    """Per-GPU utilization, memory, temperature, power: pynvml, else nvidia-smi, else nothing."""

    QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"

    def __init__(self):
        self.nvml = None
        self.smi = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self.nvml = pynvml
        except Exception:
            self.smi = shutil.which("nvidia-smi")

    def sample(self) -> dict:
        found: dict = {}
        if self.nvml is not None:
            nvml = self.nvml
            try:
                for index in range(nvml.nvmlDeviceGetCount()):
                    handle = nvml.nvmlDeviceGetHandleByIndex(index)
                    memory = nvml.nvmlDeviceGetMemoryInfo(handle)
                    found[f"gpu{index}_util_percent"] = nvml.nvmlDeviceGetUtilizationRates(handle).gpu
                    found[f"gpu{index}_memory_used_gb"] = round(memory.used / 2**30, 3)
                    found[f"gpu{index}_memory_percent"] = (
                        round(100.0 * memory.used / memory.total, 1) if memory.total else None
                    )
                    try:
                        found[f"gpu{index}_temperature_c"] = nvml.nvmlDeviceGetTemperature(handle, 0)
                        found[f"gpu{index}_power_w"] = round(nvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1)
                    except Exception:
                        pass
            except Exception:
                pass
            return found
        if self.smi:
            try:
                out = subprocess.run(
                    [self.smi, f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                self.smi = None
                return found
            for index, line in enumerate(row for row in out.splitlines() if row.strip()):
                values = [_number(item) for item in line.split(",")]
                if len(values) < 5:
                    continue
                util, used, total, temperature, power = values[:5]
                found[f"gpu{index}_util_percent"] = util
                found[f"gpu{index}_memory_used_gb"] = round(used / 1024, 3) if used is not None else None
                found[f"gpu{index}_memory_percent"] = (
                    round(100.0 * used / total, 1) if used is not None and total else None
                )
                found[f"gpu{index}_temperature_c"] = temperature
                found[f"gpu{index}_power_w"] = power
        return found


def _number(text: str):
    try:
        return float(text.strip())
    except ValueError:
        return None


class Sampler:
    """A daemon thread appending one entry to `<folder>/system/metrics.jsonl` every `interval` seconds (the first at
    once, the last when it stops)."""

    def __init__(self, folder: Path, interval: float = INTERVAL, gpus: bool = True):
        self.folder, self.interval = Path(folder), max(0.05, float(interval))
        self.file = self.folder / "system" / "metrics.jsonl"
        try:
            import psutil

            self.source = _Psutil(psutil)
        except Exception:
            self.source = _Fallback()
        self.gpus = _Gpus() if gpus else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = time.monotonic()

    def sample(self) -> dict:
        found = {"time": _now(), "elapsed_s": round(time.monotonic() - self._start, 3)}
        try:
            found.update(self.source.sample(self.folder))
        except Exception:
            pass
        try:
            usage = shutil.disk_usage(self.folder)
            found["disk_percent"] = round(100.0 * usage.used / usage.total, 1)
            found["disk_free_gb"] = round(usage.free / 2**30, 3)
        except OSError:
            pass
        if self.gpus is not None:
            try:
                found.update(self.gpus.sample())
            except Exception:
                pass
        return found

    def write(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self.sample(), allow_nan=False, default=str) + "\n")
        except (OSError, ValueError):
            pass

    def start(self) -> "Sampler":
        self.write()
        self._thread = threading.Thread(target=self._loop, name="cairn-system", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.write()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None
        self.write()


# ------------------------------------------------------------------ output capture


class _StreamTee:
    """A Python stream that also appends what is written to a file (where the stream has no file descriptor)."""

    def __init__(self, stream, file):
        self._stream, self._file = stream, file

    def write(self, text):
        written = self._stream.write(text)
        try:
            self._file.write(text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text))
            self._file.flush()
        except (OSError, ValueError):
            pass
        return written

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class Capture:
    """Tee one standard stream (`which`: "stdout" or "stderr") into `path` until `stop()` (see the module doc)."""

    def __init__(self, which: str, path: Path, fd_level: bool = True):
        self.which, self.path = which, Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("ab", buffering=0)
        self.stream = getattr(sys, which)
        self.fd = None
        self.saved = None
        self.thread: threading.Thread | None = None
        self.wrapper = None
        fd = None
        if fd_level:
            try:
                fd = self.stream.fileno()
            except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is both
                fd = None
        if fd is not None:
            self._start_fd(fd)
        else:
            self.wrapper = _StreamTee(self.stream, self.file)
            setattr(sys, which, self.wrapper)

    def _start_fd(self, fd: int) -> None:
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass
        self.fd, self.saved = fd, os.dup(fd)
        read, write = os.pipe()
        os.dup2(write, fd)
        os.close(write)
        self.thread = threading.Thread(target=self._pump, args=(read,), name=f"cairn-{self.which}", daemon=True)
        self.thread.start()

    def _pump(self, read: int) -> None:
        try:
            while True:
                chunk = os.read(read, 65536)
                if not chunk:
                    break
                try:
                    os.write(self.saved, chunk)
                except OSError:
                    pass
                try:
                    self.file.write(chunk)
                except (OSError, ValueError):
                    pass
        finally:
            os.close(read)

    def stop(self) -> None:
        if self.wrapper is not None:
            if getattr(sys, self.which) is self.wrapper:
                setattr(sys, self.which, self.stream)
            self.wrapper = None
        elif self.fd is not None:
            try:
                getattr(sys, self.which).flush()
            except (OSError, ValueError, AttributeError):
                pass
            os.dup2(self.saved, self.fd)  # closes our pipe end: the pump sees the end (unless a child still holds it)
            if self.thread is not None:
                self.thread.join(timeout=2)
            os.close(self.saved)
            self.fd = self.saved = self.thread = None
        try:
            self.file.close()
        except OSError:
            pass
