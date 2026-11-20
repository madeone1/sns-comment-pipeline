"""Cloud Run job: isolated GCS state, resumable YouTube read and Sheets sync."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .api import YouTube, ValidationError
from .auth import load_credentials
from .google_cloud import CloudState
from .pipeline import run, initialize
from .sheet_sync import sync, Sheets
from . import privacy
from .storage import STATE, private_dir, read_json, write_json


def main():
    config = read_json(Path(__file__).with_name('pipeline_config.json'))
    private_dir(STATE)
    cloud = CloudState()
    with cloud.lease():
        path = STATE/'comments.sqlite3'
        version = cloud.restore(path)
        control_raw,_=cloud.get('privacy/control.json')
        control=privacy.validate_control(json.loads(control_raw) if control_raw else None)
        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            privacy_result=privacy.apply(db,control)
            rules=privacy.exclusions(db)
            raw_cleanup=privacy.purge_cloud(cloud,db)
            version=cloud.save(db,path,version)
            history_cleared=privacy.clear_processing_history(Sheets(config['spreadsheet_id'],cloud.google),db)
        secret = Path(os.environ.get('YOUTUBE_AUTH_FILE','/secrets/youtube/oauth.json'))
        if control.get('collection_enabled',True):
            write_json(STATE/'authorized-user.json',read_json(secret))
            credentials = load_credentials()
        capture = uuid.uuid4().hex
        uploaded = set()
        def record(number, resource, value):
            write_json(STATE/'pipeline-raw'/capture/f'{number:05d}-{resource}.json',privacy.filtered_response(resource,value,rules))
        def save(db):
            nonlocal version
            version = cloud.save(db,path,version)
            for file in (STATE/'pipeline-raw'/capture).glob('*.json'):
                if file.name not in uploaded:
                    cloud.put('raw/'+capture+'/'+file.name,file.read_bytes(),0)
                    uploaded.add(file.name)
        if control.get('collection_enabled',True):
            api = YouTube(credentials,max_requests=config.get('max_requests',2000),record=record)
            result = run(api,STATE,max_requests=config.get('max_requests',2000),
                daily_budget=config.get('daily_budget',8000),seconds=config.get('seconds',2400),checkpoint=save,
                force_new_cycle=os.environ.get('YOUTUBE_FORCE_CYCLE') == 'true',
                progress=lambda item:print(json.dumps({'state':item['state'],
                    'requests':item['api_requests'],'stored':item['stored_comment_ids']},ensure_ascii=False),flush=True))
        else:
            result={'state':'disconnected','api_requests':0,'stored_comment_ids':0,
                    'checked_at':privacy.datetime.now(privacy.timezone.utc).isoformat(),'full_cycle_complete':False}
        if result.get('error')=='oauth_authorization_revoked' or result.get('reason')=='oauth_authorization_revoked':
            # A permanent invalid_grant is a withdrawal signal, not a network failure.
            # This uses the same stored-copy deletion path as an owner request.
            control['collection_enabled']=False
            control['requests'].append({'request_id':'revoked-'+privacy.digest_id(read_json(secret)['refresh_token'])[:32],
                'kind':'all','target_hashes':[],'verified_by':'oauth_invalid_grant',
                'received_at':privacy.datetime.now(privacy.timezone.utc).isoformat()})
            cloud.put('privacy/control.json',json.dumps(control).encode())
            with closing(sqlite3.connect(path)) as db:
                privacy_result=privacy.apply(db,control)
                raw_cleanup=privacy.purge_cloud(cloud,db)
                history_cleared+=privacy.clear_processing_history(Sheets(config['spreadsheet_id'],cloud.google),db)
                version=cloud.save(db,path,version)
            result={'state':'disconnected','api_requests':result.get('api_requests',0),'stored_comment_ids':0,
                    'checked_at':privacy.datetime.now(privacy.timezone.utc).isoformat(),'full_cycle_complete':False}
        # Even a YouTube authorization error can be shown using the independent
        # Cloud service identity that owns access to this one spreadsheet.
        # Reconcile on idle runs too: collection may have finished immediately
        # before a prior Sheets transfer failed. Cached rows make this cheap.
        projection = sync(path,config['spreadsheet_id'],result=result,google=cloud.google)
        with closing(sqlite3.connect(path)) as db:
            revoked=privacy.revoke_connection(read_json(secret),db) if not control.get('collection_enabled',True) else 'not_requested'
            version = cloud.save(db,path,version)
        outcome={'collection':result,'sheet':projection,'privacy':dict(privacy_result,**raw_cleanup,history_cleared_rows=history_cleared,oauth_revocation=revoked)}
        cloud.put('state/latest.json',json.dumps(outcome,ensure_ascii=False).encode())
        cloud.put('privacy/latest.json',json.dumps(outcome['privacy'],ensure_ascii=False).encode())
        print(json.dumps(outcome,ensure_ascii=False),flush=True)
        return 1 if result['state']=='error' else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValidationError as error:
        print(json.dumps({'state':'error','reason':error.reason,'http_status':error.status}),flush=True)
        raise SystemExit(0 if error.reason=='youtube_job_already_running' else 1) from None
    except Exception:
        print(json.dumps({'state':'error','reason':'youtube_pipeline_failed'}),flush=True)
        raise SystemExit(1) from None
