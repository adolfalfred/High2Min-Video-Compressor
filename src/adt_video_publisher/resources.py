"""Cross-platform hardware discovery and conservative worker selection."""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import struct
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import InvalidInputError, ResourceLimitError
from .planning import DEFAULT_MAXIMUM_BYTES
from .processes import hidden_process_options

MIB = 1024 * 1024
MINIMUM_MEMORY_PER_WORKER = 512 * MIB
MINIMUM_MEMORY_RESERVE = 256 * MIB
MEMORY_RESERVE_RATIO = 0.10
KNOWN_H264_ENCODERS = (
    "h264_nvenc",
    "h264_qsv",
    "h264_amf",
    "h264_videotoolbox",
    "h264_vaapi",
)


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    logical_cpu_count: int
    physical_cpu_count: int | None
    total_memory_bytes: int
    available_memory_bytes: int
    available_disk_bytes: int
    hardware_encoders: tuple[str, ...]
    platform: str

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["hardware_encoders"] = list(self.hardware_encoders)
        return document

    def to_contract_dict(self) -> dict[str, object]:
        return {
            "logical_cpu_count": self.logical_cpu_count,
            "physical_cpu_count": self.physical_cpu_count,
            "available_memory_bytes": self.available_memory_bytes,
            "available_disk_bytes": self.available_disk_bytes,
            "hardware_encoders": list(self.hardware_encoders),
        }


@dataclass(frozen=True, slots=True)
class WorkerPlan:
    requested_workers: str | int
    workers: int
    encoder_threads_per_worker: int
    cpu_worker_limit: int
    memory_worker_limit: int
    item_limit: int
    limiting_factor: str
    estimated_required_disk_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _windows_memory() -> tuple[int, int]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0, 0
    return int(status.total_physical), int(status.available_physical)


def _linux_memory() -> tuple[int, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            match = re.match(r"^(MemTotal|MemAvailable):\s+(\d+)\s+kB$", line)
            if match:
                values[match.group(1)] = int(match.group(2)) * 1024
    except OSError:
        return 0, 0
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def _macos_memory() -> tuple[int, int]:
    try:
        total = int(
            subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
                **hidden_process_options(),
            ).stdout.strip()
        )
        page_size = int(
            subprocess.run(
                ["sysctl", "-n", "hw.pagesize"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
                **hidden_process_options(),
            ).stdout.strip()
        )
        vm_output = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            **hidden_process_options(),
        ).stdout
        available_pages = 0
        for label in ("Pages free", "Pages inactive", "Pages speculative"):
            match = re.search(rf"^{label}:\s+(\d+)\.", vm_output, re.MULTILINE)
            if match:
                available_pages += int(match.group(1))
        return total, available_pages * page_size
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, 0


def _memory() -> tuple[int, int]:
    system = platform.system()
    if system == "Windows":
        return _windows_memory()
    if system == "Linux":
        return _linux_memory()
    if system == "Darwin":
        return _macos_memory()
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = page_size * os.sysconf("SC_PHYS_PAGES")
        available = page_size * os.sysconf("SC_AVPHYS_PAGES")
        return int(total), int(available)
    except (AttributeError, OSError, ValueError):
        return 0, 0


def _windows_physical_cores() -> int | None:
    relation_processor_core = 0
    length = ctypes.c_ulong(0)
    function = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
    function(relation_processor_core, None, ctypes.byref(length))
    if length.value == 0:
        return None
    buffer = ctypes.create_string_buffer(length.value)
    if not function(relation_processor_core, buffer, ctypes.byref(length)):
        return None
    offset = 0
    core_count = 0
    while offset + 8 <= length.value:
        relationship, size = struct.unpack_from("II", buffer.raw, offset)
        if size < 8 or offset + size > length.value:
            return None
        if relationship == relation_processor_core:
            core_count += 1
        offset += size
    return core_count or None


def _linux_physical_cores() -> int | None:
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pairs: set[tuple[str, str]] = set()
    for block in text.split("\n\n"):
        physical = re.search(r"^physical id\s*:\s*(.+)$", block, re.MULTILINE)
        core = re.search(r"^core id\s*:\s*(.+)$", block, re.MULTILINE)
        if physical and core:
            pairs.add((physical.group(1).strip(), core.group(1).strip()))
    return len(pairs) or None


def _physical_cores() -> int | None:
    system = platform.system()
    if system == "Windows":
        return _windows_physical_cores()
    if system == "Linux":
        return _linux_physical_cores()
    if system == "Darwin":
        try:
            return int(
                subprocess.run(
                    ["sysctl", "-n", "hw.physicalcpu"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                    **hidden_process_options(),
                ).stdout.strip()
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def _nearest_existing(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise InvalidInputError(f"No existing parent was found for '{path}'.")
        candidate = parent
    return candidate


def detect_hardware_encoders(ffmpeg_path: str | os.PathLike[str] | None) -> tuple[str, ...]:
    if not ffmpeg_path:
        return ()
    path = Path(ffmpeg_path).expanduser().resolve()
    if not path.is_file():
        return ()
    try:
        result = subprocess.run(
            [str(path), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            **hidden_process_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    description = "\n".join((result.stdout, result.stderr))
    compiled = tuple(
        encoder for encoder in KNOWN_H264_ENCODERS if re.search(rf"\b{encoder}\b", description)
    )
    usable: list[str] = []
    for encoder in compiled:
        try:
            trial = subprocess.run(
                [
                    str(path),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:r=1:d=0.1",
                    "-frames:v",
                    "1",
                    "-an",
                    "-c:v",
                    encoder,
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=4,
                check=False,
                **hidden_process_options(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if trial.returncode == 0:
            usable.append(encoder)
    return tuple(usable)


def detect_resources(
    output_path: str | os.PathLike[str],
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
) -> ResourceSnapshot:
    logical = max(1, os.cpu_count() or 1)
    physical = _physical_cores()
    total_memory, available_memory = _memory()
    disk_root = _nearest_existing(Path(output_path))
    available_disk = shutil.disk_usage(disk_root).free
    return ResourceSnapshot(
        logical_cpu_count=logical,
        physical_cpu_count=physical,
        total_memory_bytes=max(0, total_memory),
        available_memory_bytes=max(0, available_memory),
        available_disk_bytes=max(0, available_disk),
        hardware_encoders=detect_hardware_encoders(ffmpeg_path),
        platform=f"{platform.system()}-{platform.machine()}",
    )


def select_worker_plan(
    snapshot: ResourceSnapshot,
    *,
    item_count: int,
    requested_workers: str | int = "auto",
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
) -> WorkerPlan:
    if item_count < 1:
        raise InvalidInputError("At least one item is required for worker planning.")
    if requested_workers != "auto" and (
        not isinstance(requested_workers, int) or isinstance(requested_workers, bool) or requested_workers < 1
    ):
        raise InvalidInputError("Workers must be 'auto' or a positive integer.")

    physical = snapshot.physical_cpu_count or max(1, snapshot.logical_cpu_count // 2)
    cpu_limit = max(1, physical // 2)
    reserve = max(MINIMUM_MEMORY_RESERVE, int(snapshot.total_memory_bytes * MEMORY_RESERVE_RATIO))
    usable_memory = max(0, snapshot.available_memory_bytes - reserve)
    memory_limit = usable_memory // MINIMUM_MEMORY_PER_WORKER
    if memory_limit < 1:
        raise ResourceLimitError(
            "Less than 512 MiB of working memory remains after the safety reserve."
        )

    limits = {
        "cpu": cpu_limit,
        "memory": int(memory_limit),
        "items": item_count,
    }
    if isinstance(requested_workers, int):
        limits["requested"] = requested_workers
    workers = max(1, min(limits.values()))
    limiting_factor = min(limits, key=limits.get)
    threads = max(1, snapshot.logical_cpu_count // workers)
    required_disk = math_ceil_ratio(maximum_bytes * (item_count + workers), 0.10)
    if snapshot.available_disk_bytes < required_disk:
        raise ResourceLimitError(
            f"Insufficient output disk space: {required_disk} bytes required, "
            f"{snapshot.available_disk_bytes} bytes available."
        )
    return WorkerPlan(
        requested_workers=requested_workers,
        workers=workers,
        encoder_threads_per_worker=threads,
        cpu_worker_limit=cpu_limit,
        memory_worker_limit=int(memory_limit),
        item_limit=item_count,
        limiting_factor=limiting_factor,
        estimated_required_disk_bytes=required_disk,
    )


def math_ceil_ratio(value: int, ratio: float) -> int:
    """Return value plus a rounded-up safety ratio without floating-point undercount."""

    return value + int(value * ratio + 0.999999)
