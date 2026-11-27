import unittest
from collections import Counter
from dataclasses import replace

from src.acv import (
    ALL_STRATEGIES, Fault, FaultKind, Scenario, SimConfig,
    generate_scenarios, run_all, run_strategy, simulate_releases,
)
from src.acv.models import LoadShock, ReleaseChange
from src.acv.topology import CONFIG_OVERLAYS, DEPENDENCIES, REGIONS, SERVICES, TARGETS, impact_services, target_for


SEED = 42
AGENTIC = "AgenticCanary"


class TopologyTests(unittest.TestCase):
    def test_service_graph_and_regional_overlays(self):
        self.assertEqual(set(SERVICES), {
            "frontend", "gateway", "cart", "checkout", "payment", "currency",
            "shipping", "inventory", "pricing", "auth", "recommendation", "ledger",
        })
        self.assertEqual(set(REGIONS), {"us-east", "us-west", "eu-central", "ap-south", "ap-east", "latam"})
        self.assertEqual(len(set(TARGETS)), 72)
        self.assertEqual(set(TARGETS), CONFIG_OVERLAYS.keys())

        def visit(service, ancestors):
            self.assertNotIn(service, ancestors, "the dependency graph must be acyclic")
            for dependency in DEPENDENCIES[service]:
                visit(dependency, ancestors | {service})

        for service in SERVICES:
            visit(service, set())
            overlays = [CONFIG_OVERLAYS[target_for(service, region)] for region in REGIONS]
            self.assertGreater(len({item.flag_enabled for item in overlays}), 1)
            self.assertGreater(len({item.currency for item in overlays}), 1)
        self.assertEqual(impact_services("frontend"), ("frontend",))
        self.assertIn("frontend", impact_services("ledger"))
        self.assertNotIn("auth", impact_services("ledger"))


class ScenarioTests(unittest.TestCase):
    def test_seeded_workload_has_requested_mix(self):
        scenarios = generate_scenarios(SEED)
        self.assertEqual(scenarios, generate_scenarios(SEED))
        self.assertNotEqual(scenarios, generate_scenarios(SEED + 1))
        self.assertEqual([item.change.release_id for item in scenarios], list(range(1, 81)))
        self.assertEqual(Counter(item.fault and item.fault.kind for item in scenarios), {
            None: 20, FaultKind.SEMANTIC: 15, FaultKind.CONFIG_DRIFT: 20,
            FaultKind.TAIL_LATENCY: 15, FaultKind.BROWNOUT: 10,
        })
        clean = [item for item in scenarios if item.fault is None]
        self.assertTrue(any(item.shocks for item in clean))
        self.assertTrue(any(not item.shocks for item in clean))
        self.assertGreater(len({shock.region for item in clean for shock in item.shocks}), 1)
        recurring = Counter(item.fault.recurrence_key for item in scenarios
                            if item.fault is not None and item.fault.recurrence_key is not None)
        self.assertEqual(sum(recurring.values()), 15)
        self.assertTrue(all(count >= 2 for count in recurring.values()))
        for scenario in scenarios:
            if scenario.fault is not None and scenario.fault.kind == FaultKind.CONFIG_DRIFT:
                regions = {target.region for target in scenario.fault.targets}
                self.assertTrue(1 <= len(regions) <= 2)
                self.assertNotIn("us-east", regions)

    def test_graph_recall_hides_actual_cones_without_changing_the_workload(self):
        for recall, hidden_count in ((1, 0), (.85, 9), (.5, 30), (0, 60)):
            with self.subTest(recall=recall):
                full = generate_scenarios(SEED, SimConfig(graph_recall=1))
                partial = generate_scenarios(SEED, SimConfig(graph_recall=recall))
                hidden = 0
                for complete, incomplete in zip(full, partial):
                    self.assertEqual(complete, replace(incomplete, change=complete.change))
                    observed = set(incomplete.change.observed_blast_graph)
                    self.assertLessEqual(observed, set(complete.change.observed_blast_graph))
                    if incomplete.fault is not None:
                        targets = set(incomplete.fault.targets)
                        if observed.isdisjoint(targets):
                            hidden += 1
                        else:
                            self.assertLessEqual(targets, observed)
                self.assertEqual(hidden, hidden_count)

    def test_invalid_sensitivity_configuration(self):
        for options in ({"graph_recall": -.1}, {"graph_recall": 1.1},
                        {"shock_scale": -1}, {"shock_scale": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                SimConfig(**options)


class SimulatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = {metric.strategy: metric for metric in run_all(SEED)}

    def test_all_strategies_are_deterministic_and_use_paired_scenarios(self):
        self.assertEqual(tuple(self.metrics), ALL_STRATEGIES)
        self.assertEqual(tuple(self.metrics.values()), run_all(SEED))
        paired = generate_scenarios(SEED)
        for strategy, metrics in self.metrics.items():
            with self.subTest(strategy=strategy):
                self.assertEqual(metrics, run_strategy(strategy, SEED, scenarios=iter(paired)))
                history = simulate_releases(strategy, SEED, scenarios=paired[:6])
                self.assertEqual(
                    [(row.release_id, row.fault_kind) for row in history],
                    [(item.change.release_id, item.fault and item.fault.kind) for item in paired[:6]],
                )
                self.assertEqual(metrics.to_dict()["seed"], SEED)
                self.assertTrue(0 <= metrics.fault_detection_rate <= 1)
                self.assertTrue(0 <= metrics.false_rollback_rate <= 1)
                self.assertLessEqual(metrics.mttd_min, metrics.mttr_min)
                self.assertTrue(0 <= metrics.escaped_outages <= 60)

    def test_imperfect_graph_affects_agentic_probing_and_fallback_still_works(self):
        full = SimConfig(graph_recall=1)
        partial = SimConfig(graph_recall=.5)
        complete = run_strategy(AGENTIC, SEED, full)
        incomplete = run_strategy(AGENTIC, SEED, partial)
        self.assertGreater(incomplete.fault_detection_rate, 0)
        self.assertGreater(complete.fault_detection_rate, incomplete.fault_detection_rate)
        self.assertLess(complete.mttd_min, incomplete.mttd_min)
        no_graph = "AgenticCanary-NoBlastGraph"
        self.assertEqual(run_strategy(no_graph, SEED, full), run_strategy(no_graph, SEED, partial))

    def test_rule_memory_only_blocks_future_reproductions_after_rollback(self):
        target = target_for("checkout", "eu-central")
        graph = tuple(target_for(service, region)
                      for service in impact_services("checkout") for region in REGIONS)
        release = Scenario(
            ReleaseChange(1, "checkout", "write", graph),
            Fault(FaultKind.SEMANTIC, (target,), "edge", 1.0, "recurrence"),
            (), 16,
        )
        benign = replace(release, change=replace(release.change, release_id=2), fault=None)
        recurrence = replace(release, change=replace(release.change, release_id=3))
        stream = (release, benign, recurrence)
        first, middle, last = simulate_releases(AGENTIC, SEED, scenarios=stream)
        self.assertEqual(first.outcome, "rollback")
        self.assertTrue(first.mined_rule)
        self.assertIsNone(first.rule_source_release_id)
        self.assertEqual(middle.outcome, "deploy")
        self.assertEqual(last.outcome, "shift_left_block")
        self.assertEqual(last.rule_source_release_id, first.release_id)
        self.assertIsNone(last.production_recovery_min)
        self.assertEqual(simulate_releases(AGENTIC, SEED, scenarios=stream[:2]), (first, middle))
        later_alone, = simulate_releases(AGENTIC, SEED, scenarios=(recurrence,))
        self.assertEqual(later_alone.outcome, "rollback")
        self.assertIsNone(later_alone.rule_source_release_id)
        ablated = simulate_releases("AgenticCanary-NoShiftLeft", SEED, scenarios=stream)
        self.assertEqual(ablated[-1].outcome, "rollback")

    def test_learned_rules_trace_to_earlier_rollbacks_in_generated_stream(self):
        releases = simulate_releases(AGENTIC, SEED)
        by_id = {result.release_id: result for result in releases}
        blocked = [result for result in releases if result.outcome == "shift_left_block"]
        self.assertTrue(blocked)
        self.assertEqual(self.metrics[AGENTIC].shift_left_prevented_count, len(blocked))
        for result in blocked:
            source = by_id[result.rule_source_release_id]
            self.assertLess(source.release_id, result.release_id)
            self.assertEqual(source.outcome, "rollback")
            self.assertTrue(source.mined_rule)
        self.assertEqual(releases[:40], simulate_releases(
            AGENTIC, SEED, scenarios=generate_scenarios(SEED)[:40],
        ))

    def test_semantic_fault_does_not_get_credit_for_a_traffic_shock(self):
        scenario = next(s for s in generate_scenarios(SEED)
                        if s.fault is not None and s.fault.kind == FaultKind.SEMANTIC)
        scenario = replace(scenario, shocks=tuple(LoadShock(r, 0, 3, 1.3) for r in REGIONS))
        result, = simulate_releases("StaticCanary5xxP99", SEED, scenarios=(scenario,))
        self.assertEqual(result.outcome, "rollback")
        self.assertFalse(result.fault_detected)
        self.assertIsNone(result.production_detection_min)

    def test_aa_has_no_injected_incidents_and_preserves_background_noise(self):
        paired = generate_scenarios(SEED)
        aa = run_all(SEED, SimConfig(aa_mode=True), scenarios=paired)
        for result in aa:
            self.assertEqual(result.fault_detection_rate, 0)
            self.assertEqual(result.shift_left_prevented_count, 0)
            self.assertEqual(result.escaped_outages, 0)
            self.assertEqual(result.mttr_min, 0)
        results = {result.strategy: result for result in aa}
        self.assertGreater(results["CanaryNoCausal"].false_rollback_rate, 0)
        self.assertLess(results[AGENTIC].false_rollback_rate, results["CanaryNoCausal"].false_rollback_rate)

    def test_requested_baseline_comparisons_and_ablations(self):
        agentic = self.metrics[AGENTIC]
        staging = self.metrics["FixedStaging"]
        self.assertGreater(agentic.config_drift_recall, staging.config_drift_recall)
        self.assertLess(agentic.release_cycle_min, staging.release_cycle_min)
        self.assertGreaterEqual(staging.semantic_bug_recall, .70)
        self.assertGreater(agentic.semantic_bug_recall, self.metrics["StaticCanary5xxP99"].semantic_bug_recall)
        self.assertLess(agentic.false_rollback_rate, self.metrics["CanaryNoCausal"].false_rollback_rate)
        self.assertLess(agentic.false_rollback_rate, self.metrics["AgenticCanary-NoCausalDiD"].false_rollback_rate)
        self.assertGreater(agentic.semantic_bug_recall,
                           self.metrics["AgenticCanary-NoShadowProbe"].semantic_bug_recall)
        self.assertLess(agentic.probe_cost_units, self.metrics["AgenticCanary-NoBlastGraph"].probe_cost_units)

    def test_invalid_simulation_input(self):
        with self.assertRaisesRegex(ValueError, "unknown strategy"):
            run_strategy("unknown", SEED)
        with self.assertRaisesRegex(ValueError, "increasing release IDs"):
            run_strategy(AGENTIC, SEED, scenarios=reversed(generate_scenarios(SEED)[:2]))


if __name__ == "__main__":
    unittest.main()
