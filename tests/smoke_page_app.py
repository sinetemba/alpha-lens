"""Streamlit script used by test_page_smoke.py: render one dashboard page.

PAGE = "module:function" and SMOKE_DB = a SQLAlchemy URL come from the
environment (AppTest runs this file in-process, so the test's network
blocks apply).
"""
import os
from importlib import import_module

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

module, func = os.environ["PAGE"].split(":")
engine = create_engine(os.environ["SMOKE_DB"])
db = sessionmaker(bind=engine)()
try:
    getattr(import_module(f"app.dashboard.{module}"), func)(db)
finally:
    db.close()
