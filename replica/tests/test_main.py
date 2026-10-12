import io
import logging

from replica.__main__ import TokenMaskFilter

FAKE_TOKEN = "111:FAKE"


def _capture_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(TokenMaskFilter(FAKE_TOKEN))
    logger = logging.Logger("mask-test")
    logger.addHandler(handler)
    return logger, stream


def test_token_is_masked_in_message() -> None:
    logger, stream = _capture_logger()
    logger.error("request for token %s failed", FAKE_TOKEN)
    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    assert "<TOKEN>" in output


def test_token_is_masked_in_traceback() -> None:
    logger, stream = _capture_logger()
    try:
        raise ValueError(f"url /bot{FAKE_TOKEN}/getUpdates")
    except ValueError:
        logger.exception("update failed")
    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    assert "<TOKEN>" in output
