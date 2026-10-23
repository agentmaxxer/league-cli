> [!NOTE]
> **This is a fork** of [hsukenooi/locg-cli](https://github.com/hsukenooi/locg-cli)
> (MIT, by hsukenooi), based on its final pre-archive commit. Upstream development
> moved into the [comic-pipeline](https://github.com/hsukenooi/comic-pipeline)
> monorepo. This fork keeps the standalone CLI and fixes the issues listed in
> [Fork changes](#fork-changes-kc-fixes) below. All credit for the original tool
> goes to the upstream author.

# locg

Command-line interface for [League of Comic Geeks](https://leagueofcomicgeeks.com). Browse releases, search series, manage your collection, pull list, wish list, and read list.

All output is JSON, making it easy to pipe into `jq` or other tools.

## Fork changes (kc-fixes)

Version `0.2.0+kc.1`. Every change below was tested against the live site.

- **Cloudflare, in the client instead of local patches.** API calls run as
  `fetch()` inside the cleared page (Playwright's `page.request` channel gets
  challenged even with a valid clearance). Headless runs send a normal Chrome
  user-agent; on Windows Chrome runs as a real window parked off-screen, since
  Cloudflare refuses hidden Chrome there. Warm-up waits out the Turnstile page
  instead of `networkidle`. Overrides: `LOCG_HEADLESS=0|1`, `LOCG_USER_AGENT`.
- **Challenge detection.** A Cloudflare page (403/503 or an interstitial) is
  detected, the clearance is re-warmed and the request retried once; if it is
  still challenged the command fails with a clear message and exit code 5
  instead of `{"error": "Expecting value..."}`. A non-JSON list reply is reported
  as such, never as an empty result.
- **Numeric titles.** `search` (and so `lookup`) found nothing for titles like
  "1776": League's list search ignores purely numeric titles. It now falls back
  to the site's `/search/series` page when the list search is empty.
- **Full variant lists.** `find` used to page the whole series by offset, which
  dropped entries on series with thousands of variants (Absolute Batman #19 lost
  its 616 Comics / Mike Mayhew covers). It now asks League for the issue directly
  (`title=#<issue>` scoped to the series), which returns every variant. `lookup`
  no longer sends `format[]=1`, which hid all variants from variant specs.
- **Printings.** League titles are `Series #1 <span>6th Printing</span>`; the old
  parser glued them into `#16th Printing`, so a reprint looked like issue 16.
  Rows now carry `full_title`, `issue_number`, `variant_name` and `printing`, and
  `find --printing N` selects the Nth printing. The old way (`--issue 16` for
  #1 6th printing) still works; those rows carry `matched_as`. Turn it off with
  `--no-legacy-printing`. `lookup "Series:1:2nd Printing"` works too.
- **`find` rows include list membership** (`lists`) when logged in. Use this to
  check a variant: `check <variant id>` reads `/comic/<id>`, and variant IDs
  there resolve to unrelated comics.
- **`find --variant` takes several words in any order** (`"mayhew red foil"`).
- **`batch` mode.** `locg batch FILE` (or stdin) runs many commands through one
  Chrome and one Cloudflare clearance, one JSON line per command, with a
  default 8 s gap between live calls.
- **TBD release dates.** Issues with no release date carry a negative timestamp
  that crashed `find`/`series`/`lookup` (`[Errno 22]` on Windows); the visible
  date text is returned instead.
- **Windows.** `collection_cache` works without `fcntl` and skips the directory
  `fsync` Windows cannot do.
- **Credentials.** `~/.config/locg/credentials.env` is read as well as `.env`,
  and `LOCG_USER`/`LOCG_PASS` are accepted as aliases.

## Installation

```bash
pip install .
# or
pipx install .
```

For development:

```bash
pip install -e ".[test]"
```

## Authentication

Some commands (collection, pull-list, wish-list, read-list, add, remove, update) require a League of Comic Geeks account.

```bash
locg login
```

This persists your session in `~/.config/locg/playwright-profile/`.

## Commands

### search

Search for comic series by title.

```bash
locg search "batman"
locg search "amazing spider-man" --pretty
```

### releases

View new comic releases for a given week. Defaults to the current week (Wednesday is new comic day).

```bash
locg releases
locg releases --date 2026-04-02 --pretty
```

### find

```bash
locg find --series-id 178012 --issue 19                      # issue 19, every variant
locg find --series-id 178012 --issue 19 --variant "mayhew red foil"
locg find --series-id 178012 --issue 1 --printing 6          # 6th printings of #1
locg find --series-id 178012 --issue 1 --printing 6 --exact  # plain 6th printing
```

### batch

```bash
cat > books.txt <<'TXT'
find --series-id 178012 --issue 19 --variant mayhew --fields id,full_title,lists
lookup "1776:1"
["add", "collection", "7943775", "--price", "12"]
TXT
locg batch books.txt            # or: ... | locg batch -
```

Each line is either shell-style arguments or a JSON array. Output is one JSON
object per line: `{"line": 1, "args": [...], "ok": true, "result": ...}`.
`--delay` sets the gap between live calls (default 8 s), `--stop-on-error`
stops at the first failure. `login` and nested `batch` are refused.

### marking read

```bash
locg add read <comic_id>      # mark as read
locg remove read <comic_id>   # unmark
```

### check

Check which lists a comic belongs to. Accepts one or more comic IDs (requires login).

```bash
locg check 9559460
locg check 9559460 8823401 --pretty
```

### lookup

Resolve LOCG comic IDs in batch from `Series:Issue[:Variant]` specs. Groups requests by series so each unique series is searched only once, then uses a title-filtered query against that series_id to pinpoint each issue (a small response per issue, no pagination dance through 140-issue pages). By default also fetches your collection once and reports `in_collection` per row; pass `--no-collection` to skip the auth + fetch.

```bash
locg lookup "Uncanny X-Men:185" "Batman:224" "Amazing Spider-Man:142" --pretty
locg lookup "Uncanny X-Men:179:Newsstand" --no-collection
locg lookup "Batman: The Long Halloween:9"   # series names with internal ":" are fine
```

Output per row:

```json
{
  "series_name": "Uncanny X-Men", "issue_number": "185", "variant": null,
  "series_id": 108806, "locg_id": 1081721, "locg_variant_id": null,
  "issue_name": "Uncanny X-Men #185", "in_collection": false,
  "from_cache": false
}
```

Resolved IDs are cached to disk at `$XDG_CACHE_HOME/locg/ids.json` (default `~/.cache/locg/ids.json`) and reused on subsequent runs — repeat lookups skip the API entirely (`from_cache: true`). Pass `--no-cache` to bypass for both reads and writes. Manage the cache with `locg cache stats` and `locg cache clear`.

Series matching prefers the canonical run (exact name, then preferred publisher, then oldest start year, then highest issue count), so `"Batman"` resolves to the 1940 DC run instead of a recent one-shot. If a series or issue can't be resolved, that row gets an `"error"` field; the rest of the batch still completes.

### cache

Inspect or clear the on-disk LOCG ID cache used by `lookup`. The cache is a single JSON file; entries never auto-expire because LOCG IDs are stable.

```bash
locg cache stats     # path, entry count, file size
locg cache clear     # delete every cached ID
```

### comic

Get full details for a specific comic by ID (publisher, price, creators, description, etc.).

```bash
locg comic 9559460
locg comic 9559460 --pretty
```

### series

Get a series overview and its issue list.

```bash
locg series 149498
locg series 149498 --pretty
```

### collection

View your collected comics (requires login). Filter by title with `--title`. Check if a specific title is in your collection with `has`.

```bash
locg collection --pretty
locg collection --title "batman" --pretty
locg collection has "Amazing Spider-Man #300"
```

### pull-list

View your pull list (requires login).

```bash
locg pull-list --pretty
```

### wish-list

View your wish list (requires login).

```bash
locg wish-list --pretty
```

### read-list

View your read list (requires login).

```bash
locg read-list --pretty
```

### add

Add a comic to a list (requires login). Lists: `pull`, `collection`, `wish`, `read`. When adding to `collection`, optionally record grade and price in the same step.

```bash
locg add collection 9559460
locg add pull 9559460
locg add collection 9559460 --grade 8.5 --price 390
```

`--grade` accepts CGC scale values: `0`, `0.1`, `0.3`, `0.5`, `1.0` ... `9.8`, `9.9`, `10.0`. Only valid for `collection`.

### remove

Remove a comic from a list (requires login). Lists: `pull`, `collection`, `wish`, `read`.

```bash
locg remove collection 9559460
locg remove wish 9559460
```

### update

Update grade, price, or condition notes on a comic already in your collection (requires login). At least one flag is required.

```bash
locg update 9559460 --grade 9.2
locg update 9559460 --price 500 --condition "white pages"
locg update 9559460 --grade 9.8 --price 450 --condition "off-white pages"
```

### login

Log in to League of Comic Geeks. Prompts for username and password.

```bash
locg login
```

## Global Flags

| Flag | Description |
|------|-------------|
| `--pretty` | Pretty-print JSON output with indentation |
| `--fields name,id` | Limit output to specific fields (works on any command) |
| `--debug` | Print HTTP request/response debug info to stderr |
| `--version` | Show version and exit |

## Output Format

All commands output JSON. Errors are written to stderr as `{"error": "message"}`.

Example output for `locg search "batman" --pretty`:

```json
[
  {
    "id": 149498,
    "name": "Batman",
    "publisher": "DC Comics",
    "start_year": 2016,
    "end_year": 2020,
    "issue_count": 150,
    "cover_url": "https://...",
    "url": "https://leagueofcomicgeeks.com/comics/series/149498/batman"
  }
]
```

## Configuration

Config and cookies are stored in `~/.config/locg/` (respects `XDG_CONFIG_HOME`).

## License

MIT

Original work by [hsukenooi](https://github.com/hsukenooi/locg-cli); fork changes under the same license. See [LICENSE](LICENSE).
