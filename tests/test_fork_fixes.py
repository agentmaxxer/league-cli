"""Tests for the kc-fixes fork changes (numeric search, full variant lists,
printings, batch mode, Cloudflare detection, TBD dates)."""
from __future__ import annotations

import io
import json
import sys
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

from locg.client import looks_like_challenge
from locg.commands import cmd_find, cmd_search
from locg.models import extract_issue, parse_printing
from locg.parser import extract_date_timestamp, parse_list_response


def _li(cid: int, base: str, variant: str | None = None) -> str:
    span = f' <span class="variant-name"> {variant}</span>' if variant else ""
    return (
        f'<li class="issue" data-comic="{cid}"><div class="title">'
        f'<a href="/comic/{cid}/x">{base}{span}</a></div></li>'
    )


def _resp(lis: list[str]) -> MagicMock:
    r = MagicMock()
    r.text = json.dumps({"count": len(lis), "list": "".join(lis)})
    r.status_code = 200
    return r


def test_extract_issue_splits_glued_printing_title():
    soup = BeautifulSoup(_li(7, "Absolute Batman #1", "6th Printing Nick Dragotta"), "html.parser")
    e = extract_issue(soup.li)
    assert e["name"] == "Absolute Batman #16th Printing Nick Dragotta"  # legacy glued form kept
    assert e["full_title"] == "Absolute Batman #1 6th Printing Nick Dragotta"
    assert e["issue_number"] == "1"
    assert e["printing"] == 6


def test_parse_printing():
    assert parse_printing("2nd Printing") == 2
    assert parse_printing("Ark-M 3rd print variant") == 3
    assert parse_printing("Foil Virgin Variant") is None


def test_find_uses_title_filter_and_filters_printing():
    lis = [
        _li(1, "Absolute Batman #1"),
        _li(2, "Absolute Batman #1", "6th Printing Nick Dragotta"),
        _li(3, "Absolute Batman #1", "6th Printing Foil Variant"),
        _li(4, "Absolute Batman #1", "2nd Printing"),
    ]
    client = MagicMock()
    client.get.side_effect = lambda path, params=None: _resp(lis if "list_mode_offset" not in params else [])
    res = cmd_find(client, series_id=9, issue="1", printing=6)
    assert [r["id"] for r in res] == [2, 3]
    assert client.get.call_args_list[0][1]["params"]["title"] == "#1"
    res = cmd_find(client, series_id=9, issue="1", printing=6, exact=True)
    assert [r["id"] for r in res] == [2]


def test_find_legacy_glued_issue_still_finds_reprint():
    pages = {
        "#16": [_li(16, "Absolute Batman #16")],
        "#1": [_li(1, "Absolute Batman #1"), _li(2, "Absolute Batman #1", "6th Printing Nick Dragotta")],
    }
    client = MagicMock()
    client.get.side_effect = lambda path, params=None: _resp(
        [] if "list_mode_offset" in params else pages.get(params.get("title"), [])
    )
    res = cmd_find(client, series_id=9, issue="16")
    ids = [r["id"] for r in res]
    assert ids == [16, 2]
    assert res[1]["title"] == "Absolute Batman #16th Printing Nick Dragotta"
    assert "legacy" in res[1]["matched_as"]


def test_search_falls_back_to_series_page_for_numeric_title():
    empty = MagicMock(text=json.dumps({"count": 0, "list": ""}), status_code=200)
    page = MagicMock(status_code=200, text=(
        '<ul id="comic-list-titles"><li><div class="title">'
        '<a href="/comics/series/193542/1776" data-id="193542">1776</a></div></li></ul>'
    ))
    client = MagicMock()
    client.get.side_effect = [empty, page]
    res = cmd_search(client, "1776")
    assert res[0]["id"] == 193542 and res[0]["name"] == "1776"
    assert client.get.call_args_list[1][0][0] == "/search/series"


def test_negative_tbd_timestamp_does_not_crash():
    soup = BeautifulSoup('<div><span class="date" data-date="-62169984000">TBD</span></div>', "html.parser")
    assert extract_date_timestamp(soup.div) == "TBD"


def test_challenge_detection():
    assert looks_like_challenge(403, "")
    assert looks_like_challenge(200, "<!DOCTYPE html><html><head><title>Just a moment...</title>")
    assert not looks_like_challenge(200, '{"count": 0, "list": ""}')
    assert not looks_like_challenge(200, "<html><script src='/cdn-cgi/challenge-platform/x.js'></script>")


def test_non_json_list_reply_is_a_clear_error():
    with pytest.raises(ValueError, match="not an empty result"):
        parse_list_response("<html>Just a moment</html>")


def test_batch_runs_commands_with_one_client(monkeypatch, capsys):
    import locg.cli as cli

    made = []

    class FakeClient:
        def __init__(self):
            made.append(self)

        def close(self):
            pass

    monkeypatch.setattr(cli, "LOCGClient", FakeClient)
    monkeypatch.setattr(cli, "cmd_comic", lambda c, i: {"id": i})
    monkeypatch.setattr(sys, "stdin", io.StringIO('comic 1\n# note\n["comic", "2"]\nsearch\n'))
    monkeypatch.setattr(sys, "argv", ["locg", "batch", "--delay", "0"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [o["ok"] for o in out] == [True, True, False]
    assert out[1]["result"] == {"id": 2}
    assert len(made) == 1
    assert e.value.code == 1
