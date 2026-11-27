"""Multi-region progressive production verification benchmark."""

from .models import Fault, FaultKind, ReleaseResult, RunMetrics, Scenario, SimConfig, Target
from .scenarios import SCENARIO_COUNTS, generate_scenarios
from .simulator import ALL_STRATEGIES, run_all, run_strategy, simulate_releases

__all__ = [
    "ALL_STRATEGIES",
    "SCENARIO_COUNTS",
    "Fault",
    "FaultKind",
    "ReleaseResult",
    "RunMetrics",
    "Scenario",
    "SimConfig",
    "Target",
    "generate_scenarios",
    "run_all",
    "run_strategy",
    "simulate_releases",
]
