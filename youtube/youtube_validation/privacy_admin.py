"""Queue a verified deletion request using existing project IAM; no DB download."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import uuid

from .google_cloud import Google,CloudState
from .privacy import digest_id,validate_control
from .storage import read_json


def prepare(request,now=None):
    now=now or datetime.now(timezone.utc)
    if request.get('verified_by')!='syan@madeone.kr' or request.get('kind') not in ('comment','author','all'):
        raise ValueError('verified_operator_request_required')
    targets=request.get('targets',[])
    if request['kind']!='all' and (not targets or any(not isinstance(x,str) or not x.strip() for x in targets)):
        raise ValueError('explicit_identifiers_required')
    return {'request_id':request.get('request_id') or uuid.uuid4().hex,'received_at':request.get('received_at') or now.isoformat(),
            'verified_by':request['verified_by'],'kind':request['kind'],
            'target_hashes':[digest_id(x.strip()) for x in targets] if request['kind']!='all' else []}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--request-file',type=Path,required=True)
    parser.add_argument('--apply-verified-request',action='store_true')
    args=parser.parse_args()
    item=prepare(read_json(args.request_file))
    if not args.apply_verified_request:
        print(json.dumps({'mode':'dry_run','request':item,'production_changed':False},indent=2));return
    cloud=CloudState(Google(local=True))
    raw,version=cloud.get('privacy/control.json')
    control=validate_control(json.loads(raw) if raw else None)
    if item['kind']=='all': control['collection_enabled']=False
    if not any(x['request_id']==item['request_id'] for x in control['requests']): control['requests'].append(item)
    validate_control(control)
    cloud.put('privacy/control.json',json.dumps(control).encode(),version)
    print(json.dumps({'queued':True,'request_id':item['request_id'],'collection_enabled':control['collection_enabled']}))


if __name__=='__main__': main()
