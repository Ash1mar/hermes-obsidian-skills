"""Read the full, exact native transcript without admitting recovered defaults."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import stat

from session_usage_reader import SessionStorageUnavailable, managed_session_reader, session_storage_read


def _tool_free(value):
    return value is None or value in ('', '[]')


def _raw_rows(db, session_id, *, managed):
    with session_storage_read(legacy=True):
        sessions = db.execute('SELECT source FROM sessions WHERE id=?', (session_id,)).fetchall()
        if sessions != [('kanban',)]:
            return None
        if managed:
            return db.execute('SELECT id,session_id,role,content,tool_calls FROM messages '
                              'WHERE session_id=? ORDER BY id', (session_id,)).fetchall()
        return db.execute('SELECT role,content,tool_calls FROM messages '
                          'WHERE session_id=? ORDER BY rowid', (session_id,)).fetchall()


@contextmanager
def _raw_store(path):
    with session_storage_read(legacy=True):
        db = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True,
                             check_same_thread=False)
    with closing(db):
        yield db


def transcript_target(home):
    if home is not None:
        return None, Path(home) / 'state.db'

    from hermes_constants import get_hermes_home

    active_home = get_hermes_home()
    try:
        from persistence_assembly import profile_scope, store_location
    except ModuleNotFoundError as error:
        if error.name != 'persistence_assembly':
            raise
        return None, active_home / 'state.db'

    with session_storage_read():
        location = store_location(profile_scope(home=active_home))
    return active_home, Path(location) if location is not None else None


def transcript_identity(path):
    if path is None:
        # The managed view may normalize unreadable tool metadata. Exact
        # receipt proof requires an independent lossless row representation.
        raise SessionStorageUnavailable('configured transcript store does not expose lossless row evidence')
    with session_storage_read(legacy=True):
        try:
            identity = path.stat()
        except FileNotFoundError:
            return None
    if not stat.S_ISREG(identity.st_mode):
        raise SessionStorageUnavailable('configured transcript store is not a regular file')
    return identity.st_dev, identity.st_ino


@contextmanager
def session_transcripts(profile, path, identity):
    """Yield an exact-session reader for the active store or an explicit SQLite archive."""
    if identity is None:
        yield lambda _: None
        return

    def require_current_store():
        if transcript_identity(path) != identity:
            raise SessionStorageUnavailable('transcript store was replaced during evidence review')

    if profile is None:
        with _raw_store(path) as db:
            def read_archived(session_id):
                require_current_store()
                return _raw_rows(db, session_id, managed=False)

            yield read_archived
        return

    from persistence.conversation.messages import TranscriptRead, TranscriptView

    request = TranscriptRead(view=TranscriptView.ALL, limit=None, include_lineage=False)
    with managed_session_reader(profile) as store, _raw_store(path) as db:
        def read(session_id):
            if not isinstance(session_id, str) or not session_id:
                return None
            require_current_store()
            with session_storage_read():
                session = store.sessions.get(session_id)
                if session is None or session.session_id != session_id or session.source != 'kanban':
                    return None
                records = tuple(store.messages.read_transcript(session_id, request))
            exact = _raw_rows(db, session_id, managed=True)
            require_current_store()
            if exact is None or len(exact) != len(records):
                return None
            by_id = {record.id: record for record in records}
            for row_id, owner, role, content, tool_calls in exact:
                record = by_id.get(row_id)
                # The public API decodes JSON null and sometimes substitutes
                # [] for malformed tool metadata. Accept only the raw values
                # that the historical exact proof considered tool-free.
                if (record is None or record.session_id != owner or owner != session_id
                        or record.role != role or record.content != content or not _tool_free(tool_calls)
                        or record.tool_calls not in (None, '', [], ())):
                    return None
            return [(role, content, tool_calls) for _, _, role, content, tool_calls in exact]

        yield read
