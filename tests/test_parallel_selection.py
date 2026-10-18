from __future__ import annotations

import pytest


@pytest.fixture
def indirect_qtbot(qtbot):
    return qtbot


def test_nested_qt_fixture_is_marked_ui(indirect_qtbot, request):
    assert request.node.get_closest_marker("ui") is not None


@pytest.mark.ui
def test_explicit_ui_marker_is_preserved(request):
    assert request.node.get_closest_marker("ui") is not None


def test_test_without_ui_fixtures_stays_unmarked(request):
    assert request.node.get_closest_marker("ui") is None
