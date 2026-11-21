"""Tests for the cross-process job lock's in-process thread behaviour."""
import threading

from app.services.process_lock import InterProcessJobLock


def test_reentrant_on_the_same_thread(isolated_job_lock):
    lock = isolated_job_lock
    assert lock.acquire()
    assert lock.acquire()  # owner re-acquires instantly
    lock.release()
    assert lock.held       # still held at depth 1
    lock.release()
    assert not lock.held


def test_blocks_other_threads_while_held(isolated_job_lock):
    """A second thread must wait - the daemon's scheduler runs different
    jobs on different threads, and they used to share the depth counter."""
    lock = isolated_job_lock
    assert lock.acquire()

    acquired = []
    def _try():
        acquired.append(lock.acquire(timeout=0.5))

    t = threading.Thread(target=_try)
    t.start()
    t.join()
    assert acquired == [False]

    lock.release()
    t = threading.Thread(target=_try)
    t.start()
    t.join()
    assert acquired == [False, True]
    # The worker thread owns the lock; release() from another thread is a no-op.
    lock.release()
    assert lock.held


def test_release_only_counts_owner_calls(tmp_path):
    lock = InterProcessJobLock(tmp_path / "other.lock")
    assert lock.acquire()
    # A release from a thread that never acquired changes nothing.
    t = threading.Thread(target=lock.release)
    t.start()
    t.join()
    assert lock.held
    lock.release()
    assert not lock.held
