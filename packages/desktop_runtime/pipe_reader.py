"""Read subprocess pipes with bounded buffering and bounded caller waits."""
from __future__ import annotations

import os
from queue import Empty, Full, Queue
from threading import Event, Thread


class PipeReader:
    def __init__(self):
        self._ready = Queue(maxsize=2)
        self._stopped = Event()
        self.pending = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._stopped.set()

    def add(self, pipe, destination):
        """The thread owns a duplicate so caller cleanup never waits on a read."""
        descriptor = os.dup(pipe.fileno())
        try:
            Thread(target=self._read_pipe, args=(descriptor, destination), daemon=True).start()
        except BaseException:
            os.close(descriptor)
            raise
        self.pending += 1

    def _read_pipe(self, descriptor, destination):
        try:
            while not self._stopped.is_set():
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as error:
                    chunk = error
                while not self._stopped.is_set():
                    try:
                        self._ready.put((destination, chunk), timeout=.05)
                        break
                    except Full:
                        continue
                if not chunk or isinstance(chunk, OSError):
                    break
        finally:
            os.close(descriptor)

    def read(self, timeout):
        """Return a chunk (empty at EOF), or None when the wait expires."""
        try:
            destination, chunk = self._ready.get(timeout=max(0, timeout))
        except Empty:
            return None
        if isinstance(chunk, OSError):
            raise chunk
        if not chunk:
            self.pending -= 1
        return destination, chunk
