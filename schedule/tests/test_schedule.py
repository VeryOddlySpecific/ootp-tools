"""Tests for ootp_schedule.

Run from the schedule/ directory:
    python -m unittest discover tests

The interleague example takes over a minute to generate, so its end-to-end
test only runs when OOTP_TOOLS_SLOW=1 is set.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import ootp_schedule as sch  # noqa: E402

EXAMPLES = os.path.join(HERE, "examples")
NO_IL = os.path.join(EXAMPLES, "bbl_no_interleague.json")
IL = os.path.join(EXAMPLES, "bbl_interleague.json")


def generate(config, out_dir, seed=20260326, tag="s"):
    """Run the CLI quietly; returns the .lsdl path."""
    lsdl = os.path.join(out_dir, f"{tag}.lsdl")
    csv = os.path.join(out_dir, f"{tag}.csv")
    with contextlib.redirect_stdout(io.StringIO()):
        sch.main(["--config", config, "--seed", str(seed),
                  "--lsdl", lsdl, "--csv", csv])
    return lsdl


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class HelpersTest(unittest.TestCase):

    def test_series_lengths(self):
        self.assertEqual(sch.series_lengths(9), [3, 3, 3])
        self.assertEqual(sch.series_lengths(10), [3, 3, 4])
        self.assertEqual(sch.series_lengths(11), [3, 3, 3, 2])

    def test_host_split_balances_odd_totals(self):
        # With an odd total, every team over-hosts exactly half its rivals.
        n = 5
        over = Counter()
        for a in range(n):
            for b in range(n):
                if a != b and sch.host_split(23, a, b, n) == 12:
                    over[a] += 1
        self.assertEqual(set(over.values()), {2})

    def test_calendar_has_52_blocks(self):
        for path in (NO_IL, IL):
            blocks = sch.build_blocks(sch.load_config(path))
            self.assertEqual(len(blocks), 52, path)
            self.assertTrue(blocks[0].opening)

    def test_team_hash_ignores_names(self):
        a = sch.Team(1, "Anchorage", "Otters", 0, 0, 21)
        self.assertEqual(hash(a), hash(1))

    def test_type_code(self):
        config = sch.load_config(IL)
        teams = sch.build_teams(config)
        self.assertEqual(sch.type_code(config, teams),
                         "ILY_BGN_G162_SL1_D1_T5_D2_T5_SL2_D1_T5_D2_T5_C_bbl")


class GenerateTest(unittest.TestCase):

    def test_no_interleague_schedule_is_valid_and_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = generate(NO_IL, tmp, tag="a")
            second = generate(NO_IL, tmp, tag="b")
            self.assertEqual(read_bytes(first), read_bytes(second),
                             "same seed must give the same schedule")

            games = ET.parse(first).getroot().find("GAMES").findall("GAME")
            per_team = Counter()
            for g in games:
                per_team[g.get("away")] += 1
                per_team[g.get("home")] += 1
            self.assertEqual(len(per_team), 22)
            self.assertEqual(set(per_team.values()), {162})

    def test_different_seed_gives_a_different_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = generate(NO_IL, tmp, seed=1, tag="a")
            b = generate(NO_IL, tmp, seed=2, tag="b")
            self.assertNotEqual(read_bytes(a), read_bytes(b))

    @unittest.skipUnless(os.environ.get("OOTP_TOOLS_SLOW"),
                         "set OOTP_TOOLS_SLOW=1 to run (over a minute)")
    def test_interleague_schedule_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            lsdl = generate(IL, tmp)
            games = ET.parse(lsdl).getroot().find("GAMES").findall("GAME")
            self.assertEqual(len(games), 20 * 162 // 2)


if __name__ == "__main__":
    unittest.main()
