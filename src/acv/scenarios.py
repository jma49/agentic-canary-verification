"""Generate a chronological, strategy-independent workload from a seed."""

from random import Random
from typing import Optional, Tuple

from .models import Fault, FaultKind, LoadShock, ReleaseChange, Scenario, SimConfig
from .topology import REGIONS, SERVICES, impact_services, target_for


SCENARIO_COUNTS = (
    (None, 20),
    (FaultKind.SEMANTIC, 15),
    (FaultKind.CONFIG_DRIFT, 20),
    (FaultKind.TAIL_LATENCY, 15),
    (FaultKind.BROWNOUT, 10),
)
_RECURRING_COUNTS = {
    FaultKind.SEMANTIC: 4, FaultKind.CONFIG_DRIFT: 5,
    FaultKind.TAIL_LATENCY: 4, FaultKind.BROWNOUT: 2,
}
_OPERATIONS = ("read", "write", "retry", "refresh", "batch")


def generate_scenarios(seed: int, config: Optional[SimConfig] = None) -> Tuple[Scenario, ...]:
    """Keep faults, shocks, and chronology constant in graph-recall sweeps.

    Each release has a regional routing leg, including benign releases. To
    model stale regional route discovery, independently rank clean and faulty
    releases for omission of that routing leg in the observed dependency
    graph. The mask is independent of fault kind and recurrence within the
    faulty group and uses the same omission rate in the clean group.
    """
    config = SimConfig() if config is None else config
    rng = Random(seed)
    entries = [(kind, index) for kind, count in SCENARIO_COUNTS for index in range(count)]
    rng.shuffle(entries)
    graph_rng = Random(f"acv-graph-{seed}")
    hidden = set()
    for faulty in (False, True):
        eligible = [i for i, (kind, _) in enumerate(entries) if (kind is not None) == faulty]
        graph_rng.shuffle(eligible)
        omissions = round(len(eligible) * (1.0 - config.graph_recall))
        hidden.update(eligible[:omissions])

    recurrent = {}
    for kind in _RECURRING_COUNTS:
        # The same change surface and rollout leg can also appear on a clean release.
        recurrent[kind] = (
            rng.choice(SERVICES), rng.choice(_OPERATIONS),
            tuple(rng.sample(REGIONS[1:], rng.choice((1, 2)))),
        )

    scenarios = []
    for position, (kind, index) in enumerate(entries):
        is_recurring = kind is not None and index < _RECURRING_COUNTS[kind]
        if is_recurring:
            service, operation, routing_leg = recurrent[kind]
        elif kind is None and index < len(recurrent):
            service, operation, routing_leg = tuple(recurrent.values())[index]
        else:
            service = rng.choice(SERVICES)
            operation = rng.choice(_OPERATIONS)
            eligible_regions = REGIONS[1:] if kind == FaultKind.CONFIG_DRIFT else REGIONS
            routing_leg = tuple(rng.sample(eligible_regions, rng.choice((1, 1, 2))))

        cone = impact_services(service)
        observed_regions = [region for region in REGIONS
                            if position not in hidden or region not in routing_leg]
        graph = tuple(target_for(caller, region) for caller in cone for region in observed_regions)
        change = ReleaseChange(position + 1, service, operation, graph)
        fault = None
        if kind is not None:
            affected_services = cone[:3] if kind == FaultKind.BROWNOUT else cone[:1]
            targets = tuple(target_for(caller, region)
                            for caller in affected_services for region in routing_leg)
            fixture = "standard"
            if kind == FaultKind.SEMANTIC:
                fixture = "edge" if index < 4 or 8 <= index < 11 else "standard"
                if index >= 11:
                    fixture = "production"
            fault = Fault(
                kind, targets, fixture, rng.uniform(.75, 1.25),
                f"{kind.value}:{service}:{operation}" if is_recurring else None,
            )

        shocks = ()
        if rng.random() < .63:
            first_shock = LoadShock(
                rng.choice(REGIONS), rng.randrange(4), rng.choice((1, 2, 3)),
                rng.uniform(.55, 1.35),
            )
            shocks = (first_shock,)
            if rng.random() < .28:
                another_region = rng.choice(tuple(r for r in REGIONS if r != first_shock.region))
                shocks += (LoadShock(another_region, rng.randrange(4), 2,
                                     rng.uniform(.45, 1.10)),)
        scenarios.append(Scenario(change, fault, shocks, rng.uniform(16, 32)))
    return tuple(scenarios)
