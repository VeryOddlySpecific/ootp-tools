# gotw — Commissioner's Game of the Week

Scans every game an OOTP league played over a window of sim-days, scores each
one on a **Watchability Index**, and writes a Markdown report crowning the
Game of the Week, with a linescore, turning points, Players of the Week,
honourable mentions and the whole week ranked.

It reads the HTML reports OOTP publishes to
[statsplus](https://statsplus.net), so it works for any league hosted there.
You don't need the league file or commissioner access.

```
==================================================================
COMMISSIONER'S GAME OF THE WEEK  (2050-10-15 to 2050-10-22)
  Toronto Beavers 11, San Diego Groot 10  (2050-10-16)
  Watchability Index 70.7: one-run game; 4 lead changes; comeback from 5 down
==================================================================
```

## Setup

Requires Python 3.9+.

```sh
cd gotw
pip install -r requirements.txt
cp leagues.example.json leagues.json   # then add your leagues
```

`leagues.json` maps a short league slug to its statsplus site:

```json
{
  "sdmb": {
    "site": "https://statsplus.net/sdmbootp/",
    "league_id": 100,
    "org": "Your Team Name",
    "aliases": ["sd"]
  }
}
```

| Key | Meaning |
| --- | --- |
| `site` | The league's statsplus base URL, without the `/reports` part. |
| `league_id` | The `NNN` in the site's `league_NNN_home.html` for the top level (usually 100). |
| `org` | Optional. Your own club, used by `--team mine`. |
| `aliases` | Optional. Other slugs that should map to this league. |

A league missing from `leagues.json` still works if its site is
`https://statsplus.net/<slug>ootp/` with league id 100. Otherwise, pass
`--site` and `--league-id`.

## Usage

```sh
python ootp_gotw.py --league sdmb                     # the last 7 sim-days
python ootp_gotw.py --league sdmb --days 3            # a Game of the Series
python ootp_gotw.py --league sdmb --end 2050-07-20    # an earlier week this season
python ootp_gotw.py --league sdmb --team mine         # your team's Game of the Week
python ootp_gotw.py --league sdmb --team groot        # any team, by any unique part of its name
```

| Option | Default | Purpose |
| --- | --- | --- |
| `--league <slug>` | required | League slug or alias from `leagues.json`. |
| `--days <n>` | 7 | Window length in sim-days. |
| `--end YYYY-MM-DD` | latest completed day | Last sim date in the window. Must be in the current season. |
| `--team <name>` | all games | Score only this team's games. `mine` uses `org` from `leagues.json`. |
| `--player-basis game\|week` | `game` | Rank players by their single best game, or by totals over the window. |
| `--top-players <n>` | 5 | Rows in the Players of the Week tables (0 hides them). |
| `--archive-top <n>` | 1 | Also save the full box score and game log of the top *n* games (0 disables). |
| `--site <url>` / `--league-id <n>` | from `leagues.json` | Override the league's site. |
| `--base-dir <path>` | this folder | Where `gamedata/` is written. |
| `--cache-dir <path>` | `gamedata/<league>/.cache` | Raw HTML cache. |
| `--delay <sec>` | 0.35 | Pause between live requests, to go easy on statsplus. |
| `--verbose` | off | Print per-game parse warnings. |

### Output

```
gamedata/<league>/gotw/<league>_gotw_<end>.md          the report
gamedata/<league>/gotw/<league>_gotw_<team>_<end>.md   a --team report
gamedata/<league>/games/<league>_game_<id>.md          archived games (--archive-top)
gamedata/<league>/<league>_index.md                    index of archived games
gamedata/<league>/.cache/                              raw HTML
```

The reports use YAML frontmatter and `[[wikilinks]]`, so a `gamedata/`
folder can be opened directly as an [Obsidian](https://obsidian.md) vault.

## How it scores games

Each game gets six components, and their sum is the Watchability Index:

| Component | Range | Rewards |
| --- | --- | --- |
| CLOSE | 0–25 | A close final, and staying close from the 4th inning on |
| DRAMA | 0–42 | Lead changes and ties (late ones count more), extra innings, walk-offs |
| COMEBACK | 0–20 | The biggest deficit the winner came back from |
| CLUTCH | 0–15 | Tying or go-ahead runs from the 7th inning on |
| STAR | 0–10 | 4-hit games, multi-homer games, 5+ RBI, dominant starts |
| SPECIAL | no cap | No-hitters, perfect games, cycles, triple plays, grand slams |

A typical game scores 15–35, 45+ is a real contender, and 70+ is special.
[SCORING.md](SCORING.md) explains every rule, how the components interact,
and what the rubric can't see. All the point values are in `WEIGHTS` near
the top of `ootp_gotw.py` if you want to tune them.

## Scoring real games

`historical.py` runs real games through the same rubric:

```sh
python historical.py            # list the bundled games
python historical.py ws2011g6   # 2011 World Series, Game 6: WI 80.4
python historical.py ws1993g4   # 1993 World Series, Game 4: WI 58.4
```

To add a game, add a `Game(...)` entry to `GAMES` with the linescore and
the scoring plays from any play-by-play source, such as
[Retrosheet](https://www.retrosheet.org). The file's docstring explains the
format.

## Customizing

### Your leagues

Everything league-specific lives in `leagues.json`, which is git-ignored, so
pulling updates never overwrites it. Add one entry per league, using any
short slug you like as its key. The slug is what you pass to `--league` and
what the output folders are named after. You can follow several leagues
from one install, and each gets its own folder under `gamedata/`.

To find the values for a league:

- **`site`**: open any page of the league's statsplus reports. The URL looks
  like `https://statsplus.net/<name>/reports/news/html/...`. Everything
  before `/reports` is the site.
- **`league_id`**: open the league's home page in those reports. The file is
  named `league_<id>_home.html`. This is usually 100 for the top league. If
  your league has minor leagues, use the top league's id.
- **`org`**: the full team name as it appears in box scores, like
  `San Diego Groot`.

The tool needs OOTP's standard HTML report layout (`reports/news/html/`
with `box_scores/` and `game_logs/`). A league that uploads its reports
somewhere other than statsplus should work with `--site` if it keeps that
layout.

### How games are scored

All the point values are in the `WEIGHTS` dict near the top of
`ootp_gotw.py`, grouped by component, with a comment on each. For example:

- To value late-inning drama more, raise `lead_change_very_late` and
  `walkoff`, or raise `swing_cap` so dramatic games can separate from each
  other.
- To make an individual performance count more, raise `star_cap` and the
  `star_*` values.
- To make no-hitters less decisive, lower `no_hitter` and `perfect_game`.

[SCORING.md](SCORING.md) explains what each value does. After changing any
of them, run the tests: the golden tests will show how the two real games'
scores moved. Update the expected numbers in `tests/test_gotw.py` if the
change is what you meant.

### How players are ranked

The Players of the Week ranking uses `PLAYER_WEIGHTS`, just below
`WEIGHTS`. Batters earn points for total bases, RBI, runs, walks and homers,
minus a small amount per out. Pitchers earn points for innings and
strikeouts and lose them for earned runs, walks and hits. Starters also gain
or lose a little depending on how far their Game Score was above or below
50. The comment in the code explains the
balance between hitters and pitchers, which is worth keeping in mind before
changing it.

### The report

The report is built in `write_report()` in `ootp_gotw.py`. Things you might
want to change there:

- **The title.** "Commissioner's Game of the Week" is the heading for a
  league-wide report.
- **How many honourable mentions** are listed (`games[1:min(len(games), 4)]`,
  so three).
- **Obsidian links.** The report links to other files with `[[wikilinks]]`,
  which show up as plain text outside Obsidian. Remove the lines that build
  them if you don't use Obsidian.

Other defaults: turning points are picked by `turning_points()` (three per
game), and the console marks games scoring 45 or more with `!` (in
`main()`). The defaults for window length, player rows and archiving are
command-line options, so you can change them per run instead of in the
code.

### Adding real games

Add a `Game(...)` entry to `GAMES` in `historical.py` (see its docstring).
If you add a test for it like the two in `tests/test_gotw.py`, it becomes
another fixed point for checking changes to the weights.

## How it finds games

statsplus has no list of games, so the tool works it out:

- The league home page links the most recent sim-day's box scores, which
  gives the newest game id.
- Game ids below that are in date order for the current season, so the tool
  binary-searches box-score dates to find the window and then fetches only
  those games (two pages each).
- Box-score files from **last season** stay on the server until OOTP
  overwrites them, so ids above the newest one can't be trusted. Cached
  pages are checked against the current year and fetched again if they're
  from a different season. This also means **past seasons can't be
  rebuilt**; run the tool while the games are still current.

Everything fetched is cached, so re-running over the same window is nearly
free. If statsplus ever blocks requests, open the box score and game log in
a browser, save them into `.cache/` as `game_box_<id>.html` and
`log_<id>.html`, and run again.

## Files

| File | Purpose |
| --- | --- |
| `ootp_gotw.py` | The tool: finding games, parsing, scoring and the report |
| `ootp_boxscore.py` | Fetching, caching and HTML-to-Markdown, shared with gotw. Also usable on its own: `python ootp_boxscore.py <box_score_url> --league <slug>` saves one game as Markdown |
| `historical.py` | Scores real-life games |
| `SCORING.md` | The Watchability Index in detail |
| `tests/` | `python -m unittest discover tests` |
