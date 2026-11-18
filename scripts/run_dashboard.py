r"""
Launch the AlphaLens Streamlit dashboard with the project's own settings.

Used by the "AlphaLensDashboard" scheduled task (scripts\install_dashboard_task.ps1)
and fine to run by hand:

    .venv\Scripts\python.exe scripts\run_dashboard.py

Why not plain `streamlit run main.py`:
- it binds to every network interface - STREAMLIT_SERVER_ADDRESS /
  STREAMLIT_SERVER_PORT in .env were never passed to Streamlit, so the
  dashboard (holdings, sync and backup buttons, no login) was reachable from
  any network the PC joined. This passes them through (default: localhost);
- whichever `streamlit` is first on PATH runs - the dashboard had been
  running on a global Python with a different Streamlit than the tested
  venv. Run this with the venv's interpreter and it uses the venv's.

Under pythonw (no console) Streamlit's output goes to data/logs/dashboard.log.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app.config.settings import settings  # noqa: E402

LOG_FILE = os.path.join(ROOT, "data", "logs", "dashboard.log")


def main() -> None:
    if sys.stdout is None or sys.stderr is None:
        # pythonw: no console streams - Streamlit would crash writing to None.
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        log = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log

    from streamlit.web import cli as streamlit_cli

    sys.argv = [
        "streamlit", "run", os.path.join(ROOT, "main.py"),
        "--server.address", settings.streamlit_server_address,
        "--server.port", str(settings.streamlit_server_port),
        "--server.headless", "true",          # don't open a browser tab at logon
        "--browser.gatherUsageStats", "false",
    ]
    sys.exit(streamlit_cli.main())


if __name__ == "__main__":
    main()
