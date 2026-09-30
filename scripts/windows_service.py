r"""
Windows Service wrapper for the AlphaLens headless updater.

Requires pywin32:
    pip install pywin32

Usage (run as Administrator):
    python scripts\windows_service.py --install
    python scripts\windows_service.py --start
    python scripts\windows_service.py --stop
    python scripts\windows_service.py --remove

The service is configured to start automatically and runs the same 3x daily
schedule as the scheduled-task install.
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from loguru import logger

if sys.platform != "win32":
    print("This service wrapper is Windows-only.")
    sys.exit(1)

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
except ImportError as exc:
    print("pywin32 is required for the Windows service. Install it with: pip install pywin32")
    raise

from app.services.updater import UpdaterService


class AlphaLensUpdaterService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AlphaLensUpdater"
    _svc_display_name_ = "AlphaLens Updater"
    _svc_description_ = "Updates AlphaLens stock prices, news, historical data and dividends on a schedule."
    _exe_name_ = sys.executable

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.updater = UpdaterService()
        self.shutdown_event = threading.Event()
        self.service_thread = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.shutdown_event.set()
        win32event.SetEvent(self.hWaitStop)
        if self.service_thread and self.service_thread.is_alive():
            self.service_thread.join(timeout=30)
        self.updater.stop()
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, "")
        )

        self.service_thread = threading.Thread(target=self._run, daemon=True)
        self.service_thread.start()

        # Wait for the SCM to tell us to stop.
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

        # Give the updater thread a moment to shut down cleanly.
        self.shutdown_event.set()
        if self.service_thread and self.service_thread.is_alive():
            self.service_thread.join(timeout=30)

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, "")
        )

    def _run(self):
        try:
            self.updater.start(mode="daemon", stop_event=self.shutdown_event)
        except Exception as exc:
            logger.error(f"Updater service error: {exc}")
            # Sleep a bit so the service doesn't restart-loop instantly.
            time.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(AlphaLensUpdaterService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(AlphaLensUpdaterService)
