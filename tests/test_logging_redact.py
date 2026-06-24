from __future__ import annotations

import logging

from app.utils.logging import _TelegramTokenRedactFilter


def test_telegram_token_redact_filter_strips_token_in_msg() -> None:
    tok = "123456789:AAFAKE_test_token_suffix"
    f = _TelegramTokenRedactFilter(tok)
    r = logging.LogRecord("n", logging.INFO, "", 0, "prefix " + tok + " suffix", (), None)
    assert f.filter(r)
    assert tok not in r.msg
    assert "<BOT_TOKEN>" in r.msg


def test_telegram_token_redact_filter_replaces_in_args_tuple() -> None:
    tok = "123456789:AAOTHERFAKEtokenhere"
    f = _TelegramTokenRedactFilter(tok)
    r = logging.LogRecord("n", logging.INFO, "", 0, "x %s y", (tok,), None)
    assert f.filter(r)
    assert r.args and tok not in r.args[0]
    assert "<BOT_TOKEN>" in r.args[0]
