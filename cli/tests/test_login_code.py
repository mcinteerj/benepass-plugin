"""Extracting the login code from whatever BENEPASS_OTP_COMMAND prints.

The command is the user's own — a mail CLI, a script, an MCP shim — so its
output is arbitrary text around the digits. What must never happen is slicing a
longer number (an order id, a timestamp) into something that looks like a code,
and never preferring a four-digit YEAR to the six digits that are actually the
code: either gets sent to Cognito, fails, and the error blames the code rather
than the parser.
"""

from __future__ import annotations

from benepass.auth import extract_code


def test_bare_code() -> None:
    assert extract_code("123456") == "123456"


def test_code_inside_a_sentence() -> None:
    assert (
        extract_code("Your Benepass login code is 654321. It expires soon.") == "654321"
    )


def test_first_code_wins_when_output_has_trailing_noise() -> None:
    assert extract_code("111222\nfetched 2 messages\n") == "111222"


def test_trailing_newline_and_whitespace() -> None:
    assert extract_code("  456789  \n") == "456789"


def test_a_longer_digit_run_is_not_a_code() -> None:
    """A 13-digit epoch must not be chopped into an 8-digit 'code'."""
    assert extract_code("message id 1757808000123") is None


def test_short_runs_are_not_codes() -> None:
    assert extract_code("found 2 messages in 1 thread") is None


def test_no_digits_at_all() -> None:
    assert extract_code("no new mail") is None


def test_code_after_a_short_number() -> None:
    assert extract_code("1 result: code 987654") == "987654"


# -- a 4-8 digit run is not good enough: a year is one ----------------------


def test_a_date_header_year_is_not_the_code() -> None:
    """The output shape of every mail CLI: headers first, code later."""
    body = (
        "From: Benepass <donotreply@getbenepass.com>\n"
        "Date: Sun, 14 Sep 2026 03:06:00 +0000\n"
        "Subject: Your Benepass Login Code\n\n"
        "Your login code is 314159\n"
    )
    assert extract_code(body) == "314159"


def test_an_iso_date_line_is_not_the_code() -> None:
    assert extract_code("2026-09-14T03:06:00Z\nYour code: 654321") == "654321"


def test_a_copyright_year_is_not_the_code() -> None:
    assert extract_code("(c) 2026 Benepass\nYour code: 314159") == "314159"


def test_a_timezone_offset_is_not_the_code() -> None:
    assert extract_code("Date: 14 Sep 26 03:06 +0000\ncode 220044") == "220044"


def test_the_code_word_wins_over_an_earlier_six_digit_number() -> None:
    """An order number is six digits as often as a code is."""
    assert extract_code("Order 998877 shipped\nYour login code is 123456") == "123456"


def test_a_four_digit_code_still_works_when_nothing_else_matches() -> None:
    """The wide range is the fallback, not the first thing tried."""
    assert extract_code("your code: 4821") == "4821"
