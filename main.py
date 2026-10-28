"""
JSE Stock Analysis Platform - Main Application Entry Point
"""

import sys
import os

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
from app.dashboard.home import show_home
from app.database.init_db import init_database
from app.config.settings import settings
from loguru import logger

# Initialize logging + database on first run only.
# st.cache_resource ensures this body executes once per process: the whole
# script re-executes on every widget interaction (Streamlit's rerun model), so
# logger.add / create_all at module level would add a duplicate log sink and
# fire ~a dozen table-existence checks on every single rerun.
@st.cache_resource(show_spinner=False)
def _init_database_once():
    logger.add(
        settings.log_file,
        rotation="10 MB",
        retention="30 days",
        level=settings.log_level
    )
    # init_database() runs create_all plus the lightweight schema migrations.
    init_database()
    return True


try:
    _init_database_once()
    logger.info("Database initialized successfully")
except Exception as e:
    logger.error(f"Error initializing database: {e}")

# Background data fetching is handled entirely by the headless updater
# (scripts/run_updater.py via Windows Task Scheduler) - see AGENTS.md. The
# dashboard itself never schedules background jobs; it only reads from the
# database and offers a manual "Refresh Prices" button on the Home page.

# Run the dashboard
if __name__ == "__main__":
    try:
        show_home()
    except Exception as e:
        logger.error(f"Error running dashboard: {e}")
        raise
