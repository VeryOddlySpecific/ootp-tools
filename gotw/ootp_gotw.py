#!/usr/bin/env python3
"""
ootp_gotw.py -- Commissioner's Game of the Week for an OOTP league on
statsplus.net.

Scans every game the league played in a window of sim-days (default: the last
7, ending at the most recent completed day), scores each one on watchability,
and writes a Markdown report crowning the Commissioner's Game of the Week.

Usage:
    python ootp_gotw.py --league <slug> [--days 7] [--end YYYY-MM-DD]
                        [--team <name>|mine] [--player-basis game|week]
                        [--top-players 5]

Output (relative to this script's directory, or --base-dir):
    gamedata/<league>/gotw/<league>_gotw_<end>.md   the report
    gamedata/<league>/gotw/<league>_gotw_<team>_<end>.md  the --team report
    gamedata/<league>/games/<league>_game_<n>.md    winner archived via
                                                    ootp_boxscore (--archive-top)
    gamedata/<league>/.cache/                       raw HTML, shared with
                                                    ootp_boxscore

How games are discovered
------------------------
statsplus serves OOTP's raw HTML reports. There is no directory listing, but:

  * ``leagues/league_<id>_home.html`` links the box scores of the most recent
    completed sim-day -- that gives the current *frontier* game id.
  * Below the frontier, game ids are chronological this season (each sim-day's
    games get consecutive ids), so the tool binary-searches box-score dates to
    find the id range covering the window, then fetches exactly those games.
  * Above the frontier, files from *last* season linger on the server (OOTP
    overwrites them as the new season progresses), which is why "probe until
    404" would be wrong. Cached pages whose date-year mismatches the frontier
    year are re-fetched for the same reason.

The Watchability Index
----------------------
Each game gets a component breakdown (weights in WEIGHTS, tune freely):

  CLOSE    0-25   final margin + how long the game stayed within reach
  DRAMA    0-42   lead changes and new ties (late ones count more),
                  extra innings, walk-off finish
  COMEBACK 0-20   largest deficit the eventual winner climbed out of
  CLUTCH   0-15   tying / go-ahead scoring plays from the 7th on
                  (two-out-style bonus for hits, later = bigger)
  STAR     0-10   individual gems: 4+ hit games, multi-HR, high pitcher
                  game scores, big strikeout counts
  SPECIAL  open   no-hitters, perfect games, cycles, grand slams, triple
                  plays, late no-hit bids... rare things that make a game
                  historic

The sum is the Watchability Index; highest index in the window wins.

``--team`` narrows all of that to one club: only their games are scored and
the report becomes their Game of the Week. ``--team mine`` resolves the
league's own org from the "org" key in leagues.json.

Players of the Week
-------------------
Every report also ranks the window's players with PLAYER_WEIGHTS -- hitters
on total bases, RBI, runs and walks net of outs; pitchers on innings and
strikeouts net of damage, plus credit for a start above a league-average
Game Score of 50. Under --team the leaderboards cover that team's players
only.

By default a row is a player's *single best game*, which is what a Game of
the Week report wants: the 4-for-6, two-homer night stands on its own instead
of being averaged away by the week's 0-for-4s. ``--player-basis week`` ranks
season-style totals over the window instead.

Requires ``requests`` and ``beautifulsoup4`` (same as ootp_boxscore.py, whose
fetching/caching/HTML-to-Markdown machinery this tool imports).
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, timedelta

import requests

import ootp_boxscore as ob

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Scoring weights -- the whole rubric in one place
# ---------------------------------------------------------------------------

WEIGHTS = {
    # CLOSE: points by final margin, plus up to `tightness_max` for the share
    # of half-innings (4th on) the game sat within a swing of tying.
    "margin": {1: 14.0, 2: 9.0, 3: 5.0, 4: 2.0},
    "tightness_max": 11.0,

    # DRAMA
    "lead_change": 3.0,          # 7th-8th inning: `late`, 9th+: `very_late`
    "lead_change_late": 5.0,
    "lead_change_very_late": 8.0,
    "new_tie": 1.5,
    "new_tie_late": 3.0,
    "new_tie_very_late": 5.0,
    "swing_cap": 22.0,           # cap on lead-change + tie points
    "extra_inning": 3.0,         # per inning beyond the 9th
    "extra_cap": 9.0,
    "walkoff": 8.0,
    "walkoff_hr_bonus": 3.0,

    # COMEBACK: 3 * (deficit - 1) once the winner has trailed by 2+, cap 20.
    "comeback_per_run": 3.0,
    "comeback_cap": 20.0,

    # CLUTCH (7th inning on, tying or go-ahead scoring plays)
    "clutch_base": 2.0,
    "clutch_per_inning": 1.0,    # per inning past the 7th, capped at +3
    "clutch_hit_bonus": 1.0,
    "clutch_cap": 15.0,

    # STAR (capped)
    "star_cap": 10.0,
    "star_4hits": 2.0,
    "star_5hits": 4.0,
    "star_2hr": 2.0,
    "star_5rbi": 3.0,
    "star_gamescore_85": 4.0,
    "star_gamescore_90": 6.0,
    "star_k12": 2.0,

    # SPECIAL (uncapped)
    "no_hitter": 25.0,
    "perfect_game": 50.0,
    "combined_no_hitter": 12.0,
    "late_no_hit_bid": 8.0,      # first hit allowed in the 8th or later
    "cycle": 8.0,
    "grand_slam": 3.0,
    "triple_play": 6.0,
    "three_hr_game": 6.0,
    "k15": 5.0,
    "steal_of_home": 4.0,
    "inside_the_park": 4.0,
}


# ---------------------------------------------------------------------------
# Player-of-the-Week weights -- the second rubric, same idea as WEIGHTS
# ---------------------------------------------------------------------------

PLAYER_WEIGHTS = {
    # Batting: total bases carry it, with credit for driving in and scoring
    # runs and a small tax on outs so 1-for-5 cannot outrank 2-for-3.
    "bat_total_base": 1.0,
    "bat_rbi": 0.8,
    "bat_run": 0.7,
    "bat_walk": 0.4,
    "bat_hr": 1.0,                # on top of the homer's four total bases
    "bat_out": -0.35,

    # Pitching: innings and strikeouts up, damage down, plus a light nudge
    # for a start that beat a league-average Game Score of 50 (starters only
    # -- OOTP only prints Game Score for them). The nudge stays small
    # because Game Score already folds in the same innings, strikeouts and
    # damage counted above; it is a tiebreaker, not a second helping. The
    # whole block is scaled so an ace's two-start week and a hitter's hot
    # week land in the same range instead of pitchers owning every top spot.
    "pit_inning": 1.0,
    "pit_k": 0.7,
    "pit_er": -2.0,
    "pit_walk": -0.4,
    "pit_hit": -0.3,
    "pit_gamescore_over_50": 0.15,
}


# ---------------------------------------------------------------------------
# Fetching (thin wrapper over ootp_boxscore's cache-aware get_html)
# ---------------------------------------------------------------------------

class Fetcher:
    """Cache-aware fetcher with a politeness delay on live requests."""

    def __init__(self, cache_dir, delay=0.35, max_retries=4, timeout=30,
                 referer=None):
        self.session = requests.Session()
        self.cache_dir = cache_dir
        self.delay = delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.referer = referer
        self.live_fetches = 0

    def get(self, url, refresh=False):
        cached = os.path.exists(
            os.path.join(self.cache_dir, ob.cache_name_for(url)))
        if refresh or not cached:
            if self.live_fetches:
                time.sleep(self.delay)
            self.live_fetches += 1
        return ob.get_html(self.session, url, self.cache_dir, refresh=refresh,
                           max_retries=self.max_retries, timeout=self.timeout,
                           referer=self.referer)


# ---------------------------------------------------------------------------
# Discovery: frontier + date bisection over game ids
# ---------------------------------------------------------------------------

def frontier_game_ids(fetcher, site):
    """Game ids linked from the league home page (most recent sim-day)."""
    url = site["home_url"]
    html = fetcher.get(url, refresh=True)  # changes every sim-day
    ids = sorted({int(m) for m in re.findall(r"game_box_(\d+)\.html", html)})
    if not ids:
        raise RuntimeError(
            f"No box-score links on {url} -- is the league between seasons? "
            f"Pass --end for a historical window once games exist."
        )
    return ids


class GamePages:
    """Fetches box pages by id and memoises the parsed titles/dates."""

    def __init__(self, fetcher, site, expect_year=None):
        self.fetcher = fetcher
        self.site = site
        self.expect_year = expect_year
        self._meta = {}

    def box_url(self, gid):
        return f"{self.site['box_base']}game_box_{gid}.html"

    def meta(self, gid):
        """{'title', 'away', 'home', 'date' (date|None), 'html'} for a game id."""
        if gid in self._meta:
            return self._meta[gid]
        html = self.fetcher.get(self.box_url(gid))
        title = ob.page_title(html)
        info = ob.parse_box_title(title)
        d = _iso_to_date(info["date_iso"])
        # A cached page can be last season's leftovers (same filename, older
        # date). If the year disagrees with the frontier's, trust the server.
        if d and self.expect_year and d.year != self.expect_year:
            html = self.fetcher.get(self.box_url(gid), refresh=True)
            title = ob.page_title(html)
            info = ob.parse_box_title(title)
            d = _iso_to_date(info["date_iso"])
        m = {"title": title, "away": info["away"], "home": info["home"],
             "date": d, "html": html}
        self._meta[gid] = m
        return m

    def date_near(self, gid, lo=1):
        """Date for gid, walking down a few ids if the title won't parse."""
        for g in range(gid, max(lo, gid - 6) - 1, -1):
            d = self.meta(g)["date"]
            if d:
                return d
        return None


def _iso_to_date(iso):
    if not iso:
        return None
    try:
        y, m, d = iso.split("-")
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def find_window_ids(pages, frontier_id, cutoff, end):
    """[lo, hi] game-id range whose dates fall inside [cutoff, end].

    Game ids below the frontier are chronological, so two binary searches
    bracket the window; the caller still date-filters the final list.
    """
    # Smallest id with date >= cutoff.
    lo, hi = 1, frontier_id
    while lo < hi:
        mid = (lo + hi) // 2
        d = pages.date_near(mid)
        if d is None or d < cutoff:
            lo = mid + 1
        else:
            hi = mid
    first = lo

    # Largest id with date <= end.
    lo, hi = first, frontier_id
    while lo < hi:
        mid = (lo + hi + 1) // 2
        d = pages.date_near(mid)
        if d is not None and d <= end:
            lo = mid
        else:
            hi = mid - 1
    last = lo

    return first, last


# ---------------------------------------------------------------------------
# Parsing one game (box score + game log Markdown)
# ---------------------------------------------------------------------------

ORDINAL_RE = r"\d+(?:st|nd|rd|th)"

SUMMARY_RE = re.compile(
    r"^\|\s*(Top|Bottom) of the (\d+)(?:st|nd|rd|th) over - "
    r"(\d+) runs?, (\d+) hits?, (\d+) errors?, (\d+) left on base;\s*"
    r"(.+?)\s+(\d+)\s*-\s*(.+?)\s+(\d+)\s*\|"
)

PLAY_RE = re.compile(
    r"^\|\s*(?:Batting|Pinch Hitting):\s*[A-Z]{3}\s+(.+?)\s*\|\s*(.*?)\s*\|\s*$")

HIT_WORDS = ("SINGLE", "DOUBLE", "TRIPLE", "HOME RUN")

# Runs scored by a multi-run homer are in its label, not in "scores" text.
HR_RUNS = (("GRAND SLAM", 4), ("3-RUN", 3), ("2-RUN", 2))


def _runs_in_play(text):
    """Runs scored on one play, from every phrasing OOTP logs use.

    "X scores" (runners), "tags up, SCORES" (sac flies -- upper-case),
    "tries for Home, SAFE" (sent runners), "steals home", and the homer's
    own label (SOLO / 2-RUN / 3-RUN / GRAND SLAM covers the batter and
    everyone aboard). Half-inning totals are still reconciled against the
    log's own summary line, so an exotic phrasing can't corrupt the score.
    """
    runs = len(re.findall(r"\bscores\b", text, re.I))
    runs += text.count("tries for Home, SAFE")
    runs += len(re.findall(r"\bsteals home\b", text, re.I))
    if "HOME RUN" in text:
        for label, n in HR_RUNS:
            if label in text:
                runs += n
                break
        else:
            runs += 1
    return runs


def _tables_and_text(md):
    """Split Markdown into a flat list of ('table', rows) / ('text', line)."""
    out = []
    rows = []
    for line in md.splitlines():
        if line.startswith("|"):
            rows.append(line)
        else:
            if rows:
                out.append(("table", rows))
                rows = []
            if line.strip():
                out.append(("text", line.strip()))
    if rows:
        out.append(("table", rows))
    return out


def _cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def parse_linescore(box_md):
    """{'innings': int, 'away': [runs...], 'home': [...], 'away_rhe', 'home_rhe',
    'away_name_rec', 'home_name_rec', 'recap'} from the box-score Markdown."""
    blocks = _tables_and_text(box_md)
    for i, (kind, payload) in enumerate(blocks):
        if kind != "table" or len(payload) < 4:
            continue
        header = _cells(payload[0])
        if len(header) >= 5 and header[1] == "1" and header[-3:] == ["R", "H", "E"]:
            away = _cells(payload[2])
            home = _cells(payload[3])
            n_inn = len(header) - 4  # leading blank + R,H,E

            def runs(cells):
                vals = []
                for c in cells[1:1 + n_inn]:
                    vals.append(int(c) if c.isdigit() else None)  # None = 'X'
                return vals

            def rhe(cells):
                return tuple(int(x) for x in cells[1 + n_inn:4 + n_inn])

            # The OOTP recap paragraph is the first text block after the
            # linescore table.
            recap = ""
            for kind2, payload2 in blocks[i + 1:]:
                if kind2 == "table":
                    break
                if len(payload2) > 120:  # skip stray heading-ish lines
                    recap = payload2
                    break
            return {
                "innings": n_inn,
                "away": runs(away), "home": runs(home),
                "away_rhe": rhe(away), "home_rhe": rhe(home),
                "away_name_rec": away[0], "home_name_rec": home[0],
                "recap": recap,
            }
    raise ValueError("linescore table not found in box score")


def _notes_after(blocks, i, marker):
    """The `marker` notes line following the table at `blocks[i]`.

    Scans the text blocks between this table and the next one -- stopping at
    that table keeps one team's notes from being read as the other's -- and
    slices from `marker`, because OOTP runs the substitution legend ("a - S.
    Kokura pinch hit for E. Baker in the 8th") into the same line.
    """
    for kind, payload in blocks[i + 1:]:
        if kind == "table":
            return ""
        at = payload.find(marker)
        if at != -1:
            return payload[at:]
    return ""


def parse_batting(box_md):
    """Per-team batter rows + the BATTING notes line, in (away, home) order."""
    teams = []
    blocks = _tables_and_text(box_md)
    for i, (kind, payload) in enumerate(blocks):
        if kind != "table":
            continue
        header = _cells(payload[0])
        if header[:5] == ["Player", "AB", "R", "H", "RBI"]:
            batters = []
            for row in payload[2:]:
                c = _cells(row)
                if len(c) < 8 or c[0].startswith("Totals"):
                    continue
                try:
                    batters.append({
                        # Strip the position suffix, incl. "PH-DH"-style ones.
                        "name": re.sub(r"\s+[A-Z0-9]{1,3}(?:-[A-Z0-9]{1,3})*$",
                                       "", c[0]),
                        "ab": int(c[1]), "r": int(c[2]), "h": int(c[3]),
                        "rbi": int(c[4]), "bb": int(c[5]), "k": int(c[6]),
                    })
                except ValueError:
                    continue
            teams.append({"batters": batters,
                          "notes": _notes_after(blocks, i, "BATTING")})
    return teams[:2]


PITCH_DECOR_RE = re.compile(
    r"\s*(?:(W|L|SV|BS|H|HLD)\s*(?:\(\s*[\d)(, -]*\))?\s*)+$")


def parse_pitching(box_md):
    """Per-team pitcher rows + game scores, in (away, home) order."""
    teams = []
    blocks = _tables_and_text(box_md)
    for i, (kind, payload) in enumerate(blocks):
        if kind != "table":
            continue
        header = _cells(payload[0])
        if header[:5] == ["Player", "IP", "H", "R", "ER"]:
            pitchers = []
            for row in payload[2:]:
                c = _cells(row)
                if len(c) < 9:
                    continue
                raw = c[0]
                name = PITCH_DECOR_RE.sub("", raw).strip()
                try:
                    ip_whole, _, ip_frac = c[1].partition(".")
                    outs = int(ip_whole) * 3 + (int(ip_frac) if ip_frac else 0)
                    pitchers.append({
                        "name": name, "raw": raw, "outs": outs,
                        "h": int(c[2]), "r": int(c[3]), "er": int(c[4]),
                        "bb": int(c[5]), "k": int(c[6]),
                        "bf": int(c[8]) if c[8].isdigit() else None,
                    })
                except ValueError:
                    continue
            scores = {}
            seg = _notes_after(blocks, i, "PITCHING").split("Game Score:", 1)
            if len(seg) == 2:
                seg = seg[1].split("Batters Faced")[0].strip()
                for m in re.finditer(r"([^,]+?)\s+(\d+)\s*(?:,|$)", seg):
                    scores[m.group(1).strip()] = int(m.group(2))
            teams.append({"pitchers": pitchers, "game_scores": scores})
    return teams[:2]


def parse_game_notes(box_md):
    """Player of the Game / ballpark / attendance from the GAME NOTES block."""
    notes = {}
    m = re.search(r"### GAME NOTES\n(.*?)(?:\n##|\Z)", box_md, re.S)
    if not m:
        return notes
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip()]
    for i, line in enumerate(lines[:-1]):
        key = line.rstrip(":").strip().lower().replace(" ", "_")
        if line.endswith(":"):
            notes[key] = lines[i + 1]
    return notes


def parse_timeline(log_md, away_name, home_name, warnings):
    """Play-by-play score timeline from the game-log Markdown.

    Returns (plays, half_ends). Runs per play = mentions of "scores" plus one
    for the batter on a HOME RUN play; each half-inning is reconciled against
    the log's own "N runs" summary line, which is authoritative.
    """
    plays = []
    half_ends = []
    away = home = 0
    buffer = []  # plays since the last summary line

    for line in log_md.splitlines():
        if not line.startswith("|"):
            continue
        sm = SUMMARY_RE.match(line)
        if sm:
            half = "T" if sm.group(1) == "Top" else "B"
            inning = int(sm.group(2))
            stated_runs = int(sm.group(3))
            name_a, score_a = sm.group(7), int(sm.group(8))
            score_b = int(sm.group(10))
            # Summary lines list the away team first; swap only if the first
            # short name unambiguously belongs to the home team instead.
            s_away, s_home = score_a, score_b
            if home_name.startswith(name_a) and not away_name.startswith(name_a):
                s_away, s_home = score_b, score_a

            counted = sum(p["runs"] for p in buffer)
            if counted != stated_runs and buffer:
                warnings.append(
                    f"{half}{inning}: counted {counted} runs, log says "
                    f"{stated_runs}; trusting the log")
                # Push the correction onto the last scoring (or last) play.
                target = next((p for p in reversed(buffer) if p["runs"]), buffer[-1])
                target["runs"] += stated_runs - counted

            for p in buffer:
                p["inning"] = inning
                p["half"] = half
                p["away_before"], p["home_before"] = away, home
                if half == "T":
                    away += p["runs"]
                else:
                    home += p["runs"]
                p["away_after"], p["home_after"] = away, home
            plays.extend(buffer)
            buffer = []

            if (away, home) != (s_away, s_home):
                warnings.append(
                    f"{half}{inning}: running score {away}-{home} != "
                    f"log {s_away}-{s_home}; snapping to the log")
                away, home = s_away, s_home
            half_ends.append({"inning": inning, "half": half,
                              "away": away, "home": home})
            continue

        pm = PLAY_RE.match(line)
        if pm:
            batter, text = pm.group(1), pm.group(2)
            buffer.append({
                "batter": batter, "text": text, "runs": _runs_in_play(text),
                "is_hit": any(w in text for w in HIT_WORDS),
                "is_hr": "HOME RUN" in text,
            })
            continue

        # Non-batting rows (wild pitch between PAs, pickoffs...) can score too.
        if not line.startswith("| ---"):
            text = " ".join(_cells(line))
            runs = _runs_in_play(text)
            if runs:
                buffer.append({
                    "batter": "", "text": text, "runs": runs,
                    "is_hit": False, "is_hr": False,
                })

    if buffer:
        warnings.append(f"{len(buffer)} plays after the last inning summary")
    return plays, half_ends


def parse_game(gid, box_html, log_html, box_url, log_url):
    """Everything the scorer needs for one game."""
    warnings = []
    box_md = ob.html_to_markdown(box_html)
    log_md = ob.html_to_markdown(log_html)
    title = ob.page_title(box_html)
    info = ob.parse_box_title(title)

    line = parse_linescore(box_md)
    batting = parse_batting(box_md)
    pitching = parse_pitching(box_md)
    notes = parse_game_notes(box_md)
    plays, half_ends = parse_timeline(log_md, info["away"], info["home"],
                                      warnings)

    away_final, home_final = line["away_rhe"][0], line["home_rhe"][0]
    if half_ends and (half_ends[-1]["away"], half_ends[-1]["home"]) != \
            (away_final, home_final):
        warnings.append(
            f"timeline final {half_ends[-1]['away']}-{half_ends[-1]['home']} "
            f"!= linescore {away_final}-{home_final}")

    return {
        "gid": gid, "title": title,
        "away": info["away"], "home": info["home"],
        "date": _iso_to_date(info["date_iso"]), "date_iso": info["date_iso"],
        "box_url": box_url, "log_url": log_url,
        "line": line, "batting": batting, "pitching": pitching,
        "notes": notes, "plays": plays, "half_ends": half_ends,
        "away_final": away_final, "home_final": home_final,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _fmt_pts(x):
    return f"{x:g}" if x == int(x) else f"{x:.1f}"


def score_game(g):
    """Component scores + notable-moment strings for one parsed game."""
    W = WEIGHTS
    away_final, home_final = g["away_final"], g["home_final"]
    margin = abs(away_final - home_final)
    winner = "home" if home_final > away_final else "away"
    plays = g["plays"]
    notes = []

    # ---- CLOSE ----------------------------------------------------------
    close = W["margin"].get(margin, 0.0)
    late_ends = [h for h in g["half_ends"] if h["inning"] >= 4]
    if late_ends:
        credit = [max(0.0, 3 - abs(h["away"] - h["home"])) / 3 for h in late_ends]
        close += W["tightness_max"] * sum(credit) / len(credit)
    if margin == 1:
        notes.append("one-run game")

    # ---- DRAMA ----------------------------------------------------------
    swings = 0.0
    lead_changes = ties = 0
    last_sign = 0
    scored_yet = False
    for p in plays:
        if not p["runs"]:
            continue
        diff = p["home_after"] - p["away_after"]
        sign = (diff > 0) - (diff < 0)
        inning = p["inning"]
        if sign == 0 and scored_yet:
            ties += 1
            swings += (W["new_tie_very_late"] if inning >= 9 else
                       W["new_tie_late"] if inning >= 7 else W["new_tie"])
        elif sign != 0 and last_sign != 0 and sign != last_sign:
            lead_changes += 1
            swings += (W["lead_change_very_late"] if inning >= 9 else
                       W["lead_change_late"] if inning >= 7 else
                       W["lead_change"])
        elif sign != 0 and last_sign == 0 and scored_yet:
            # Breaking a tie is half a lead change.
            swings += 0.5 * (W["lead_change_very_late"] if inning >= 9 else
                             W["lead_change_late"] if inning >= 7 else
                             W["lead_change"])
        if sign != 0:
            last_sign = sign
        scored_yet = True
    drama = min(W["swing_cap"], swings)
    if lead_changes >= 3:
        notes.append(f"{lead_changes} lead changes")

    innings = g["line"]["innings"]
    if innings > 9:
        drama += min(W["extra_cap"], W["extra_inning"] * (innings - 9))
        notes.append(f"{innings} innings")

    walkoff = None
    if plays and winner == "home":
        final_play = plays[-1]
        if (final_play["half"] == "B" and final_play["runs"] > 0
                and final_play["home_before"] <= final_play["away_before"]):
            walkoff = final_play
            drama += W["walkoff"]
            if final_play["is_hr"]:
                drama += W["walkoff_hr_bonus"]
                notes.append("walk-off HR")
            else:
                notes.append("walk-off")

    # ---- COMEBACK -------------------------------------------------------
    max_deficit = 0
    for p in plays:
        w, l = ((p["home_after"], p["away_after"]) if winner == "home"
                else (p["away_after"], p["home_after"]))
        max_deficit = max(max_deficit, l - w)
    comeback = 0.0
    if max_deficit >= 2:
        comeback = min(W["comeback_cap"],
                       W["comeback_per_run"] * (max_deficit - 1))
        notes.append(f"comeback from {max_deficit} down")

    # ---- CLUTCH ---------------------------------------------------------
    clutch = 0.0
    clutch_plays = []
    for p in plays:
        if p["inning"] < 7 or not p["runs"]:
            continue
        if p["half"] == "T":
            before = p["away_before"] - p["home_before"]
            after = p["away_after"] - p["home_after"]
        else:
            before = p["home_before"] - p["away_before"]
            after = p["home_after"] - p["away_after"]
        if before <= 0 <= after:  # tied it up, or took the lead
            pts = (W["clutch_base"]
                   + min(3.0, W["clutch_per_inning"] * (p["inning"] - 7)))
            if p["is_hit"]:
                pts += W["clutch_hit_bonus"]
            clutch += pts
            clutch_plays.append(p)
    clutch = min(W["clutch_cap"], clutch)

    # ---- STAR -----------------------------------------------------------
    star = 0.0
    for side in g["batting"]:
        hr_seg = _notes_segment(side["notes"], "Home Runs:")
        for b in side["batters"]:
            if b["h"] >= 5:
                star += W["star_5hits"]
                notes.append(f"{b['name']} {b['h']}-for-{b['ab']}")
            elif b["h"] >= 4:
                star += W["star_4hits"]
                notes.append(f"{b['name']} {b['h']}-for-{b['ab']}")
            if b["rbi"] >= 5:
                star += W["star_5rbi"]
                notes.append(f"{b['name']} {b['rbi']} RBI")
            n_hr = _count_in_segment(hr_seg, b["name"])
            if n_hr == 2:
                star += W["star_2hr"]
                notes.append(f"{b['name']} 2 HR")
    for side in g["pitching"]:
        for p in side["pitchers"]:
            gs = side["game_scores"].get(p["name"])
            if gs is not None and gs >= 90:
                star += W["star_gamescore_90"]
                notes.append(f"{p['name']} game score {gs}")
            elif gs is not None and gs >= 85:
                star += W["star_gamescore_85"]
                notes.append(f"{p['name']} game score {gs}")
            if 12 <= p["k"] < 15:
                star += W["star_k12"]
                notes.append(f"{p['name']} {p['k']} K")
    star = min(W["star_cap"], star)

    # ---- SPECIAL --------------------------------------------------------
    special = 0.0
    for idx, side in enumerate(g["pitching"]):
        opp_hits = (g["line"]["home_rhe"][1] if idx == 0
                    else g["line"]["away_rhe"][1])
        if opp_hits == 0 and side["pitchers"]:
            if len(side["pitchers"]) == 1:
                p = side["pitchers"][0]
                if p["bf"] is not None and p["bf"] == p["outs"]:
                    special += W["perfect_game"]
                    notes.append(f"PERFECT GAME ({p['name']})")
                else:
                    special += W["no_hitter"]
                    notes.append(f"NO-HITTER ({p['name']})")
            else:
                special += W["combined_no_hitter"]
                notes.append("combined no-hitter")
    # Late no-hit bid: first hit off either side in the 8th or later.
    for half in ("T", "B"):
        first_hit = next((p for p in plays
                          if p["half"] == half and p["is_hit"]), None)
        hits = (g["line"]["away_rhe"][1] if half == "T"
                else g["line"]["home_rhe"][1])
        if first_hit and first_hit["inning"] >= 8 and hits > 0:
            special += W["late_no_hit_bid"]
            batting_team = g["away"] if half == "T" else g["home"]
            notes.append(
                f"no-hit bid vs {batting_team} into the {first_hit['inning']}th")

    for side in g["batting"]:
        d_seg = _notes_segment(side["notes"], "Doubles:")
        t_seg = _notes_segment(side["notes"], "Triples:")
        hr_seg = _notes_segment(side["notes"], "Home Runs:")
        for b in side["batters"]:
            if not b["name"]:
                continue
            n_hr = _count_in_segment(hr_seg, b["name"])
            if b["h"] >= 4 and b["name"] in d_seg and b["name"] in t_seg \
                    and n_hr:
                special += W["cycle"]
                notes.append(f"CYCLE ({b['name']})")
            if n_hr >= 3:
                special += W["three_hr_game"]
                notes.append(f"{b['name']} {n_hr} HR")
        special += W["grand_slam"] * len(
            re.findall(r"3 on", hr_seg))
    for side in g["pitching"]:
        for p in side["pitchers"]:
            if p["k"] >= 15:
                special += W["k15"]
                notes.append(f"{p['name']} {p['k']} K")

    log_blob = " ".join(p["text"] for p in plays)
    if re.search(r"triple play", log_blob, re.I):
        special += W["triple_play"]
        notes.append("triple play")
    if re.search(r"steals home", log_blob, re.I):
        special += W["steal_of_home"]
        notes.append("steal of home")
    if re.search(r"inside.the.park", log_blob, re.I):
        special += W["inside_the_park"]
        notes.append("inside-the-park HR")
    n_slam = len(re.findall(r"GRAND SLAM", log_blob))
    if n_slam:
        notes.append("grand slam" + (f" x{n_slam}" if n_slam > 1 else ""))

    components = {
        "CLOSE": round(close, 1), "DRAMA": round(drama, 1),
        "COMEBACK": round(comeback, 1), "CLUTCH": round(clutch, 1),
        "STAR": round(star, 1), "SPECIAL": round(special, 1),
    }
    total = round(sum(components.values()), 1)

    # De-duplicate notes, keep order.
    seen = set()
    notes = [n for n in notes if not (n in seen or seen.add(n))]

    return {
        "total": total, "components": components, "notes": notes,
        "winner": winner, "margin": margin,
        "lead_changes": lead_changes, "ties": ties,
        "max_deficit": max_deficit, "walkoff": walkoff,
        "clutch_plays": clutch_plays,
    }


def _count_in_segment(seg, name):
    """How many of an event a batter had, from a BATTING notes segment.

    OOTP lists multiples as e.g. "J. Rosas 2 (12, 5th Inning ...)" -- the
    count follows the name; a bare "(" after the name means one.
    """
    if not name or name not in seg:
        return 0
    m = re.search(re.escape(name) + r"\s+(\d+)\s*\(", seg)
    return int(m.group(1)) if m else 1


def _notes_segment(notes_line, label):
    """Slice a 'BATTING ...' notes line from `label` to the next known label."""
    if label not in notes_line:
        return ""
    seg = notes_line.split(label, 1)[1]
    for stop in ("Doubles:", "Triples:", "Home Runs:", "Total Bases:",
                 "2-out RBI:", "Runners left", "GIDP:", "Sac Bunt:",
                 "Sac Fly:", "SF:", "SH:", "HBP:", "IBB:", "Team LOB:",
                 "BASERUNNING", "FIELDING", "Hit by Pitch"):
        if stop != label and stop in seg:
            seg = seg.split(stop, 1)[0]
    return seg


# ---------------------------------------------------------------------------
# Players of the Week
# ---------------------------------------------------------------------------

def _total_bases(notes_line):
    """{batter: total bases} from a BATTING notes line.

    The segment reads "Total Bases: D. Silva , C. Schaefer 4 , P. Snider" --
    a bare name means one base, a trailing number means that many.
    """
    out = {}
    for entry in _notes_segment(notes_line, "Total Bases:").split(","):
        entry = entry.strip()
        if not entry:
            continue
        m = re.match(r"(.+?)\s+(\d+)$", entry)
        if m:
            out[m.group(1).strip()] = int(m.group(2))
        else:
            out[entry] = 1
    return out


def _fmt_ip(outs):
    return f"{outs // 3}.{outs % 3}"


def score_batter(e):
    W = PLAYER_WEIGHTS
    outs = max(0, e["ab"] - e["h"])
    return round(W["bat_total_base"] * e["tb"] + W["bat_rbi"] * e["rbi"]
                 + W["bat_run"] * e["r"] + W["bat_walk"] * e["bb"]
                 + W["bat_hr"] * e["hr"] + W["bat_out"] * outs, 1)


def score_pitcher(e):
    W = PLAYER_WEIGHTS
    return round(W["pit_inning"] * (e["outs"] / 3.0) + W["pit_k"] * e["k"]
                 + W["pit_er"] * e["er"] + W["pit_walk"] * e["bb"]
                 + W["pit_hit"] * e["h"]
                 + W["pit_gamescore_over_50"] * e["gs_bonus"], 1)


BAT_FIELDS = ("ab", "r", "h", "rbi", "bb", "k", "tb", "hr")
PIT_FIELDS = ("outs", "h", "r", "er", "bb", "k", "gs_bonus")


def player_game_lines(games, team=None):
    """One batting and one pitching line per player per game.

    Each line carries its game context (opponent, date, id) so a single
    performance can be named in the report. Batting and pitching tables come
    out of the box score in (away, home) order, which is how a line gets
    attributed to a team.
    """
    bat, pit = [], []
    for g, _ in games:
        for side, tname in enumerate((g["away"], g["home"])):
            if team and tname != team:
                continue
            ctx = {
                "team": tname,
                "opp": g["home"] if side == 0 else g["away"],
                "at": "@" if side == 0 else "vs",
                "gid": g["gid"], "date": g["date"],
                "date_iso": g["date_iso"], "g": 1,
            }
            if side < len(g["batting"]):
                bt = g["batting"][side]
                hr_seg = _notes_segment(bt["notes"], "Home Runs:")
                tb_map = _total_bases(bt["notes"])
                for b in bt["batters"]:
                    bat.append(dict(
                        ctx, name=b["name"],
                        ab=b["ab"], r=b["r"], h=b["h"], rbi=b["rbi"],
                        bb=b["bb"], k=b["k"],
                        hr=_count_in_segment(hr_seg, b["name"]),
                        # No notes line means no extra-base detail, so hits
                        # are the floor: every hit is at least a single.
                        tb=tb_map.get(b["name"], b["h"])))
            if side < len(g["pitching"]):
                pt = g["pitching"][side]
                for p in pt["pitchers"]:
                    gs = pt["game_scores"].get(p["name"])
                    pit.append(dict(
                        ctx, name=p["name"], outs=p["outs"], h=p["h"],
                        r=p["r"], er=p["er"], bb=p["bb"], k=p["k"],
                        best_gs=gs,
                        gs_bonus=0.0 if gs is None else float(gs - 50)))
    return bat, pit


def _merge_lines(lines, fields):
    """Sum per-game lines into one totals entry per (team, player)."""
    out = {}
    for e in lines:
        acc = out.get((e["team"], e["name"]))
        if acc is None:
            acc = out[(e["team"], e["name"])] = {
                "name": e["name"], "team": e["team"], "g": 0}
            acc.update({f: 0 for f in fields})
            if "best_gs" in e:
                acc["best_gs"] = None
        acc["g"] += 1
        for f in fields:
            acc[f] += e[f]
        if e.get("best_gs") is not None and (
                acc["best_gs"] is None or e["best_gs"] > acc["best_gs"]):
            acc["best_gs"] = e["best_gs"]
    return list(out.values())


def _best_line(lines, scorer):
    """Each player's single best game, one entry per (team, player)."""
    best = {}
    for e in lines:
        key = (e["team"], e["name"])
        if key not in best or scorer(e) > scorer(best[key]):
            best[key] = e
    return list(best.values())


def collect_players(games, team=None, basis="game"):
    """Rank the window's players, best-first by PLAYER_WEIGHTS.

    Returns (batters, pitchers). `team` restricts both lists to one club.
    `basis` picks what a row means:

      "game"  each player's single best performance -- the 4-for-6, two-homer
              night, which a week of totals would bury under the 0-for-4s
      "week"  each player's totals across the whole window
    """
    bat, pit = player_game_lines(games, team)
    if basis == "week":
        bat = _merge_lines(bat, BAT_FIELDS)
        pit = _merge_lines(pit, PIT_FIELDS)
    else:
        bat = _best_line(bat, score_batter)
        pit = _best_line(pit, score_pitcher)
    for e in bat:
        e["score"] = score_batter(e)
    for e in pit:
        e["score"] = score_pitcher(e)
    bat.sort(key=lambda e: (-e["score"], e["name"]))
    pit.sort(key=lambda e: (-e["score"], e["name"]))
    return bat, pit


def player_of_the_week(batters, pitchers):
    """('bat'|'pit', line) for the best player in the window, or None."""
    best = None
    for kind, rows in (("bat", batters), ("pit", pitchers)):
        if rows and (best is None or rows[0]["score"] > best[1]["score"]):
            best = (kind, rows[0])
    return best


def game_cell(e):
    """'Aug 12 @ Atlanta Black Crackers' -- when a line is one game."""
    when = _fmt_date(e["date"], year=False) if e["date"] else e["date_iso"]
    return f"{when} {e['at']} {e['opp']}"


def player_summary(kind, e):
    """The stat line behind a player's score, in one phrase."""
    if kind == "bat":
        bits = [f"{e['h']}-for-{e['ab']}"]
        if e["hr"]:
            bits.append(f"{e['hr']} HR")
        bits += [f"{e['rbi']} RBI", f"{e['r']} R"]
        if e["bb"]:
            bits.append(f"{e['bb']} BB")
        unit = "game"
    else:
        bits = [f"{_fmt_ip(e['outs'])} IP", f"{e['er']} ER", f"{e['k']} K"]
        if e["best_gs"] is not None:
            label = "Game Score" if "opp" in e else "best Game Score"
            bits.append(f"{label} {e['best_gs']}")
        unit = "appearance"
    line = ", ".join(bits)
    if "opp" in e:                      # a single game, so name it
        return f"{line} ({game_cell(e)})"
    return line + f" over {e['g']} {unit}" + ("s" if e["g"] != 1 else "")


def _table_head(cells):
    """Header + alignment rows, right-aligning everything but the labels."""
    sep = ["---" if c in ("Player", "Team", "Game") else "---:" for c in cells]
    return ["| " + " | ".join(cells) + " |",
            "| " + " | ".join(sep) + " |"]


def player_sections(batters, pitchers, top_n, team, basis):
    """The Players of the Week block, as report lines."""
    per_game = basis != "week"
    # One column tells you which week it was: the game for a single
    # performance, the appearance count for a set of totals.
    when = "Game" if per_game else "G"

    lines = []
    best = player_of_the_week(batters, pitchers)
    if best:
        kind, e = best
        who = e["name"] if team else f"{e['name']} ({e['team']})"
        lines += [
            f"## Player of the Week: {who}",
            "",
            f"**{player_summary(kind, e)}** — player score {e['score']}",
            "",
        ]

    if batters:
        lines += [
            "### Best hitting performances" if per_game else "### Top hitters",
            "",
        ]
        lines += _table_head(["#", "Player", "Team", when, "AB", "H", "HR",
                              "RBI", "R", "BB", "TB", "Score"])
        for rank, e in enumerate(batters[:top_n], 1):
            lines.append(
                f"| {rank} | {e['name']} | {e['team']} "
                f"| {game_cell(e) if per_game else e['g']} "
                f"| {e['ab']} | {e['h']} | {e['hr']} | {e['rbi']} "
                f"| {e['r']} | {e['bb']} | {e['tb']} "
                f"| **{_fmt_pts(e['score'])}** |")
        lines.append("")

    if pitchers:
        lines += [
            "### Best pitching performances" if per_game
            else "### Top pitchers",
            "",
        ]
        lines += _table_head(["#", "Player", "Team", when, "IP", "H", "ER",
                              "BB", "K", "GS" if per_game else "Best GS",
                              "Score"])
        for rank, e in enumerate(pitchers[:top_n], 1):
            gs = "—" if e["best_gs"] is None else str(e["best_gs"])
            lines.append(
                f"| {rank} | {e['name']} | {e['team']} "
                f"| {game_cell(e) if per_game else e['g']} "
                f"| {_fmt_ip(e['outs'])} | {e['h']} | {e['er']} "
                f"| {e['bb']} | {e['k']} | {gs} "
                f"| **{_fmt_pts(e['score'])}** |")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Turning points (for the report)
# ---------------------------------------------------------------------------

def turning_points(g, scored, top_n=3):
    """The plays that most swung the game, late-and-close weighted."""
    ranked = []
    for p in g["plays"]:
        if not p["runs"]:
            continue
        diff_before = abs(p["home_before"] - p["away_before"])
        lateness = 1.0 + 0.25 * max(0, p["inning"] - 5)
        closeness = 1.0 / (1.0 + diff_before)
        if p["half"] == "T":
            before = p["away_before"] - p["home_before"]
            after = p["away_after"] - p["home_after"]
        else:
            before = p["home_before"] - p["away_before"]
            after = p["home_after"] - p["away_after"]
        crossed = 1.5 if (before <= 0 and after >= 0) else 1.0
        ranked.append((p["runs"] * lateness * closeness * crossed, p))
    ranked.sort(key=lambda t: -t[0])
    picks = [p for _, p in ranked[:top_n]]
    if scored["walkoff"] and scored["walkoff"] not in picks:
        picks[-1:] = [scored["walkoff"]]
    picks.sort(key=lambda p: (p["inning"], p["half"] == "B",
                              g["plays"].index(p)))
    return picks


def describe_play(g, p):
    """One line for a play: half-inning, batter, trimmed action, new score."""
    half = "T" if p["half"] == "T" else "B"
    team = g["away"] if p["half"] == "T" else g["home"]
    action = p["text"]
    # Drop the pitch-by-pitch prefix: keep everything after the last count.
    m = list(re.finditer(r"\d-\d:\s*", action))
    if m:
        action = action[m[-1].end():]
    action = re.sub(r"\s+", " ", action).strip()
    who = f"{p['batter']} " if p["batter"] else ""
    return (f"**{half}{p['inning']}, {team}** — {who}{action} "
            f"({g['away']} {p['away_after']}, {g['home']} {p['home_after']})")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_date(d, year=True):
    """'June 6, 2050' without strftime's zero-padded day (%-d is not portable)."""
    base = f"{d.strftime('%B')} {d.day}"
    return f"{base}, {d.year}" if year else base


def result_line(g):
    if g["home_final"] > g["away_final"]:
        return (f"{g['home']} {g['home_final']}, {g['away']} "
                f"{g['away_final']}")
    return f"{g['away']} {g['away_final']}, {g['home']} {g['home_final']}"


def team_slug(name):
    """'San Diego Groot' -> 'san_diego_groot', for filenames."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def write_report(league, league_dir, games, start, end, days, archived,
                 team=None, top_players=5, basis="game"):
    """games = [(game, scored), ...] already sorted best-first.

    `team` scopes the whole report (and its filename) to one club; `basis`
    is what a Players of the Week row means (see collect_players).
    """
    gotw_dir = os.path.join(league_dir, "gotw")
    os.makedirs(gotw_dir, exist_ok=True)
    stem = f"{league}_gotw_"
    if team:
        stem += f"{team_slug(team)}_"
    path = os.path.join(gotw_dir, f"{stem}{end.isoformat()}.md")

    g, s = games[0]
    span = f"{_fmt_date(start, year=False)} – {_fmt_date(end)}"
    batters, pitchers = collect_players(games, team, basis)

    lines = [
        "---",
        f"league: {league}",
        "kind: gotw",
        f"window_start: {start.isoformat()}",
        f"window_end: {end.isoformat()}",
        f"days: {days}",
        f"games_scanned: {len(games)}",
        f"winner_game: {g['gid']}",
    ]
    if team:
        lines.append(f"team: {team}")
    if top_players > 0:
        lines.append(f"player_basis: {basis}")
    lines += [
        f"tags: [ootp, ootp/{league}, gotw]",
        "---",
        "",
        f"[[{league}_index|← {league.upper()} game index]]",
        "",
        (f"# 🏆 {team} Game of the Week — {span}" if team
         else f"# 🏆 Commissioner's Game of the Week — {span}"),
        "",
        f"{len(games)} " + (f"{team} games" if team else "games")
        + f" scanned, sim dates {start.isoformat()} to {end.isoformat()}.",
        "",
        f"## Game of the Week: {g['away']} at {g['home']}, "
        f"{_fmt_date(g['date'])}",
        "",
        f"**Final: {result_line(g)}"
        + (f" ({g['line']['innings']} innings)**" if g["line"]["innings"] != 9
           else "**"),
        "",
        f"**Watchability Index: {s['total']}** — " + (
            "; ".join(s["notes"]) if s["notes"] else "the best of a quiet week"),
        "",
    ]

    if g["line"]["recap"]:
        lines += [f"> {g['line']['recap']}", ""]

    # Linescore.
    n = g["line"]["innings"]
    header = "| |" + "|".join(str(i) for i in range(1, n + 1)) + "|R|H|E|"
    sep = "|" + "---|" * (n + 4)

    def ls_row(name, runs, rhe):
        cells = [("X" if r is None else str(r)) for r in runs]
        return ("| " + name + " |" + "|".join(cells)
                + f"|**{rhe[0]}**|{rhe[1]}|{rhe[2]}|")

    lines += [
        header, sep,
        ls_row(g["away"], g["line"]["away"], g["line"]["away_rhe"]),
        ls_row(g["home"], g["line"]["home"], g["line"]["home_rhe"]),
        "",
    ]

    tps = turning_points(g, s)
    if tps:
        lines += ["**Turning points:**", ""]
        lines += [f"- {describe_play(g, p)}" for p in tps]
        lines.append("")

    meta_bits = []
    notes = g["notes"]
    if notes.get("player_of_the_game"):
        meta_bits.append(f"Player of the Game: {notes['player_of_the_game']}")
    if notes.get("ballpark"):
        meta_bits.append(f"at {notes['ballpark']}")
    if notes.get("attendance"):
        meta_bits.append(f"attendance {notes['attendance']}")
    if meta_bits:
        lines += [" · ".join(meta_bits), ""]

    comp = games[0][1]["components"]
    lines += [
        "**Score breakdown:** "
        + " · ".join(f"{k} {_fmt_pts(v)}" for k, v in comp.items()),
        "",
    ]

    link_bits = []
    if g["gid"] in archived:
        link_bits.append(f"[[{league}_game_{g['gid']}|Full box score & game log]]")
    link_bits.append(f"[Box score]({g['box_url']})")
    link_bits.append(f"[Game log]({g['log_url']})")
    lines += [" · ".join(link_bits), ""]

    # Players of the Week.
    if top_players > 0:
        lines += player_sections(batters, pitchers, top_players, team, basis)

    # Honourable mentions.
    if len(games) > 1:
        lines += ["## Honourable mentions", ""]
        for game, sc in games[1:min(len(games), 4)]:
            why = "; ".join(sc["notes"][:4]) or "steady tension throughout"
            lines += [
                f"**{result_line(game)}** ({game['date_iso']}) — WI "
                f"{sc['total']} — {why} · [box]({game['box_url']})",
                "",
            ]

    # Full standings table.
    lines += [
        f"## {team}'s week, ranked" if team else "## The week, ranked",
        "",
        "| # | WI | Matchup | Final | Date | CLOSE | DRAMA | CMBK | CLUTCH "
        "| STAR | SPEC | Notable |",
        "| ---: | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: "
        "| ---: | --- |",
    ]
    for rank, (game, sc) in enumerate(games, 1):
        c = sc["components"]
        final = (f"{game['away_final']}-{game['home_final']}"
                 + ("" if game["line"]["innings"] == 9
                    else f" ({game['line']['innings']})"))
        matchup = f"[{game['away']} @ {game['home']}]({game['box_url']})"
        notable = "; ".join(sc["notes"][:3])
        lines.append(
            f"| {rank} | **{sc['total']}** | {matchup} | {final} "
            f"| {game['date_iso']} | {_fmt_pts(c['CLOSE'])} "
            f"| {_fmt_pts(c['DRAMA'])} | {_fmt_pts(c['COMEBACK'])} "
            f"| {_fmt_pts(c['CLUTCH'])} | {_fmt_pts(c['STAR'])} "
            f"| {_fmt_pts(c['SPECIAL'])} | {notable} |")
    lines.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path, batters, pitchers


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_league_config(league):
    """(canonical_slug, entry) from leagues.json next to this script.

    Accepts aliases (e.g. bbl -> bwb); returns the input slug and an empty
    entry when the league is unknown so callers fall back to the old
    statsplus.net/<slug>ootp/ + league-id-100 defaults.
    """
    path = os.path.join(SCRIPT_DIR, "leagues.json")
    if not os.path.exists(path):
        return league, {}
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    if league in cfg:
        return league, cfg[league]
    for slug, entry in cfg.items():
        if league in entry.get("aliases", ()):
            return slug, entry
    return league, {}


def my_org(league, lcfg):
    """The user's own club in `league` (leagues.json "org"), or None."""
    return lcfg.get("org") or None


def resolve_team(query, names):
    """Match `query` against the team names seen in the window.

    Exact (case-insensitive) first, then unique substring -- so "groot",
    "san diego" and "San Diego Groot" all land on the same club. Raises
    with the options listed when the query is ambiguous or unknown.
    """
    q = query.strip().lower()
    names = sorted(names)
    exact = [n for n in names if n.lower() == q]
    if exact:
        return exact[0]
    hits = [n for n in names if q in n.lower()]
    if len(hits) == 1:
        return hits[0]
    listing = "\n  ".join(names)
    if not hits:
        raise RuntimeError(
            f"--team {query!r} matched no team playing in this window. "
            f"Teams in the window:\n  {listing}")
    raise RuntimeError(
        f"--team {query!r} is ambiguous ({', '.join(hits)}). "
        f"Teams in the window:\n  {listing}")


def build_site(args, lcfg):
    base = (args.site or lcfg.get("site")
            or f"https://statsplus.net/{args.league}ootp/").rstrip("/")
    league_id = (args.league_id if args.league_id is not None
                 else lcfg.get("league_id", 100))
    root = f"{base}/reports/news/html/"
    return {
        "root": root,
        "home_url": f"{root}leagues/league_{league_id}_home.html",
        "box_base": f"{root}box_scores/",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score a window of OOTP games and crown the "
                    "Commissioner's Game of the Week.")
    parser.add_argument("--league", required=True,
                        help="League slug (e.g. sdmb) or an alias from "
                             "leagues.json (e.g. bbl, woba2). Sets "
                             "gamedata/<league> and the statsplus site URL.")
    parser.add_argument("--days", type=int, default=7,
                        help="Window length in sim-days (default 7).")
    parser.add_argument("--end", default=None,
                        help="Window end, YYYY-MM-DD sim date (default: most "
                             "recent completed day).")
    parser.add_argument("--team", default=None,
                        help="Only score this team's games, and crown their "
                             "Game of the Week. Matches a full name or any "
                             "unique part of one (e.g. 'groot'); 'mine' uses "
                             "the league's own org.")
    parser.add_argument("--player-basis", choices=("game", "week"),
                        default="game",
                        help="What a Players of the Week row means: 'game' "
                             "(default) ranks each player's single best "
                             "performance, 'week' their totals over the "
                             "window.")
    parser.add_argument("--top-players", type=int, default=5,
                        help="Rows in the Players of the Week leaderboards "
                             "(default 5; 0 hides the section).")
    parser.add_argument("--site", default=None,
                        help="League site base URL (default: from "
                             "leagues.json, else "
                             "https://statsplus.net/<league>ootp/).")
    parser.add_argument("--league-id", type=int, default=None,
                        help="OOTP league id in the report filenames "
                             "(default: from leagues.json, else 100 = the "
                             "top league).")
    parser.add_argument("--base-dir", default=SCRIPT_DIR,
                        help="Root that holds gamedata/ (default: the "
                             "script's directory).")
    parser.add_argument("--cache-dir", default=None,
                        help="Raw-HTML cache (default: "
                             "gamedata/<league>/.cache, shared with "
                             "ootp_boxscore).")
    parser.add_argument("--archive-top", type=int, default=1,
                        help="Write full game .md files (via ootp_boxscore) "
                             "for the top N games (default 1; 0 disables).")
    parser.add_argument("--delay", type=float, default=0.35,
                        help="Seconds between live fetches (politeness).")
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-game parse warnings.")
    args = parser.parse_args(argv)

    # Canonicalise the slug (bbl -> bwb, woba2 -> wwoba) so gamedata/ and
    # site defaults agree no matter which name was typed.
    args.league, lcfg = load_league_config(args.league.lower())

    league_dir = os.path.join(args.base_dir, "gamedata", args.league)
    cache_dir = args.cache_dir or os.path.join(league_dir, ".cache")
    os.makedirs(cache_dir, exist_ok=True)

    site = build_site(args, lcfg)
    fetcher = Fetcher(cache_dir, delay=args.delay,
                      max_retries=args.max_retries, timeout=args.timeout,
                      referer=site["root"])

    print("Finding the frontier (most recent completed sim-day)...")
    ids = frontier_game_ids(fetcher, site)
    frontier_id = max(ids)
    pages = GamePages(fetcher, site)
    # Fetch the frontier box fresh: a cached copy under the same filename
    # could be last season's, and we can't year-validate before knowing
    # the current year.
    fetcher.get(pages.box_url(frontier_id), refresh=True)
    frontier_meta = pages.meta(frontier_id)
    if not frontier_meta["date"]:
        raise RuntimeError(
            f"Could not parse a date from game {frontier_id}'s box score.")
    pages.expect_year = frontier_meta["date"].year
    league_abbr = frontier_meta["title"].split(" Box Score", 1)[0].strip()
    print(f"  frontier: game {frontier_id} on {frontier_meta['date']} "
          f"({league_abbr})")

    if args.end:
        end = _iso_to_date(args.end)
        if not end:
            parser.error(f"--end {args.end!r} is not YYYY-MM-DD")
        if end > frontier_meta["date"]:
            end = frontier_meta["date"]
        if end.year != frontier_meta["date"].year:
            parser.error(
                "--end is in a different season than the current one; old "
                "seasons are overwritten on the server and cannot be rebuilt.")
    else:
        end = frontier_meta["date"]
    start = end - timedelta(days=args.days - 1)
    print(f"  window  : {start} to {end} ({args.days} sim-days)")

    print("Locating the window's game ids (date bisection)...")
    first, last = find_window_ids(pages, frontier_id, start, end)
    print(f"  candidate ids {first}..{last} ({last - first + 1} games)")

    # Decide the window's games first (title metadata only), so --team can
    # be resolved against the real names and non-matching games never cost
    # a game-log fetch.
    in_window = []
    for gid in range(first, last + 1):
        meta = pages.meta(gid)
        if not meta["date"] or not (start <= meta["date"] <= end):
            continue
        if not meta["title"].startswith(league_abbr + " Box Score"):
            print(f"  skip {gid}: different league ({meta['title'][:40]})")
            continue
        in_window.append((gid, meta))

    if not in_window:
        print("No games found in the window; nothing to do.")
        return 1

    team = None
    if args.team:
        query = args.team
        from_org = args.team.lower() in ("mine", "me", "my", "self")
        if from_org:
            query = my_org(args.league, lcfg)
            if not query:
                parser.error(
                    f"--team mine: no org recorded for {args.league}. Add an "
                    f'"org" key to leagues.json or name the team directly.')
            print(f"  --team mine -> {query}")
        names = {n for _, m in in_window for n in (m["away"], m["home"])}
        try:
            team = resolve_team(query, names)
        except RuntimeError as exc:
            # A name that came from the org config is a real club, so it not
            # turning up means an off-week (or a playoff they missed), not a
            # typo worth a usage error.
            if from_org:
                print(f"{query} played no games between {start} and {end}; "
                      f"nothing to do. Try a longer --days.")
                return 1
            parser.error(str(exc))
        before = len(in_window)
        in_window = [(gid, m) for gid, m in in_window
                     if team in (m["away"], m["home"])]
        print(f"  team    : {team} -- {len(in_window)} of {before} games")

    games = []
    for gid, meta in in_window:
        box_url = pages.box_url(gid)
        try:
            log_url = ob.derive_game_log_url(box_url, meta["html"])
            log_html = fetcher.get(log_url)
            g = parse_game(gid, meta["html"], log_html, box_url, log_url)
            s = score_game(g)
        except Exception as exc:
            print(f"  skip {gid}: {exc}")
            continue
        games.append((g, s))
        flag = " !" if s["total"] >= 45 else ""
        print(f"  game {gid}: {result_line(g):50s} WI {s['total']:5.1f}{flag}")
        if args.verbose:
            for w in g["warnings"]:
                print(f"      warn: {w}")

    if not games:
        if team:
            print(f"{team} played no games in the window; nothing to do.")
        else:
            print("No games found in the window; nothing to do.")
        return 1

    games.sort(key=lambda t: (-t[1]["total"], -t[1]["components"]["DRAMA"],
                              -t[1]["components"]["CLOSE"], t[0]["gid"]))

    archived = set()
    for game, _ in games[:max(0, args.archive_top)]:
        print(f"\nArchiving game {game['gid']} via ootp_boxscore...")
        try:
            ob.main([game["box_url"], "--league", args.league,
                     "--base-dir", args.base_dir,
                     "--cache-dir", cache_dir,
                     "--max-retries", str(args.max_retries),
                     "--timeout", str(args.timeout)])
            archived.add(game["gid"])
        except Exception as exc:
            print(f"  archive failed (report still written): {exc}")
    # Wikilinks also resolve for games archived on earlier runs.
    games_dir = os.path.join(league_dir, "games")
    for game, _ in games:
        if os.path.exists(os.path.join(
                games_dir, f"{args.league}_game_{game['gid']}.md")):
            archived.add(game["gid"])

    path, batters, pitchers = write_report(
        args.league, league_dir, games, start, end, args.days, archived,
        team=team, top_players=args.top_players, basis=args.player_basis)

    g, s = games[0]
    heading = (f"{team.upper()} GAME OF THE WEEK" if team
               else "COMMISSIONER'S GAME OF THE WEEK")
    print(f"\n{'=' * 66}")
    print(f"{heading}  ({start} to {end})")
    print(f"  {result_line(g)}  ({g['date_iso']})")
    print(f"  Watchability Index {s['total']}: " + "; ".join(s["notes"][:5]))
    print(f"{'=' * 66}")
    print("\nTop of the table:")
    for rank, (game, sc) in enumerate(games[:5], 1):
        print(f"  {rank}. WI {sc['total']:5.1f}  {result_line(game)}"
              f"  ({game['date_iso']})")

    best = player_of_the_week(batters, pitchers)
    if args.top_players > 0 and best:
        kind, e = best
        who = e["name"] if team else f"{e['name']} ({e['team']})"
        print(f"\nPLAYER OF THE WEEK: {who}")
        print(f"  {player_summary(kind, e)}  [score {e['score']}]")
        for label, rows in (("Hitters", batters), ("Pitchers", pitchers)):
            if not rows:
                continue
            print(f"  {label}:")
            for rank, p in enumerate(rows[:min(3, args.top_players)], 1):
                tail = (f"  {game_cell(p)}" if args.player_basis == "game"
                        else f"  ({p['g']} G)")
                print(f"    {rank}. {p['score']:6.1f}  {p['name']}"
                      f"  ({p['team']}){tail}")

    print(f"\nWrote report: {path}")
    print(f"Live fetches: {fetcher.live_fetches}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
