# ootp-tools

Tools for running and following leagues in
[Out of the Park Baseball](https://www.ootpdevelopments.com/out-of-the-park-baseball/)
(OOTP), especially online leagues hosted on [statsplus](https://statsplus.net).

## Why this exists

OOTP can simulate a whole season in an afternoon and records everything, but
it doesn't tell you what was worth paying attention to. Commissioners and
players in online leagues end up doing that by hand, like picking the game
of the week, keeping track of records, or writing recaps.

The tools here are for that work. They:

- **Work from what the league publishes.** Most tools read the HTML reports
  OOTP uploads to statsplus, so anyone in a league can run them, not just the
  commissioner with the league file.
- **Write plain Markdown.** Reports can be read anywhere, pasted into a
  league forum or Discord, kept in git, or opened as an
  [Obsidian](https://obsidian.md) vault.
- **Are small command-line scripts.** Each tool is a Python script with few
  dependencies, one folder and its own README. Nothing to host.
- **Go easy on the servers they read from.** They cache every page they
  fetch, pause between requests, and don't download the same page twice.

## Tools

| Tool | What it does |
| --- | --- |
| [gotw](gotw/) | **Commissioner's Game of the Week.** Scores every game in a window of sim-days on a Watchability Index (closeness, lead changes, comebacks, clutch hits, star performances, no-hitters and other rare events) and writes a report crowning the Game of the Week and the Players of the Week. It can also score real-life games. |

More tools will be added over time.

## Getting started

Requires Python 3.9 or newer.

```sh
git clone https://github.com/VeryOddlySpecific/ootp-tools.git
cd ootp-tools/gotw
pip install -r requirements.txt
cp leagues.example.json leagues.json   # add your league's statsplus URL
python ootp_gotw.py --league <your-league-slug>
```

Each tool's README covers its setup and options in full.

## Repository layout

```
ootp-tools/
├── README.md          this file
└── <tool>/            one folder per tool
    ├── README.md      how to use it
    ├── *.py           the tool
    ├── requirements.txt
    └── tests/         run with: python -m unittest discover tests
```

Every tool stands on its own, with its own dependencies, config and tests,
so you can copy one folder out and use it without the rest. Personal config
(`leagues.json`) and generated output (`gamedata/`) are git-ignored.

## Contributing

Issues and pull requests are welcome, especially box-score formats from
OOTP versions or league setups the tools don't handle yet. When changing
how a tool scores or parses games, run its tests, and update the expected
values if the change is intended.

OOTP and Out of the Park Baseball are trademarks of Out of the Park
Developments. This project is not affiliated with or endorsed by them or by
statsplus.

## License

[MIT](LICENSE)
