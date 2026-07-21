"""User-facing gateway error messages for the Web UI."""

from qa_agent.server import _user_facing_error


def test_user_facing_rate_limit():
    msg = _user_facing_error(RuntimeError("Error code: 429 - rate_limit"), fallback="x")
    assert "۴۲۹" in msg


def test_user_facing_content_blocked():
    msg = _user_facing_error(
        RuntimeError("Error code: 400 - {'error': {'code': 'content-blocked'}}"),
        fallback="x",
    )
    assert "content-blocked" in msg


def test_user_facing_new_api_panic():
    msg = _user_facing_error(
        RuntimeError(
            "Error code: 500 - {'error': {'message': 'Panic detected, error: "
            "interface conversion: interface {} is nil, not types.OpenAIError', "
            "'type': 'new_api_panic'}}"
        ),
        fallback="x",
    )
    assert "new-api panic" in msg
    assert "پروکسی" in msg


def test_user_facing_fallback_truncates():
    long = "plain failure " + ("y" * 600)
    msg = _user_facing_error(RuntimeError(long), fallback="x")
    assert len(msg) <= 500
