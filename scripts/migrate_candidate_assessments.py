"""Preview explicit resume associations; apply only a separately approved manifest.

Private manifests/backup files must be outside the repository. No AI calls.
"""
import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
import duckdb
from src.candidate_migration import make_preview, apply_preview, verify_applied

ROOT=Path(__file__).resolve().parents[1]
IMPLEMENTATION_FILES = ('src/candidate_assessments.py','src/candidate_migration.py','scripts/migrate_candidate_assessments.py')


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def external(path):
    resolved=path.expanduser().resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError('Private migration artifacts must be outside the repository')
    return resolved


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--preview',type=Path)
    p.add_argument('--apply',type=Path)
    p.add_argument('--first-name')
    p.add_argument('--last-name')
    p.add_argument('--linkedin')
    p.add_argument('--resume-hash',action='append',default=[])
    a=p.parse_args()
    db=a.database.expanduser().resolve()
    if not db.is_file():raise FileNotFoundError(db)
    if bool(a.preview)==bool(a.apply):p.error('Choose --preview or --apply')
    if a.preview:
        out=external(a.preview)
        if out.exists():raise FileExistsError(out)
        out.parent.mkdir(parents=True,exist_ok=True)
        with duckdb.connect(str(db),read_only=True) as c:
            preview=make_preview(c,a.first_name,a.last_name,a.linkedin,a.resume_hash)
            if Path(str(db)+'.wal').exists():raise ValueError('WAL present; refusing unsafe file copy')
            backup=out.parent/('jobs.backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.duckdb')
            expected=file_hash(db)
            with db.open('rb') as src,backup.open('xb') as dst:shutil.copyfileobj(src,dst)
            if file_hash(db)!=expected or file_hash(backup)!=expected:raise ValueError('Backup mismatch')
        with duckdb.connect(str(backup),read_only=True) as c:
            assert c.execute('SELECT count(*) FROM resume_job_scores').fetchone()[0]>=len(preview['assessments'])
        envelope={'database':str(db),'backup':str(backup),'backup_sha256':expected,'preview':preview,
            'implementation_files_sha256':{name:file_hash(ROOT/name) for name in IMPLEMENTATION_FILES}}
        out.write_text(json.dumps(envelope,indent=2)+'\n')
        print(json.dumps({'manifest':str(out),'candidate':preview['candidate'],'assessments':len(preview['assessments']),'active_jobs':len(preview['active']),'duplicate_groups':len(preview['duplicate_groups']),'skipped':preview['skipped'],'backup':str(backup),'backup_sha256':expected},indent=2))
    else:
        envelope=json.loads(external(a.apply).read_text())
        if str(db)!=envelope['database']:raise ValueError('Wrong database')
        if envelope.get('implementation_files_sha256')!={name:file_hash(ROOT/name) for name in IMPLEMENTATION_FILES}:
            raise ValueError('Migration implementation changed; regenerate and approve the preview')
        if file_hash(envelope['backup'])!=envelope['backup_sha256']:raise ValueError('Backup changed')
        with duckdb.connect(str(db)) as c:
            c.execute('BEGIN TRANSACTION')
            try:
                apply_preview(c,envelope['preview'])
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
        with duckdb.connect(str(db),read_only=True) as c:
            verify_applied(c,envelope['preview'])
        print('Migration committed and independently reverified; source scores, statuses and logs preserved.')


if __name__=='__main__':main()
