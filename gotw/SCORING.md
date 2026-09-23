# The Watchability Index — what the six components mean

Every game in the scan window gets scored on six components; their sum is the
Watchability Index (WI), and the highest WI in the window is Game of the Week.
Each component answers one question a fan would ask about whether a game was
worth watching. All point values live in `WEIGHTS` in `ootp_gotw.py` and can
be tuned freely.

For reference: a typical WI is 15–35, a strong candidate is 45+ (the console
output flags these with `!`), and 70+ is a genuinely special game.

## CLOSE (0–25) — *was the game close, the whole way?*

Two parts:

- **Final margin** (up to 14): 1-run game = 14, 2 runs = 9, 3 = 5, 4 = 2,
  5+ = 0. A blowout final score zeroes this half no matter how it got there.
- **Tightness** (up to 11): for every half-inning from the 4th on, the game
  earns credit for how close the score stood when the half ended — full
  credit if tied, ⅔ for a 1-run gap, ⅓ for 2 runs, nothing at 3+. The 11
  points are scaled by the *average* credit, so this rewards games that
  stayed within reach for hours, not just ones that ended close.

The two parts are deliberately separate: a game can end 1-run close after a
late rally (margin points, little tightness) or sit tied all night before a
3-run 9th (tightness points, fewer margin points).

## DRAMA (0–42) — *did the game swing?*

Three parts, roughly "scoreboard chaos":

- **Lead changes and ties** (capped at 22): every time the lead flips or a
  new tie is created, points are added — and *when* it happens matters.
  A lead change is worth 3 in innings 1–6, 5 in the 7th–8th, 8 in the 9th
  or later; new ties are worth 1.5 / 3 / 5 on the same schedule. Breaking
  a tie (going from tied to ahead) counts as half a lead change.
- **Extra innings** (capped at 9): 3 points per inning beyond the 9th.
- **Walk-off** (8, +3 if it was a home run): the home team's final play
  won a game they were losing or tied in.

Nominal max is 22 + 9 + 11 = 42, though in practice a game rarely maxes all
three.

## COMEBACK (0–20) — *did the winner climb out of a hole?*

Tracks the largest deficit the *eventual winner* faced at any point. Down by
1 earns nothing (too routine); from 2 on, it's 3 points per run beyond the
first — down 2 = 3 pts, down 4 = 9, down 5 = 12, capped at 20 (a 7+ run
comeback). Note this is about the winner: a losing team's furious rally that
falls short shows up in CLOSE and DRAMA instead.

## CLUTCH (0–15) — *were the big moments late?*

Every scoring play from the 7th inning on that **tied the game or put the
batting team ahead** earns points: 2 base, +1 per inning past the 7th (so a
9th-inning play gets +2, capped at +3 for deep extras), +1 if the play was a
hit rather than a walk, error, or sac fly. Capped at 15.

This overlaps with DRAMA on purpose — a 9th-inning go-ahead hit is both a
scoreboard swing *and* a clutch moment — so late tension is deliberately
double-weighted relative to early scoring.

## STAR (0–10) — *did an individual have a monster game?*

Flat bonuses per performance, summed across both teams, capped at 10:

| Performance | Points |
| --- | ---: |
| 4-hit game | 2 |
| 5-hit game | 4 |
| Exactly 2 HR | 2 |
| 5+ RBI | 3 |
| Pitcher game score 85–89 | 4 |
| Pitcher game score 90+ | 6 |
| 12–14 strikeouts | 2 |

The low cap is intentional: a pitchers' duel or a slugger's day makes a game
*better*, but individual brilliance alone shouldn't out-rank a genuinely
dramatic team game. The truly historic versions of these feats (3+ HR,
15+ K) graduate to SPECIAL.

## SPECIAL (uncapped) — *did something rare happen?*

The only uncapped component — rare events should be able to force their way
into the report regardless of how the game otherwise scored:

| Event | Points |
| --- | ---: |
| Perfect game | 50 |
| No-hitter (solo) | 25 |
| Combined no-hitter | 12 |
| Late no-hit bid (first hit allowed in the 8th+) | 8 |
| Cycle | 8 |
| Triple play | 6 |
| 3+ HR by one batter | 6 |
| 15+ strikeouts | 5 |
| Steal of home | 4 |
| Inside-the-park HR | 4 |
| Grand slam | 3 each |

The tier order encodes rarity: a perfect game (50) essentially guarantees
Game of the Week; a no-hitter (25) beats all but the wildest slugfests; the
smaller items are tiebreaker-sized garnish.

## How they interact

CLOSE and DRAMA are the backbone — a taut, swingy game scores 40–60 on those
two alone (the SAHL debut winner, an 11-inning walk-off HR after a 2-run
comeback, hit 75.8). COMEBACK and CLUTCH reward *shape*: the same 5–4 final
scores far higher if the winning runs came late and from behind. STAR is a
garnish, and SPECIAL is the override switch for history.

## Calibration against real games

`historical.py` runs real games through the same rubric, and the tests pin
their scores:

| Game | WI | CLOSE | DRAMA | CMBK | CLUTCH | STAR | SPEC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2011 World Series, Game 6 — Cardinals 10, Rangers 9 (11) | 80.4 | 20.4 | 39.0 | 6 | 15 | 0 | 0 |
| 1993 World Series, Game 4 — Blue Jays 15, Phillies 14 | 58.4 | 18.9 | 15.5 | 12 | 4 | 8 | 0 |

Two games that are both remembered as classics land far apart, and the
reason is timing: 2011's swings came in the 9th, 10th and 11th, while 1993's
lead changes were mostly in the first three innings.

## Known blind spots

Things a fan would count that the rubric currently does not:

- **Count and outs.** A game-tying hit with two outs and two strikes scores
  the same as one with nobody out.
- **Stakes.** A Game 7 is scored like a Tuesday in May.
- **Rallies that fall short of the lead.** CLUTCH only credits plays that
  tie the game or take the lead, so the runs that cut a 5-run deficit to 1
  earn nothing on their own.
- **Blown leads.** Losing a big late lead shows up only indirectly, through
  COMEBACK for the other side.
- **Caps hide the very best games.** 2011 Game 6 would score about 100
  uncapped; DRAMA and CLUTCH both hit their ceilings.
- **The first run can be a "turning point."** Turning points boost plays
  that move a team from tied to ahead, and 0-0 counts as tied.
