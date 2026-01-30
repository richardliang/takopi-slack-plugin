from takopi_slack_plugin.bridge import (
    MAX_BLOCK_TEXT,
    SlackPresenter,
    _build_archive_blocks,
    _build_cancel_blocks,
    _format_elapsed,
    _render_final_text,
    _split_text,
    _trim_block_text,
    _trim_text,
)


class _State:
    def __init__(self, *, engine: str) -> None:
        self.engine = engine
        self.action_count = 0
        self.actions = []
        self.context_line = None
        self.resume_line = None


def test_format_elapsed() -> None:
    assert _format_elapsed(5) == "5s"
    assert _format_elapsed(65) == "1m 05s"
    assert _format_elapsed(3661) == "1h 01m"


def test_trim_text() -> None:
    assert _trim_text("hello", 10) == "hello"
    assert _trim_text("hello world", 5) == "he..."


def test_split_text() -> None:
    assert _split_text("hello", 10) == ["hello"]
    assert _split_text("hello", 2) == ["he", "ll", "o"]


def test_trim_block_text() -> None:
    assert _trim_block_text("hello") == "hello"


def test_build_cancel_blocks() -> None:
    blocks = _build_cancel_blocks("hello")
    assert blocks[0]["type"] == "section"
    assert blocks[1]["type"] == "actions"
    assert blocks[1]["elements"][0]["action_id"].startswith("takopi-slack")


def test_build_archive_blocks_splits_long_text() -> None:
    text = "a" * (MAX_BLOCK_TEXT + 10)
    blocks = _build_archive_blocks(text, thread_id="123")
    sections = [block for block in blocks if block["type"] == "section"]
    assert "".join(block["text"]["text"] for block in sections) == text
    assert blocks[-1]["type"] == "actions"


def test_presenter_split_followups() -> None:
    presenter = SlackPresenter(message_overflow="split", max_chars=5)
    state = _State(engine="codex")
    rendered = presenter.render_final(
        state,
        elapsed_s=1,
        status="ok",
        answer="hello world",
    )
    followups = rendered.extra.get("followups") or []
    chunks = [rendered.text] + [item.text for item in followups]
    expected = _render_final_text(state, elapsed_s=1, status="ok", answer="hello world")
    assert "".join(chunks) == expected
