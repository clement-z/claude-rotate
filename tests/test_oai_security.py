"""Offline validation and long-conversation regressions for the OpenAI translator."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oai_compat import oai_to_anthropic


def request(**extra):
    return {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}], **extra}


def test_malformed_request_shapes_return_validation_errors():
    malformed = [None, [], "text", 42, {}, request(messages=None),
                 request(messages="text"), request(messages={}),
                 request(messages=[None]), request(messages=[42]),
                 request(messages=[{"role": "user", "content": [{"type": "text", "text": 42}]}])]
    for data in malformed:
        body, error = oai_to_anthropic(data)
        assert body is None and isinstance(error, str) and error, repr(data)


def test_token_limits_require_positive_integers():
    for name in ("max_tokens", "max_completion_tokens"):
        for value in (True, False, 0, -1, 1.5, "bad", "256", [], {}):
            body, error = oai_to_anthropic(request(**{name: value}))
            assert body is None and "positive integer" in error, (name, value)
    for extra, expected in (({}, 4096), ({"max_tokens": None}, 4096),
                            ({"max_tokens": 64}, 64), ({"max_completion_tokens": 64}, 64),
                            ({"max_tokens": 32, "max_completion_tokens": 64}, 32)):
        body, error = oai_to_anthropic(request(**extra))
        assert error is None and body["max_tokens"] == expected


def test_invalid_stop_shapes_return_validation_errors():
    for stop in (42, True, {"text": "END"}, [42], ["END", None]):
        body, error = oai_to_anthropic(request(stop=stop))
        assert body is None and "stop" in error, repr(stop)


def test_large_adjacent_message_runs_preserve_exact_text_and_roles():
    # A long run previously copied the growing string once for every message.
    # Assert exact output at that shape without relying on machine-specific timing.
    parts = [f"{i}:" + "x" * 100 for i in range(20000)]
    messages = [{"role": "system", "content": "first"},
                *({"role": "user", "content": part} for part in parts),
                {"role": "developer", "content": "second"},
                {"role": "assistant", "content": "answer"},
                {"role": "assistant", "content": "continued"},
                {"role": "user", "content": "next"}]
    body, error = oai_to_anthropic(request(messages=messages))
    assert error is None
    assert body["system"] == "first\n\nsecond"
    assert body["messages"] == [
        {"role": "user", "content": "\n\n".join(parts)},
        {"role": "assistant", "content": "answer\n\ncontinued"},
        {"role": "user", "content": "next"},
    ]


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"ok   {name}")
    print(f"{len(tests)} passed")
