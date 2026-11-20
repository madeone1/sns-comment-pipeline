"""Read-only retention inventory and deletion planning; never deletes data."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

OWNED_TABLES=('comments','observations','runs','pipeline_seen','pipeline_cursors',
              'pipeline_page_tokens','pipeline_attempts','pipeline_cycles','pipeline_sheet_rows')


def date(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None: raise ValueError('timezone_required')
    return result.astimezone(timezone.utc)


def audit_local(state, now=None):
    state=Path(state).resolve()
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('timezone_required')
    cutoff=now-timedelta(days=30)
    result={'state':str(state),'mode':'read_only','table_counts':{},'comment_freshness':None,'files':{}}
    path=state/'comments.sqlite3'
    if path.is_symlink(): raise ValueError('symlink_not_allowed')
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in OWNED_TABLES:
                if name in tables:
                    result['table_counts'][name]=db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
            if 'comments' in tables:
                ages=[date(r[0]) for r in db.execute('SELECT last_seen FROM comments')]
                result['comment_freshness']={'rows':len(ages),'not_refreshed_30_days':sum(x<cutoff for x in ages),
                    'oldest_refresh':min(ages).isoformat() if ages else None,'newest_refresh':max(ages).isoformat() if ages else None}
    for prefix in ('runs','pipeline-raw'):
        files=[p for p in (state/prefix).rglob('*') if p.is_file() and not p.is_symlink()] if (state/prefix).exists() else []
        result['files'][prefix]={'count':len(files),'bytes':sum(p.stat().st_size for p in files),
                               'older_than_30_days_by_file_time':sum(datetime.fromtimestamp(p.stat().st_mtime,timezone.utc)<cutoff for p in files)}
    return result


def cloud_metadata_summary(objects, soft_deleted, now=None):
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('timezone_required')
    cutoff=now-timedelta(days=30)
    invalid=[]; expired=[]
    for item in objects:
        try: created=date(item['timeCreated'])
        except (KeyError,ValueError,TypeError,AttributeError):
            invalid.append(item.get('name','unknown')); continue
        if created<cutoff: expired.append({'name':item['name'],'generation':item.get('generation')})
    return {'live_object_count':len(objects),'live_bytes':sum(int(x.get('size',0)) for x in objects),
            'raw_object_count':sum(x.get('name','').startswith('raw/') for x in objects),
            'expired_objects':expired,'invalid_timestamp_count':len(invalid),
            'soft_deleted_versions':len(soft_deleted),'soft_deleted_bytes':sum(int(x.get('size',0)) for x in soft_deleted),
            'last_physical_expiry':max((x.get('hardDeleteTime','') for x in soft_deleted),default=None),
            'database_row_freshness':'not_inspected_in_cloud_metadata_audit','payloads_downloaded':False}


def deletion_procedure():
    return {'mode':'plan_only','production_changes_applied':False,'scope':'stored application copies, never original YouTube content',
        'steps':[
            'Verify requester and channel or comment identifiers; record request time and seven-calendar-day deadline.',
            'Pause the dedicated collector before a confirmed owner disconnect; do not infer revocation from a transient network error.',
            'For owner disconnect, revoke the YouTube app grant and stop use of the dedicated OAuth Secret versions; preserve other services.',
            'Process the canonical database inside the authorized cloud runtime. Remove scoped comments, observations, runs containing bodies, relevant cursors, page-token caches and projection caches; vacuum after approved deletion.',
            'Clear scoped API-owned Sheet cells, inspect the separate processing-history tab and operator notes, and verify that duplicate IDs or stale rows cannot resurrect the deleted records.',
            'Remove matching raw-response objects and retained copies; if mixed raw files cannot be scoped safely, obtain approval for a broader raw-cache purge. Inventory every generation and soft-deleted copy.',
            'Remove matching local raw-response files and isolated review tokens. Confirm the storage provider hard-delete deadlines before promising physical erasure.',
            'Validate no recollection, no Sheet rows, no database rows and no restorable copies in scope. Keep only a minimal non-content deletion receipt.'
        ],
        'unresolved_before_publication':[
            'No production deletion/retention executor is deployed.',
            'The current bucket keeps recoverable objects for seven days, including overwritten database generations. Enabled recovery windows have a seven-day minimum; decide whether to disable soft delete for future deletions or use a verified deadline-compatible procedure. Existing soft-deleted copies retain their original expiry.',
            'The Sheet has domain-wide edit sharing, duplicate IDs, and an additional processing-history tab; scope and integrity need owner review.',
            'Public application-specific privacy and terms copy is not published.'
        ]}


def main():
    parser=argparse.ArgumentParser(description='Read-only YouTube data retention audit')
    parser.add_argument('--state',type=Path)
    parser.add_argument('--objects',type=Path)
    parser.add_argument('--soft-deleted',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'procedure':deletion_procedure()}
    if args.state: report['local_existing_copy']=audit_local(args.state)
    if args.objects:
        report['cloud_metadata']=cloud_metadata_summary(json.loads(args.objects.read_text()),json.loads(args.soft_deleted.read_text()) if args.soft_deleted else [])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');args.output.chmod(0o600)
    print(json.dumps({'written':str(args.output),'mode':'read_only','cloud_expired_objects':len(report.get('cloud_metadata',{}).get('expired_objects',[]))},ensure_ascii=False))


if __name__=='__main__': main()
