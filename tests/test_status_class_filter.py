"""Tests for the status_class Jinja2 filter."""
from fm.api.app import _STATUS_CLASS_MAP, status_class


def test_drift_maps_to_drifted():
    assert status_class("drift") == "drifted"


def test_done_maps_to_completed():
    assert status_class("done") == "completed"


def test_in_sync_passes_through():
    assert status_class("in_sync") == "in_sync"


def test_pending_passes_through():
    assert status_class("pending") == "pending"


def test_error_passes_through():
    assert status_class("error") == "error"


def test_modified_passes_through():
    assert status_class("modified") == "modified"


def test_missing_passes_through():
    assert status_class("missing") == "missing"


def test_new_passes_through():
    assert status_class("new") == "new"



def test_queued_passes_through():
    assert status_class("queued") == "queued"


def test_running_passes_through():
    assert status_class("running") == "running"


def test_awaiting_input_passes_through():
    assert status_class("awaiting_input") == "awaiting_input"


def test_ignored_passes_through():
    assert status_class("ignored") == "ignored"

def test_map_contains_only_two_entries():
    assert _STATUS_CLASS_MAP == {"drift": "drifted", "done": "completed"}
