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
from app.models.base import Base, engine
from app.services.scheduler import SchedulerService
from app.config.settings import settings
from loguru import logger

# Configure logging
logger.add(
    settings.log_file,
    rotation="10 MB",
    retention="30 days",
    level=settings.log_level
)

# Initialize database on first run.
# st.cache_resource ensures this only runs once per process, since the whole
# script re-executes on every widget interaction (Streamlit's rerun model).
@st.cache_resource(show_spinner=False)
def _init_database_once():
    init_database()
    return True


try:
    _init_database_once()
    logger.info("Database initialized successfully")
except Exception as e:
    logger.error(f"Error initializing database: {e}")

# Ensure any new tables introduced by model updates are created, even when the
# cached init above has not re-run (e.g. after a code change while the app is up).
# This is a lightweight no-op when tables already exist.
try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    logger.info("Ensured all database tables exist")
except Exception as e:
    logger.error(f"Error ensuring database tables: {e}")

# Start background scheduler (in production, this would run in a separate process)
# st.cache_resource keeps this to a single instance across Streamlit reruns,
# since the whole script re-executes on every widget interaction.
@st.cache_resource(show_spinner=False)
def _get_scheduler():
    if not settings.scheduler_enabled:
        return None
    try:
        scheduler = SchedulerService()
        scheduler.start()
        logger.info("Background scheduler started")
        return scheduler
    except Exception as e:
        logger.error(f"Error starting scheduler: {e}")
        return None


scheduler = _get_scheduler()

# Run the dashboard
if __name__ == "__main__":
    try:
        show_home()
    except Exception as e:
        logger.error(f"Error running dashboard: {e}")
        raise
