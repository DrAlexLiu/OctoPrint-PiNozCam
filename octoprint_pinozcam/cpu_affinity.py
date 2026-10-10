"""Backend-independent CPU topology and inference-affinity selection."""
import os
from collections import namedtuple

DEFAULT_SYSFS_ROOT = "/sys/devices/system/cpu"

# Minimum relative tier gap treated as a heterogeneous-core boundary.
_MIN_GAP_RATIO = 0.15

CpuTopology = namedtuple(
    "CpuTopology",
    ["allowed", "performance_pool", "is_heterogeneous", "method"])

CpuSelection = namedtuple("CpuSelection", ["cpus", "description"])


def _read_int_file(path):
    """Read one integer sysfs attribute, returning None if unavailable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


DEFAULT_HWMON_ROOT = "/sys/class/hwmon"

PowerState = namedtuple("PowerState", ["undervoltage"])


def _read_undervoltage(hwmon_root=DEFAULT_HWMON_ROOT):
    """Return True/False for present under-voltage, or None if unreadable.

    The Raspberry Pi firmware exposes this through a hwmon device named
    ``rpi_volt``. Its index is not stable -- it follows driver probe order,
    and on a Jetson Orin index 1 is an NVMe drive -- so the device is located
    by name. Absence means "cannot tell", never "healthy".

    ``in0_lcrit_alarm`` reflects the voltage *now*. It is deliberately not
    mixed with vcgencmd's latched history bits, which stay set for the rest
    of the boot after a single dip, so a board that sagged once would
    otherwise be reported as under-voltage forever. Reading it also needs no
    privileges, where /dev/vcio is root-only on some images and
    video-group on others.

    There is deliberately no companion "clock is capped" reading. On a
    Raspberry Pi the firmware throttles below Linux: measured on a sagging
    Pi 3B+, ``scaling_cur_freq`` reported a steady 1400000 in the same
    instant the firmware reported 600 MHz. cpufreq cannot see this, so a
    check built on it would confidently report full speed while the board
    ran at 43% of it.
    """
    try:
        entries = sorted(os.listdir(hwmon_root))
    except OSError:
        return None
    for entry in entries:
        device = os.path.join(hwmon_root, entry)
        try:
            with open(os.path.join(device, "name"),
                      encoding="utf-8") as handle:
                if handle.read().strip() != "rpi_volt":
                    continue
        except (OSError, ValueError):
            continue
        value = _read_int_file(os.path.join(device, "in0_lcrit_alarm"))
        if value is None:
            return None
        return value != 0
    return None


def read_power_state(hwmon_root=DEFAULT_HWMON_ROOT):
    """Return present board power state; the field is None if unmeasurable.

    None where the machine does not expose it, so a caller can distinguish
    "measured healthy" from "not measurable" and say nothing rather than
    claim the power supply is fine.
    """
    return PowerState(undervoltage=_read_undervoltage(hwmon_root))


def _read_allowed_cpus():
    """Return the CPUs this process may currently use."""
    try:
        return frozenset(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        # Fall back to every CPU visible to Python.
        try:
            return frozenset(range(os.cpu_count() or 1))
        except Exception:                           # noqa: BLE001
            return frozenset((0,))


def read_cpu_topology(sysfs_root=DEFAULT_SYSFS_ROOT, allowed_cpus=None):
    """Read per-CPU capacity and maximum frequency without raising."""
    allowed = (frozenset(allowed_cpus) if allowed_cpus is not None
               else _read_allowed_cpus())
    cpus = {}
    for cpu_id in sorted(allowed):
        base = os.path.join(sysfs_root, f"cpu{cpu_id}")
        cpus[cpu_id] = {
            "capacity": _read_int_file(os.path.join(base, "cpu_capacity")),
            "max_freq": _read_int_file(
                os.path.join(base, "cpufreq", "cpuinfo_max_freq")),
        }
    return {"allowed": allowed, "cpus": cpus}


def _metric_values(per_cpu, allowed, metric):
    """{cpu_id: value} for every allowed CPU that has a real reading."""
    values = {}
    for cpu_id in allowed:
        value = (per_cpu.get(cpu_id) or {}).get(metric)
        if value is not None:
            values[cpu_id] = value
    return values


def _reliable_split(per_cpu, allowed, metric):
    """Try to split `allowed` into a performance-core pool using one
    metric ("capacity" or "max_freq").

    Sorts the DISTINCT values seen, finds the single largest gap
    between consecutive values, and -- if that gap is big enough
    relative to its lower neighbour (see _MIN_GAP_RATIO) -- returns
    every CPU at or above the gap. This is a max-gap split, not an
    "equals the maximum" split: on
    [414,414,414,414,1002,1002,1024,1024] the gap is 414->1002 and the
    pool is every CPU >= 1002, i.e. {4,5,6,7}, not just the two CPUs
    that happen to read exactly 1024.

    Returns a list of cpu ids (unsorted), or None if this metric has no
    reading at all for `allowed`, or no gap worth trusting -- flat
    values, a single distinct reading, or a gap that looks like DVFS/
    measurement noise rather than a real cluster boundary.
    """
    values = _metric_values(per_cpu, allowed, metric)
    distinct = sorted(set(values.values()))
    if len(distinct) < 2:
        return None                # nothing to compare: flat, or empty
    best_gap, lower, upper = max(
        ((distinct[i + 1] - distinct[i], distinct[i], distinct[i + 1])
         for i in range(len(distinct) - 1)),
        key=lambda gap_lower_upper: gap_lower_upper[0])
    if lower <= 0 or (best_gap / float(lower)) < _MIN_GAP_RATIO:
        return None                # too small to trust as a real split
    return [cpu_id for cpu_id, value in values.items() if value >= upper]


def _pool_sort_key(per_cpu):
    """Sort key for performance-pool order: capacity desc, then
    max_freq desc, then CPU number asc.

    Only affects which cores select_ai_cpus prefers first WITHIN a
    tier, never how many cores end up in the pool. A missing reading
    sorts as if it were the lowest possible value, so a CPU with a real
    reading always outranks one without -- relevant when a CPU reached
    the pool through one metric (e.g. max_freq, during fallback) but
    has no reading at all for the other.
    """
    def key(cpu_id):
        """Return one CPU's descending-capacity/frequency sort tuple."""
        info = per_cpu.get(cpu_id) or {}
        capacity = info.get("capacity")
        max_freq = info.get("max_freq")
        return (-(capacity if capacity is not None else -1),
                -(max_freq if max_freq is not None else -1),
                cpu_id)
    return key


def detect_cpu_topology(raw_topology):
    """Pure function: raw sysfs readings -> a CpuTopology. No IO.

    Detection order: try cpu_capacity first; if it gives no
    trustworthy gap (missing entirely, missing for most CPUs, or
    present but flat), fall back to cpuinfo_max_freq; if that also
    gives no trustworthy gap, call the board homogeneous. Never
    guesses a split that the numbers do not support.

    A CPU whose value is unreadable for whichever metric found the
    split is left out of performance_pool -- it is not CONFIRMED to be
    a performance core, so it is not included on a guess. It still
    appears in `allowed`.
    """
    allowed = frozenset(raw_topology.get("allowed") or ())
    per_cpu = raw_topology.get("cpus") or {}

    for metric in ("capacity", "max_freq"):
        pool = _reliable_split(per_cpu, allowed, metric)
        if pool is not None:
            ordered = tuple(sorted(pool, key=_pool_sort_key(per_cpu)))
            return CpuTopology(allowed=allowed, performance_pool=ordered,
                               is_heterogeneous=True, method=metric)

    return CpuTopology(allowed=allowed,
                       performance_pool=tuple(sorted(allowed)),
                       is_heterogeneous=False, method="homogeneous")


def select_ai_cpus(cpu_speed_control, topology):
    """Pure function: (0.25/0.5/0.75/1.0, CpuTopology) -> CpuSelection.

    No IO, no randomness, no global state. Takes the requested share of
    topology.performance_pool (already priority-ordered by
    detect_cpu_topology), rounding to the nearest CPU count and never
    landing on zero or on more than the pool has.

    Homogeneous boards keep detect.py's existing _thread_calculation()
    behaviour of reserving one CPU for OctoPrint/gcode streaming below
    100%. Heterogeneous boards skip that reservation on purpose: the
    LITTLE cores outside the pool are free for the system regardless of
    how much of the performance pool AI takes.
    """
    pool = topology.performance_pool
    pool_size = len(pool)
    if pool_size == 0:
        # Defensive only -- a live process is always allowed at least
        # one CPU, so read_cpu_topology can never really produce this.
        return CpuSelection(cpus=frozenset(),
                            description="AI affinity: none (no CPUs)")

    # round() is Python's built-in round-half-to-even: exactly on a .5
    # boundary (only reachable at 50% on an ODD pool_size) this ties to
    # the nearest EVEN count, not always down and not always up -- a
    # 5-cpu pool gives round(2.5) == 2, a 3-cpu pool gives
    # round(1.5) == 2. No board in this project's fleet has an odd
    # performance-pool size today, so this has never mattered in
    # practice; pinned in test_odd_pool_size_rounds_half_to_even so a
    # future change here cannot silently swap the rounding rule.
    wanted = int(round(pool_size * cpu_speed_control))
    wanted = max(1, min(pool_size, wanted))
    if (not topology.is_heterogeneous and cpu_speed_control < 1.0
            and pool_size > 1):
        wanted = min(wanted, pool_size - 1)
    chosen = pool[:wanted]

    pct = int(round(cpu_speed_control * 100))
    cpu_list = ",".join(str(cpu_id) for cpu_id in sorted(chosen))
    if topology.is_heterogeneous:
        description = (f"AI affinity: {cpu_list} "
                       f"({pct}% of {pool_size} performance cores)")
    else:
        description = (f"AI affinity: {cpu_list} "
                       f"({pct}% of {pool_size} cores)")
    return CpuSelection(cpus=frozenset(chosen), description=description)
