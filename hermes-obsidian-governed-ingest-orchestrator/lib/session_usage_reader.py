"""Read session accounting from the active Hermes profile or an explicit offline file."""
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
import sqlite3


USAGE_FIELDS = {
    'input': 'input_tokens',
    'output': 'output_tokens',
    'cache_read_subset': 'cache_read_tokens',
    'reasoning': 'reasoning_tokens',
    'api_calls': 'api_call_count',
}


class SessionStorageUnavailable(RuntimeError):
    """The profile store could not establish whether session evidence exists."""


@contextmanager
def session_storage_read(*, legacy=False):
    """Translate storage failures while preserving unexpected programming errors."""
    if legacy:
        failures = (OSError, sqlite3.DatabaseError)
    else:
        from persistence import STORE_FAILURES

        failures = STORE_FAILURES
    try:
        yield
    except failures as error:
        if isinstance(error, sqlite3.ProgrammingError):
            raise
        raise SessionStorageUnavailable(str(error)) from error


@contextmanager
def managed_session_reader(home=None):
    """Open the current public persistence reader, or yield None on legacy Hermes."""
    try:
        from persistence_assembly import open_persistence_reader, profile_scope
    except ModuleNotFoundError as error:
        if error.name != 'persistence_assembly':
            raise
        yield None
        return

    with ExitStack() as lifetime:
        with session_storage_read():
            store = lifetime.enter_context(open_persistence_reader(profile_scope(home=home).profile_dir))
            store.ready()
        yield store


@contextmanager
def active_session_records():
    """Yield exact session lookup while the Hermes reader is open."""
    with managed_session_reader() as store:
        if store is None:
            from hermes_state import SessionDB

            with ExitStack() as lifetime:
                with session_storage_read(legacy=True):
                    database = lifetime.enter_context(closing(SessionDB(read_only=True)))

                def read_legacy(session_id):
                    with session_storage_read(legacy=True):
                        return database.get_session(session_id)

                yield read_legacy
            return

        def lookup(session_id):
            with session_storage_read():
                record = store.sessions.get(session_id)
                return record.as_session_row() if record is not None else None

        yield lookup


@contextmanager
def session_records(database=None):
    """Read the explicitly selected SQLite snapshot for offline usage reports."""
    if database is None:
        yield lambda _session_id: None
        return

    columns = ', '.join(USAGE_FIELDS.values())
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row

        def lookup(session_id):
            row = connection.execute(f'SELECT {columns} FROM sessions WHERE id=?', (session_id,)).fetchone()
            return dict(row) if row is not None else None

        yield lookup
