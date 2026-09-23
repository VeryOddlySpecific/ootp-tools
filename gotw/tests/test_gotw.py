"""Tests for ootp_gotw's scoring and parsing helpers.

Run from the gotw/ directory:
    python -m unittest discover tests

The golden totals come from real games (historical.py), so a change to
WEIGHTS or the scoring logic shows up here as a changed number. If the
change is intended, update the expected values alongside it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import historical  # noqa: E402
import ootp_gotw as gw  # noqa: E402


def score(key):
    game = next(g for g in historical.GAMES if g.key == key)
    g = historical.build(game)
    return g, gw.score_game(g)


class HistoricalGoldenTest(unittest.TestCase):

    def test_2011_ws_game_6(self):
        g, s = score("ws2011g6")
        self.assertEqual(s["components"], {
            "CLOSE": 20.4, "DRAMA": 39.0, "COMEBACK": 6.0,
            "CLUTCH": 15.0, "STAR": 0.0, "SPECIAL": 0.0})
        self.assertEqual(s["total"], 80.4)
        self.assertEqual(s["winner"], "home")
        self.assertEqual(s["max_deficit"], 3)
        self.assertIn("walk-off HR", s["notes"])
        self.assertIn("11 innings", s["notes"])
        self.assertIs(s["walkoff"], g["plays"][-1])

    def test_1993_ws_game_4(self):
        _, s = score("ws1993g4")
        self.assertEqual(s["components"], {
            "CLOSE": 18.9, "DRAMA": 15.5, "COMEBACK": 12.0,
            "CLUTCH": 4.0, "STAR": 8.0, "SPECIAL": 0.0})
        self.assertEqual(s["total"], 58.4)
        self.assertEqual(s["winner"], "away")
        self.assertEqual(s["max_deficit"], 5)
        self.assertIsNone(s["walkoff"])
        for note in ("T. Fernandez 5 RBI", "M. Thompson 5 RBI",
                     "L. Dykstra 2 HR"):
            self.assertIn(note, s["notes"])

    def test_build_rejects_plays_that_miss_the_final(self):
        game = historical.GAMES[0]._replace(plays=historical.GAMES[0].plays[:-1])
        with self.assertRaises(ValueError):
            historical.build(game)


class ScoringRulesTest(unittest.TestCase):
    """Small hand-built games that pin one rule each."""

    def game(self, plays, away_line, home_line):
        return historical.build(historical.Game(
            key="t", title="t", away="Away", home="Home",
            away_line=away_line, home_line=home_line,
            away_rhe=(sum(away_line), 8, 0), home_rhe=(sum(home_line), 8, 0),
            plays=plays, batting=([], [])))

    def test_blowout_gets_no_margin_points(self):
        g = self.game([(1, "T", "A", "GRAND SLAM HOME RUN", 4, True, True),
                       (2, "T", "B", "3-RUN HOME RUN", 3, True, True)],
                      [4, 3, 0, 0, 0, 0, 0, 0, 0], [0] * 9)
        s = gw.score_game(g)
        self.assertEqual(s["components"]["CLOSE"], 0.0)
        self.assertEqual(s["components"]["DRAMA"], 0.0)

    def test_walkoff_single_in_the_ninth(self):
        g = self.game([(9, "B", "A", "SINGLE, B scores", 1, True, False)],
                      [0] * 9, [0] * 8 + [1])
        s = gw.score_game(g)
        self.assertIsNotNone(s["walkoff"])
        self.assertIn("walk-off", s["notes"])
        # base 2 + 2 innings past the 7th + 1 for a hit
        self.assertEqual(s["components"]["CLUTCH"], 5.0)


class ParsingHelpersTest(unittest.TestCase):

    def test_runs_in_play(self):
        self.assertEqual(gw._runs_in_play("SINGLE, Smith scores, Jones scores"), 2)
        self.assertEqual(gw._runs_in_play("Fly out, Smith tags up, SCORES"), 1)
        self.assertEqual(gw._runs_in_play("GRAND SLAM HOME RUN"), 4)
        self.assertEqual(gw._runs_in_play("SOLO HOME RUN"), 1)
        self.assertEqual(gw._runs_in_play("Smith steals home"), 1)
        self.assertEqual(gw._runs_in_play("Strikeout"), 0)

    def test_count_in_segment(self):
        seg = " J. Rosas 2 (12, 5th Inning), P. Snider (4, 8th Inning)"
        self.assertEqual(gw._count_in_segment(seg, "J. Rosas"), 2)
        self.assertEqual(gw._count_in_segment(seg, "P. Snider"), 1)
        self.assertEqual(gw._count_in_segment(seg, "A. Nobody"), 0)

    def test_total_bases(self):
        notes = "BATTING Total Bases: D. Silva , C. Schaefer 4 , P. Snider"
        self.assertEqual(gw._total_bases(notes),
                         {"D. Silva": 1, "C. Schaefer": 4, "P. Snider": 1})

    def test_resolve_team(self):
        names = {"San Diego Groot", "Toronto Beavers"}
        self.assertEqual(gw.resolve_team("groot", names), "San Diego Groot")
        with self.assertRaises(RuntimeError):
            gw.resolve_team("nobody", names)


if __name__ == "__main__":
    unittest.main()
