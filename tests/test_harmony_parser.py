"""Tests for scripts/harmony_parser.py.

These use only Python's standard-library ``unittest`` module so the repository
needs no test dependency just to verify the parser.
"""

from __future__ import annotations

import sys
import unittest
from fractions import Fraction
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.harmony_parser import (  # noqa: E402
    HarmonyParseError,
    parse_chord,
    parse_harmony,
    parse_harmony_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ChordParsingTests(unittest.TestCase):
    def test_dominant_seventh_is_major_triad_plus_modifier(self) -> None:
        chord = parse_chord("57")
        self.assertEqual(chord.root, "5")
        self.assertEqual(chord.root_degree, 5)
        self.assertEqual(chord.root_alteration, 0)
        self.assertEqual(chord.quality, "major")
        self.assertEqual(chord.modifiers, ["7"])
        self.assertEqual(chord.bass, "5")
        self.assertFalse(chord.bass_explicit)

    def test_minor_seventh_preserves_minor_triad_quality(self) -> None:
        chord = parse_chord("6-7")
        self.assertEqual(chord.root, "6")
        self.assertEqual(chord.quality, "minor")
        self.assertEqual(chord.modifiers, ["7"])

    def test_altered_root_is_primary_root_identity(self) -> None:
        chord = parse_chord("b7")
        self.assertEqual(chord.root, "b7")
        self.assertEqual(chord.root_degree, 7)
        self.assertEqual(chord.root_alteration, -1)
        # Root-position bass is populated explicitly for easy bass-motion queries.
        self.assertEqual(chord.bass, "b7")
        self.assertEqual(chord.bass_degree, 7)
        self.assertEqual(chord.bass_alteration, -1)
        self.assertFalse(chord.bass_explicit)

    def test_slash_bass(self) -> None:
        chord = parse_chord("57/7")
        self.assertEqual(chord.root, "5")
        self.assertEqual(chord.modifiers, ["7"])
        self.assertEqual(chord.bass, "7")
        self.assertTrue(chord.bass_explicit)

    def test_compact_modifier_is_preserved(self) -> None:
        chord = parse_chord("594")
        self.assertEqual(chord.root, "5")
        self.assertEqual(chord.modifiers, ["94"])


class BarTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = parse_harmony(FIXTURES / "parser_features.harm")

    def test_two_even_chords(self) -> None:
        bar = self.data.sections["A"].bars[2]  # (1 4)
        self.assertEqual(bar.subdivisions, 2)
        self.assertEqual([e.position for e in bar.events], [Fraction(0), Fraction(1, 2)])

    def test_dots_delay_second_onset(self) -> None:
        bar = self.data.sections["A"].bars[3]  # (1 . . 4)
        self.assertEqual(bar.subdivisions, 4)
        self.assertEqual([e.position for e in bar.events], [Fraction(0), Fraction(3, 4)])

    def test_triple_grid(self) -> None:
        bar = self.data.sections["B"].bars[2]  # (1 4 .)
        self.assertEqual(bar.subdivisions, 3)
        self.assertEqual([e.position for e in bar.events], [Fraction(0), Fraction(1, 3)])

    def test_delayed_first_onset(self) -> None:
        bar = self.data.sections["B"].bars[3]  # (. 4)
        self.assertEqual(bar.subdivisions, 2)
        self.assertEqual(len(bar.events), 1)
        self.assertEqual(bar.events[0].position, Fraction(1, 2))
        self.assertEqual(bar.events[0].chord.root, "4")

    def test_whole_bar_dot_has_no_new_onset(self) -> None:
        bar = self.data.sections["B"].bars[5]
        self.assertEqual(bar.subdivisions, 1)
        self.assertEqual(bar.events, [])


class FormTests(unittest.TestCase):
    def test_numbered_a_variants_map_in_order(self) -> None:
        data = parse_harmony(FIXTURES / "bm01.harm")
        self.assertEqual(data.form, "AABA")
        self.assertEqual(data.form_sections, ["A1", "A2", "B", "A3"])
        self.assertEqual(len(data.sections["A1"].bars), 8)

    def test_single_section_is_reused_for_repeated_form_symbol(self) -> None:
        data = parse_harmony_text(
            """% Title: Reuse Test
% TuneID: reuse01
Form: AABA
A:
1
B:
5
"""
        )
        self.assertEqual(data.form_sections, ["A", "A", "B", "A"])

    def test_incomplete_numbered_variants_are_error(self) -> None:
        with self.assertRaises(HarmonyParseError):
            parse_harmony_text(
                """% Title: Bad Form
% TuneID: bad01
Form: AABA
A1:
1
A2:
1
B:
5
"""
            )

    def test_section_alias_preserves_distinct_formal_section(self) -> None:
        data = parse_harmony_text(
            """% Title: Alias Test
% TuneID: alias01
Form: VC
V:
1 67 27 27
57 57 1 1
C = V
"""
        )
        self.assertEqual(data.form_sections, ["V", "C"])
        self.assertEqual(data.sections["C"].alias_of, "V")
        self.assertEqual(
            [bar.raw for bar in data.sections["C"].bars],
            [bar.raw for bar in data.sections["V"].bars],
        )
        self.assertIsNot(data.sections["C"].bars, data.sections["V"].bars)

    def test_numbered_section_alias_resolves_in_form(self) -> None:
        data = parse_harmony_text(
            """% Title: Numbered Alias Test
% TuneID: alias02
Form: AABA
A1:
1 1 4 4
A2:
1 1 5 5
B:
4 4 1 1
A3 = A2
"""
        )
        self.assertEqual(data.form_sections, ["A1", "A2", "B", "A3"])
        self.assertEqual(data.sections["A3"].alias_of, "A2")
        self.assertEqual(
            [bar.raw for bar in data.sections["A3"].bars],
            [bar.raw for bar in data.sections["A2"].bars],
        )

    def test_alias_metadata_appears_only_on_alias_in_json_data(self) -> None:
        data = parse_harmony_text(
            """% Title: Alias JSON Test
% TuneID: alias03
Form: VC
V:
1
C = V
"""
        ).to_dict()
        self.assertNotIn("alias_of", data["sections"]["V"])
        self.assertEqual(data["sections"]["C"]["alias_of"], "V")

    def test_alias_must_reference_previous_section(self) -> None:
        with self.assertRaisesRegex(HarmonyParseError, "undefined or later section"):
            parse_harmony_text(
                """% Title: Forward Alias Test
% TuneID: alias04
Form: VC
C = V
V:
1
"""
            )


class SyntaxErrorTests(unittest.TestCase):
    def test_compressed_dots_are_rejected(self) -> None:
        with self.assertRaisesRegex(HarmonyParseError, "whitespace-separated"):
            parse_harmony_text(
                """% Title: Bad Dots
% TuneID: bad02
Form: A
A:
(1..4)
"""
            )

    def test_missing_required_metadata_is_error(self) -> None:
        with self.assertRaises(HarmonyParseError):
            parse_harmony_text("Form: A\nA:\n1\n")

    def test_top_level_bars_need_whitespace_between_them(self) -> None:
        with self.assertRaisesRegex(HarmonyParseError, "separated by whitespace"):
            parse_harmony_text(
                """% Title: Bad Boundary
% TuneID: bad03
Form: A
A:
1(4 5)
"""
            )


if __name__ == "__main__":
    unittest.main()