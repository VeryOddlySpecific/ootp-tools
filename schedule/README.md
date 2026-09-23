# schedule — custom OOTP league schedules

Generates a custom 162-game schedule and writes it as an `.lsdl` file that
OOTP can import. It's for leagues whose structure doesn't match any of
OOTP's built-in schedules, such as two subleagues of different sizes, or a
specific number of games against division rivals.

Each schedule is checked before it's written, and a run that fails the
checks writes nothing. Checks include 162 games and 81 home games for every
team, the right number of games against each opponent, one series per team
per block, no team scheduled twice in a day, and no opponent seen in two
series in a row.

It uses only the Python standard library.

## Usage

```sh
cd schedule
python ootp_schedule.py --config examples/bbl_interleague.json
python ootp_schedule.py --config examples/bbl_no_interleague.json --seed 7
```

| Option | Default | Purpose |
| --- | --- | --- |
| `--config <path>` | required | League config (see below). |
| `--seed <n>` | 20260326 | Random seed. The same seed and config always give the same schedule. Try another seed for a different schedule, or if a run fails. |
| `--lsdl <path>` | `<type-code>.lsdl` | Where to write the schedule. |
| `--csv <path>` | `<abbr>_blocks_<iln\|ily>.csv` | Where to write the audit grid. |

A no-interleague schedule takes about a second. An interleague one can take
a minute or two.

### Output

- **`<type-code>.lsdl`**, the schedule. The filename follows OOTP's naming
  scheme and describes the league's structure, for example
  `ILY_BGN_G162_SL1_D1_T5_D2_T5_SL2_D1_T5_D2_T5_C_bbl.lsdl`
  (interleague, unbalanced, 162 games, two subleagues of 5+5 teams).
- **`<abbr>_blocks_<iln|ily>.csv`**, one row per block of days and one
  column per team, so you can read any team's season at a glance.
- **An audit printed to the console**, showing each team's games, home and
  away split, series lengths and longest homestand or road trip.

### Importing into OOTP

Copy the `.lsdl` file into OOTP's `data/schedules` folder, then pick it when
you create or edit the league's schedule. OOTP only accepts it if the
league's setup matches the file header: interleague on or off, unbalanced,
162 games, and the same number of teams in each division.

## Config

```json
{
  "league_name": "Bushwood Baseball League",
  "league_abbr": "BBL",
  "games_per_team": 162,
  "inter_league": true,
  "balanced_games": false,
  "default_game_time": "1905",
  "sunday_game_time": "1335",
  "calendar": {
    "start_month": 4,
    "start_day": 6,
    "start_day_of_week": 2,
    "season_days": 186,
    "allstar_game_day": 100,
    "allstar_break_days": [99, 100, 101, 102]
  },
  "subleagues": [
    {
      "name": "Ty Webb League",
      "games_vs_division_rival": 18,
      "games_vs_cross_division": 12,
      "interleague_games": 30,
      "divisions": [
        {
          "name": "The Links Division",
          "teams": [{"city": "Anchorage", "nickname": "Otters", "statsplus_id": 21}]
        }
      ]
    }
  ]
}
```

| Key | Meaning |
| --- | --- |
| `league_abbr` | Used in the output filenames. |
| `inter_league` | Whether the two subleagues play each other. |
| `default_game_time` / `sunday_game_time` | Start times as `HHMM`. Sunday games use the second. |
| `calendar.start_*` | Opening day. `start_day_of_week` runs 1 = Sunday to 7 = Saturday, and must be a Monday (2) or Thursday (5). |
| `calendar.season_days` | Length of the season in days, including off days. |
| `calendar.allstar_break_days` | Days with no games. They must fill one Monday–Thursday block. |
| `games_vs_division_rival` | Games against each team in the same division. |
| `games_vs_cross_division` | Games against each team in the other division of the same subleague. |
| `interleague_games` | Interleague games per team (interleague configs only). |
| `teams` | `city` and `nickname`. `statsplus_id` is for reference and isn't used. |

**Team IDs:** the `.lsdl` refers to teams by number. OOTP numbers teams
alphabetically by city within each division, going through the divisions and
subleagues in order. The tool does the same, so list divisions in the same
order as your OOTP league.

## What it supports

This isn't a general scheduler. It builds the MLB-style calendar the BBL
uses:

- A 186-day season: a 4-day opening block (Thursday–Sunday or
  Monday–Thursday), then weeks split into
  a Monday–Thursday block and a Friday–Sunday weekend series. That's 52
  playable blocks after the all-star break, with every team playing one
  series per block.
- Two subleagues, each with two equal divisions of **5 or 6 teams**.
- Series of 3 games where possible, with 4s and 2s to make the counts work.
  4-game series run Monday–Thursday. 3-game series run Tuesday–Thursday
  with Monday off, and 2-game series Tuesday–Wednesday.

The game counts in the config are load-bearing. They only work when each
team's number of series adds up to exactly 52 blocks. These combinations are
tested:

| Config | Teams | Division / cross-division / interleague games |
| --- | --- | --- |
| `bbl_no_interleague.json` | 6+6 and 5+5 | 18 / 12 / – and 23 / 14 / – |
| `bbl_interleague.json` | 5+5 and 5+5 | 18 / 12 / 30 for both |

The code also handles a 6+6 vs 5+5 interleague league (13 / 12 / 25 and
18 / 12 / 30). Other sizes or counts stop with an error before anything is
written. Supporting them means working out new series patterns, which the
module docstring in `ootp_schedule.py` describes.

## Making a schedule for your league

1. **Check the shape fits.** Your league needs two subleagues, each split
   into two equal divisions of 5 or 6 teams, and a 162-game season. See
   *What it supports* above.
2. **Copy an example config.** Start from `examples/bbl_interleague.json`
   if your subleagues play each other, or `examples/bbl_no_interleague.json`
   if they don't. Save it under any name, such as `my_league.json`.
3. **Enter your teams.** Replace the subleague, division and team names.
   List subleagues and divisions in the same order as in your OOTP league,
   because that order decides the team numbers in the file. The order of
   teams within a division doesn't matter, since they're sorted by city.
4. **Set the game counts** to a tested combination from the table above.
   The counts for each subleague must add up to 162: games against division
   rivals × (division size − 1), plus cross-division games × division size,
   plus interleague games.
5. **Set the calendar.**
   - `start_month`, `start_day` and `start_day_of_week` are your opening
     day. It must be a Monday or a Thursday.
   - Keep `season_days` at 186. That's the length that gives 52 blocks.
   - `allstar_break_days` must be the four days of one Monday–Thursday week,
     counted from opening day as day 1, and `allstar_game_day` is one of
     them. The examples use days 110–113 (Thursday opener) and 99–102
     (Monday opener), which both fall in mid-July.
   - `default_game_time` and `sunday_game_time` are `HHMM` start times.
6. **Run it** and look for `Validation: PASS`. A note about homestands or
   road trips longer than 3 series is a soft target, not a failure.
7. **Read the CSV** to check each team's season. If you don't like a
   schedule, run again with a different `--seed`; each seed gives a
   different, valid schedule. Keep a note of the seed you use so you can
   make the same file again.
8. **Import the `.lsdl`** as described in *Importing into OOTP*.

Supporting other division sizes or game counts means changing the code: the
series patterns (`build_pair_shapes` and `build_pair_shapes_inter`) and how
matchups are grouped into blocks (`rounds_even` and `rounds_odd`). The
module docstring explains the math behind the current ones.

## Tests

```sh
python -m unittest discover tests
OOTP_TOOLS_SLOW=1 python -m unittest discover tests   # also runs the interleague example
```
