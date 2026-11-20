"""Loopback-only review build: genuine OAuth and a bounded, redacted GET sample.

Uses an isolated state directory; never deploys, writes to YouTube, or accesses
the production Cloud database. Screens are suitable for an English review video.
"""
import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from urllib.parse import urlsplit

from .api import HANDLE, SCOPE, ValidationError, YouTube, verify_channel
from .auth import load_credentials
from .server import make_server
from .storage import private_dir, read_json, write_json

STYLE = '''body{margin:0;background:#f3f5f8;color:#182435;font:17px/1.6 -apple-system,BlinkMacSystemFont,Arial,sans-serif}main{max-width:1000px;margin:36px auto;padding:32px;background:white;border:1px solid #dce2e9;border-radius:14px}h1{font-size:30px;margin:8px 0 14px}h2{font-size:22px}small{color:#586779}code{font-size:14px;overflow-wrap:anywhere}.status{background:#edf4ff;padding:16px;border-radius:8px}button,a.button{background:#1956c4;color:white;border:0;border-radius:7px;padding:12px 18px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}a{color:#165bb3}nav a{margin-right:20px}table{border-collapse:collapse;width:100%;font-size:15px}th,td{padding:11px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}.notice{background:#fff4d5;padding:16px}.ok{color:#14754a}pre{white-space:pre-wrap;background:#f6f7f9;padding:16px;font-size:14px}'''


def page(title, body):
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style><main><small>MADEONE · OAUTH REVIEW BUILD · LOCAL ONLY</small><h1>'+html.escape(title)+'</h1><nav><a href="/">Connection</a><a href="/privacy">Data handling review</a><a href="/evidence">API sample</a></nav>'+body+'</main></html>'


def sample(state):
    credentials = load_credentials(state)
    api = YouTube(credentials,max_requests=8)
    channel = verify_channel(api,HANDLE)
    result = {'checked_at':datetime.now(timezone.utc).isoformat(),'channel':channel,
              'mode':'limited_live_read_only_sample','responses':[],'api_requests':0}
    parent = None
    for status in ('published','heldForReview','likelySpam'):
        try:
            data=api.get('commentThreads',part='snippet,replies',allThreadsRelatedToChannelId=channel['id'],
                         moderationStatus=status,maxResults=5 if status=='published' else 1,textFormat='plainText',order='time')
            result['responses'].append({'method':'GET commentThreads.list','filter':status,'returned_items':len(data['items']),
                                        'more_pages':bool(data.get('nextPageToken')),'content':'Comment bodies and author identifiers are not persisted in the review recording.'})
            if status=='published' and data['items']:
                thread=next((x for x in data['items'] if x.get('snippet',{}).get('totalReplyCount')),data['items'][0])
                parent=thread.get('snippet',{}).get('topLevelComment',{}).get('id')
        except ValidationError as error:
            result['responses'].append({'method':'GET commentThreads.list','filter':status,'error':error.reason})
    if parent:
        try:
            data=api.get('comments',part='snippet',parentId=parent,maxResults=5,textFormat='plainText')
            result['responses'].append({'method':'GET comments.list','filter':'replies to one published parent',
                                        'returned_items':len(data['items']),'more_pages':bool(data.get('nextPageToken'))})
        except ValidationError as error:
            result['responses'].append({'method':'GET comments.list','error':error.reason})
    result['api_requests']=api.requests
    write_json(state/'authorized-user.json',json.loads(credentials.to_json()))
    write_json(state/'review-evidence.json',result)
    return result


def make_review_server(state,port=8765):
    state=private_dir(state)
    server=make_server(state,port,authorization_language='en')
    Base=server.RequestHandlerClass
    class Handler(Base):
        def do_GET(self):
            if not self.valid_host(): return self.send(403,'Invalid host.')
            if not self.authenticated(): return super().do_GET()
            path=urlsplit(self.path).path
            if path=='/':
                connected=(state/'connection.json').exists() and (state/'authorized-user.json').exists()
                connection=read_json(state/'connection.json')['channel'] if connected else None
                body='<p>Operator tool for the WATERBOMB official YouTube channel. This review build uses the same OAuth client and GET-only YouTube transport as the collector.</p>'
                body+='<p class="status">'+('Channel ownership verified: <b>'+html.escape(connection['title'])+'</b><br><code>'+html.escape(connection['id'])+'</code>' if connected else 'Ready to connect the official channel owner account.')+'</p>'
                body+='<p>Requested scope: <code>'+SCOPE+'</code>. Google describes this scope as including edits and deletions; this collector calls GET methods only.</p>'
                body+='<p><b>Data use:</b> retrieve published comments, replies and owner-only review queues for channel staff. Production results are synchronized to an operator-managed Google Sheet using a separate service account.</p>'
                body+='<p>Before connecting, read the <a href="https://www.waterbombfestival.com/privacy">published privacy policy</a> and <a href="https://www.waterbombfestival.com/comment-validation">application and service information</a>. The production collector implements retention and verified stored-copy deletion.</p>'
                body+='<form method="post" action="/authorize"><button>Connect with Google — official channel owner</button></form>'
                if connected: body+='<p><form method="post" action="/sample"><button>Run limited live GET sample</button></form></p>'
                body+='<p><small>This local interface demonstrates the existing production OAuth client. The same channel-verified grant has been promoted to the dedicated production Secret. No token or comment body is displayed.</small></p>'
                return self.send(200,page('MadeOne Comment Validation',body))
            if path=='/privacy':
                return self.send(200,page('Retention and stored-copy deletion','<p>Read the <a href="https://www.waterbombfestival.com/privacy">published privacy policy</a>.</p><p>Latest comments are refreshed or removed within 21 days of the last API confirmation. Raw API copies and processing history use 21-day retention. Operator notes survive automatic expiry; verified deletion requests cover associated notes, including notes retained after expiry.</p><p>The authenticated operator queues a verified comment, author or owner-disconnect request. The dedicated cloud runtime processes its database in place, clears scoped Sheet copies and history, prevents recollection with identifier hashes, and purges raw copies in bounded batches. Full disconnect stops collection and revokes the OAuth token. Confirmed permanent invalid_grant also starts this path; transient network errors do not.</p><p>Requests: <a href="mailto:syan@madeone.kr">syan@madeone.kr</a>. <a href="https://security.google.com/settings/security/permissions">Google permissions</a>. Stored-copy deletion does not delete original YouTube comments. Deleted cloud objects have a seven-day recovery period; provider-managed Sheets history and separately inventoried repair/export copies require the documented operator procedure.</p>'))
            if path=='/evidence':
                if not (state/'review-evidence.json').exists():
                    return self.send(200,page('Live API sample','<p>No sample has been run yet. Connect the channel owner and use the sample button.</p>'))
                data=read_json(state/'review-evidence.json')
                body='<p class="status">Real API responses · limited sample · '+str(data['api_requests'])+' GET requests · '+html.escape(data['checked_at'])+'</p><p>Channel ownership verified against '+html.escape(data['channel']['handle'])+'. Comment bodies and personal identifiers are excluded from this recording.</p><table><tr><th>Actual API call</th><th>Filter</th><th>Returned items</th><th>Result</th></tr>'
                for row in data['responses']:
                    body+='<tr><td>'+html.escape(row['method'])+'</td><td>'+html.escape(row.get('filter',''))+'</td><td>'+str(row.get('returned_items','—'))+'</td><td>'+html.escape(row.get('error','Success; additional pages available' if row.get('more_pages') else 'Success'))+'</td></tr>'
                body+='</table><p>This recording demonstrates one sample page per queue and one reply query. It does not claim a full-channel collection or modify any YouTube content.</p><p>The separate production pipeline performs resumable pagination and Sheet synchronization.</p>'
                return self.send(200,page('Live API sample',body))
            return super().do_GET()

        def do_POST(self):
            if self.path!='/sample': return super().do_POST()
            if not self.valid_host() or not self.authenticated() or self.headers.get('Origin')!=f'http://127.0.0.1:{server.server_port}':
                return self.send(403,'Use the local review session.')
            try: sample(state)
            except ValidationError as error:
                return self.send(400,page('Sample unavailable','<p>'+html.escape(error.reason)+'</p>'))
            return self.send(303,location='/evidence')
    server.RequestHandlerClass=Handler
    return server


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',type=Path,required=True)
    args=parser.parse_args()
    server=make_review_server(args.state)
    print(server.launch_url,flush=True)
    try: server.serve_forever()
    finally: server.server_close()
