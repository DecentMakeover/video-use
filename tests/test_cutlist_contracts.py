from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

import cutlist  # noqa: E402


DURATIONS = {"part1": 600.0, "part2": 300.0}


def make_cutlist(cuts=(), chapters=(), parts=None) -> dict:
    if parts is None:
        parts = [
            {"source": "part1", "file": "../part1.mp4"},
            {"source": "part2", "file": "../part2.mp4"},
        ]
    return {"version": 1, "parts": parts, "cuts": list(cuts), "chapters": list(chapters)}


class InvertCutTests(unittest.TestCase):
    def test_no_cuts_keeps_the_whole_source(self) -> None:
        self.assertEqual(cutlist.invert_cuts(100.0, []), [(0.0, 100.0)])

    def test_head_and_tail_cuts_leave_no_empty_segments(self) -> None:
        self.assertEqual(
            cutlist.invert_cuts(100.0, [(0.0, 10.0), (90.0, 100.0)]),
            [(10.0, 90.0)],
        )

    def test_adjacent_cuts_merge_before_inversion(self) -> None:
        self.assertEqual(
            cutlist.invert_cuts(100.0, [(20.0, 30.0), (10.0, 20.0)]),
            [(0.0, 10.0), (30.0, 100.0)],
        )

    def test_cutting_everything_yields_no_segments(self) -> None:
        self.assertEqual(cutlist.invert_cuts(100.0, [(0.0, 100.0)]), [])


class ValidationTests(unittest.TestCase):
    def test_schema_rejects_structural_problems(self) -> None:
        bad = make_cutlist(
            cuts=[
                {"source": "ghost", "start": 1.0, "end": 2.0},
                {"source": "part1", "start": 5.0, "end": 5.0},
                {"source": "part1", "start": 10.0, "end": 20.0},
                {"source": "part1", "start": 15.0, "end": 25.0},
            ],
            parts=[
                {"source": "part1", "file": "../part1.mp4"},
                {"source": "part1", "file": "../copy.mp4"},
            ],
        )
        errors = "\n".join(cutlist.schema_errors(bad))
        self.assertIn("unknown source", errors)
        self.assertIn("end must be after start", errors)
        self.assertIn("overlapping cuts", errors)
        self.assertIn("duplicate part source", errors)

    def test_chapter_inside_a_cut_is_an_error(self) -> None:
        bad = make_cutlist(
            cuts=[{"source": "part1", "start": 10.0, "end": 60.0}],
            chapters=[{"source": "part1", "at": 30.0, "title": "Lost"}],
        )
        errors = "\n".join(cutlist.schema_errors(bad))
        self.assertIn("falls inside the removed range", errors)

    def test_bounds_are_checked_against_probed_durations(self) -> None:
        bad = make_cutlist(
            cuts=[{"source": "part2", "start": 250.0, "end": 400.0}],
            chapters=[{"source": "part1", "at": 700.0, "title": "Beyond"}],
        )
        errors = "\n".join(cutlist.media_errors(bad, DURATIONS))
        self.assertIn("past the source duration", errors)
        self.assertIn("Beyond", errors)

    def test_compile_raises_on_invalid_cutlists(self) -> None:
        bad = make_cutlist(cuts=[{"source": "ghost", "start": 1.0, "end": 2.0}])
        with self.assertRaises(cutlist.CutlistError):
            cutlist.compile_cutlist(bad, DURATIONS)


class CompileTests(unittest.TestCase):
    def test_output_timeline_follows_confirmed_part_order(self) -> None:
        result = cutlist.compile_cutlist(
            make_cutlist(
                cuts=[
                    {"source": "part1", "start": 0.0, "end": 60.0},
                    {"source": "part2", "start": 100.0, "end": 150.0},
                ]
            ),
            DURATIONS,
        )
        self.assertEqual(
            [
                (s["source"], s["source_in"], s["source_out"], s["output_in"], s["output_out"])
                for s in result["segments"]
            ],
            [
                ("part1", 60.0, 600.0, 0.0, 540.0),
                ("part2", 0.0, 100.0, 540.0, 640.0),
                ("part2", 150.0, 300.0, 640.0, 790.0),
            ],
        )
        self.assertEqual(result["totals"]["input"], 900.0)
        self.assertEqual(result["totals"]["output"], 790.0)
        self.assertEqual(result["totals"]["removed"], 110.0)

    def test_edl_matches_the_render_contract(self) -> None:
        result = cutlist.compile_cutlist(
            make_cutlist(cuts=[{"source": "part1", "start": 0.0, "end": 60.0}]),
            DURATIONS,
        )
        edl = result["edl"]
        self.assertEqual(set(edl), {"sources", "ranges"})
        self.assertEqual(
            edl["sources"], {"part1": "../part1.mp4", "part2": "../part2.mp4"}
        )
        for r in edl["ranges"]:
            self.assertEqual(set(r), {"source", "start", "end"})
            self.assertIsInstance(r["start"], float)
            self.assertIsInstance(r["end"], float)
        self.assertLess(r["start"], r["end"])

    def test_fully_removed_part_is_dropped_from_the_edl(self) -> None:
        result = cutlist.compile_cutlist(
            make_cutlist(cuts=[{"source": "part1", "start": 0.0, "end": 600.0}]),
            DURATIONS,
        )
        self.assertNotIn("part1", result["edl"]["sources"])
        self.assertTrue(all(r["source"] != "part1" for r in result["edl"]["ranges"]))

    def test_chapters_map_into_output_time_across_parts(self) -> None:
        result = cutlist.compile_cutlist(
            make_cutlist(
                cuts=[
                    {"source": "part1", "start": 0.0, "end": 60.0},
                    {"source": "part2", "start": 100.0, "end": 150.0},
                ],
                chapters=[
                    {"source": "part1", "at": 120.0, "title": "Intro"},
                    {"source": "part2", "at": 200.0, "title": "Questions"},
                ],
            ),
            DURATIONS,
        )
        by_title = {c["title"]: c["output_at"] for c in result["chapters"]}
        self.assertEqual(by_title["Intro"], 60.0)
        self.assertEqual(by_title["Questions"], 690.0)

    def test_removed_time_is_totalled_by_category(self) -> None:
        result = cutlist.compile_cutlist(
            make_cutlist(
                cuts=[
                    {"source": "part1", "start": 0.0, "end": 30.0, "category": "logistics"},
                    {"source": "part1", "start": 50.0, "end": 60.0, "category": "logistics"},
                    {"source": "part2", "start": 10.0, "end": 15.0},
                ]
            ),
            DURATIONS,
        )
        self.assertEqual(
            result["totals"]["removed_by_category"],
            {"logistics": 40.0, "uncategorized": 5.0},
        )


class WarningTests(unittest.TestCase):
    def test_short_kept_segments_are_flagged_not_dropped(self) -> None:
        result = cutlist.compile_cutlist(
            make_cutlist(
                cuts=[
                    {"source": "part1", "start": 10.0, "end": 20.0},
                    {"source": "part1", "start": 20.5, "end": 30.0},
                ]
            ),
            DURATIONS,
        )
        self.assertTrue(any("only 0.50s" in w for w in result["warnings"]))
        self.assertIn(
            (20.0, 20.5),
            [(s["source_in"], s["source_out"]) for s in result["segments"]],
        )

    def test_cut_edges_inside_words_are_flagged(self) -> None:
        words = {
            "part1": [{"type": "word", "start": 9.8, "end": 10.4, "text": "hello"}]
        }
        result = cutlist.compile_cutlist(
            make_cutlist(cuts=[{"source": "part1", "start": 10.0, "end": 20.0}]),
            DURATIONS,
            words_by_source=words,
        )
        self.assertTrue(any("mid-word" in w and "hello" in w for w in result["warnings"]))

    def test_source_key_differing_from_file_stem_is_flagged(self) -> None:
        result = cutlist.compile_cutlist(
            make_cutlist(
                parts=[{"source": "part1", "file": "../Recording 7 (1).mp4"}]
            ),
            {"part1": 600.0},
        )
        self.assertTrue(any("file stem" in w for w in result["warnings"]))


class DocContractTests(unittest.TestCase):
    def test_example_cutlist_passes_schema_validation(self) -> None:
        example = json.loads(
            (ROOT / "workflows" / "cutlist.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(cutlist.schema_errors(example), [])

    def test_docs_reference_the_cutlist_helper(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        podcast_edit = (ROOT / "workflows" / "podcast-edit.md").read_text(encoding="utf-8")
        self.assertIn("cutlist.py", skill)
        self.assertIn("cutlist.py", podcast_edit)
        self.assertIn("cutlist.json", podcast_edit)


if __name__ == "__main__":
    unittest.main()
