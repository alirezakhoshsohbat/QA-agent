"""Developer-mode job log storage."""

from qa_agent.jobs import JobStore, _clip_log_data


def test_append_log_roundtrip():
    store = JobStore()
    job = store.create("query", budget=100)
    store.append_log(job.id, "hello", level="info", source="job", data={"a": 1})
    store.append_log(job.id, "boom", level="error", source="tool", data="trace")

    payload = store.to_dict(store.get(job.id))
    assert len(payload["logs"]) == 2
    assert payload["logs"][0]["message"] == "hello"
    assert payload["logs"][0]["data"] == {"a": 1}
    assert payload["logs"][1]["level"] == "error"


def test_clip_log_data_truncates_long_strings():
    clipped = _clip_log_data("x" * 5000)
    assert isinstance(clipped, str)
    assert clipped.endswith("…")
    assert len(clipped) <= 4000


def test_append_log_caps_history():
    store = JobStore()
    job = store.create("q", budget=1)
    for i in range(850):
        store.append_log(job.id, f"line-{i}")
    assert len(store.get(job.id).logs) == 800
    assert store.get(job.id).logs[0]["message"] == "line-50"
