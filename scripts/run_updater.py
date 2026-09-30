"""
Convenience entry point for the AlphaLens headless updater.
Can be called directly by Windows Task Scheduler or from a Windows Service.
"""
import os
import sys

# Ensure the repository root is on sys.path regardless of the current directory.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services.updater import main

if __name__ == "__main__":
    main()
