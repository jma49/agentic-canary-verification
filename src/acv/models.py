"""Immutable inputs, audit records, and metrics for the release benchmark."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional, Tuple


class FaultKind(str, Enum):
    SEMANTIC = "semantic"
    CONFIG_DRIFT = "config_drift"
    TAIL_LATENCY = "tail_latency"
    BROWNOUT = "brownout"


@dataclass(frozen=True)
class SimConfig:
    """Probe timing, graph visibility, and environmental sensitivity knobs."""

    graph_recall: float = 0.85
    shock_scale: float = 1.0
    aa_mode: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.graph_recall <= 1.0:
            raise ValueError("graph_recall must be between 0 and 1")
        if not 0.0 <= self.shock_scale < float("inf"):
            raise ValueError("shock_scale must be finite and nonnegative")


@dataclass(frozen=True, order=True)
class Target:
    service: str
    region: str
    flag: str


@dataclass(frozen=True)
class ReleaseChange:
    """Information available from the release manifest and observed call graph."""

    release_id: int
    service: str
    operation: str
    observed_blast_graph: Tuple[Target, ...]


@dataclass(frozen=True)
class Fault:
    """Injection parameters for the environment and ground-truth scorer."""

    kind: FaultKind
    targets: Tuple[Target, ...]
    fixture: str
    magnitude: float
    recurrence_key: Optional[str] = None


@dataclass(frozen=True)
class LoadShock:
    region: str
    start_window: int
    duration: int
    intensity: float


@dataclass(frozen=True)
class Scenario:
    change: ReleaseChange
    fault: Optional[Fault]
    shocks: Tuple[LoadShock, ...]
    staging_queue_min: float


@dataclass(frozen=True)
class ReleaseResult:
    """Scored release and provenance of any rule used before deployment."""

    release_id: int
    fault_kind: Optional[FaultKind]
    outcome: str
    fault_detected: bool
    production_detection_min: Optional[float]
    production_recovery_min: Optional[float]
    release_cycle_min: float
    probe_cost_units: float
    rule_source_release_id: Optional[int] = None
    mined_rule: bool = False


@dataclass(frozen=True)
class RunMetrics:
    """Rates use release counts. Probe cost and escaped outages are totals.

    MTTD averages production detections. MTTR averages production incidents,
    including escaped faults that require manual restoration. Pre-deploy
    findings contribute to recall and cycle time without creating an incident.
    A rollback counts toward recall when its triggering signal requires the
    injection. An incidental rollback still prevents a fault from escaping.
    Empty groups report zero.
    """

    strategy: str
    seed: int
    fault_detection_rate: float
    semantic_bug_recall: float
    config_drift_recall: float
    false_rollback_rate: float
    mttd_min: float
    mttr_min: float
    release_cycle_min: float
    shift_left_prevented_count: int
    probe_cost_units: float
    escaped_outages: int

    def to_dict(self) -> dict:
        return asdict(self)
