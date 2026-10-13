"""Smoke-test the actual Streamlit page in the same offline project suite."""

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_dashboard_streamlit_renders_with_no_paper_state_or_live_services():
    page = Path(__file__).resolve().parents[1] / "dashboard.py"
    app = AppTest.from_file(str(page), default_timeout=20).run()
    assert not app.exception
    labels = {widget.label for widget in app.metric}
    assert {"Open paper", "Open P/L", "Realized P/L"} <= labels
    # No synthetic live Paper P/L is shown if durable state is unavailable.
    open_pnl = next(metric for metric in app.metric if metric.label == "Open P/L")
    assert open_pnl.value == "UNKNOWN"
