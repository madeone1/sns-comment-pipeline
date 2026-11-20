"""Notion MCP OAuth with PKCE, one-time browser consent and cloud refresh storage."""

import base64
import ctypes
import getpass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from .api import NoRedirect, PilotError, https_context


ORIGIN = "https://mcp.notion.com"
SERVICE = "kr.madeone.sns-comment.notion-oauth"
ACCOUNT = "waterbomb_official"
REDIRECT = "http://localhost:8766/notion/callback"


def oauth_http(url, value=None, form=False, opener=None):
    if urllib.parse.urlsplit(url).hostname != "mcp.notion.com" or not url.startswith(ORIGIN+"/"):
        raise PilotError("Notion 공식 OAuth 경로가 아닙니다.")
    data = None if value is None else (urllib.parse.urlencode(value).encode() if form else json.dumps(value).encode())
    req = urllib.request.Request(url,data=data,headers={"Content-Type":"application/x-www-form-urlencoded" if form else "application/json","Accept":"application/json"})
    opener = opener or urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=https_context()))
    try:
        with opener.open(req,timeout=30) as response:
            result = json.load(response)
        if not isinstance(result,dict):
            raise ValueError
        return result
    except urllib.error.HTTPError as error:
        raise PilotError(f"Notion OAuth 실패 (HTTP {error.code}). 다시 연결해야 합니다.") from None
    except (OSError,ValueError):
        raise PilotError("Notion OAuth 통신 실패. 인증정보는 출력하지 않습니다.") from None


def keychain_libraries():
    if sys.platform != "darwin":
        raise PilotError("초기 연결 저장은 macOS 키체인에서 실행해주세요.")
    library = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer, size = ctypes.c_void_p, ctypes.c_uint32
    library.SecKeychainFindGenericPassword.argtypes = [pointer,size,ctypes.c_char_p,size,ctypes.c_char_p,ctypes.POINTER(size),ctypes.POINTER(pointer),ctypes.POINTER(pointer)]
    library.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    library.SecKeychainAddGenericPassword.argtypes = [pointer,size,ctypes.c_char_p,size,ctypes.c_char_p,size,ctypes.c_char_p,ctypes.POINTER(pointer)]
    library.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    library.SecKeychainItemModifyAttributesAndData.argtypes = [pointer,pointer,size,ctypes.c_char_p]
    library.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    library.SecKeychainItemFreeContent.argtypes = [pointer,pointer]
    core.CFRelease.argtypes = [pointer]
    return library,core


def read_local(keychain_service=SERVICE, keychain_account=ACCOUNT, json_value=True):
    library,core = keychain_libraries()
    service,account = keychain_service.encode(),keychain_account.encode()
    length,password,item = ctypes.c_uint32(),ctypes.c_void_p(),ctypes.c_void_p()
    status = library.SecKeychainFindGenericPassword(None,len(service),service,len(account),account,ctypes.byref(length),ctypes.byref(password),ctypes.byref(item))
    try:
        if status:
            raise PilotError(f"Notion OAuth 키체인 읽기 실패 (상태 {status}). 연결·접근 상태를 확인해주세요.")
        raw=ctypes.string_at(password,length.value)
        if not json_value:
            return raw.decode()
        data = json.loads(raw)
        if not data.get("access_token") or not data.get("client_id") or data.get("token_endpoint") != ORIGIN+"/token":
            raise ValueError
        return data
    except (ValueError,TypeError):
        raise PilotError("Notion OAuth 저장 형식을 확인해주세요.") from None
    finally:
        if password:
            library.SecKeychainItemFreeContent(None,password)
        if item:
            core.CFRelease(item)


def save_local(value, keychain_service=SERVICE, keychain_account=ACCOUNT, json_value=True):
    """Use Security.framework directly: secret JSON never enters process argv."""
    library,core = keychain_libraries()
    pointer,size = ctypes.c_void_p,ctypes.c_uint32
    service,account = keychain_service.encode(),keychain_account.encode()
    raw = json.dumps(value,separators=(",",":")).encode() if json_value else value.encode()
    length,password,item = size(),pointer(),pointer()
    status = library.SecKeychainFindGenericPassword(None,len(service),service,len(account),account,ctypes.byref(length),ctypes.byref(password),ctypes.byref(item))
    try:
        if status == 0:
            status = library.SecKeychainItemModifyAttributesAndData(item,None,len(raw),raw)
        elif status == -25300:
            status = library.SecKeychainAddGenericPassword(None,len(service),service,len(account),account,len(raw),raw,ctypes.byref(item))
        if status:
            raise PilotError(f"Notion OAuth 키체인 저장 실패 (상태 {status}).")
    finally:
        if password:
            library.SecKeychainItemFreeContent(None,password)
        if item:
            core.CFRelease(item)


def updated_tokens(bundle, response):
    if not isinstance(response,dict) or not response.get("access_token"):
        raise PilotError("Notion OAuth 응답에 액세스 토큰이 없습니다.")
    result = {**bundle,**response}
    if response.get("expires_in"):
        result["expires_at"] = time.time()+int(response["expires_in"])
    return result


class OAuthCredentials:
    def __init__(self, load, save):
        self.load,self.save = load,save
        self.bundle = load()

    def token(self):
        if self.bundle.get("expires_at",float("inf")) < time.time()+90:
            return self.refresh()
        return self.bundle["access_token"]

    def refresh(self):
        # Always reload: another setup or a prior cloud execution may have
        # rotated the single-use refresh token.
        self.bundle = self.load()
        if not self.bundle.get("refresh_token"):
            raise PilotError("Notion 연결을 다시 승인해야 합니다.")
        response = oauth_http(self.bundle["token_endpoint"],{
            "grant_type":"refresh_token","client_id":self.bundle["client_id"],"refresh_token":self.bundle["refresh_token"]},form=True)
        self.bundle = updated_tokens(self.bundle,response)
        self.save(self.bundle)
        return self.bundle["access_token"]


def cloud_credentials(transport, secret_resource):
    if not secret_resource.startswith("projects/") or "/secrets/" not in secret_resource or "/versions/" in secret_resource:
        raise PilotError("Notion OAuth 비밀 저장소 경로를 확인해주세요.")

    def load():
        result = transport.json("GET","https://secretmanager.googleapis.com/v1/"+secret_resource+"/versions/latest:access")
        value = json.loads(base64.b64decode(result["payload"]["data"]))
        if value.get("token_endpoint") != ORIGIN+"/token":
            raise PilotError("Notion OAuth 토큰 서버가 다릅니다.")
        return value

    def save(value):
        transport.json("POST","https://secretmanager.googleapis.com/v1/"+secret_resource+":addVersion",
                       {"payload":{"data":base64.b64encode(json.dumps(value).encode()).decode()}})

    return OAuthCredentials(load,save)


def connect():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    metadata = oauth_http(ORIGIN+"/.well-known/oauth-authorization-server")
    for key in ("authorization_endpoint","token_endpoint","registration_endpoint"):
        if not metadata.get(key,"").startswith(ORIGIN+"/"):
            raise PilotError("Notion OAuth 메타데이터가 예상한 공식 서버와 다릅니다.")
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Authorization codes and state never enter request logs.

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(url.query)
            valid = url.path == "/notion/callback" and hmac.compare_digest(values.get("state",[""])[0],state)
            if not valid:
                self.send_error(400,"Invalid OAuth state")
                return
            if values.get("error"):
                outcome["error"] = "Notion 연결 승인이 취소되었습니다."
            elif values.get("code"):
                outcome["code"] = values["code"][0]
            else:
                outcome["error"] = "Notion 승인 코드를 확인하지 못했습니다."
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Security-Policy","default-src 'none'")
            self.end_headers()
            self.wfile.write("<h2>Notion 승인 응답을 받았습니다.</h2><p>이 창을 닫아도 됩니다. 연결 검증은 파이프라인에서 이어집니다.</p>".encode())

    with HTTPServer(("127.0.0.1",8766),Callback) as server:
        server.timeout = 1
        client = oauth_http(metadata["registration_endpoint"],{
            "client_name":"MADEONE SNS Comments Pipeline","redirect_uris":[REDIRECT],
            "grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"})
        auth_url = metadata["authorization_endpoint"]+"?"+urllib.parse.urlencode({
            "client_id":client["client_id"],"redirect_uri":REDIRECT,"response_type":"code","scope":"default",
            "state":state,"code_challenge":challenge,"code_challenge_method":"S256","prompt":"consent"})
        if not webbrowser.open(auth_url):
            raise PilotError("브라우저 인증 창을 열지 못했습니다. connect-notion.command를 직접 열어주세요.")
        print("Notion OAuth 승인창을 열었습니다. 일일 업무가 있는 워크스페이스에서 연결을 승인해주세요.",flush=True)
        stop = time.time()+600
        while not outcome and time.time()<stop:
            server.handle_request()
        if not outcome.get("code"):
            raise PilotError(outcome.get("error","Notion 연결 승인 대기 시간이 지났습니다."))
    response = oauth_http(metadata["token_endpoint"],{
        "grant_type":"authorization_code","client_id":client["client_id"],"code":outcome["code"],
        "redirect_uri":REDIRECT,"code_verifier":verifier},form=True)
    bundle = updated_tokens({"mode":"mcp_oauth","client_id":client["client_id"],"token_endpoint":metadata["token_endpoint"]},response)
    save_local(bundle)
    print(json.dumps({"status":"oauth_connected","token_displayed":False,"cloud_transfer_pending":True}),flush=True)
    return bundle


if __name__ == "__main__":
    try:
        if "--check" in sys.argv:
            value = read_local()
            print(json.dumps({"oauth_registered":True,"has_refresh_token":bool(value.get("refresh_token")),"token_displayed":False}))
        else:
            connect()
    except (PilotError,OSError,ValueError,subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error,PilotError) else "Notion OAuth 초기 연결에 실패했습니다. 인증정보는 출력하지 않습니다.",file=sys.stderr)
        sys.exit(1)
