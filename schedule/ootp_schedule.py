#!/usr/bin/env python3
"""OOTP custom schedule generator.

Generates an OOTP-importable .lsdl schedule, plus a block-by-block audit
CSV, from a league config JSON (see examples/).  Every schedule is
validated before it is written; a failing run exits non-zero.

Matchup math
------------
Per-pair series lengths follow one rule: prefer 3-game series, absorb
remainders with a single 4 (total % 3 == 1) or single 2 (total % 3 == 2).
Odd per-pair totals (Czervik's 23) split 12/11 with a regular-tournament
orientation so every team over-hosts exactly half its rivals and lands on
exactly 81 home games.

Calendar
--------
The 186-day season splits into an opening Thu-Sun block plus 26 Mon-Sun
weeks (Mon-Thu 4-day block + Fri-Sun weekend block), minus the all-star
break block = 52 playable blocks.  Every team plays exactly one series
per block, so a subleague round = a perfect matching:

  * 6-team divisions: K6 1-factorization (2 hexagon factors carrying the
    10/8 pairs + 3 prism factors) plus 6 cross-division rotations.
  * 5-team divisions: odd round-robin -- each division's idle team plays
    the other division's idle team (40 mixed rounds), plus 12 all-cross
    rounds along the K5,5 diagonals.

Because pure 3-game seasons for the 12-team subleague need 54 series but
only 52 blocks exist, one division pair per team (alphabetical cycle)
plays a 10/8 home split decomposed 3+3+4 / 4+4: every team gets 46
threes + 6 fours and the calendar closes exactly (20 off days).

In-block day rules: 4-game series fill Mon-Thu; 3-game series play
Tue-Thu (Monday off); 2-game series play Tue-Wed (Mon+Thu off); weekend
series are always Fri-Sun; opening-block series start on day 1.

A seeded hill-climb orders rounds (no repeat opponent in consecutive
blocks, 4s on 4-day blocks, 2s on non-opening 4-day blocks) and a second
one assigns home/away order to limit homestand/road-trip length.

Interleague configs take a different path (typed-block decomposition,
see the "interleague variant" section below).

Usage:
    python ootp_schedule.py --config examples/bbl_interleague.json [--seed N]
"""

import argparse
import csv
import itertools
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass


# ---------------------------------------------------------------- teams

@dataclass(frozen=True)
class Team:
    sched_id: int          # 1-based OOTP schedule ID (alphabetical within division)
    city: str
    nickname: str
    subleague: int
    division: int
    statsplus_id: int

    # Hash on the int id alone. The generated hash would include the city
    # and nickname strings, and Python randomizes string hashes per process,
    # so set/frozenset iteration order -- and with it the seeded search --
    # would differ from run to run despite the same --seed.
    def __hash__(self):
        return hash(self.sched_id)

    def __str__(self):
        return f"{self.city} ({self.sched_id})"

    @property
    def abbr(self):
        return self.city.replace(" ", "")[:4].upper()


def load_config(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def build_teams(config):
    """Assign OOTP schedule IDs: alphabetical by city within division,
    divisions in listed order, subleague by subleague."""
    teams = []
    next_id = 1
    for sl_idx, sl in enumerate(config["subleagues"]):
        for div_idx, div in enumerate(sl["divisions"]):
            for t in sorted(div["teams"], key=lambda t: t["city"]):
                teams.append(Team(next_id, t["city"], t["nickname"],
                                  sl_idx, div_idx, t["statsplus_id"]))
                next_id += 1
    return teams


# ------------------------------------------------------------- calendar

@dataclass(frozen=True)
class Block:
    idx: int
    days: tuple            # consecutive season day numbers
    kind: str              # 'four' (Mon-Thu or opening) or 'weekend'
    opening: bool


def day_of_week(config, day):
    """1=Sun .. 7=Sat, from the configured opening-day weekday."""
    return (config["calendar"]["start_day_of_week"] - 1 + day - 1) % 7 + 1


def build_blocks(config):
    cal = config["calendar"]
    n_days = cal["season_days"]
    asb = set(cal["allstar_break_days"])

    def dow(day):
        return day_of_week(config, day)

    # Opening day must start a 4-day block: Thursday (Thu-Sun, season ends
    # on a Sunday) or Monday (Mon-Thu, season ends on a Thursday).  Either
    # way the season is one opening block + 26 weeks = 52 playable blocks
    # after the all-star break block is removed.
    assert dow(1) in (2, 5), "opening day must be a Monday or Thursday"
    blocks = [Block(0, (1, 2, 3, 4), "four", True)]
    d = 5
    while d <= n_days:
        wd = dow(d)
        if wd == 2:                                   # Mon-Thu block
            week = tuple(range(d, d + 4))
            if not set(week) <= asb:
                blocks.append(Block(len(blocks), week, "four", False))
            d += 4
        elif wd == 6:                                 # Fri-Sun block
            week = tuple(range(d, d + 3))
            assert not (set(week) & asb), "all-star break must be a Mon-Thu block"
            blocks.append(Block(len(blocks), week, "weekend", False))
            d += 3
        else:
            raise AssertionError(f"unexpected block start weekday {wd} at day {d}")
    assert blocks[-1].days[-1] == n_days, "season must end on the last block day"
    return blocks


def series_days(block, length):
    """Which days of its block a series occupies (see module docstring)."""
    if block.opening or length == len(block.days):
        return block.days[:length]
    if block.kind == "four" and length == 3:
        return block.days[1:4]                    # Tue-Thu, Monday off
    if block.kind == "four" and length == 2:
        return block.days[1:3]                    # Tue-Wed, Mon+Thu off
    raise ValueError(f"length {length} does not fit {block.kind} block")


# ------------------------------------------------- per-pair series shapes

def series_lengths(total):
    """All 3s, plus at most one 4 (rem 1) or one 2 (rem 2)."""
    r = total % 3
    if r == 0:
        return [3] * (total // 3)
    if r == 1:
        return [3] * ((total - 4) // 3) + [4]
    return [3] * (total // 3) + [2]


def host_split(total, idx_a, idx_b, n_teams):
    """Games team-at-idx_a hosts of `total` vs team-at-idx_b.  Odd totals:
    a over-hosts iff b is among the floor(n/2) teams cyclically after a."""
    if total % 2 == 0:
        return total // 2
    if (idx_b - idx_a) % n_teams <= n_teams // 2:
        return total // 2 + 1
    return total // 2


def build_pair_shapes(config, teams, n_blocks):
    """dict frozenset{a,b} -> list of (home_team, length).  The 12-team
    subleague's naive all-3s shape yields n_blocks+2 series per team, so
    the alphabetical-cycle pairs switch to a 10/8 split (3+3+4 / 4+4),
    removing 2 series per team with home totals unchanged."""
    shapes = {}
    for sl_idx, sl in enumerate(config["subleagues"]):
        g_div, g_cross = sl["games_vs_division_rival"], sl["games_vs_cross_division"]
        divs = defaultdict(list)
        for t in teams:
            if t.subleague == sl_idx:
                divs[t.division].append(t)
        for d in divs.values():
            d.sort(key=lambda t: t.sched_id)
        sizes = {len(d) for d in divs.values()}
        assert len(divs) == 2 and len(sizes) == 1, "need 2 equal divisions"
        n = sizes.pop()

        cycle_pairs = set()
        naive = 0
        for j in range(1, n):
            h = host_split(g_div, 0, j, n)
            naive += len(series_lengths(h)) + len(series_lengths(g_div - h))
        naive += n * (len(series_lengths(g_cross // 2))
                      + len(series_lengths(g_cross - g_cross // 2)))
        if naive == n_blocks + 2:
            assert g_div % 2 == 0, "10/8 adjustment expects an even division total"
            cycle_pairs = {frozenset((i, (i + 1) % n)) for i in range(n)}
        elif naive != n_blocks:
            sys.exit(f"{sl['name']}: {naive} series/team cannot fill {n_blocks} blocks")

        for dteams in divs.values():
            for i, j in itertools.combinations(range(n), 2):
                a, b = dteams[i], dteams[j]
                inst = []
                if frozenset((i, j)) in cycle_pairs:
                    over, under = (a, b) if (j - i) % n == 1 else (b, a)
                    inst += [(over, 3), (over, 3), (over, 4)]   # hosts 10
                    inst += [(under, 4), (under, 4)]            # hosts 8
                else:
                    a_hosts = host_split(g_div, i, j, n)
                    inst += [(a, l) for l in series_lengths(a_hosts)]
                    inst += [(b, l) for l in series_lengths(g_div - a_hosts)]
                shapes[frozenset((a, b))] = inst
        for a in divs[0]:
            for b in divs[1]:
                inst = [(a, l) for l in series_lengths(g_cross // 2)]
                inst += [(b, l) for l in series_lengths(g_cross - g_cross // 2)]
                shapes[frozenset((a, b))] = inst
    return shapes


# --------------------------------------------------------------- rounds

def rounds_even(dteams_a, dteams_b, cross_uses):
    """Rounds for a 6+6 subleague.  Hexagon factors (the 10/8 cycle
    pairs) are flagged 'four' so all their meetings land on 4-day blocks,
    which their three 4-game series need."""
    n = len(dteams_a)
    assert n == 6, "even-division factorization implemented for 6-team divisions"
    hex_f = [[(0, 1), (2, 3), (4, 5)], [(1, 2), (3, 4), (5, 0)]]
    prism_f = [[(0, 2), (3, 5), (1, 4)], [(2, 4), (5, 1), (0, 3)],
               [(4, 0), (1, 3), (2, 5)]]
    rounds = []
    for f_idx, fac in enumerate(hex_f):
        pairs = [frozenset((da[i], da[j])) for da in (dteams_a, dteams_b)
                 for i, j in fac]
        rounds += [{"pairs": frozenset(pairs), "need": "four"}] * 5
    for fac in prism_f:
        pairs = [frozenset((da[i], da[j])) for da in (dteams_a, dteams_b)
                 for i, j in fac]
        rounds += [{"pairs": frozenset(pairs), "need": None}] * 6
    for k in range(n):
        pairs = [frozenset((dteams_a[i], dteams_b[(i + k) % n])) for i in range(n)]
        rounds += [{"pairs": frozenset(pairs), "need": None}] * cross_uses
    return rounds


def rounds_odd(dteams_a, dteams_b):
    """Rounds for a 5+5 subleague: 40 mixed rounds (each division's idle
    team plays the other's idle team) + 12 all-cross diagonal rounds."""
    n = len(dteams_a)
    assert n == 5, "odd-division rotation implemented for 5-team divisions"

    def near_factor(dteams, r):
        return [frozenset((dteams[(r + 1) % n], dteams[(r + 4) % n])),
                frozenset((dteams[(r + 2) % n], dteams[(r + 3) % n]))]

    rounds = []
    for i in range(n):
        for j in range(n):
            u = 2 if (j - i) % n in (0, 1, 2) else 1
            pairs = (near_factor(dteams_a, i) + near_factor(dteams_b, j)
                     + [frozenset((dteams_a[i], dteams_b[j]))])
            rounds += [{"pairs": frozenset(pairs), "need": None}] * u
    for d in range(n):
        m = 4 - (2 if d in (0, 1, 2) else 1)
        pairs = [frozenset((dteams_a[i], dteams_b[(i + d) % n])) for i in range(n)]
        rounds += [{"pairs": frozenset(pairs), "need": None}] * m
    return rounds


def build_rounds(config, teams, shapes, n_blocks):
    per_sl = {}
    for sl_idx, sl in enumerate(config["subleagues"]):
        divs = defaultdict(list)
        for t in teams:
            if t.subleague == sl_idx:
                divs[t.division].append(t)
        for d in divs.values():
            d.sort(key=lambda t: t.sched_id)
        da, db = divs[0], divs[1]
        if len(da) % 2 == 0:
            g_cross = sl["games_vs_cross_division"]
            cross_uses = (len(series_lengths(g_cross // 2))
                          + len(series_lengths(g_cross - g_cross // 2)))
            rounds = rounds_even(da, db, cross_uses)
        else:
            rounds = rounds_odd(da, db)
        assert len(rounds) == n_blocks, f"{sl['name']}: {len(rounds)} rounds != {n_blocks}"
        per_sl[sl_idx] = rounds
    return per_sl


# ---------------------------------------------------- round sequencing

def sequence_rounds(rounds, blocks, shapes, rng, restarts=80, iters=20000):
    """Order rounds onto blocks: no pair meets in consecutive blocks, and
    every pair gets enough 4-day meetings for its 4s (and non-opening
    4-day meetings for its 2s).  Seeded hill-climb with restarts."""
    n = len(blocks)
    pair_rounds = defaultdict(list)
    for r_idx, r in enumerate(rounds):
        for p in r["pairs"]:
            pair_rounds[p].append(r_idx)
    # per-pair needs
    need4, need2 = {}, {}
    for p, rids in pair_rounds.items():
        lens = Counter(l for _, l in shapes[p])
        need4[p], need2[p] = lens[4], lens[2]

    fourday = [b.idx for b in blocks if b.kind == "four"]
    open_idx = next(b.idx for b in blocks if b.opening)

    def ok(r_idx, b_idx):
        return rounds[r_idx]["need"] != "four" or blocks[b_idx].kind == "four"

    def adjacency_viol(order, pos):
        v = 0
        if pos > 0 and order[pos] is not None and order[pos - 1] is not None:
            v += len(rounds[order[pos]]["pairs"] & rounds[order[pos - 1]]["pairs"])
        return v

    # Quotas for 4s and 2s are scored independently; in this league family a
    # pair never carries both, so the two counts cannot compete for slots.
    def score(order):
        pos = [0] * len(rounds)
        for i, r in enumerate(order):
            pos[r] = i
        s = 0
        for i in range(1, n):
            s += len(rounds[order[i]]["pairs"] & rounds[order[i - 1]]["pairs"])
        for p, rids in pair_rounds.items():
            if need4[p]:
                c4 = sum(1 for r in rids if blocks[pos[r]].kind == "four")
                s += max(0, need4[p] - c4)
            if need2[p]:
                c2 = sum(1 for r in rids
                         if blocks[pos[r]].kind == "four" and pos[r] != open_idx)
                s += max(0, need2[p] - c2)
        return s

    for _ in range(restarts):
        # initial: rounds needing 4-day blocks go there first, rest random
        order = [None] * n
        forced = [i for i, r in enumerate(rounds) if r["need"] == "four"]
        slots4 = rng.sample(fourday, len(forced))
        for r_idx, b_idx in zip(forced, slots4):
            order[b_idx] = r_idx
        rest = [i for i in range(len(rounds)) if i not in set(forced)]
        rng.shuffle(rest)
        empties = [i for i in range(n) if order[i] is None]
        for r_idx, b_idx in zip(rest, empties):
            order[b_idx] = r_idx
        cur = score(order)
        for _ in range(iters):
            if cur == 0:
                break
            i, j = rng.randrange(n), rng.randrange(n)
            if i == j or not (ok(order[i], j) and ok(order[j], i)):
                continue
            order[i], order[j] = order[j], order[i]
            new = score(order)
            if new <= cur:
                cur = new
            else:
                order[i], order[j] = order[j], order[i]
        if cur == 0:
            return order
    sys.exit("round sequencing failed; try a different --seed")


# ------------------------------------- instance assignment (lengths+venues)

@dataclass
class PlacedSeries:
    home: Team
    away: Team
    length: int
    block: Block

    @property
    def days(self):
        return series_days(self.block, self.length)


def assign_instances(shapes, order, rounds, blocks, rng):
    """For each pair, map its (home, length) instances onto its meeting
    blocks: 4s on 4-day blocks, 2s on non-opening 4-day blocks."""
    pos = {r: i for i, r in enumerate(order)}
    assignment = {}                      # pair -> list of (block_idx, instance)
    for r_idx, r in enumerate(rounds):
        for p in r["pairs"]:
            assignment.setdefault(p, []).append(pos[r_idx])
    for p, block_ids in assignment.items():
        block_ids.sort()
        inst = list(shapes[p])
        rng.shuffle(inst)
        placed = [None] * len(block_ids)
        for want, eligible in (
                (4, lambda b: blocks[b].kind == "four"),
                (2, lambda b: blocks[b].kind == "four" and not blocks[b].opening)):
            for k in range(len(inst)):
                if inst[k] is not None and inst[k][1] == want:
                    slot = next(i for i, b in enumerate(block_ids)
                                if placed[i] is None and eligible(b))
                    placed[slot] = inst[k]
                    inst[k] = None
        rest = [x for x in inst if x is not None]
        for i in range(len(placed)):
            if placed[i] is None:
                placed[i] = rest.pop()
        assignment[p] = list(zip(block_ids, placed))
    return assignment


def optimize_venues(assignment, teams, blocks, rng, iters=60000, max_run=3):
    """Hill-climb swapping instances within pairs to limit consecutive
    home/away series runs to max_run."""
    timeline = {t: [None] * len(blocks) for t in teams}   # 'H'/'A' per block
    for p, placed in assignment.items():
        for b_idx, (home, l) in placed:
            a, b = tuple(p)
            timeline[home][b_idx] = "H"
            timeline[a if home == b else b][b_idx] = "A"

    def team_pen(t):
        pen, run, prev = 0, 0, None
        for v in timeline[t]:
            run = run + 1 if v == prev else 1
            prev = v
            if run > max_run:
                pen += 1
        return pen

    pen = {t: team_pen(t) for t in teams}
    pairs = list(assignment)
    for _ in range(iters):
        if sum(pen.values()) == 0:
            break
        p = pairs[rng.randrange(len(pairs))]
        placed = assignment[p]
        i, j = rng.randrange(len(placed)), rng.randrange(len(placed))
        if i == j:
            continue
        (b1, in1), (b2, in2) = placed[i], placed[j]
        if in1 == in2:
            continue

        def fits(b_idx, length):
            blk = blocks[b_idx]
            if length == 4:
                return blk.kind == "four"
            if length == 2:
                return blk.kind == "four" and not blk.opening
            return True
        if not (fits(b1, in2[1]) and fits(b2, in1[1])):
            continue
        a, b = tuple(p)
        affected = (a, b)
        old = pen[a] + pen[b]
        placed[i], placed[j] = (b1, in2), (b2, in1)
        for b_idx, (home, l) in (placed[i], placed[j]):
            timeline[home][b_idx] = "H"
            timeline[a if home == b else b][b_idx] = "A"
        na, nb = team_pen(a), team_pen(b)
        if na + nb <= old:
            pen[a], pen[b] = na, nb
        else:
            placed[i], placed[j] = (b1, in1), (b2, in2)
            for b_idx, (home, l) in (placed[i], placed[j]):
                timeline[home][b_idx] = "H"
                timeline[a if home == b else b][b_idx] = "A"
    return sum(pen.values())


def materialize(assignment, blocks):
    placed = []
    for p, entries in assignment.items():
        a, b = tuple(p)
        for b_idx, (home, length) in entries:
            away = a if home == b else b
            placed.append(PlacedSeries(home, away, length, blocks[b_idx]))
    placed.sort(key=lambda s: (s.block.idx, s.home.sched_id))
    return placed


# ------------------------------------------------------------ validation

def validate_matchups(config, teams, placed):
    errors = []
    gpt = config["games_per_team"]
    home, away, pair_games = Counter(), Counter(), Counter()
    for s in placed:
        home[s.home] += s.length
        away[s.away] += s.length
        pair_games[frozenset((s.home, s.away))] += s.length
    for t in teams:
        if home[t] + away[t] != gpt:
            errors.append(f"{t}: {home[t] + away[t]} games, expected {gpt}")
        if home[t] != gpt // 2:
            errors.append(f"{t}: {home[t]} home games, expected {gpt // 2}")
    for sl_idx, sl in enumerate(config["subleagues"]):
        sl_teams = [t for t in teams if t.subleague == sl_idx]
        for a, b in itertools.combinations(sl_teams, 2):
            expected = (sl["games_vs_division_rival"] if a.division == b.division
                        else sl["games_vs_cross_division"])
            got = pair_games.pop(frozenset((a, b)), 0)
            if got != expected:
                errors.append(f"{a} vs {b}: {got} games, expected {expected}")
    if config.get("inter_league"):
        big = 0 if len([t for t in teams if t.subleague == 0]) > \
            len([t for t in teams if t.subleague == 1]) else 1
        tw, cz = _subleague_sorted(teams, big), _subleague_sorted(teams, 1 - big)
        for i, t in enumerate(tw):
            for j, c in enumerate(cz):
                expected = 3 if len(tw) == len(cz) else interleague_pair_length(i, j)
                got = pair_games.pop(frozenset((t, c)), 0)
                if got != expected:
                    errors.append(f"{t} vs {c}: {got} interleague games, "
                                  f"expected {expected}")
    for p in pair_games:
        a, b = tuple(p)
        errors.append(f"illegal matchup: {a} vs {b}")
    return errors


def validate_calendar(config, teams, placed, blocks):
    errors = []
    cal = config["calendar"]
    asb = set(cal["allstar_break_days"])
    by_block = defaultdict(list)
    day_teams = defaultdict(set)
    off_target = (cal["season_days"] - len(asb) - config["games_per_team"])
    games_on = Counter()
    for s in placed:
        by_block[(s.block.idx, s.home)].append(s)
        by_block[(s.block.idx, s.away)].append(s)
        for d in s.days:
            for t in (s.home, s.away):
                if t in day_teams[d]:
                    errors.append(f"{t} double-booked on day {d}")
                day_teams[d].add(t)
            games_on[d] += 1
    for b in blocks:
        for t in teams:
            if len(by_block[(b.idx, t)]) != 1:
                errors.append(f"{t} has {len(by_block[(b.idx, t)])} series in block {b.idx}")
    for d in asb:
        if games_on[d]:
            errors.append(f"games during all-star break on day {d}")
    for d in (1, cal["season_days"]):
        if len(day_teams[d]) != len(teams):
            errors.append(f"day {d}: only {len(day_teams[d])}/{len(teams)} teams play")
    for t in teams:
        play_days = sum(1 for d in day_teams if t in day_teams[d])
        off = cal["season_days"] - len(asb) - play_days
        if off != off_target:
            errors.append(f"{t}: {off} off days, expected {off_target}")
    # no same opponent in consecutive blocks
    opp = {(s.block.idx, s.home): s.away for s in placed}
    opp.update({(s.block.idx, s.away): s.home for s in placed})
    for t in teams:
        for b in range(1, len(blocks)):
            if opp[(b, t)] == opp[(b - 1, t)]:
                errors.append(f"{t} plays {opp[(b, t)].city} in consecutive blocks {b-1},{b}")
    return errors


# --------------------------------------------------------------- output

MONTHS = [(31, "Jan"), (28, "Feb"), (31, "Mar"), (30, "Apr"), (31, "May"),
          (30, "Jun"), (31, "Jul"), (31, "Aug"), (30, "Sep"), (31, "Oct"),
          (30, "Nov"), (31, "Dec")]


def date_str(config, day):
    m = config["calendar"]["start_month"] - 1
    dom = config["calendar"]["start_day"] + day - 1
    while dom > MONTHS[m][0]:
        dom -= MONTHS[m][0]
        m = (m + 1) % 12
    return f"{MONTHS[m][1]} {dom}"


def print_audit(config, teams, placed):
    print(f"{config['league_name']}: {len(placed)} series, "
          f"{sum(s.length for s in placed)} total games\n")
    for sl_idx, sl in enumerate(config["subleagues"]):
        print(f"--- {sl['name']} ---")
        print(f"{'team':<28}{'G':>5}{'H':>5}{'A':>5}   series by length; longest H/A streak (series)")
        for t in [t for t in teams if t.subleague == sl_idx]:
            mine = sorted((s for s in placed if t in (s.home, s.away)),
                          key=lambda s: s.block.idx)
            th = sum(s.length for s in mine if s.home == t)
            ta = sum(s.length for s in mine if s.away == t)
            lens = Counter(s.length for s in mine)
            shape = "  ".join(f"{l}g x{lens[l]}" for l in sorted(lens))
            runs, run, prev = [], 0, None
            for s in mine:
                v = "H" if s.home == t else "A"
                run = run + 1 if v == prev else 1
                prev = v
                runs.append(run)
            print(f"{t.city + ' ' + t.nickname:<28}{th + ta:>5}{th:>5}{ta:>5}"
                  f"   {shape}; max run {max(runs)}")
        print()


def write_csv(config, teams, placed, blocks, path):
    grid = {}
    for s in placed:
        d0, d1 = s.days[0], s.days[-1]
        span = f"{date_str(config, d0)}-{date_str(config, d1).split(' ')[1]}"
        grid[(s.block.idx, s.home)] = f"vs {s.away.abbr} x{s.length} {span}"
        grid[(s.block.idx, s.away)] = f"@ {s.home.abbr} x{s.length} {span}"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["block", "type", "days"] + [t.abbr for t in teams])
        for b in blocks:
            span = f"{date_str(config, b.days[0])} - {date_str(config, b.days[-1])}"
            w.writerow([b.idx, "opening" if b.opening else b.kind, span]
                       + [grid.get((b.idx, t), "") for t in teams])


# ------------------------------------------------- interleague variant
#
# With interleague on, blocks mix in-league and interleague games in one
# league-wide matching, so instead of per-subleague factor algebra we use
# a typed-block decomposer: walk the 52 blocks chronologically, extract a
# full perfect matching per block by randomized greedy search with
# feasibility guards, seeded restarts on dead ends.
#
# Shapes (verified: 162 games, 81 home, 52 series per team):
#   12-team subleague: division 13 (7/6 split, [3,4]/[3,3]; teams at even
#   division position over-host 3 rivals, odd 2); cross 12 with four 6/6
#   pairs ([3,3] each) and two uneven 8/4 pairs ([4,4]/[4]); interleague
#   one series vs all 10: five 3s + five 2s.
#   10-team subleague: division 18 -- distance-2 rivals 9/9 ([3,3,3]),
#   cycle rivals 10/8 ([3,3,4]/[4,4]); cross 12 as three 6/6 + two 8/4;
#   interleague one series vs all 12: six 3s + six 2s.
#   3- vs 2-game interleague pairs by parity: (pos_a + pos_b) even -> 2.

def _subleague_sorted(teams, sl_idx):
    return sorted((t for t in teams if t.subleague == sl_idx),
                  key=lambda t: t.sched_id)


# Division over-hosting tournaments for 6-team divisions (winner hosts 7
# of the 13).  Out-degrees are 3,3,3,2,2,2 with the 3s placed so that
# each *position-parity* class holds exactly three 3s across the two
# divisions -- required for the interleague venue quotas to balance
# within each parity component (see build_pair_shapes_inter).
TOURN_6 = [
    {0: {1, 3, 4}, 1: {2, 4, 5}, 2: {0, 3, 5}, 3: {1, 4}, 4: {2, 5}, 5: {0, 3}},
    {0: {1, 2, 4}, 1: {3, 4, 5}, 2: {1, 4}, 3: {0, 2, 5}, 4: {3, 5}, 5: {0, 2}},
]


def gale_ryser(rows, row_quota, cols, col_capacity):
    """Orient all row x col pairs: returns set of (row, col) where the row
    side hosts.  Exact for complete bipartite components: process rows in
    descending quota order, giving each row the columns with the most
    remaining capacity."""
    cap = {c: col_capacity for c in cols}
    hosts = set()
    for r in sorted(rows, key=lambda r: -row_quota[r]):
        for c in sorted(cols, key=lambda c: -cap[c])[:row_quota[r]]:
            if cap[c] == 0:
                raise ValueError("infeasible venue quotas")
            cap[c] -= 1
            hosts.add((r, c))
    if any(cap.values()):
        raise ValueError("infeasible venue quotas")
    return hosts


def interleague_pair_length(pos_a, pos_b):
    return 2 if (pos_a + pos_b) % 2 == 0 else 3


def build_pair_shapes_inter(config, teams, n_blocks):
    shapes = {}
    sizes = {}
    counts = [sum(len(d["teams"]) for d in sl["divisions"])
              for sl in config["subleagues"]]
    for sl_idx, sl in enumerate(config["subleagues"]):
        g_div, g_cross = sl["games_vs_division_rival"], sl["games_vs_cross_division"]
        divs = defaultdict(list)
        for t in teams:
            if t.subleague == sl_idx:
                divs[t.division].append(t)
        for d in divs.values():
            d.sort(key=lambda t: t.sched_id)
        da, db = divs[0], divs[1]
        n = len(da)
        sizes[sl_idx] = n
        inter = sl["interleague_games"]
        total = (n - 1) * g_div + n * g_cross + inter
        assert total == config["games_per_team"], \
            f"{sl['name']}: {total} != {config['games_per_team']}"
        if n == 6:
            assert (g_div, g_cross, inter) == (13, 12, 25), \
                "12-team interleague shape expects 13/12/25"
            for d_idx, d in enumerate((da, db)):
                beats = TOURN_6[d_idx]
                for i, j in itertools.combinations(range(n), 2):
                    over, under = (d[i], d[j]) if j in beats[i] else (d[j], d[i])
                    shapes[frozenset((d[i], d[j]))] = \
                        [(over, 3), (over, 4), (under, 3), (under, 3)]
            for i in range(n):
                shapes[frozenset((da[i], db[i]))] = \
                    [(da[i], 4), (da[i], 4), (db[i], 4)]          # a hosts 8
                shapes[frozenset((da[i], db[(i + 1) % n]))] = \
                    [(db[(i + 1) % n], 4), (db[(i + 1) % n], 4), (da[i], 4)]
            for i in range(n):
                for k in range(2, n):
                    a, b = da[i], db[(i + k) % n]
                    shapes[frozenset((a, b))] = [(a, 3), (a, 3), (b, 3), (b, 3)]
        elif n == 5:
            assert (g_div, g_cross, inter) == (18, 12, 30), \
                "10-team interleague shape expects 18/12/30"
            for d in (da, db):
                for i in range(n):
                    a, b = d[i], d[(i + 1) % n]                   # cycle: 10/8
                    shapes[frozenset((a, b))] = \
                        [(a, 3), (a, 3), (a, 4), (b, 4), (b, 4)]
                for i in range(n):
                    a, b = d[i], d[(i + 2) % n]                   # distance 2: 9/9
                    if frozenset((a, b)) not in shapes:
                        shapes[frozenset((a, b))] = \
                            [(a, 3), (a, 3), (a, 3), (b, 3), (b, 3), (b, 3)]
            # Cross 8/4 pairs only exist to shave series counts when the
            # other subleague is bigger (it forces extra interleague
            # series); with equal subleagues all five cross pairs are 6/6.
            uneven = counts[1 - sl_idx] - 2 * n
            assert uneven in (0, 2), f"unsupported subleague sizes {counts}"
            for i in range(n):
                if uneven:
                    shapes[frozenset((da[i], db[i]))] = \
                        [(da[i], 4), (da[i], 4), (db[i], 4)]
                    shapes[frozenset((da[i], db[(i + 1) % n]))] = \
                        [(db[(i + 1) % n], 4), (db[(i + 1) % n], 4), (da[i], 4)]
                for k in range(uneven, n):
                    a, b = da[i], db[(i + k) % n]
                    shapes[frozenset((a, b))] = [(a, 3), (a, 3), (b, 3), (b, 3)]
        else:
            sys.exit(f"unsupported division size {n} for interleague")

    # Interleague pairs.
    big = 0 if sizes[0] > sizes[1] else 1
    tw, cz = _subleague_sorted(teams, big), _subleague_sorted(teams, 1 - big)

    if len(tw) == len(cz):
        # Equal subleagues: one 3-game series vs every opponent; circulant
        # venue split (host the cyclically nearest half) gives each team
        # an even home/away interleague split.
        m = len(cz)
        for i, t in enumerate(tw):
            for j, c in enumerate(cz):
                home = t if (j - i) % m < m // 2 else c
                shapes[frozenset((t, c))] = [(home, 3)]
    else:
        # Unequal (12 vs 10): length by parity, venue by per-team quota.
        # k = rivals over-hosted, from the division tournaments; home-series
        # quotas follow: h3 = 5-k threes, h2 = k twos (home total 15-k).
        def k_of(t):
            div = [x for x in tw if x.division == t.division]
            return len(TOURN_6[t.division][div.index(t)])

        quota = {t: {3: 5 - k_of(t), 2: k_of(t)} for t in tw}
        # 2-pairs connect same-parity positions, 3-pairs opposite parity;
        # each component is complete bipartite, solved exactly by
        # gale_ryser (the smaller side hosts 3 in every component, so
        # away capacity is |rows| - 3).
        for length in (2, 3):
            for tw_par in (0, 1):
                cz_par = tw_par if length == 2 else 1 - tw_par
                rows = [t for i, t in enumerate(tw) if i % 2 == tw_par]
                cols = [c for j, c in enumerate(cz) if j % 2 == cz_par]
                hosts = gale_ryser(rows, {t: quota[t][length] for t in rows},
                                   cols, len(rows) - 3)
                for t in rows:
                    for c in cols:
                        home = t if (t, c) in hosts else c
                        shapes[frozenset((t, c))] = [(home, length)]

    per_team = Counter()
    for p, inst in shapes.items():
        for t in p:
            per_team[t] += len(inst)
    assert set(per_team.values()) == {n_blocks}, \
        f"series per team {set(per_team.values())} != blocks {n_blocks}"
    return shapes


def decompose_typed(teams, shapes, blocks, rng, restarts=400):
    """Chronological block-by-block league-wide matching with lengths.
    Guards: weekend blocks consume only 3s; a 3 may burn on a 4-day block
    only if both teams keep enough 3s for their remaining weekends; teams
    whose remaining non-3s exactly fill their remaining 4-day blocks must
    play a 4 or 2 now; no pair repeats from the previous block."""
    pair_list = {t: [] for t in teams}
    for p in shapes:
        for t in p:
            pair_list[t].append(p)

    for _ in range(restarts):
        remaining = {p: Counter(l for _, l in inst) for p, inst in shapes.items()}
        threes = Counter()
        non3 = Counter()
        for p, inst in shapes.items():
            for t in p:
                threes[t] += sum(1 for _, l in inst if l == 3)
                non3[t] += sum(1 for _, l in inst if l != 3)
        rw = sum(1 for b in blocks if b.kind == "weekend")
        r4 = sum(1 for b in blocks if b.kind == "four")
        result, prev_pairs, failed = [], frozenset(), False

        for b in blocks:
            if b.kind == "weekend":
                rw -= 1
            else:
                r4 -= 1
            got = None
            for _ in range(300):
                got = _match_block(b, teams, pair_list, remaining, prev_pairs,
                                   threes, non3, rw, r4, rng)
                if got is not None:
                    break
            if got is None:
                failed = True
                break
            for p, length in got:
                remaining[p][length] -= 1
                for t in p:
                    if length == 3:
                        threes[t] -= 1
                    else:
                        non3[t] -= 1
            result.append(got)
            prev_pairs = frozenset(p for p, _ in got)
        if not failed:
            return result
    sys.exit("interleague decomposition failed; try a different --seed")


def _match_block(block, teams, pair_list, remaining, prev_pairs,
                 threes, non3, rw, r4, rng):
    """One randomized greedy attempt at a full matching for this block.
    Returns list of (pair, length) or None."""
    unmatched = set(teams)
    picks = []

    def options(t):
        opts = []
        for p in pair_list[t]:
            other = next(x for x in p if x != t)
            if other not in unmatched or p in prev_pairs:
                continue
            rem = remaining[p]
            if block.kind == "weekend":
                if rem[3] > 0:
                    opts.append((p, 3))
                continue
            must = any(non3[x] >= r4 + 1 for x in p)
            if rem[4] > 0:
                opts.append((p, 4))
            if rem[2] > 0 and not block.opening:
                opts.append((p, 2))
            if rem[3] > 0 and not must and all(threes[x] - 1 >= rw for x in p):
                opts.append((p, 3))
        return opts

    while unmatched:
        t = min(unmatched, key=lambda x: (len(options(x)), rng.random()))
        opts = options(t)
        if not opts:
            return None
        if block.kind == "weekend":
            weights = [remaining[p][3] for p, _ in opts]
        else:
            weights = [{4: 40, 2: 30, 3: 1}[l] for _, l in opts]
        p, length = rng.choices(opts, weights=weights)[0]
        picks.append((p, length))
        unmatched -= set(p)
    return picks


def instances_from_decomposition(shapes, per_block, rng):
    """Match each pair's concrete (home, length) instances to the blocks
    the decomposer gave it; venue choice among same-length instances is
    random here and polished by optimize_venues."""
    assignment = defaultdict(list)
    pool = {p: list(inst) for p, inst in shapes.items()}
    for b_idx, picks in enumerate(per_block):
        for p, length in picks:
            cand = [i for i, inst in enumerate(pool[p])
                    if inst is not None and inst[1] == length]
            k = rng.choice(cand)
            assignment[p].append((b_idx, pool[p][k]))
            pool[p][k] = None
    return dict(assignment)


def type_code(config, teams):
    """Schedule code per OOTP naming convention, e.g.
    ILN_BGN_G162_SL1_D1_T6_D2_T6_SL2_D1_T5_D2_T5_C_bwb"""
    parts = ["ILY" if config["inter_league"] else "ILN",
             "BGY" if config["balanced_games"] else "BGN",
             f"G{config['games_per_team']}"]
    for sl_idx, sl in enumerate(config["subleagues"]):
        parts.append(f"SL{sl_idx + 1}")
        for d_idx, d in enumerate(sl["divisions"]):
            parts += [f"D{d_idx + 1}", f"T{len(d['teams'])}"]
    parts += ["C", config.get("league_abbr", "custom").lower()]
    return "_".join(parts)


def write_lsdl(config, teams, placed, path):
    """Emit the OOTP-importable schedule file.  GAME day attributes are
    season day numbers (day 1 = opening day); away/home are the 1-based
    schedule team IDs.  Sunday games get the afternoon start time."""
    cal = config["calendar"]
    t_default = config.get("default_game_time", "1905")
    t_sunday = config.get("sunday_game_time", "1335")
    games = []
    for s in placed:
        for d in s.days:
            time = t_sunday if day_of_week(config, d) == 1 else t_default
            games.append((d, time, s.away.sched_id, s.home.sched_id))
    games.sort()
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n')
        f.write(f'<SCHEDULE type="{type_code(config, teams)}" '
                f'inter_league="{int(config["inter_league"])}" '
                f'balanced_games="{int(config["balanced_games"])}" '
                f'games_per_team="{config["games_per_team"]}" '
                f'start_month="{cal["start_month"]}" '
                f'start_day="{cal["start_day"]}" '
                f'start_day_of_week="{cal["start_day_of_week"]}" '
                f'allstar_game_day="{cal["allstar_game_day"]}">\n')
        f.write("<GAMES>\n")
        for d, time, away, home in games:
            f.write(f'<GAME day="{d}" time="{time}" away="{away}" home="{home}" />\n')
        f.write("</GAMES>\n</SCHEDULE>\n")
    return len(games)


def verify_lsdl(path, expected_games):
    """Re-parse the written file as XML and sanity-check the game count."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    n = len(root.find("GAMES").findall("GAME"))
    assert root.tag == "SCHEDULE" and n == expected_games, \
        f"lsdl self-check failed: {n} games, expected {expected_games}"


# ----------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="OOTP custom schedule generator")
    ap.add_argument("--config", required=True,
                    help="league config JSON (see examples/)")
    ap.add_argument("--seed", type=int, default=20260326)
    ap.add_argument("--csv", default=None,
                    help="block-grid CSV path (default: <abbr>_blocks_<iln|ily>.csv)")
    ap.add_argument("--lsdl", default=None,
                    help="output path (default: <type-code>.lsdl in cwd)")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    config = load_config(args.config)
    teams = build_teams(config)
    blocks = build_blocks(config)
    print(f"calendar: {len(blocks)} blocks "
          f"({sum(1 for b in blocks if b.kind == 'four')} four-day, "
          f"{sum(1 for b in blocks if b.kind == 'weekend')} weekend)")

    if config.get("inter_league"):
        shapes = build_pair_shapes_inter(config, teams, len(blocks))
        per_block = decompose_typed(teams, shapes, blocks, rng)
        assignment = instances_from_decomposition(shapes, per_block, rng)
    else:
        shapes = build_pair_shapes(config, teams, len(blocks))
        rounds_per_sl = build_rounds(config, teams, shapes, len(blocks))
        assignment = {}
        for sl_idx, rounds in rounds_per_sl.items():
            sl_shapes = {p: inst for p, inst in shapes.items()
                         if next(iter(p)).subleague == sl_idx}
            order = sequence_rounds(rounds, blocks, sl_shapes, rng)
            assignment.update(assign_instances(sl_shapes, order, rounds, blocks, rng))

    residual = optimize_venues(assignment, teams, blocks, rng)
    placed = materialize(assignment, blocks)

    print_audit(config, teams, placed)
    errors = validate_matchups(config, teams, placed) \
        + validate_calendar(config, teams, placed, blocks)
    if errors:
        print("VALIDATION FAILED:")
        for e in errors[:40]:
            print(f"  - {e}")
        sys.exit(1)
    print("Validation: PASS (totals, 81 home, pair counts, one series per block,")
    print("  no double-booking, off days, all-star break, opening/final day,")
    print("  no repeat opponent in consecutive series)")
    if residual:
        print(f"note: {residual} homestand/road-trip runs exceed 3 series (soft target)")
    csv_path = args.csv or (f"{config.get('league_abbr', 'league').lower()}"
                            f"_blocks_{'ily' if config.get('inter_league') else 'iln'}.csv")
    write_csv(config, teams, placed, blocks, csv_path)
    print(f"block grid written to {csv_path}")

    lsdl_path = args.lsdl or f"{type_code(config, teams)}.lsdl"
    n_games = write_lsdl(config, teams, placed, lsdl_path)
    verify_lsdl(lsdl_path, n_games)
    print(f"{n_games} games written to {lsdl_path} (XML self-check OK)")


if __name__ == "__main__":
    main()
