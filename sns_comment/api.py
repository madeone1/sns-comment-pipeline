"""Read-only Graph transport. Tokens never appear in URLs or error messages."""

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class PilotError(Exception):
    """A safe diagnostic that can be displayed and persisted."""


class RequestBudgetExceeded(PilotError):
    """A resumable local request budget, distinct from a remote API failure."""


class GraphError(PilotError):
    def __init__(self, status=0, code=None, subcode=None):
        self.status = status
        self.code = code if isinstance(code, int) else None
        self.subcode = subcode if isinstance(subcode, int) else None
        label = "인증·권한 확인 필요" if status in (401, 403) or code in (10, 190, 200) else "API 요청 실패"
        if status == 429 or code in (4, 17, 32, 613):
            label = "API 호출 제한: 대기 후 수동 재실행 필요"
        super().__init__(f"{label} (HTTP {status}, code={self.code}, subcode={self.subcode})")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_context():
    context = ssl.create_default_context()
    # A python.org macOS install can have an empty default CA store. Keep
    # certificate verification enabled and use the OS bundle in that case.
    custom_ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")
    if sys.platform == "darwin" and not custom_ca and not context.get_ca_certs():
        try:
            context.load_verify_locations(cafile="/etc/ssl/cert.pem")
        except (OSError, ssl.SSLError):
            raise PilotError("Python의 HTTPS 인증서 저장소를 확인해주세요. 인증서 검증은 유지합니다.") from None
    return context


def redact(value, token):
    if isinstance(value, dict):
        return {key: redact(item, token) for key, item in value.items()
                if key.lower() not in {"access_token", "authorization", "appsecret_proof"}}
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, str) and token:
        return value.replace(token, "[REDACTED]")
    return value


class GraphClient:
    """Read-only Facebook Graph transport, pinned to a chosen API version."""

    def __init__(self, token, version, max_requests=500, opener=None, api_host="graph.facebook.com"):
        if api_host not in {"graph.facebook.com", "graph.instagram.com"}:
            raise PilotError("허용되지 않은 Meta API 호스트입니다.")
        if not re.fullmatch(r"v[1-9]\d*\.0", version):
            raise PilotError("앱에 설정된 API 버전을 v숫자.0 형식으로 입력해주세요.")
        if not token or any(c.isspace() for c in token):
            raise PilotError("공백 없는 액세스 토큰을 로컬에서 입력해주세요.")
        self._token = token
        self.api_host = api_host
        self._base = f"https://{api_host}/{version}"
        self._custom_opener = opener
        self._thread_state = threading.local()
        self._counter_lock = threading.Lock()
        self._opener = opener or urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=https_context())
        )
        self.max_requests = max_requests
        self.request_count = 0
        self.active_requests = 0
        self.max_active_requests = 0
        self.request_metrics = {}

    def get(self, path, **params):
        identity_paths = {"me"} if self.api_host == "graph.instagram.com" else {
            "me", "me/permissions", "me/assigned_instagram_accounts", "me/accounts"}
        if path not in identity_paths and not re.fullmatch(r"\d+(?:/(?:media|comments|replies))?", path):
            raise PilotError("허용되지 않은 API 조회 경로입니다.")
        if set(params) - {"fields", "limit", "after"}:
            raise PilotError("허용되지 않은 API 조회 매개변수입니다.")
        with self._counter_lock:
            if self.request_count >= self.max_requests:
                raise RequestBudgetExceeded("실행당 API 요청 상한에 도달했습니다. 저장된 원본을 유지하고 부분 실패로 종료합니다.")
            self.request_count += 1
            self.active_requests += 1
            self.max_active_requests = max(self.max_active_requests,self.active_requests)
        began=time.perf_counter()
        kind=path.rsplit("/",1)[-1] if "/" in path else "account_or_media"
        try:
            return self._request(path,params)
        finally:
            with self._counter_lock:
                self.active_requests-=1
                metric=self.request_metrics.setdefault(kind,{"requests":0,"request_seconds_total":0.0})
                metric["requests"]+=1
                metric["request_seconds_total"]+=time.perf_counter()-began

    def metrics(self):
        with self._counter_lock:
            return {"peak_concurrent_requests":self.max_active_requests,
                    "by_endpoint":{k:{"requests":v["requests"],"request_seconds_total":round(v["request_seconds_total"],3)}
                                   for k,v in self.request_metrics.items()}}

    def _request(self,path,params):
        request = urllib.request.Request(
            f"{self._base}/{path}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            if self._custom_opener is not None or threading.current_thread() is threading.main_thread():
                opener = self._opener
            else:
                if not hasattr(self._thread_state, "opener"):
                    self._thread_state.opener = urllib.request.build_opener(
                        NoRedirect(), urllib.request.HTTPSHandler(context=https_context()))
                opener = self._thread_state.opener
            with opener.open(request, timeout=30) as response:
                raw = response.read(10_000_001)
                if len(raw) > 10_000_000:
                    raise PilotError("API 응답이 파일당 처리 상한을 초과했습니다.")
                payload = json.loads(raw)
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read(65536))
                details = body.get("error", {}) if isinstance(body, dict) else {}
                if not isinstance(details, dict):
                    details = {}
            except (ValueError, OSError):
                details = {}
            raise GraphError(error.code, details.get("code"), details.get("error_subcode")) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise PilotError("API 연결 또는 JSON 응답 처리 실패. 저장된 원본을 확인하고 수동 재실행해주세요.") from None
        if not isinstance(payload, dict):
            raise PilotError("예상한 API 객체 응답이 아닙니다.")
        if "error" in payload:
            details = payload["error"] if isinstance(payload["error"], dict) else {}
            raise GraphError(200, details.get("code"), details.get("error_subcode"))
        return redact(payload, self._token)


def page_data(payload):
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise PilotError("API 목록 응답에 정상적인 data 배열이 없습니다. 0건으로 처리하지 않습니다.")
    return data


def next_cursor(payload):
    paging = payload.get("paging", {})
    if not isinstance(paging, dict):
        raise PilotError("페이지 정보 형식을 확인해야 합니다.")
    if not paging.get("next"):
        return None
    # Never follow or store next URLs: they can contain credentials.
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(paging["next"]).query)
        after = values.get("after", [None])[0]
        if not after:
            after = paging.get("cursors", {}).get("after")
    except (TypeError, ValueError, AttributeError):
        after = None
    if not isinstance(after, str) or not after:
        raise PilotError("다음 페이지가 있으나 처리 가능한 커서가 없습니다. 부분 실패로 기록합니다.")
    return after
