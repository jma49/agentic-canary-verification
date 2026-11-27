"""Progressive rollout environment, verification policies, and scoring."""

from dataclasses import dataclass, replace
from random import Random
from statistics import fmean
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from .models import FaultKind, ReleaseChange, ReleaseResult, RunMetrics, Scenario, SimConfig, Target
from .scenarios import generate_scenarios
from .topology import CONFIG_OVERLAYS, REGIONS, REGION_WEIGHTS, TARGETS, ConfigOverlay, target_for


ALL_STRATEGIES = (
    "FixedStaging",
    "StaticCanary5xxP99",
    "ShadowDiffOnly",
    "CanaryNoCausal",
    "AgenticCanary",
    "AgenticCanary-NoBlastGraph",
    "AgenticCanary-NoShadowProbe",
    "AgenticCanary-NoCausalDiD",
    "AgenticCanary-NoShiftLeft",
)

_WINDOW_MIN = (2.0, 4.0, 7.0, 10.0, 14.0)
_TRAFFIC_SPLIT = (.02, .05, .15, .35, .60)
_LOAD_ACTIVATION = (.00, .20, .60, 1.00, 1.00)
_STAGING_SUITE_MIN = 12.0
_ROLLBACK_MIN = 3.0
_MANUAL_RECOVERY_MIN = 55.0
_UNIT_THRESHOLDS = (0.018, 37.0, 0.07, 0.05)
_PROBE_BUDGET = 19
_EXPLORATION_BUDGET = 5
_SHADOW_BUDGET = 24


class Signals(NamedTuple):
    error_rate: float
    p99_ms: float
    synthetic_mismatch_rate: float
    shadow_mismatch_rate: float

    def difference(self, other: "Signals") -> "Signals":
        return Signals(*(value - control for value, control in zip(self, other)))


_NO_EFFECT = Signals(0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class _Cohort:
    signals: Signals
    requests: float
    overlay: ConfigOverlay
    config_sample_fraction: float = 0.0

    def measured(self, contract: Optional[ConfigOverlay]) -> Signals:
        if contract is None or self.overlay == contract:
            return self.signals
        mismatches = _bound_rate(self.signals.synthetic_mismatch_rate + self.config_sample_fraction)
        return self.signals._replace(synthetic_mismatch_rate=mismatches)


@dataclass(frozen=True)
class _Observation:
    stable_before: _Cohort
    candidate_before: _Cohort
    stable_after: _Cohort
    candidate_after: _Cohort

    def change(self, causal: bool, contract: Optional[ConfigOverlay]) -> Signals:
        observed = self.candidate_after.measured(contract).difference(
            self.candidate_before.measured(contract)
        )
        if causal:
            control = self.stable_after.measured(contract).difference(
                self.stable_before.measured(contract)
            )
            return observed.difference(control)
        return observed


@dataclass(frozen=True)
class _Quote:
    subtotal_cents: int = 2200
    tax_cents: int = 185
    total_cents: int = 2385

    @property
    def valid(self) -> bool:
        return self.total_cents == self.subtotal_cents + self.tax_cents


@dataclass(frozen=True)
class _Evidence:
    target: Target
    channel: int
    contract: Optional[ConfigOverlay]


@dataclass(frozen=True)
class _Decision:
    outcome: str
    probe_cost: float
    detection_min: Optional[float] = None
    evidence: Optional[_Evidence] = None
    rule_source: Optional[int] = None


@dataclass(frozen=True)
class _Rule:
    evidence: _Evidence
    source_release_id: int


class _ReleaseEnvironment:
    """Own hidden injections and expose only cohort observations and live replays."""

    def __init__(self, scenario: Scenario, seed: int, config: SimConfig) -> None:
        self._scenario = scenario
        self._seed = seed
        self._config = config
        self._fault = None if config.aa_mode else scenario.fault
        self._observations: Dict[Tuple[int, Target], _Observation] = {}
        self._without_injection: Dict[Tuple[int, Target], _Observation] = {}
        # Even matched strata can have residual cohort imbalance during rollout.
        routing = Random(f"acv-routing-{seed}-{scenario.change.release_id}")
        self._skew = (
            routing.choice(REGIONS), routing.randrange(len(_WINDOW_MIN)),
            routing.uniform(.48, .86) if routing.random() < .10 else 0.0,
        )

    def _effect(self, target: Target, load: float) -> Signals:
        fault = self._fault
        if fault is None or target not in fault.targets:
            return _NO_EFFECT
        magnitude = fault.magnitude
        if fault.kind == FaultKind.SEMANTIC:
            quote = replace(_Quote(), total_cents=2200)
            mismatches = .21 * magnitude if not quote.valid else 0.0
            synthetic = mismatches if fault.fixture == "standard" else 0.0
            return Signals(0.0, 0.0, synthetic, mismatches)
        if fault.kind == FaultKind.CONFIG_DRIFT:
            return Signals(.010 * magnitude, 13 * magnitude, 0.0, .24 * magnitude)
        if fault.kind == FaultKind.TAIL_LATENCY:
            return Signals(0.0, 112 * magnitude * load, 0.0, 0.0)
        return Signals(.064 * magnitude * load, 108 * magnitude * load, 0.0, 0.0)

    def _candidate_overlay(self, target: Target) -> ConfigOverlay:
        expected = CONFIG_OVERLAYS[target]
        fault = self._fault
        if fault is None or fault.kind != FaultKind.CONFIG_DRIFT or target not in fault.targets:
            return expected
        if expected.flag_enabled:
            return replace(expected, flag_enabled=False)
        return replace(expected, currency="XXX")

    def staging(self, change: ReleaseChange) -> bool:
        """Run the fixed quote fixtures on the isolated primary-region stack."""
        fault = self._fault
        if fault is None or fault.kind != FaultKind.SEMANTIC:
            return False
        staging_route = target_for(change.service, "us-east")
        # Staging directly invokes the changed code with its standard and edge fixtures.
        # Production routing, traffic, regional overlays, and dependencies are absent.
        if fault.fixture not in ("standard", "edge") or staging_route.service != fault.targets[0].service:
            return False
        return not replace(_Quote(), total_cents=2200).valid

    def replay(self, evidence: _Evidence) -> bool:
        """Compare the retained fixture against the candidate in an isolated replay."""
        effect = self._effect(evidence.target, load=1.0)
        overlay = self._candidate_overlay(evidence.target)
        config_sample_fraction = (effect.shadow_mismatch_rate
                                  if overlay != CONFIG_OVERLAYS[evidence.target] else 0.0)
        sample = _Cohort(effect, 1.0, overlay, config_sample_fraction)
        return sample.measured(evidence.contract)[evidence.channel] >= _UNIT_THRESHOLDS[evidence.channel]

    def observe(self, window: int, target: Target) -> _Observation:
        key = (window, target)
        cached = self._observations.get(key)
        if cached is not None:
            return cached

        scenario = self._scenario
        rng = Random(f"acv-cohort-{self._seed}-{scenario.change.release_id}-{window}-{target}")
        overlay = CONFIG_OVERLAYS[target]
        shock = self._config.shock_scale * sum(
            event.intensity for event in scenario.shocks
            if event.region == target.region
            and event.start_window <= window < event.start_window + event.duration
        )
        initial_load = rng.uniform(0.96, 1.04)
        current_load = initial_load + shock * 0.65 + rng.uniform(-.025, .025)
        initial_p99 = overlay.timeout_ms * .51 + rng.uniform(-8.0, 8.0)
        change = self._effect(target, _LOAD_ACTIVATION[window])
        volume = 5200 * REGION_WEIGHTS[target.region]
        split = _TRAFFIC_SPLIT[window]

        def cohort(load: float, spike: float, share: float) -> _Cohort:
            requests = volume * load * share
            noise = (max(requests, 16.0) / 50.0) ** -0.35
            errors = .003 + .028 * spike + rng.uniform(-.002, .002) * noise
            latency = initial_p99 + 64.0 * spike + rng.uniform(-7.0, 7.0) * noise
            synthetic = .004 + .10 * spike + rng.uniform(-.005, .005) * noise
            shadow = .007 + .067 * spike + rng.uniform(-.006, .006) * noise
            return _Cohort(Signals(
                _bound_rate(errors), latency, _bound_rate(synthetic), _bound_rate(shadow),
            ), requests, overlay)

        skew_region, skew_window, skew = self._skew
        routing_spike = skew if target.region == skew_region and window == skew_window else 0.0
        unfaulted = _Observation(
            cohort(initial_load, 0.0, 1.0 - split), cohort(initial_load, 0.0, split),
            cohort(current_load, shock, 1.0 - split),
            cohort(current_load, shock + routing_spike, split),
        )
        observation = unfaulted
        if change != _NO_EFFECT:
            resolved = self._candidate_overlay(target)
            nominal = unfaulted.candidate_after.signals
            injected = Signals(
                _bound_rate(nominal.error_rate + change.error_rate),
                nominal.p99_ms + change.p99_ms,
                _bound_rate(nominal.synthetic_mismatch_rate + change.synthetic_mismatch_rate),
                _bound_rate(nominal.shadow_mismatch_rate + change.shadow_mismatch_rate),
            )
            observation = replace(
                unfaulted,
                candidate_after=replace(
                    unfaulted.candidate_after, signals=injected, overlay=resolved,
                    config_sample_fraction=change.shadow_mismatch_rate if resolved != overlay else 0.0,
                ),
            )
        self._without_injection[key] = unfaulted
        self._observations[key] = observation
        return observation

    def attributable(self, strategy: str, decision: _Decision) -> bool:
        if self._fault is None:
            return False
        if decision.outcome in ("staging_block", "shift_left_block"):
            return True
        if decision.outcome != "rollback":
            return False
        window = _WINDOW_MIN.index(decision.detection_min)
        evidence = decision.evidence
        if evidence is None:
            unaffected = (self._without_injection[window, target] for target in TARGETS)
            return not _static_breach(unaffected)
        unaffected = self._without_injection[window, evidence.target]
        score = unaffected.change(_uses_did(strategy), evidence.contract)[evidence.channel]
        return score <= _UNIT_THRESHOLDS[evidence.channel]


_Observe = Callable[[int, Target], _Observation]
_Replay = Callable[[_Evidence], bool]


def _bound_rate(value: float) -> float:
    return min(1.0, max(0.0, value))


def _uses_did(strategy: str) -> bool:
    return strategy not in ("CanaryNoCausal", "AgenticCanary-NoCausalDiD", "ShadowDiffOnly")


def _global_health(cohorts: Iterable[_Cohort]) -> Tuple[float, float]:
    """Pool request-weighted latency buckets before computing the percentile."""
    total = 0.0
    errors = 0.0
    buckets = []
    for cohort in cohorts:
        count = cohort.requests
        p99 = cohort.signals.p99_ms
        total += count
        errors += count * cohort.signals.error_rate
        buckets.extend(((p99 * .62, count * .965), (p99, count * .030),
                        (p99 * 1.55, count * .005)))
    threshold = total * .99
    cumulative = 0.0
    for latency, count in sorted(buckets):
        cumulative += count
        if cumulative >= threshold:
            return errors / total, latency
    raise ValueError("global health requires nonempty telemetry")


def _static_breach(observations: Iterable[_Observation]) -> bool:
    telemetry = tuple(observations)
    before = _global_health(item.candidate_before for item in telemetry)
    after = _global_health(item.candidate_after for item in telemetry)
    return after[0] - before[0] >= .008 or after[1] - before[1] >= 34.0


def _static_canary(observe: _Observe) -> _Decision:
    for window, minute in enumerate(_WINDOW_MIN):
        if _static_breach(observe(window, target) for target in TARGETS):
            return _Decision("rollback", 0.0, minute)
    return _Decision("deploy", 0.0)


def _rotate(targets: Tuple[Target, ...], start: int, size: int) -> Tuple[Target, ...]:
    if len(targets) <= size:
        return targets
    start %= len(targets)
    return (targets[start:] + targets[:start])[:size]


def _probe_canary(
    strategy: str, change: ReleaseChange, seed: int, observe: _Observe
) -> _Decision:
    causal = _uses_did(strategy)
    shadow_only = strategy == "ShadowDiffOnly"
    use_shadow = strategy != "AgenticCanary-NoShadowProbe"
    use_graph = strategy != "AgenticCanary-NoBlastGraph" and not shadow_only
    # Compile expected per-region flag and configuration from the pre-deploy
    # repository snapshot. Runtime discovery can add contracts for fallback probes.
    graph = change.observed_blast_graph if use_graph else ()
    contracts = {target: CONFIG_OVERLAYS[target] for target in graph}
    primary = tuple(target for target in graph if target.service == change.service)
    callers = tuple(target for target in graph if target.service != change.service)
    ordering = list(TARGETS)
    Random(f"acv-probes-{seed}-{change.release_id}").shuffle(ordering)
    broad = tuple(ordering)
    probe_cost = 0.0

    for window, minute in enumerate(_WINDOW_MIN):
        if use_graph:
            available = _PROBE_BUDGET - _EXPLORATION_BUDGET - len(primary)
            targeted = primary + _rotate(callers, window * available, available)
            fallback = tuple(target for target in broad if target not in targeted)
            spare = _PROBE_BUDGET - len(targeted)
            targets = targeted + _rotate(fallback, window * _EXPLORATION_BUDGET, spare)
        else:
            budget = _SHADOW_BUDGET if shadow_only else _PROBE_BUDGET
            targets = _rotate(broad, window * budget, budget)

        probe_cost += len(targets) * (0.25 if shadow_only else 1.0 + .25 * use_shadow)
        strongest = None
        strength = 1.0
        channels = (3,) if shadow_only else range(4 if use_shadow else 3)
        for target in targets:
            contract = None if shadow_only else contracts.setdefault(target, CONFIG_OVERLAYS[target])
            sample = observe(window, target)
            delta = sample.change(causal, contract)
            for channel in channels:
                if channel < 2 and sample.candidate_after.requests < 24:
                    continue
                score = delta[channel] / _UNIT_THRESHOLDS[channel]
                if score > strength:
                    strongest = _Evidence(target, channel, contract)
                    strength = score
        if strongest is not None:
            return _Decision("rollback", probe_cost, minute, strongest)
    return _Decision("deploy", probe_cost)


class _VerificationPolicy:
    def __init__(self, strategy: str) -> None:
        self.strategy = strategy
        self._uses_memory = strategy.startswith("AgenticCanary") and strategy != "AgenticCanary-NoShiftLeft"
        self._memory: Dict[Tuple[str, str], List[_Rule]] = {}

    def decide(
        self, change: ReleaseChange, seed: int, observe: _Observe, replay: _Replay,
        staging: Callable[[ReleaseChange], bool],
    ) -> Tuple[_Decision, bool]:
        if self.strategy == "FixedStaging":
            outcome = "staging_block" if staging(change) else "deploy"
            return _Decision(outcome, 18.0), False
        if self.strategy == "StaticCanary5xxP99":
            return _static_canary(observe), False

        signature = (change.service, change.operation)
        replay_cost = 0.0
        for rule in self._memory.get(signature, ()):
            replay_cost += 1.0
            if replay(rule.evidence):
                return _Decision("shift_left_block", replay_cost,
                                 rule_source=rule.source_release_id), False

        decision = _probe_canary(self.strategy, change, seed, observe)
        evidence = decision.evidence
        mined = False
        if self._uses_memory and decision.outcome == "rollback" and evidence is not None:
            replay_cost += 1.0
            if replay(evidence):
                self._memory.setdefault(signature, []).append(_Rule(evidence, change.release_id))
                mined = True
        return replace(decision, probe_cost=decision.probe_cost + replay_cost), mined


def _score_release(
    scenario: Scenario, strategy: str, decision: _Decision, mined: bool,
    detected: bool, aa_mode: bool,
) -> ReleaseResult:
    fault = None if aa_mode else scenario.fault
    rollout_min = 0.0
    detection_min = decision.detection_min
    recovery_min = None
    if strategy == "FixedStaging":
        rollout_min = scenario.staging_queue_min + _STAGING_SUITE_MIN
    if decision.outcome == "deploy":
        rollout_min += _WINDOW_MIN[-1]
        if fault is not None:
            recovery_min = _MANUAL_RECOVERY_MIN
    elif decision.outcome == "rollback":
        rollout_min += detection_min + _ROLLBACK_MIN
        if fault is not None:
            recovery_min = rollout_min
    elif decision.outcome == "shift_left_block":
        rollout_min = decision.probe_cost * .25
    return ReleaseResult(
        scenario.change.release_id, None if fault is None else fault.kind, decision.outcome,
        detected, detection_min if detected else None, recovery_min,
        rollout_min, decision.probe_cost, decision.rule_source, mined,
    )


def _simulate(
    strategies: Tuple[str, ...], seed: int, config: SimConfig,
    scenarios: Optional[Iterable[Scenario]],
) -> Dict[str, List[ReleaseResult]]:
    unknown = set(strategies).difference(ALL_STRATEGIES)
    if unknown:
        raise ValueError(f"unknown strategy: {sorted(unknown)[0]}")
    releases = generate_scenarios(seed, config) if scenarios is None else scenarios
    policies = {name: _VerificationPolicy(name) for name in strategies}
    histories: Dict[str, List[ReleaseResult]] = {name: [] for name in strategies}
    previous_id = None
    for scenario in releases:
        change = scenario.change
        if previous_id is not None and change.release_id <= previous_id:
            raise ValueError("scenarios must have strictly increasing release IDs")
        previous_id = change.release_id
        environment = _ReleaseEnvironment(scenario, seed, config)
        for name, policy in policies.items():
            decision, mined = policy.decide(
                change, seed, environment.observe, environment.replay, environment.staging,
            )
            detected = environment.attributable(name, decision)
            histories[name].append(
                _score_release(scenario, name, decision, mined, detected, config.aa_mode)
            )
    return histories


def _summarize(strategy: str, seed: int, releases: List[ReleaseResult]) -> RunMetrics:
    faults = [release for release in releases if release.fault_kind is not None]
    clean = [release for release in releases if release.fault_kind is None]

    def recall(group: List[ReleaseResult]) -> float:
        return sum(release.fault_detected for release in group) / len(group) if group else 0.0

    def mean(values: Iterable[Optional[float]]) -> float:
        present = [value for value in values if value is not None]
        return fmean(present) if present else 0.0

    return RunMetrics(
        strategy=strategy,
        seed=seed,
        fault_detection_rate=recall(faults),
        semantic_bug_recall=recall([r for r in faults if r.fault_kind == FaultKind.SEMANTIC]),
        config_drift_recall=recall([r for r in faults if r.fault_kind == FaultKind.CONFIG_DRIFT]),
        false_rollback_rate=sum(r.outcome != "deploy" for r in clean) / len(clean) if clean else 0.0,
        mttd_min=mean(r.production_detection_min for r in faults),
        mttr_min=mean(r.production_recovery_min for r in faults),
        release_cycle_min=mean(r.release_cycle_min for r in releases),
        shift_left_prevented_count=sum(r.outcome == "shift_left_block" for r in faults),
        probe_cost_units=sum(r.probe_cost_units for r in releases),
        escaped_outages=sum(r.outcome == "deploy" for r in faults),
    )


def simulate_releases(
    strategy: str, seed: int, config: Optional[SimConfig] = None, *,
    scenarios: Optional[Iterable[Scenario]] = None,
) -> Tuple[ReleaseResult, ...]:
    """Replay a full stream or chronological subset with fresh rule memory."""
    config = SimConfig() if config is None else config
    return tuple(_simulate((strategy,), seed, config, scenarios)[strategy])


def run_strategy(
    strategy: str, seed: int, config: Optional[SimConfig] = None, *,
    scenarios: Optional[Iterable[Scenario]] = None,
) -> RunMetrics:
    """Return aggregate metrics for one strategy and seed."""
    config = SimConfig() if config is None else config
    history = _simulate((strategy,), seed, config, scenarios)[strategy]
    return _summarize(strategy, seed, history)


def run_all(
    seed: int, config: Optional[SimConfig] = None, *,
    scenarios: Optional[Iterable[Scenario]] = None,
) -> Tuple[RunMetrics, ...]:
    """Run all strategies against the same release stream and cohort samples."""
    config = SimConfig() if config is None else config
    histories = _simulate(ALL_STRATEGIES, seed, config, scenarios)
    return tuple(_summarize(name, seed, histories[name]) for name in ALL_STRATEGIES)
