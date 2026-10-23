"""CLI entry point for locg."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
import time
from typing import Any, Callable, Optional

from dotenv import load_dotenv

from locg import __version__
from locg.client import AuthRequired, CloudflareChallenge, LOCGClient
from locg.config import credentials_env_path, env_path, wish_list_cache_path
from locg.commands import (
    VALID_LISTS,
    _validate_grade,
    _validate_price,
    cmd_add,
    cmd_cache_clear,
    cmd_cache_stats,
    cmd_check_lists,
    cmd_collection,
    cmd_collection_check,
    cmd_collection_doctor,
    cmd_collection_export,
    cmd_collection_has,
    cmd_collection_import,
    cmd_collection_record_win,
    cmd_collection_status,
    cmd_comic,
    cmd_find,
    cmd_login,
    cmd_lookup,
    cmd_pull_list,
    cmd_read_list,
    cmd_releases,
    cmd_remove,
    cmd_search,
    cmd_series,
    cmd_update,
    cmd_wish_list,
    cmd_wish_list_add,
    cmd_wish_list_from_cache,
    cmd_wish_list_remove,
    parse_lookup_spec,
)


class CLIError(Exception):
    """A command failed with a user-facing message (and optional partial result)."""

    def __init__(self, msg: str, code: int = 1, result: Any = None) -> None:
        super().__init__(msg)
        self.code = code
        self.result = result


def _fail(msg: str, code: int = 1, result: Any = None) -> None:
    raise CLIError(msg, code, result)


def die(msg: str, code: int = 1) -> None:
    """Print structured JSON error to stderr and exit."""
    json.dump({"error": msg}, sys.stderr)
    print(file=sys.stderr)
    sys.exit(code)


def _filter_fields(data: Any, fields: list[str]) -> Any:
    """Keep only the specified fields in dicts (or lists of dicts)."""
    if isinstance(data, list):
        return [_filter_fields(item, fields) for item in data]
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in fields}
    return data


def output(data: Any, pretty: bool = False, fields: list[str] | None = None) -> None:
    """Print JSON data to stdout."""
    if fields:
        data = _filter_fields(data, fields)
    if pretty:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(data, separators=(",", ":"), ensure_ascii=False))


def create_parser() -> argparse.ArgumentParser:
    # Shared flags available on all subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    common.add_argument("-v", "--verbose", action="store_true", help="Verbose output (INFO level)")
    common.add_argument("--debug", action="store_true", help="Debug output (DEBUG level, includes HTTP details)")
    common.add_argument("--fields", help="Comma-separated list of fields to include in output (e.g. --fields name,id)")

    parser = argparse.ArgumentParser(
        prog="locg",
        description="CLI for League of Comic Geeks",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"locg {__version__}")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # search
    p = sub.add_parser("search", parents=[common], help="Search for comic series")
    p.add_argument("query", help="Search term")

    # releases
    p = sub.add_parser("releases", parents=[common], help="New releases for a given week")
    p.add_argument("--date", help="Week date (YYYY-MM-DD), default: this week")

    # comic
    p = sub.add_parser("comic", parents=[common], help="Get comic details")
    p.add_argument("id", type=int, help="Comic ID")

    # series
    p = sub.add_parser("series", parents=[common], help="Get series details and issue list")
    p.add_argument("id", type=int, help="Series ID")

    # find — locate specific issue(s) within a series without manual pagination
    p = sub.add_parser(
        "find",
        parents=[common],
        help="Find issues by number within a series (paginates automatically)",
    )
    p.add_argument("--series-id", type=int, required=True, help="Series ID to search within")
    p.add_argument("--issue", required=True, help="Issue number, e.g. 229")
    p.add_argument(
        "--variant",
        help="Case-insensitive filter on title: a substring, or several words in any order (e.g. 'newsstand', 'mayhew red foil')",
    )
    p.add_argument(
        "--exact",
        action="store_true",
        help="Only return the plain entry (no variant text); with --printing, the plain Nth printing",
    )
    p.add_argument(
        "--printing",
        type=int,
        help="Only return the Nth printing (e.g. --issue 1 --printing 6 for a 6th printing)",
    )
    p.add_argument(
        "--no-legacy-printing",
        action="store_true",
        help="Do not also match the old glued form (--issue 16 meaning #1 6th Printing)",
    )

    # collection — LOCG view (default) + cache-based subcommands
    p = sub.add_parser("collection", parents=[common], help="View your collection or manage the local cache")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match, LOCG fetch only)")
    coll_sub = p.add_subparsers(dest="collection_command")

    # collection has — live LOCG check
    p_has = coll_sub.add_parser("has", parents=[common], help="Check if a title is in your collection via LOCG (requires login)")
    p_has.add_argument("title_query", help="Title to search for (case-insensitive substring match)")

    # collection import — local cache
    p_import = coll_sub.add_parser("import", parents=[common], help="Import a LOCG Excel export into the local collection cache")
    p_import.add_argument("path", help="Path to a LOCG Excel export (.xlsx)")

    # collection export — local cache
    p_export = coll_sub.add_parser("export", parents=[common], help="Export pending-push rows to a LOCG-compatible CSV")
    p_export.add_argument("--out", dest="out_path", help="Output CSV path (default: ~/Downloads/locg-bulk-import-<timestamp>.csv)")

    # collection status — local cache (--verbose inherited from common parent)
    coll_sub.add_parser("status", parents=[common], help="Show local collection cache status (use --verbose for extended metrics)")

    # collection check — local cache
    p_check = coll_sub.add_parser("check", parents=[common], help="Check if a specific comic is in the local collection cache")
    p_check.add_argument("--series", required=True, help="Series name (normalized match)")
    p_check.add_argument("--issue", required=True, help="Issue number (e.g. 300)")
    p_check.add_argument("--variant", help="Optional variant text to match in title")
    p_check.add_argument("--year", help="Optional publication year filter (e.g. 1988)")

    # collection doctor — local cache
    coll_sub.add_parser("doctor", parents=[common], help="Print first-run setup walkthrough and cache status")

    # collection record-win — agent win recording
    p_rw = coll_sub.add_parser(
        "record-win",
        parents=[common],
        help=(
            "Record Gixen auction wins into the local collection cache. "
            "Reads a JSON list from stdin or --from-gixen-json. "
            "Commits in batches of 25; large batches scale with Metron rate limit."
        ),
    )
    p_rw.add_argument(
        "--from-gixen-json",
        dest="gixen_json_path",
        metavar="PATH",
        help="Path to a JSON file containing wins (use '-' to read from stdin)",
    )

    # pull-list
    p = sub.add_parser("pull-list", parents=[common], help="View your pull list (requires login)")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match)")

    # wish-list
    p = sub.add_parser("wish-list", parents=[common], help="View your wish list (requires login)")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match)")
    wish_sub = p.add_subparsers(dest="wish_list_command")
    p_wish_add = wish_sub.add_parser(
        "add",
        parents=[common],
        help="Append a manual entry to the local wish-list cache",
        epilog=(
            "Writes {name: <title>, id: null} to ~/.cache/locg/wish-list.json. "
            "A subsequent `locg collection import` overwrites the cache from "
            "the LOCG XLSX export, so manually-added entries are not preserved "
            "across imports."
        ),
    )
    p_wish_add.add_argument("title", help="Title to record (e.g. 'Amazing Spider-Man #300')")
    p_wish_remove = wish_sub.add_parser(
        "remove",
        parents=[common],
        help="Remove a title from the local wish-list cache",
    )
    p_wish_remove.add_argument("title", help="Exact title to remove (e.g. 'Amazing Spider-Man #300')")

    # read-list
    p = sub.add_parser("read-list", parents=[common], help="View your read list (requires login)")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match)")

    # add
    p = sub.add_parser("add", parents=[common], help="Add a comic to a list")
    p.add_argument("list", choices=VALID_LISTS, help="Target list")
    p.add_argument("comic_id", type=int, help="Comic ID")
    p.add_argument("--grade", help="LOCG CGC grade (collection only, e.g. 8.5, 9.2, 9.8)")
    p.add_argument("--price", help="Purchase price (collection only, numeric)")

    # remove
    p = sub.add_parser("remove", parents=[common], help="Remove a comic from a list")
    p.add_argument("list", choices=VALID_LISTS, help="Target list")
    p.add_argument("comic_id", type=int, help="Comic ID")

    # update
    p = sub.add_parser("update", parents=[common], help="Update grade/price/condition on a comic in your collection")
    p.add_argument("id", type=int, help="Comic ID")
    p.add_argument("--grade", help="LOCG CGC grade (e.g. 8.5, 9.2, 9.8)")
    p.add_argument("--price", help="Purchase price (numeric)")
    p.add_argument("--condition", help="Free-text condition notes")

    # check
    p = sub.add_parser("check", parents=[common], help="Check which lists comics belong to (requires login)")
    p.add_argument("comic_ids", type=int, nargs="+", help="One or more comic IDs")

    # lookup
    p = sub.add_parser(
        "lookup",
        parents=[common],
        help="Resolve LOCG IDs for a batch of 'Series:Issue[:Variant]' specs",
        epilog=(
            "Series names may contain colons; the trailing token is treated as a "
            "variant only when the second-to-last token looks like an issue number "
            "(e.g. 'Batman: The Long Halloween:9' is parsed as series + issue)."
        ),
    )
    p.add_argument(
        "specs",
        nargs="+",
        help="One or more 'Series:Issue[:Variant]' specs (e.g. 'Uncanny X-Men:185')",
    )
    p.add_argument(
        "--no-collection",
        action="store_true",
        help="Skip collection-membership check (no auth needed)",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the on-disk ID cache (always hit the API; do not write back)",
    )

    # cache (with stats / clear / path subcommands)
    p = sub.add_parser(
        "cache",
        parents=[common],
        help="Inspect or clear the on-disk LOCG ID cache",
    )
    cache_sub = p.add_subparsers(dest="cache_command")
    cache_sub.add_parser("stats", parents=[common], help="Show cache file path, entry count, size")
    cache_sub.add_parser("clear", parents=[common], help="Delete every cached ID entry")

    # batch
    p = sub.add_parser(
        "batch",
        parents=[common],
        help="Run many locg commands with one Chrome and one Cloudflare clearance",
        epilog=(
            "Reads one command per line from FILE (or stdin with '-'). A line is "
            "either a JSON array of arguments, e.g. [\"find\", \"--series-id\", \"1\", "
            "\"--issue\", \"2\"], or the arguments as you would type them after "
            "'locg' (shell quoting rules). Blank lines and lines starting with # "
            "are skipped. Prints one JSON object per command: "
            "{\"line\": N, \"args\": [...], \"ok\": true, \"result\": ...} or "
            "{\"ok\": false, \"error\": \"...\"}. Exit code is 0 if every "
            "command succeeded, 1 otherwise."
        ),
    )
    p.add_argument("file", nargs="?", default="-", help="Command file ('-' for stdin, the default)")
    p.add_argument(
        "--delay",
        type=float,
        default=8.0,
        help="Seconds to wait between commands that hit League (default 8; League rate-limits bursts)",
    )
    p.add_argument("--stop-on-error", action="store_true", help="Stop at the first failing command")

    # login
    p = sub.add_parser(
        "login",
        parents=[common],
        help="Log in to League of Comic Geeks",
        epilog=(
            "Env vars LOCG_USERNAME and LOCG_PASSWORD (or a .env file at "
            "~/.config/locg/.env) enable automatic re-authentication when "
            "a session expires, so commands do not require a manual login."
        ),
    )
    p.add_argument("-u", "--username", help="Username (prompts if not provided)")
    p.add_argument("-p", "--password", help="Password (prompts if not provided)")

    return parser


def _global_flags(raw: list[str]) -> tuple[bool, bool, bool, Optional[list[str]]]:
    """Pre-scan global flags (argparse parent defaults can clobber them)."""
    pretty = "--pretty" in raw
    debug = "--debug" in raw
    verbose = "--verbose" in raw or "-v" in raw
    fields: Optional[list[str]] = None
    for i, arg in enumerate(raw):
        if arg == "--fields" and i + 1 < len(raw):
            fields = [f.strip() for f in raw[i + 1].split(",")]
            break
        elif arg.startswith("--fields="):
            fields = [f.strip() for f in arg.split("=", 1)[1].split(",")]
            break
    return pretty, debug, verbose, fields


# Collection cache subcommands are purely local: no browser launch.
_LOCAL_COLLECTION_SUBCMDS = {"import", "export", "status", "check", "doctor", "record-win"}


def _needs_client(args: argparse.Namespace) -> bool:
    _collection_sub = (
        getattr(args, "collection_command", None)
        if args.command == "collection"
        else None
    )
    # wish-list skips Playwright when the local cache exists; it still needs a
    # client when no cache is present (live fallback, R5).
    _wish_list_cached = args.command == "wish-list" and wish_list_cache_path().exists()
    # wish-list add/remove are pure local-cache writes: never need a client.
    _wish_list_add = (
        args.command == "wish-list"
        and getattr(args, "wish_list_command", None) in ("add", "remove")
    )
    return not (
        args.command in ("cache", "batch")
        or (_collection_sub in _LOCAL_COLLECTION_SUBCMDS)
        or _wish_list_cached
        or _wish_list_add
    )


def _dispatch(args: argparse.Namespace, get_client: Callable[[], LOCGClient]) -> Any:
    """Run one parsed command and return its result. Raises CLIError on failure."""
    _wish_list_cached = args.command == "wish-list" and wish_list_cache_path().exists()
    result: Any = None
    if args.command == "search":
        result = cmd_search(get_client(), args.query)
    elif args.command == "releases":
        result = cmd_releases(get_client(), args.date)
    elif args.command == "comic":
        result = cmd_comic(get_client(), args.id)
    elif args.command == "series":
        result = cmd_series(get_client(), args.id)
    elif args.command == "find":
        result = cmd_find(
            get_client(),
            series_id=args.series_id,
            issue=args.issue,
            variant=getattr(args, "variant", None),
            exact=getattr(args, "exact", False),
            printing=getattr(args, "printing", None),
            legacy_printing=not getattr(args, "no_legacy_printing", False),
        )
    elif args.command == "collection":
        sub_cmd = getattr(args, "collection_command", None)
        if sub_cmd == "has":
            result = cmd_collection_has(get_client(), args.title_query)
        elif sub_cmd == "import":
            result = cmd_collection_import(args.path)
        elif sub_cmd == "export":
            result = cmd_collection_export(getattr(args, "out_path", None))
        elif sub_cmd == "status":
            result = cmd_collection_status(verbose=args.verbose)
        elif sub_cmd == "check":
            result = cmd_collection_check(
                series=args.series,
                issue=args.issue,
                variant=getattr(args, "variant", None),
                year=getattr(args, "year", None),
            )
        elif sub_cmd == "doctor":
            result = cmd_collection_doctor()
        elif sub_cmd == "record-win":
            import json as _json
            import sys as _sys
            path = getattr(args, "gixen_json_path", None)
            if path is None or path == "-":
                raw = _sys.stdin.read()
            else:
                import pathlib as _pathlib
                raw = _pathlib.Path(path).read_text(encoding="utf-8")
            try:
                wins = _json.loads(raw)
            except _json.JSONDecodeError as exc:
                _fail(f"Failed to parse JSON input: {exc}", code=2)
            if not isinstance(wins, list):
                _fail("JSON input must be a list of win objects", code=2)
            result = cmd_collection_record_win(wins)
        else:
            result = cmd_collection(get_client(), title=args.title)
    elif args.command == "pull-list":
        result = cmd_pull_list(get_client(), title=args.title)
    elif args.command == "wish-list":
        if getattr(args, "wish_list_command", None) == "add":
            # The subparser positional `title` shadows the parent's
            # --title flag, so args.title is the value to append.
            result = cmd_wish_list_add(args.title)
        elif getattr(args, "wish_list_command", None) == "remove":
            result = cmd_wish_list_remove(args.title)
        elif _wish_list_cached:
            try:
                result = cmd_wish_list_from_cache(title=args.title)
            except (FileNotFoundError, json.JSONDecodeError):
                # Cache disappeared or is corrupt between the exists() check
                # and the read — fall through to live fetch.
                result = cmd_wish_list(get_client(), title=args.title)
        else:
            result = cmd_wish_list(get_client(), title=args.title)
    elif args.command == "read-list":
        result = cmd_read_list(get_client(), title=args.title)
    elif args.command == "add":
        grade = getattr(args, "grade", None)
        price = getattr(args, "price", None)
        if (grade is not None or price is not None) and args.list != "collection":
            _fail("--grade and --price are only valid when adding to collection")
        if grade is not None:
            try:
                grade = _validate_grade(grade)
            except ValueError as e:
                _fail(str(e))
        if price is not None:
            try:
                price = _validate_price(price)
            except ValueError as e:
                _fail(str(e))
        result = cmd_add(get_client(), args.list, args.comic_id, grade=grade, price=price)
        if isinstance(result, dict) and result.get("status") == "partial":
            _fail(
                f"Comic added but details not saved: {result.get('details_error', 'unknown')}",
                1,
                result,
            )
    elif args.command == "remove":
        result = cmd_remove(get_client(), args.list, args.comic_id)
    elif args.command == "update":
        grade = getattr(args, "grade", None)
        price = getattr(args, "price", None)
        condition = getattr(args, "condition", None)
        if grade is None and price is None and condition is None:
            _fail("update: at least one of --grade, --price, --condition is required")
        if grade is not None:
            try:
                grade = _validate_grade(grade)
            except ValueError as e:
                _fail(str(e))
        if price is not None:
            try:
                price = _validate_price(price)
            except ValueError as e:
                _fail(str(e))
        result = cmd_update(get_client(), args.id, grade=grade, price=price, condition=condition)
        if isinstance(result, dict) and (
            result.get("type") == "error"
            or "error" in result
        ):
            _fail(str(result.get("error") or result.get("message") or "update failed"), 1, result)
    elif args.command == "check":
        result = cmd_check_lists(get_client(), args.comic_ids)
    elif args.command == "lookup":
        try:
            requests = [parse_lookup_spec(s) for s in args.specs]
        except ValueError as e:
            _fail(str(e))
        result = cmd_lookup(
            get_client(),
            requests,
            check_collection=not args.no_collection,
            use_cache=not args.no_cache,
        )
    elif args.command == "cache":
        sub_cmd = getattr(args, "cache_command", None)
        if sub_cmd == "clear":
            result = cmd_cache_clear()
        else:
            # default to stats (also handles explicit "stats")
            result = cmd_cache_stats()
    elif args.command == "login":
        result = cmd_login(get_client(), username=args.username, password=args.password)

    return result


def _load_env() -> None:
    """Load credentials from ~/.config/locg/.env and credentials.env.

    ``LOCG_USER``/``LOCG_PASS`` are accepted as aliases for
    ``LOCG_USERNAME``/``LOCG_PASSWORD``.
    """
    load_dotenv(dotenv_path=env_path())
    load_dotenv(dotenv_path=credentials_env_path())
    for short, full in (("LOCG_USER", "LOCG_USERNAME"), ("LOCG_PASS", "LOCG_PASSWORD")):
        if not os.environ.get(full) and os.environ.get(short):
            os.environ[full] = os.environ[short]


def _configure_logging(debug: bool, verbose: bool) -> None:
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)


def _parse_batch_line(line: str) -> Optional[list[str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("["):
        argv = json.loads(line)
        if not isinstance(argv, list):
            raise ValueError("JSON line must be an array of arguments")
        return [str(a) for a in argv]
    argv = shlex.split(line)
    if argv and argv[0] == "locg":
        argv = argv[1:]
    return argv


class _ArgparseExit(Exception):
    pass


class _BatchParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ArgparseExit(message)


def _run_batch(args: argparse.Namespace) -> int:
    """Run commands from a file/stdin through one shared browser session."""
    src = sys.stdin if args.file == "-" else open(args.file, encoding="utf-8")
    parser = create_parser()
    parser.__class__ = _BatchParser
    for action in parser._subparsers._group_actions if parser._subparsers else []:
        for sp in getattr(action, "choices", {}).values():
            sp.__class__ = _BatchParser
    holder: dict[str, LOCGClient] = {}

    def get_client() -> LOCGClient:
        if "c" not in holder:
            holder["c"] = LOCGClient()
        return holder["c"]

    failures = 0
    last_live = 0.0
    try:
        for lineno, line in enumerate(src, 1):
            rec: dict[str, Any] = {"line": lineno}
            try:
                argv = _parse_batch_line(line)
                if argv is None:
                    continue
                rec["args"] = argv
                if not argv or argv[0] in ("batch", "login"):
                    raise CLIError(f"command not allowed in batch: {argv[:1]}")
                try:
                    sub_args = parser.parse_args(argv)
                except _ArgparseExit as e:
                    raise CLIError(f"bad arguments: {e}", 2)
                except SystemExit:
                    raise CLIError("bad arguments (or --help) in batch line", 2)
                _, _, _, fields = _global_flags(argv)
                live = _needs_client(sub_args)
                if live and last_live and args.delay > 0:
                    wait = args.delay - (time.monotonic() - last_live)
                    if wait > 0:
                        time.sleep(wait)
                try:
                    result = _dispatch(sub_args, get_client)
                finally:
                    if live:
                        last_live = time.monotonic()
                if fields:
                    result = _filter_fields(result, fields)
                rec.update(ok=True, result=result)
            except CLIError as e:
                rec.update(ok=False, error=str(e))
                if e.result is not None:
                    rec["result"] = e.result
            except AuthRequired as e:
                rec.update(ok=False, error=str(e))
            except Exception as e:  # keep going: one bad book must not kill the batch
                rec.update(ok=False, error=str(e))
            if not rec.get("ok"):
                failures += 1
            print(json.dumps(rec, separators=(",", ":"), ensure_ascii=False), flush=True)
            if failures and args.stop_on_error:
                break
    finally:
        if src is not sys.stdin:
            src.close()
        if "c" in holder:
            holder["c"].close()
    return 1 if failures else 0


def main() -> None:
    _load_env()

    raw = sys.argv[1:]
    pretty, debug, verbose, fields = _global_flags(raw)

    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help(sys.stderr)
        sys.exit(2)

    # Use pre-scanned values (handles both `locg --pretty releases` and `locg releases --pretty`)
    args.pretty = pretty
    args.debug = debug
    args.verbose = verbose

    _configure_logging(args.debug, args.verbose)
    logger = logging.getLogger("locg")

    if args.command == "batch":
        sys.exit(_run_batch(args))

    client: Optional[LOCGClient] = None

    def get_client() -> LOCGClient:
        nonlocal client
        if client is None:
            client = LOCGClient()
        return client

    try:
        if _needs_client(args):
            get_client()
        logger.info(f"Running command: {args.command}")
        result = _dispatch(args, get_client)
        if result is not None:
            output(result, pretty=args.pretty, fields=fields)
    except CLIError as e:
        if e.result is not None:
            output(e.result, pretty=args.pretty, fields=fields)
        die(str(e), code=e.code)
    except AuthRequired as e:
        die(str(e), code=1)
    except CloudflareChallenge as e:
        die(str(e), code=5)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        die(str(e), code=4)
    finally:
        if client is not None:
            client.close()
