"""Offline tests for pharmer/scoring.py - confidence gate, Materiality x Obscurity, ranking and
tie-breaks, the diversity rule, the label guarantee, missing upstream data, end to end on a
hand-made findings.json (Fact Checker / Quant fields filled by hand; numbers illustrative).

Run: .venv/bin/python -m unittest tests.test_scoring
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pharmer import scoring

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "scoring_findings.json"

_UNSET = object()


def reasoning(hunter="channel_checker", confidence="Medium", eps_pct=1.0, hits=0,
              impact=_UNSET, priced_in=_UNSET):
    return {
        "reasoning_id": None, "hunter": hunter, "hunters": [hunter], "claim": "Illustrative.",
        "citations": [], "confidence": confidence, "confidence_reason": None, "flags": [],
        "trend": [],
        "impact": {"eps_pct": eps_pct} if impact is _UNSET else impact,
        "priced_in": {"hits": hits, "evidence": [], "stock_move_pct": None}
        if priced_in is _UNSET else priced_in,
        "score": None,
    }


def finding(*reasonings, label=False, peer=None):
    return {"finding_id": None, "is_label_finding": label, "contested": False,
            "lead_reasoning_id": None, "reasonings": list(reasonings),
            "peer": {"status": peer, "note": ""}, "status": "candidate", "recommended": False}


def number(findings):
    for n, f in enumerate(findings, 1):
        f["finding_id"] = f"F-{n:03d}"
        for i, r in enumerate(f["reasonings"]):
            r["reasoning_id"] = f"F-{n:03d}-{chr(97 + i)}"
        f["lead_reasoning_id"] = f["reasonings"][0]["reasoning_id"]
    return findings


def selected_ids(findings):
    return [r["reasoning_id"] for f in findings for r in f["reasonings"] if r["selected"]]


def recommended(findings):
    return [f["finding_id"] for f in findings if f["recommended"]]


class TestScore(unittest.TestCase):
    def test_materiality_is_abs_eps_pct(self):
        self.assertEqual(scoring.materiality(reasoning(eps_pct=-1.8)), 1.8)

    def test_obscurity_mapping(self):
        self.assertEqual([scoring.obscurity(reasoning(hits=h)) for h in (0, 1, 3)],
                         [1.0, 0.5, 0.25])
        self.assertAlmostEqual(scoring.obscurity(reasoning(hits=2)), 1 / 3)

    def test_score_is_materiality_times_obscurity(self):
        self.assertEqual(scoring.score(reasoning(eps_pct=-3.0, hits=1)), 1.5)

    def test_gate_low_and_null_not_scored(self):
        for confidence in ("Low", None, "medium"):
            self.assertIsNone(scoring.score(reasoning(confidence=confidence, eps_pct=9.0)))
            self.assertFalse(scoring.eligible(reasoning(confidence=confidence)))

    def test_missing_impact_scores_null(self):
        for r in (reasoning(impact=None), reasoning(eps_pct=None), reasoning(eps_pct="1.2"),
                  reasoning(eps_pct=True), reasoning(eps_pct=float("nan")), reasoning(impact={})):
            self.assertIsNone(scoring.score(r))
            self.assertTrue(scoring.eligible(r))

    def test_missing_priced_in_counts_as_obscure(self):
        for r in (reasoning(priced_in=None), reasoning(hits=None), reasoning(hits=-1),
                  reasoning(hits=True)):
            self.assertEqual(scoring.obscurity(r), 1.0)
        self.assertEqual(scoring.score(reasoning(eps_pct=2.0, priced_in=None)), 2.0)

    def test_integral_float_hits_counted(self):
        self.assertEqual(scoring.obscurity(reasoning(hits=1.0)), 0.5)
        with self.assertLogs("pharmer.scoring", "WARNING"):
            self.assertEqual(scoring.obscurity(reasoning(hits=1.5)), 1.0)


class TestSelection(unittest.TestCase):
    def test_gate_drops_low_and_null(self):
        findings = number([finding(reasoning(confidence="Low", eps_pct=9.0)),
                           finding(reasoning(confidence=None, eps_pct=9.0)),
                           finding(reasoning(confidence="Medium", eps_pct=0.1))])
        scoring.apply(findings)
        self.assertEqual([f["status"] for f in findings], ["dropped", "dropped", "selected"])
        self.assertIsNone(findings[0]["reasonings"][0]["score"])
        self.assertEqual(recommended(findings), ["F-003"])

    def test_composite_ranked_by_strongest_reasoning(self):
        findings = number([
            finding(reasoning(hunter="redliner", eps_pct=2.0)),
            finding(reasoning(hunter="channel_checker", eps_pct=1.0),
                    reasoning(hunter="footnote_miner", confidence="High", eps_pct=3.0)),
        ])
        chosen = scoring.apply(findings)
        self.assertEqual(chosen[0][1]["reasoning_id"], "F-002-b")
        self.assertEqual(recommended(findings), ["F-002"])

    def test_siblings_both_take_slots(self):
        findings = number([finding(reasoning(hunter="redliner", eps_pct=5.0),
                                   reasoning(hunter="channel_checker", eps_pct=4.0)),
                           finding(reasoning(hunter="footnote_miner", eps_pct=1.0))])
        scoring.apply(findings)
        self.assertEqual(selected_ids(findings), ["F-001-a", "F-001-b", "F-002-a"])

    def test_diversity_cap_skips_third_from_one_hunter(self):
        findings = number([finding(reasoning(hunter="redliner", eps_pct=e)) for e in (9, 8, 7)]
                          + [finding(reasoning(hunter="footnote_miner", eps_pct=1.0))])
        scoring.apply(findings)
        self.assertEqual(selected_ids(findings), ["F-001-a", "F-002-a", "F-004-a"])
        self.assertEqual(findings[2]["status"], "verified")

    def test_cap_counts_each_reasoning_against_its_own_hunter(self):
        # Composite row led by redliner: its channel_checker sibling counts against channel_checker.
        findings = number([finding(reasoning(hunter="redliner", eps_pct=9.0),
                                   reasoning(hunter="channel_checker", eps_pct=8.0)),
                           finding(reasoning(hunter="redliner", eps_pct=7.0)),
                           finding(reasoning(hunter="redliner", eps_pct=6.0)),
                           finding(reasoning(hunter="channel_checker", eps_pct=5.0))])
        scoring.apply(findings)
        self.assertEqual(selected_ids(findings), ["F-001-a", "F-001-b", "F-002-a", "F-004-a"])

    def test_tie_break_confidence_then_obscurity_then_materiality(self):
        # All four score 1.0.
        findings = number([
            finding(reasoning(hunter="h1", confidence="Medium", eps_pct=1.0, hits=0)),
            finding(reasoning(hunter="h2", confidence="High", eps_pct=2.0, hits=1)),
            finding(reasoning(hunter="h3", confidence="High", eps_pct=1.0, hits=0)),
            finding(reasoning(hunter="h4", confidence="Medium", eps_pct=2.0, hits=1)),
        ])
        chosen = scoring.apply(findings)
        self.assertEqual([r["reasoning_id"] for _, r in chosen],
                         ["F-003-a", "F-002-a", "F-001-a", "F-004-a"])

    def test_label_swap_replaces_lowest_non_label(self):
        findings = number([finding(reasoning(hunter=f"h{i}", eps_pct=10 - i)) for i in range(6)]
                          + [finding(reasoning(hunter="label_lawyer", eps_pct=0.5), label=True)])
        scoring.apply(findings)
        self.assertEqual(selected_ids(findings),
                         ["F-001-a", "F-002-a", "F-003-a", "F-004-a", "F-007-a"])
        self.assertEqual(findings[4]["status"], "verified")

    def test_label_swap_takes_best_ranked_label(self):
        findings = number([finding(reasoning(hunter=f"h{i}", eps_pct=10 - i)) for i in range(6)]
                          + [finding(reasoning(hunter="label_lawyer", eps_pct=0.2), label=True),
                             finding(reasoning(hunter="label_lawyer", eps_pct=0.6), label=True)])
        scoring.apply(findings)
        self.assertEqual(selected_ids(findings),
                         ["F-001-a", "F-002-a", "F-003-a", "F-004-a", "F-008-a"])

    def test_label_fills_free_slot_when_cap_leaves_room(self):
        # The cap blocks the label unit in the greedy fill; the swap adds it without the cap check.
        findings = number([finding(reasoning(hunter="label_lawyer", eps_pct=e)) for e in (9, 8)]
                          + [finding(reasoning(hunter="h2", eps_pct=5.0)),
                             finding(reasoning(hunter="label_lawyer", eps_pct=0.1), label=True)])
        scoring.apply(findings)
        self.assertEqual(selected_ids(findings), ["F-001-a", "F-002-a", "F-003-a", "F-004-a"])

    def test_no_label_finding_warns(self):
        findings = number([finding(reasoning(eps_pct=1.0))])
        with self.assertLogs("pharmer.scoring", "WARNING") as logs:
            scoring.apply(findings)
        self.assertIn("no eligible label/product finding", logs.output[0])

    def test_fewer_than_five_eligible(self):
        findings = number([finding(reasoning(hunter="h1", eps_pct=1.0)),
                           finding(reasoning(hunter="h2", eps_pct=2.0)),
                           finding(reasoning(confidence="Low"))])
        scoring.apply(findings)
        self.assertEqual(selected_ids(findings), ["F-001-a", "F-002-a"])
        self.assertEqual(recommended(findings), ["F-002"])

    def test_exactly_one_recommended_of_five(self):
        findings = number([finding(reasoning(hunter=f"h{i}", eps_pct=i + 1.0)) for i in range(8)])
        scoring.apply(findings)
        self.assertEqual(len(selected_ids(findings)), 5)
        self.assertEqual(recommended(findings), ["F-008"])

    def test_nothing_eligible(self):
        findings = number([finding(reasoning(confidence=None)), finding(reasoning(confidence="Low"))])
        with self.assertLogs("pharmer.scoring", "WARNING"):
            self.assertEqual(scoring.apply(findings), [])
        self.assertEqual([f["status"] for f in findings], ["dropped", "dropped"])
        self.assertEqual(recommended(findings), [])

    def test_unscored_ranks_after_scored(self):
        findings = number([finding(reasoning(hunter="h1", confidence="High", impact=None)),
                           finding(reasoning(hunter="h2", eps_pct=0.01, hits=5))])
        chosen = scoring.apply(findings)
        self.assertEqual([r["reasoning_id"] for _, r in chosen], ["F-002-a", "F-001-a"])
        self.assertIsNone(findings[0]["reasonings"][0]["score"])

    def test_industry_wide_demoted_incomplete_not(self):
        findings = number([finding(reasoning(hunter="h1", eps_pct=9.0), peer="industry_wide"),
                           finding(reasoning(hunter="h2", eps_pct=5.0), peer="incomplete"),
                           finding(reasoning(hunter="h3", eps_pct=1.0), peer="distinctive")])
        chosen = scoring.apply(findings)
        self.assertEqual([f["finding_id"] for f, _ in chosen], ["F-002", "F-003", "F-001"])
        self.assertEqual(recommended(findings), ["F-002"])

    def test_missing_optional_fields_do_not_crash(self):
        bare = {"finding_id": "F-001", "reasonings": [{"reasoning_id": "F-001-a",
                                                       "confidence": "High"}]}
        scoring.apply([bare])
        self.assertEqual(bare["status"], "selected")
        self.assertTrue(bare["recommended"])
        self.assertIsNone(bare["reasonings"][0]["score"])

    def test_rerun_is_idempotent(self):
        findings = number([finding(reasoning(hunter=f"h{i}", eps_pct=i + 1.0)) for i in range(7)])
        scoring.apply(findings)
        first = json.dumps(findings)
        scoring.apply(findings)
        self.assertEqual(json.dumps(findings), first)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp()) / "NVO-20261018-0230"
        self.run_dir.mkdir()
        shutil.copy(FIXTURE, self.run_dir / "findings.json")
        self.addCleanup(shutil.rmtree, self.run_dir.parent)

    def test_run_on_fixture(self):
        asyncio.run(scoring.run(self.run_dir))
        findings = json.loads((self.run_dir / "findings.json").read_text(encoding="utf-8"))
        by_id = {f["finding_id"]: f for f in findings}
        self.assertEqual({k: f["status"] for k, f in by_id.items()},
                         {"F-001": "selected", "F-002": "dropped", "F-003": "dropped",
                          "F-004": "selected", "F-005": "selected", "F-006": "verified",
                          "F-007": "selected"})
        self.assertEqual(selected_ids(findings),
                         ["F-001-a", "F-001-b", "F-004-a", "F-005-a", "F-007-a"])
        self.assertEqual(recommended(findings), ["F-001"])
        self.assertEqual(by_id["F-001"]["reasonings"][1]["score"], 3.0)
        self.assertEqual(by_id["F-007"]["reasonings"][0]["score"], 0.1667)
        self.assertIsNone(by_id["F-006"]["reasonings"][0]["score"])


if __name__ == "__main__":
    unittest.main()
