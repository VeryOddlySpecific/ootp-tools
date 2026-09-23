#!/usr/bin/env python3
"""
ootp_boxscore.py -- Turn an OOTP (statsplus.net) box-score page into a Markdown
file containing the box score and the full game log.

Usage:
    python ootp_boxscore.py <box_score_url> --league <slug>

Output (relative to this script's directory):
    gamedata/<league>/games/<league>_game_<n>.md   one file per game
    gamedata/<league>/<league>_index.md            Obsidian index, links every game
    gamedata/<league>/.cache/                       raw HTML (hidden from Obsidian)

Each game file carries YAML frontmatter (league/game/date/home/away/tags) and a
wikilink back to the index, so the vault stays connected in Obsidian's graph.

Example:
    python ootp_boxscore.py \
        "https://statsplus.net/sdmbootp/reports/news/html/box_scores/game_box_467.html" \
        --league sdmb

Anti-bot / 429 fallback
-----------------------
statsplus can be aggressive about blocking automated requests. Two defences are
built in:

  1. Live requests use browser-like headers and retry with exponential backoff,
     honouring any ``Retry-After`` header the server sends.

  2. A local HTML cache. Every page that is fetched successfully is saved under
     the cache directory (``--cache-dir``, default = output dir) using its real
     filename, e.g. ``game_box_467.html`` and ``log_467.html``. If live fetching
     is ever walled off, just open the two pages in your browser, save them into
     the cache directory with those exact names, and re-run: the script reads the
     local files instead of hitting the network. Use ``--refresh`` to force a
     fresh download and overwrite the cache.

Only ``requests`` and ``beautifulsoup4`` are required.
"""

import argparse
import glob
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment, NavigableString


# Everything is written relative to the script's own directory:
#   gamedata/<league>/games/<league>_game_<n>.md   (one file per game)
#   gamedata/<league>/<league>_index.md            (Obsidian index / MOC)
#   gamedata/<league>/.cache/                       (raw HTML; dot-folder = hidden)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# Counterintuitively, statsplus 429s *browser-like* User-Agents (they get routed
# through bot-mitigation that expects JS/cookies) but serves plain tool clients
# the raw file. A curl-style UA sails straight through; the default
# python-requests UA is blocked. Empirically verified against game_box pages.
DEFAULT_HEADERS = {
    "User-Agent": "curl/8.4.0",
    "Accept": "*/*",
}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def cache_name_for(url):
    """Filename to cache a URL under (its last path segment)."""
    name = os.path.basename(urlparse(url).path)
    return name or "page.html"


def get_html(session, url, cache_dir, *, refresh=False, max_retries=4,
             timeout=30, referer=None):
    """Return the HTML for *url*.

    Reads from the cache file if it exists (unless --refresh), otherwise fetches
    live with retry/backoff and writes the result to the cache.
    """
    cache_path = os.path.join(cache_dir, cache_name_for(url))

    if not refresh and os.path.exists(cache_path):
        print(f"  cache hit : {cache_path}")
        with open(cache_path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")

    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer

    backoff = 2.0
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            print(f"  attempt {attempt}: network error: {exc}")
        else:
            if resp.status_code == 200:
                # OOTP pages are UTF-8, but the server omits the charset from the
                # HTTP header, so requests would default to ISO-8859-1 and mangle
                # accented names. Cache the raw bytes and decode as UTF-8.
                raw = resp.content
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, "wb") as fh:
                    fh.write(raw)
                print(f"  fetched   : {url} ({len(raw)} bytes) -> cached")
                return raw.decode("utf-8", errors="replace")

            last_error = f"HTTP {resp.status_code}"
            # Respect Retry-After for 429/503, else exponential backoff.
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if (retry_after or "").isdigit() else backoff
                print(f"  attempt {attempt}: {resp.status_code}, "
                      f"waiting {wait:.0f}s")
                time.sleep(wait)
                backoff *= 2
                continue
            print(f"  attempt {attempt}: HTTP {resp.status_code}")

        if attempt < max_retries:
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(
        f"Could not fetch {url} (last error: {last_error}).\n"
        f"  Fallback: open the page in your browser, save it as\n"
        f"    {cache_path}\n"
        f"  then re-run this command."
    )


# ---------------------------------------------------------------------------
# Cleaning + HTML -> Markdown
# ---------------------------------------------------------------------------

def strip_ignore_regions(soup):
    """Remove everything between OOTP IGNORE START / END comment markers.

    Works in document order with a suppression counter, so it is robust even
    when the markers live at different depths of the tree.
    """
    suppress = 0
    doomed = []
    for el in list(soup.descendants):
        if isinstance(el, Comment):
            text = el.strip()
            if text.startswith("OOTP IGNORE START"):
                suppress += 1
                doomed.append(el)
                continue
            if text.startswith("OOTP IGNORE END"):
                suppress = max(0, suppress - 1)
                doomed.append(el)
                continue
        if suppress > 0:
            doomed.append(el)
    for el in doomed:
        try:
            el.extract()
        except Exception:
            pass  # already removed as a child of an earlier extraction


def _cell_text(cell):
    """Normalised single-line text for a table cell."""
    text = cell.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text.replace("|", "\\|")


def table_to_markdown(table):
    """Render a *leaf* <table> as a GFM pipe table (or as plain lines if it is
    effectively a single column, e.g. a narrative block)."""
    grid = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        row = [_cell_text(c) for c in cells]
        if any(row):
            grid.append(row)
    if not grid:
        return ""

    ncols = max(len(r) for r in grid)
    if ncols < 2:
        # Not really tabular -- emit the text as paragraph lines.
        return "\n".join(r[0] for r in grid if r and r[0])

    for r in grid:
        r.extend([""] * (ncols - len(r)))

    header, body = grid[0], grid[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def html_to_markdown(html):
    """Convert one OOTP report page to Markdown."""
    soup = BeautifulSoup(html, "html.parser")

    # Drop non-content nodes.
    for tag in soup(["script", "style", "img", "link", "noscript"]):
        tag.decompose()

    strip_ignore_regions(soup)

    # The top nav bar is not inside an IGNORE region; it uses class "menu".
    for nav in soup.select(".menu"):
        nav.decompose()

    # Section titles -> h3 (h1/h2 are reserved for the file + the two sections).
    for title in soup.select(".boxtitle"):
        heading = title.get_text(" ", strip=True)
        title.replace_with(NavigableString(f"\n\n### {heading}\n\n"))

    # Render leaf tables (no nested table = real data, not layout scaffolding).
    for table in soup.find_all("table"):
        if table.find("table") is None:
            md = table_to_markdown(table)
            table.replace_with(NavigableString(f"\n\n{md}\n\n" if md else ""))

    body = soup.body or soup
    text = body.get_text()

    # Tidy whitespace: trim each line (leading tabs from HTML indentation can
    # otherwise trigger Markdown code blocks), collapse runs of blank lines.
    lines = [ln.strip() for ln in text.splitlines()]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def page_title(html):
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


# ---------------------------------------------------------------------------
# URL wrangling
# ---------------------------------------------------------------------------

def derive_game_log_url(box_url, box_html):
    """Find the game-log URL for a box score.

    Prefers the real link embedded in the page; falls back to the standard
    OOTP filename convention (game_box_N -> game_logs/log_N).
    """
    soup = BeautifulSoup(box_html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"game_logs/log_\d+\.html", href) or re.search(
            r"/log_\d+\.html$", href
        ):
            return urljoin(box_url, href)

    # Fallback: swap the path by convention.
    m = re.search(r"game_box_(\d+)\.html", box_url)
    if m:
        return urljoin(box_url, f"../game_logs/log_{m.group(1)}.html")

    raise RuntimeError("Could not determine the game-log URL from the box score.")


def game_number(box_url):
    m = re.search(r"game_box_(\d+)", box_url)
    return m.group(1) if m else "unknown"


# ---------------------------------------------------------------------------
# Obsidian frontmatter + index
# ---------------------------------------------------------------------------

# OOTP box-score <title> is consistently "<LEAGUE> Box Score, <away> at <home>,
# MM/DD/YYYY". Pull the pieces out so we can build frontmatter and a tidy index.
TITLE_RE = re.compile(
    r"Box Score,\s*(?P<away>.+?)\s+at\s+(?P<home>.+?),\s*"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s*$"
)


def parse_box_title(title):
    """Return {away, home, date_iso, date_us} from a box-score title, best-effort."""
    info = {"away": "", "home": "", "date_iso": "", "date_us": ""}
    m = TITLE_RE.search(title or "")
    if m:
        info["away"] = m.group("away").strip()
        info["home"] = m.group("home").strip()
        info["date_us"] = m.group("date")
        mm, dd, yyyy = m.group("date").split("/")
        info["date_iso"] = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return info


def build_frontmatter(league, gnum, info):
    """YAML frontmatter block (Obsidian properties) for a game file."""
    lines = ["---", f"league: {league}"]
    if gnum.isdigit():
        lines.append(f"game: {gnum}")
    if info["date_iso"]:
        lines.append(f"date: {info['date_iso']}")
    if info["away"]:
        lines.append(f'away: "{info["away"]}"')
    if info["home"]:
        lines.append(f'home: "{info["home"]}"')
    lines.append(f"tags: [ootp, ootp/{league}]")
    lines.append("---")
    return "\n".join(lines)


def read_game_meta(path):
    """Extract the fields the index needs from a generated game file.

    Reads the YAML frontmatter we wrote; falls back to the title/filename so any
    stray .md in games/ still lists sensibly.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    meta = {"stem": stem, "game": None, "away": "", "home": "", "date": ""}

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    # Frontmatter (only the simple `key: value` lines we emit).
    if text.startswith("---"):
        block = text.split("---", 2)
        if len(block) >= 3:
            for line in block[1].splitlines():
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                meta_key = key.strip()
                val = val.strip().strip('"')
                if meta_key == "game" and val.isdigit():
                    meta["game"] = int(val)
                elif meta_key in ("away", "home", "date"):
                    meta[meta_key] = val

    # Fallbacks.
    if meta["game"] is None:
        fm = re.search(r"_game_(\d+)", stem)
        if fm:
            meta["game"] = int(fm.group(1))
    if not (meta["away"] and meta["home"]):
        tm = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        if tm:
            info = parse_box_title(tm.group(1))
            meta["away"] = meta["away"] or info["away"]
            meta["home"] = meta["home"] or info["home"]
            meta["date"] = meta["date"] or info["date_iso"]
    return meta


def rebuild_index(league, league_dir, games_dir, index_stem):
    """Regenerate <league>_index.md from every game file in games_dir."""
    paths = sorted(
        p for p in glob.glob(os.path.join(games_dir, "*.md"))
        if os.path.basename(p) != f"{index_stem}.md"
    )
    metas = [read_game_meta(p) for p in paths]
    # Sort by game number (chronological); unknowns sink to the bottom.
    metas.sort(key=lambda m: (m["game"] is None, m["game"] or 0))

    rows = ["| Game | Matchup | Date |", "| ---: | --- | --- |"]
    for m in metas:
        gnum = m["game"] if m["game"] is not None else "?"
        matchup = f"{m['away']} @ {m['home']}".strip(" @") or m["stem"]
        # The alias pipe must be escaped as \| inside a table cell, otherwise
        # Obsidian reads it as a column separator and the link breaks apart.
        rows.append(f"| [[{m['stem']}\\|{gnum}]] | {matchup} | {m['date']} |")

    body = "\n".join([
        "---",
        f"league: {league}",
        f"tags: [ootp, ootp/{league}, index]",
        "---",
        "",
        f"# {league.upper()} — Game Index",
        "",
        f"{len(metas)} game{'s' if len(metas) != 1 else ''} recorded.",
        "",
        "\n".join(rows),
        "",
    ])

    index_path = os.path.join(league_dir, f"{index_stem}.md")
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return index_path, len(metas)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a Markdown box score + game log from an OOTP report URL."
    )
    parser.add_argument("url", help="URL of the OOTP box-score page.")
    parser.add_argument("--league", required=True,
                        help="League slug (e.g. sdmb). Sets the gamedata/<league> tree.")
    parser.add_argument("--base-dir", default=SCRIPT_DIR,
                        help="Root that holds gamedata/ (default: the script's directory).")
    parser.add_argument("--cache-dir", default=None,
                        help="Where raw HTML is cached (default: gamedata/<league>/.cache).")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore any cached HTML and fetch fresh copies.")
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)

    # gamedata/<league>/{games, .cache} + <league>_index.md
    league_dir = os.path.join(args.base_dir, "gamedata", args.league)
    games_dir = os.path.join(league_dir, "games")
    cache_dir = args.cache_dir or os.path.join(league_dir, ".cache")
    index_stem = f"{args.league}_index"
    os.makedirs(games_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    league_base = f"{urlparse(args.url).scheme}://{urlparse(args.url).netloc}/"

    session = requests.Session()

    print("Box score:")
    box_html = get_html(session, args.url, cache_dir, refresh=args.refresh,
                        max_retries=args.max_retries, timeout=args.timeout,
                        referer=league_base)

    log_url = derive_game_log_url(args.url, box_html)
    print("Game log:")
    log_html = get_html(session, log_url, cache_dir, refresh=args.refresh,
                        max_retries=args.max_retries, timeout=args.timeout,
                        referer=args.url)

    gnum = game_number(args.url)
    title = page_title(box_html) or f"{args.league.upper()} Game {gnum}"
    info = parse_box_title(title)

    md_parts = [
        build_frontmatter(args.league, gnum, info),
        "",
        f"[[{index_stem}|← {args.league.upper()} game index]]",
        "",
        f"# {title}",
        "",
        f"- **League:** {args.league}",
        f"- **Box score:** {args.url}",
        f"- **Game log:** {log_url}",
        "",
        "## Box Score",
        "",
        html_to_markdown(box_html),
        "",
        "## Game Log",
        "",
        html_to_markdown(log_html),
        "",
    ]
    markdown = "\n".join(md_parts)

    out_name = f"{args.league}_game_{gnum}.md"
    out_path = os.path.join(games_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    print(f"\nWrote game : {out_path} ({len(markdown)} bytes)")

    index_path, count = rebuild_index(args.league, league_dir, games_dir, index_stem)
    print(f"Wrote index: {index_path} ({count} game(s) linked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
