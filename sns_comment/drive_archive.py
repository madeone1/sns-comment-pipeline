"""Content-addressed recovery archive in an explicitly configured Drive folder."""

import hashlib
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from .api import NoRedirect, PilotError, https_context


def bundle(files):
    buffer=io.BytesIO()
    manifest={}
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            if not name or '/' in name or '\\' in name:raise PilotError('보관 파일명 오류')
            raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,sort_keys=True).encode()
            manifest[name]={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,raw)
        info=zipfile.ZipInfo('manifest.json',date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
        archive.writestr(info,json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode())
    return buffer.getvalue()


class Client:
    def __init__(self,token,folder_id,opener=None):
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',folder_id or ''):raise PilotError('Drive 보관 폴더 ID 미설정')
        self.token,self.folder=token,folder_id
        self.opener=opener or urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=https_context()))

    def request(self,url,data=None,content_type='application/json'):
        req=urllib.request.Request(url,data=data,headers={'Authorization':'Bearer '+self.token,'Content-Type':content_type})
        try:
            with self.opener.open(req,timeout=60) as response:return json.load(response)
        except (OSError,ValueError):raise PilotError('Drive 보관·대조 실패. 원본 보존, 재시도 대기.') from None

    def archive(self,key,raw):
        # Chunk before upload; each multipart request remains below 5 MB.
        full=hashlib.sha256(raw).hexdigest();parts=[raw[i:i+4_000_000] for i in range(0,len(raw),4_000_000)] or [b'']
        result=[]
        for index,part in enumerate(parts):
            identity=hashlib.sha256((key+':'+full+':'+str(index)).encode()).hexdigest()
            query=f"'{self.folder}' in parents and trashed=false and appProperties has {{ key='snsArchiveKey' and value='{identity}' }}"
            url='https://www.googleapis.com/drive/v3/files?'+urllib.parse.urlencode({'q':query,'fields':'files(id,size,md5Checksum),nextPageToken','pageSize':2})
            response=self.request(url);files=response.get('files',[])
            if response.get('nextPageToken') or len(files)>1:raise PilotError('Drive 보관 키 중복. 원본 보존, 자동 선택 중단.')
            if files:entry=files[0]
            else:
                meta={'name':key.replace('/','_')+'-'+full[:12]+f'.zip.part{index+1:04d}',
                      'parents':[self.folder],'appProperties':{'snsArchiveKey':identity,'sha256':full,'parts':str(len(parts))}}
                boundary='sns-'+uuid.uuid4().hex
                data=(f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()+json.dumps(meta).encode()+
                      f'\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+part+f'\r\n--{boundary}--\r\n'.encode())
                entry=self.request('https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,size,md5Checksum',data,'multipart/related; boundary='+boundary)
            if int(entry.get('size',-1))!=len(part) or entry.get('md5Checksum')!=hashlib.md5(part).hexdigest():
                raise PilotError('Drive 보관 파일 크기·지문 대조 실패')
            result.append(entry['id'])
        return {'status':'complete','sha256':full,'bytes':len(raw),'file_ids':result,'parts':len(parts)}
