#!/usr/bin/env python3
"""
historical.py -- Score a real-life game with the Watchability Index.

ootp_gotw.py reads its games from OOTP box scores, but the scoring itself
(``score_game``) only needs a linescore and the list of scoring plays. This
module builds that input from a hand-entered game, so any real game with a
play-by-play (Retrosheet, Baseball-Reference) can be run through the same
rubric as your league's games.

Usage:
    python historical.py                 # list the bundled games
    python historical.py ws2011g6        # score one of them

Adding a game: append a Game(...) to GAMES. Only scoring plays are needed;
each is (inning, half, batter, text, runs, is_hit, is_hr) with half "T" or
"B". STAR needs the batting lines of anyone with a big day, and SPECIAL
events (no-hitters, cycles...) need the full box, so leave those out unless
the game had one.
"""

import sys
from collections import namedtuple

import ootp_gotw as gw

Game = namedtuple("Game", "key title away home away_line home_line "
                          "away_rhe home_rhe plays batting")


def bat(name, ab, h, rbi):
    """A batting line with just the fields STAR looks at."""
    return {"name": name, "ab": ab, "r": 0, "h": h, "rbi": rbi,
            "bb": 0, "k": 0}


GAMES = [
    Game(
        key="ws2011g6",
        title="2011 World Series, Game 6 (Oct 27, 2011)",
        away="Texas Rangers", home="St. Louis Cardinals",
        away_line=[1, 1, 0, 1, 1, 0, 3, 0, 0, 2, 0],
        home_line=[2, 0, 0, 1, 0, 1, 0, 1, 2, 2, 1],
        away_rhe=(9, 15, 2), home_rhe=(10, 13, 3),
        plays=[
            (1, "T", "J. Hamilton", "SINGLE, Kinsler scores", 1, True, False),
            (1, "B", "L. Berkman", "2-RUN HOME RUN", 2, True, True),
            (2, "T", "I. Kinsler", "DOUBLE, Gentry scores", 1, True, False),
            (4, "T", "M. Moreland", "Groundout, Cruz scores", 1, False, False),
            (4, "B", "R. Theriot", "Groundout, Berkman scores", 1, False, False),
            (5, "T", "M. Young", "Young scores on an error", 1, False, False),
            (6, "B", "Y. Molina", "Walk, bases loaded, Berkman scores", 1, False, False),
            (7, "T", "A. Beltre", "SOLO HOME RUN", 1, True, True),
            (7, "T", "N. Cruz", "SOLO HOME RUN", 1, True, True),
            (7, "T", "I. Kinsler", "SINGLE, Moreland scores", 1, True, False),
            (8, "B", "A. Craig", "SOLO HOME RUN", 1, True, True),
            (9, "B", "D. Freese", "TRIPLE, Pujols scores, Berkman scores", 2, True, False),
            (10, "T", "J. Hamilton", "2-RUN HOME RUN", 2, True, True),
            (10, "B", "R. Theriot", "Groundout, Descalso scores", 1, False, False),
            (10, "B", "L. Berkman", "SINGLE, Jay scores", 1, True, False),
            (11, "B", "D. Freese", "SOLO HOME RUN", 1, True, True),
        ],
        batting=([], []),
    ),
    Game(
        key="ws1993g4",
        title="1993 World Series, Game 4 (Oct 20, 1993)",
        away="Toronto Blue Jays", home="Philadelphia Phillies",
        away_line=[3, 0, 4, 0, 0, 2, 0, 6, 0],
        home_line=[4, 2, 0, 1, 5, 1, 1, 0, 0],
        away_rhe=(15, 18, 0), home_rhe=(14, 14, 0),
        plays=[
            (1, "T", "P. Molitor", "Walk, bases loaded, Henderson scores", 1, False, False),
            (1, "T", "T. Fernandez", "SINGLE, White scores, Carter scores", 2, True, False),
            (1, "B", "J. Eisenreich", "Walk, bases loaded, Dykstra scores", 1, False, False),
            (1, "B", "M. Thompson", "TRIPLE, three runs score", 3, True, False),
            (2, "B", "L. Dykstra", "2-RUN HOME RUN", 2, True, True),
            (3, "T", "T. Fernandez", "SINGLE, Olerud scores", 1, True, False),
            (3, "T", "P. Borders", "SINGLE, Molitor scores", 1, True, False),
            (3, "T", "D. White", "SINGLE, Fernandez scores, Butler scores", 2, True, False),
            (4, "B", "M. Duncan", "SINGLE, Dykstra scores", 1, True, False),
            (5, "B", "D. Daulton", "2-RUN HOME RUN", 2, True, True),
            (5, "B", "M. Thompson", "DOUBLE, Eisenreich scores", 1, True, False),
            (5, "B", "L. Dykstra", "2-RUN HOME RUN", 2, True, True),
            (6, "T", "R. Alomar", "SINGLE, White scores", 1, True, False),
            (6, "T", "T. Fernandez", "Groundout, Alomar scores", 1, False, False),
            (6, "B", "M. Thompson", "SINGLE, Hollins scores", 1, True, False),
            (7, "B", "D. Daulton", "Hit by pitch, bases loaded, Duncan scores", 1, False, False),
            (8, "T", "P. Molitor", "DOUBLE, Carter scores", 1, True, False),
            (8, "T", "T. Fernandez", "SINGLE, Olerud scores", 1, True, False),
            (8, "T", "R. Henderson", "SINGLE, Molitor scores, Fernandez scores", 2, True, False),
            (8, "T", "D. White", "TRIPLE, Borders scores, Henderson scores", 2, True, False),
        ],
        batting=(
            ([bat("T. Fernandez", 5, 3, 5), bat("D. White", 5, 2, 4)],
             "BATTING Home Runs: none"),
            ([bat("L. Dykstra", 5, 3, 4), bat("M. Thompson", 5, 3, 5)],
             "BATTING Home Runs: L. Dykstra 2 (2nd, 5th), D. Daulton (5th)"),
        ),
    ),
]


def build(game):
    """The parsed-game dict score_game() expects, from a Game."""
    innings = len(game.away_line)
    plays, half_ends = [], []
    away = home = 0
    for inning in range(1, innings + 1):
        for half in ("T", "B"):
            for (i, h, batter, text, runs, is_hit, is_hr) in game.plays:
                if (i, h) != (inning, half):
                    continue
                p = {"inning": i, "half": h, "batter": batter, "text": text,
                     "runs": runs, "is_hit": is_hit, "is_hr": is_hr,
                     "away_before": away, "home_before": home}
                if h == "T":
                    away += runs
                else:
                    home += runs
                p.update(away_after=away, home_after=home)
                plays.append(p)
            half_ends.append({"inning": inning, "half": half,
                              "away": away, "home": home})
    if (away, home) != (game.away_rhe[0], game.home_rhe[0]):
        raise ValueError(f"{game.key}: plays add up to {away}-{home}, "
                         f"linescore says {game.away_rhe[0]}-"
                         f"{game.home_rhe[0]}")

    batting = []
    for side in game.batting or ([], []):
        batters, notes = side if isinstance(side, tuple) else (side, "")
        batting.append({"batters": batters, "notes": notes})
    return {
        "away": game.away, "home": game.home,
        "line": {"innings": innings,
                 "away": game.away_line, "home": game.home_line,
                 "away_rhe": game.away_rhe, "home_rhe": game.home_rhe},
        "plays": plays, "half_ends": half_ends,
        "away_final": game.away_rhe[0], "home_final": game.home_rhe[0],
        "batting": batting,
        "pitching": [{"pitchers": [], "game_scores": {}}] * 2,
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    by_key = {g.key: g for g in GAMES}
    if not argv or argv[0] not in by_key:
        print("Bundled games:")
        for g in GAMES:
            print(f"  {g.key:10s} {g.title}")
        return 0 if not argv else 1

    game = by_key[argv[0]]
    g = build(game)
    s = gw.score_game(g)
    print(f"{game.title}")
    print(f"Final: {gw.result_line(g)}"
          + (f" ({g['line']['innings']} innings)"
             if g["line"]["innings"] != 9 else ""))
    print(f"\nWatchability Index: {s['total']}")
    for k, v in s["components"].items():
        print(f"  {k:9s} {gw._fmt_pts(v):>5s}")
    if s["notes"]:
        print("\n" + "; ".join(s["notes"]))
    print("\nTurning points:")
    for p in gw.turning_points(g, s):
        line = gw.describe_play(g, p).replace("**", "").replace(" — ", " - ")
        print("  - " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
