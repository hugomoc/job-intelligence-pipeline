"""Explicit local candidate context; resumes never establish identity by filename."""
from src import database
from src import candidate_assessments as store


def candidates():
    with database.get_connection() as c:
        if not store.tables_exist(c):
            return []
        return c.execute('SELECT candidate_id,first_name,last_name,active_resume_hash FROM candidates ORDER BY created_at,candidate_id').fetchall()


def register(first,last,linkedin):
    with database.get_connection() as c:
        c.execute('BEGIN TRANSACTION')
        try:
            store.initialize(c)
            cid=store.register_candidate(c,first,last,linkedin)
            c.execute('COMMIT')
            return cid
        except BaseException:
            c.execute('ROLLBACK')
            raise


def cache_token(resume_hash):
    with database.get_connection() as c:
        if not resume_hash:
            row=c.execute('SELECT resume_hash FROM resume_profiles ORDER BY created_at DESC LIMIT 1').fetchone()
            resume_hash=row[0] if row else None
        ctx=store.candidate_context(c,resume_hash)
        return (ctx[0],ctx[2]) if ctx else None
