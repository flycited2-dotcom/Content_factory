"""Short state transactions that tolerate temporary contention and always close."""
from contextlib import contextmanager
import sqlite3


@contextmanager
def state_connection(path, *, timeout: float = 30, uri: bool = False):
    """Commit or roll back the scope, then release the connection immediately.

    SQLite's own context manager controls transactions but does not close its
    connection. Waiting here is limited to acquiring database locks; this helper
    never replays application code or a paid network request.
    """
    connection = sqlite3.connect(path, timeout=timeout, uri=uri)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
