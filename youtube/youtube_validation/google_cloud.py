"""Dedicated runtime identity, scoped Sheets tokens and private GCS state."""

from contextlib import contextmanager
import gzip
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.parse import quote, urlencode
import uuid

import requests

from .api import ValidationError

PROJECT = 'waterbomb-sns-pipeline'
REGION = 'asia-northeast3'
RUNTIME = 'youtube-pipeline@' + PROJECT + '.iam.gserviceaccount.com'
BUCKET = PROJECT + '-youtube'
SECRET = 'youtube-comment-oauth'
JOB = 'youtube-comments-daily'


class Google:
    def __init__(self, local=False):
        self.local = local
        self.session = requests.Session()
        self.tokens = {}

    def token(self, sheets=False):
        key = 'sheets' if sheets else 'cloud'
        cached = self.tokens.get(key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        if sheets:
            url = 'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' + RUNTIME + ':generateAccessToken'
            data = self.json('POST', url, {'scope': ['https://www.googleapis.com/auth/spreadsheets'], 'lifetime': '3600s'})
            token = data['accessToken']
        elif self.local:
            process = subprocess.run(['gcloud', 'auth', 'print-access-token', '--account=syan@madeone.kr', '--quiet'],
                                     capture_output=True, text=True, timeout=60)
            if process.returncode:
                raise ValidationError('cloud_login_required')
            token = process.stdout.strip()
        else:
            try:
                response = self.session.get(
                    'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
                    headers={'Metadata-Flavor': 'Google'}, timeout=10, allow_redirects=False)
                response.raise_for_status()
                token = response.json()['access_token']
            except Exception:
                raise ValidationError('runtime_identity_unavailable') from None
        if not token or any(character.isspace() for character in token):
            raise ValidationError('invalid_google_token')
        self.tokens[key] = (token, time.time() + 3000)
        return token

    def request(self, method, url, data=None, content_type='application/json', sheets=False, allow_missing=False):
        allowed = ('https://storage.googleapis.com/', 'https://sheets.googleapis.com/',
                   'https://iamcredentials.googleapis.com/')
        if not url.startswith(allowed):
            raise ValidationError('unexpected_google_endpoint')
        for attempt in range(4):
            try:
                response = self.session.request(method, url, data=data,
                    headers={'Authorization': 'Bearer ' + self.token(sheets), 'Content-Type': content_type},
                    timeout=120, allow_redirects=False)
            except requests.RequestException:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ValidationError('google_transport_failed') from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            if allow_missing and response.status_code == 404:
                return None
            if response.status_code >= 300:
                raise ValidationError('google_request_failed', response.status_code)
            return response.content

    def json(self, method, url, value=None, sheets=False, allow_missing=False):
        raw = self.request(method, url, json.dumps(value, ensure_ascii=False).encode() if value is not None else None,
                           sheets=sheets, allow_missing=allow_missing)
        return json.loads(raw) if raw else None


class CloudState:
    def __init__(self, google=None, bucket=BUCKET):
        if bucket != BUCKET:
            raise ValidationError('unexpected_youtube_bucket')
        self.google, self.bucket = google or Google(), bucket

    def url(self, key, **params):
        return 'https://storage.googleapis.com/storage/v1/b/' + self.bucket + '/o/' + quote(key, safe='') + (
            '?' + urlencode(params) if params else '')

    def get(self, key):
        metadata = self.google.json('GET', self.url(key), allow_missing=True)
        if metadata is None:
            return None, 0
        generation = int(metadata['generation'])
        return self.google.request('GET', self.url(key, alt='media', generation=generation)), generation

    def put(self, key, raw, generation=None):
        params = {'uploadType': 'media', 'name': key}
        if generation is not None:
            params['ifGenerationMatch'] = generation
        url = 'https://storage.googleapis.com/upload/storage/v1/b/' + self.bucket + '/o?' + urlencode(params)
        result = self.google.request('POST', url, raw, content_type='application/octet-stream')
        return int(json.loads(result)['generation'])

    def list_objects(self, prefix):
        token,seen=None,set()
        while True:
            params={'prefix':prefix,'maxResults':1000,
                    'fields':'items(name,generation,timeCreated,size),nextPageToken'}
            if token: params['pageToken']=token
            result=self.google.json('GET','https://storage.googleapis.com/storage/v1/b/'+self.bucket+'/o?'+urlencode(params))
            yield from result.get('items',[])
            token=result.get('nextPageToken')
            if not token: return
            if token in seen: raise ValidationError('repeated_storage_page_token')
            seen.add(token)

    def delete_object(self,key,generation):
        if not key.startswith('raw/'):
            raise ValidationError('privacy_delete_prefix_not_allowed')
        return self.google.request('DELETE',self.url(key,generation=generation,ifGenerationMatch=generation),allow_missing=True)

    @contextmanager
    def lease(self, duration=3000):
        raw, version = self.get('state/lease.json')
        if raw and json.loads(raw).get('expires_at', 0) > time.time():
            raise ValidationError('youtube_job_already_running')
        owner = uuid.uuid4().hex
        version = self.put('state/lease.json', json.dumps({'owner': owner,
            'expires_at': time.time() + duration}).encode(), version)
        try:
            yield
        finally:
            try:
                self.google.request('DELETE', self.url('state/lease.json', ifGenerationMatch=version))
            except ValidationError:
                pass  # Never remove a newer lease.

    def restore(self, path):
        raw, version = self.get('state/comments.sqlite3.gz')
        if raw is not None:
            Path(path).write_bytes(gzip.decompress(raw))
            Path(path).chmod(0o600)
        return version

    def save(self, db, path, version):
        temporary = Path(path).with_suffix('.snapshot.sqlite3')
        connection = sqlite3.connect(temporary)
        try:
            db.backup(connection)
        finally:
            connection.close()
        try:
            return self.put('state/comments.sqlite3.gz', gzip.compress(temporary.read_bytes()), version)
        finally:
            temporary.unlink(missing_ok=True)
