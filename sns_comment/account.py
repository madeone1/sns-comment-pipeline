"""Resumable collection with separate recent-post and activity-watch scopes."""

import datetime as dt
import time
import threading

from .api import GraphError, PilotError, RequestBudgetExceeded, next_cursor, page_data
from .collector import Collector, COMMENT_FIELDS, PARENT_FIELDS, post_key
from .storage import timestamp


KST = dt.timezone(dt.timedelta(hours=9))


def previous_month_start(end):
    stop = dt.datetime.fromisoformat(timestamp(end)).astimezone(KST)
    prior = stop.replace(day=1, hour=0, minute=0, second=0, microsecond=0)-dt.timedelta(days=1)
    return prior.replace(day=1).astimezone(dt.timezone.utc).isoformat()


def collection_window(days=7, start=None, end=None, instant=None):
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise PilotError("재조회 일수는 1 이상의 정수여야 합니다.")
    if start and not end:
        raise PilotError("시작 시각을 지정하면 종료 시각도 함께 지정해주세요.")
    if end:
        stop = dt.datetime.fromisoformat(timestamp(end))
    else:
        current = instant or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            raise PilotError("집계 기준 시각에는 시간대가 필요합니다.")
        stop = current.astimezone(KST).replace(hour=8, minute=0, second=0, microsecond=0)
        if stop > current:
            stop -= dt.timedelta(days=1)
    begin = dt.datetime.fromisoformat(timestamp(start)) if start else stop - dt.timedelta(days=days)
    if begin >= stop:
        raise PilotError("집계 종료 시각은 시작 시각보다 나중이어야 합니다.")
    return begin.astimezone(dt.timezone.utc).isoformat(), stop.astimezone(dt.timezone.utc).isoformat()


class AccountCollector(Collector):
    def __init__(self, client, store, expected_username="waterbomb_official", max_media_pages=20,
                 max_requests=500, progress=None, media_policy="all_accessible", discovery_pages=1):
        super().__init__(client, store, expected_username, max_media_pages)
        self.max_requests = max_requests
        self.request_start = client.request_count
        self.cursor_restarts = 0
        self.progress = progress
        self.last_progress = 0
        if media_policy not in {"all_accessible", "recent_posts", "tracked_activity", "previous_month_posts", "recent_comments"}:
            raise PilotError("수집할 게시물 범위 설정을 확인해주세요.")
        if not isinstance(discovery_pages, int) or isinstance(discovery_pages, bool) or discovery_pages < 1:
            raise PilotError("최근 게시물 발견 페이지 수는 1 이상이어야 합니다.")
        self.media_policy, self.discovery_pages = media_policy, discovery_pages
        self.workers = 1
        self._request_lock = threading.Lock()
        self._issued_requests = 0

    def _notify(self, scope_id, stage):
        if self.progress and time.monotonic() - self.last_progress >= 10:
            value = self.store.scope_progress(scope_id)
            self.progress({"stage": stage, "status": "running",
                           **{k: value[k] for k in ("catalog_complete", "media_discovered", "media_completed", "media_pending")}})
            self.last_progress = time.monotonic()

    @property
    def requests(self):
        return self.client.request_count - self.request_start

    def _get(self, path, fields, after=None):
        with self._request_lock:
            self._issued_requests = max(self._issued_requests,self.requests)
            if self._issued_requests >= self.max_requests:
                raise RequestBudgetExceeded("계정 수집의 실행당 요청 예산에 도달했습니다. 저장된 위치에서 재개할 수 있습니다.")
            self._issued_requests += 1
        params = {"fields": fields, "limit": 50}
        if after is not None:
            params["after"] = after
        return self.client.get(path, **params)

    def _read(self, path, fields, after, may_restart):
        try:
            return self._get(path, fields, after), False
        except GraphError as error:
            # The first-page retry is bounded and still verifies the same fixed
            # account-owned edge. Permission failures cannot become normal zero.
            if error.code != 100 or after is None or not may_restart:
                raise
            value = self._get(path, fields)
            self.cursor_restarts += 1
            return value, True

    def _catalog(self, scope_id, account_id):
        if self.media_policy not in {"all_accessible", "previous_month_posts", "recent_comments"}:
            if self.store.scope(scope_id)["catalog_complete"]:
                return
            exhaustive = False
            if self.media_policy == "recent_posts":
                # Refresh a bounded head of media metadata, then select from
                # the local cache. Never walk the historical comment archive.
                after, seen = None, set()
                for _ in range(min(self.discovery_pages, self.max_media_pages)):
                    payload = self._get(f"{account_id}/media", "id,permalink,caption,timestamp", after)
                    items, following = page_data(payload), next_cursor(payload)
                    for item in items:
                        if not isinstance(item.get("id"), str) or not item["id"].isdigit() or not isinstance(item.get("permalink"), str):
                            raise PilotError("게시물 ID·링크를 확인할 수 없습니다.")
                        post_key(item["permalink"])
                        timestamp(item.get("timestamp"))
                    if following is not None and following in seen:
                        raise PilotError("게시물 발견 커서가 반복됩니다.")
                    for item in items:
                        self.store.save_media(account_id, item)
                    if following is None:
                        exhaustive = True
                        break
                    seen.add(following)
                    after = following
            else:
                scope = self.store.scope(scope_id)
                for signal in self.store.missing_tracked_media(account_id,scope["window_start"]):
                    item = self._get(signal["media_id"], "id,permalink,caption,timestamp,owner")
                    owner = item.get("owner")
                    if item.get("id") != signal["media_id"] or not isinstance(owner, dict) or owner.get("id") != account_id:
                        raise PilotError("알림으로 추가된 게시물의 소유 계정을 확인할 수 없습니다.")
                    post_key(item.get("permalink"))
                    timestamp(item.get("timestamp"))
                    self.store.save_media(account_id,item)
            self.store.prepare_recent_scope(scope_id, exhaustive)
            self._notify(scope_id, "recent_posts" if self.media_policy == "recent_posts" else "tracked_activity")
            return
        seen, restarted, pages = set(), False, 0
        while not self.store.scope(scope_id)["catalog_complete"] and pages < self.max_media_pages:
            after = self.store.scope(scope_id)["catalog_cursor"]
            if after in seen:
                raise PilotError("계정 게시물 목록의 페이지 커서가 반복됩니다. 전체 완료로 처리하지 않습니다.")
            seen.add(after)
            payload, reset = self._read(f"{account_id}/media", "id,permalink,caption,timestamp", after, not restarted)
            if reset:
                restarted, seen = True, {None}
            items, following = page_data(payload), next_cursor(payload)
            if following is not None and following in seen:
                raise PilotError("계정 게시물 목록의 페이지 커서가 반복됩니다.")
            for item in items:
                if not isinstance(item.get("id"), str) or not item["id"].isdigit() or not isinstance(item.get("permalink"), str):
                    raise PilotError("계정 게시물의 ID·링크를 확인해야 합니다. 해당 목록 페이지는 완료 처리하지 않습니다.")
                post_key(item["permalink"])
            if self.media_policy == 'previous_month_posts':
                scope = self.store.scope(scope_id)
                floor = previous_month_start(scope['window_end'])
                # Catalog metadata is cheap. Read its end rather than assuming
                # pinned posts or API ordering imply a date boundary.
                items = [item for item in items if floor <= timestamp(item.get('timestamp')) < scope['window_end']]
            self.store.save_catalog_page(scope_id, account_id, items, following)
            self._notify(scope_id, "catalog")
            pages += 1
        if self.media_policy=='previous_month_posts' and self.store.scope(scope_id)['catalog_complete']:
            self.store.reuse_completed_media(scope_id)

    def window_page(self, scope_id, items, following, parent_id=None):
        if self.media_policy!='recent_comments':
            return items,following
        floor=self.store.scope(scope_id)['window_start']
        times=[timestamp(item.get('timestamp')) for item in items]
        if parent_id is not None:
            # Replies have their own ordering; do not stop at an old reply.
            return [item for item,at in zip(items,times) if at>=floor],following
        if times!=sorted(times,reverse=True):
            raise PilotError('댓글 페이지의 최신순 정렬을 확인하지 못해 기간 경계에서 중단하지 않습니다.')
        selected=[]
        for item,at in zip(items,times):
            replies=page_data(item['replies']) if 'replies' in item else []
            if at>=floor or any(timestamp(row.get('timestamp'))>=floor for row in replies):
                selected.append(item)
        # The live API's newest-first pages are validated before stopping.
        # Historical-parent replies not visible in these pages remain a
        # separate coverage gap; webhook events still inspect any old post.
        return selected, None if times and min(times)<floor else following

    def _edge(self, scope_id, run_id, account_id, media_id, edge, parent_id=None, initial=None):
        seen, restarted = set(), False
        while True:
            checkpoint = self.store.scope_edge(scope_id, media_id, edge)
            if checkpoint["complete"]:
                return
            after = checkpoint["next_cursor"]
            if after in seen:
                raise PilotError("댓글·답글의 페이지 커서가 반복됩니다. 해당 게시물은 미완료로 남깁니다.")
            seen.add(after)
            if initial is not None and after is None:
                payload, reset = initial, False
                initial = None
            else:
                path = f"{parent_id}/replies" if parent_id else f"{media_id}/comments"
                payload, reset = self._read(path, COMMENT_FIELDS if parent_id else PARENT_FIELDS, after, not restarted)
            if reset:
                restarted, seen = True, {None}
            items, following = page_data(payload), next_cursor(payload)
            items, following = self.window_page(scope_id,items,following,parent_id)
            if following is not None and following in seen:
                raise PilotError("댓글·답글의 페이지 커서가 반복됩니다.")
            expansions = {}
            if parent_id is None:
                for item in items:
                    if not isinstance(item.get("id"), str) or not item["id"].isdigit():
                        raise PilotError("댓글 ID 형식이 올바르지 않습니다. 해당 페이지는 완료 처리하지 않습니다.")
                    if "replies" in item:
                        if not isinstance(item["replies"], dict):
                            raise PilotError("답글 확장 응답 형식이 올바르지 않습니다.")
                        page_data(item["replies"])
                        next_cursor(item["replies"])
                        expansions[item["id"]] = item["replies"]
            # Parent page, parent cursor, and the pending reply IDs commit in
            # one transaction. Expansion URLs are never persisted in the queue.
            self.store.save_page(run_id, account_id, media_id, edge, items, following, self.owner,
                                 parent_id=parent_id, scope_id=scope_id)
            self._notify(scope_id, "replies" if parent_id else "comments")
            if parent_id is None:
                self._replies(scope_id, run_id, account_id, media_id, expansions)

    def _replies(self, scope_id, run_id, account_id, media_id, expansions=None):
        expansions = expansions or {}
        while True:
            pending = self.store.pending_scope_reply(scope_id, media_id)
            if pending is None:
                return
            self.store.stage(run_id, "replies:" + pending["parent_id"], account_id, media_id)
            self._edge(scope_id, run_id, account_id, media_id, pending["edge"], parent_id=pending["parent_id"],
                       initial=expansions.get(pending["parent_id"]))

    def collect_account(self, start, end, account_id=None, restart=False, resume_scope_id=None):
        began=time.perf_counter()
        start, end = timestamp(start), timestamp(end)
        if start >= end:
            raise PilotError("집계 종료 시각은 시작 시각보다 나중이어야 합니다.")
        self.request_start = self.client.request_count
        self._issued_requests = 0
        self.store.mark_interrupted()
        run_id = self.store.start_run("https://www.instagram.com/" + self.owner + "/")
        scope_id, reason, reused = None, None, False
        media_errors = []
        try:
            account_id = self.account(account_id)
            if resume_scope_id:
                if restart:
                    raise PilotError("기간 수집 재개와 새 대조를 함께 요청할 수 없습니다.")
                scope = self.store.scope(resume_scope_id)
                if (scope["account_id"], scope["owner"], scope["window_start"], scope["window_end"]) != (account_id, self.owner, start, end):
                    raise PilotError("재개할 기간 수집의 계정·기간이 현재 요청과 다릅니다.")
                if scope["media_policy"] != self.media_policy:
                    raise PilotError("이전 전체 게시물 수집은 현재 범위로 재개하지 않습니다. 새 실행으로 최근 게시물만 선택해주세요.")
            else:
                scope = self.store.collection_scope(account_id, self.owner, start, end, restart=restart, media_policy=self.media_policy)
            scope_id = scope["id"]
            reused = scope["status"] == "complete"
            self.store.attach_scope(run_id, scope_id)
            self.store.stage(run_id, "catalog", account_id=account_id)
            if not reused:
                self._catalog(scope_id, account_id)
                media_errors = self._collect_media(scope_id, run_id, account_id)
                if not self.store.scope(scope_id)["catalog_complete"]:
                    reason = "catalog_page_budget_reached"
                progress = self.store.finish_scope(scope_id, self.requests)
            else:
                progress = self.store.scope_progress(scope_id)
            status = "complete" if progress["status"] == "complete" else "partial"
        except RequestBudgetExceeded:
            reason = "request_budget_reached"
            if scope_id is None:
                self.store.finish(run_id, "failed", self.requests, reason)
                raise
            progress = self.store.finish_scope(scope_id, self.requests, reason)
            status = "partial"
        except BaseException as error:
            safe = str(error) if isinstance(error, PilotError) else "사용자 중단" if isinstance(error, KeyboardInterrupt) else "계정 수집 내부 처리 실패. 저장된 원본을 유지합니다."
            if scope_id:
                self.store.finish_scope(scope_id, self.requests, safe)
            self.store.finish(run_id, "partial" if scope_id else "failed", self.requests, safe)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise PilotError(safe + " 실행 기록: " + run_id) from None
        if status == "complete":
            self.store.stage(run_id, "reused_scope" if reused else "done")
        self.store.finish(run_id, status, self.requests, reason or ("일부 게시물 미완료" if media_errors else None))
        selected = self.store.scope_media_ids(scope_id) if self.media_policy != "all_accessible" else None
        return {"run_id": run_id, "scope_id": scope_id, "status": status,
                "account_id": account_id, "scope": "all_accessible_account_media" if self.media_policy == "all_accessible" else self.media_policy,
                "window_start_inclusive_utc": start, "window_end_exclusive_utc": end,
                "api_requests": self.requests, "catalog_complete": progress["catalog_complete"],
                "media_total_known": progress["media_total_known"],
                "media_discovered": progress["media_discovered"], "media_completed": progress["media_completed"],
                "media_pending": progress["media_pending"], "stored_in_window": self.store.summary(account_id, start=start, end=end, media_ids=selected),
                "discovery_exhaustive": bool(progress["catalog_complete"]) if self.media_policy in {"all_accessible", "previous_month_posts", "recent_comments"} else bool(progress["discovery_exhaustive"]),
                "discovery_policy": "bounded_head_and_cached_metadata" if self.media_policy == "recent_posts" else self.media_policy,
                "unobserved_old_post_activity": ('historical_parent_replies_require_webhook' if self.media_policy=='recent_comments' else
                    'outside_selected_post_window' if self.media_policy=='previous_month_posts' else
                    "not_monitored_without_event_delivery" if self.media_policy != "all_accessible" else "full_scan"),
                "raw_storage": "all_api_returned_observations_preserved",
                "network_workers": self.workers,
                "elapsed_seconds": round(time.perf_counter()-began,3),
                "network_metrics":self.client.metrics() if hasattr(self.client,"metrics") else None,
                "reason": reason, "media_errors": media_errors, "cursor_restarts": self.cursor_restarts,
                "reused_completed_scope": reused, "last_scope_completed_at": progress["completed_at"],
                "resume_command": "python3 -m sns_comment run --resume-scope " + scope_id}

    def _collect_media(self, scope_id, run_id, account_id):
        errors = []
        for media in self.store.pending_scope_media(scope_id):
            media_id = media["media_id"]
            self.store.stage(run_id, "comments", account_id, media_id)
            self.store.finish_scope_media(scope_id, media_id, "running")
            try:
                self._replies(scope_id, run_id, account_id, media_id)
                self._edge(scope_id, run_id, account_id, media_id, "comments")
                self.store.finish_scope_media(scope_id, media_id, "complete")
            except RequestBudgetExceeded:
                self.store.finish_scope_media(scope_id, media_id, "partial")
                raise
            except PilotError as error:
                self.store.finish_scope_media(scope_id, media_id, "failed", str(error))
                errors.append({"media_id": media_id, "error": str(error)})
                if is_global_failure(error):
                    raise
        return errors


def is_global_failure(error):
    return isinstance(error, GraphError) and (error.status in (401,403,429) or error.status>=500
        or error.code in (4,10,17,32,190,200,613))


def collect_continuously(client, store, owner, start, end, account_id=None,
                         restart=False, resume_scope_id=None, max_requests=500,
                         max_media_pages=20, max_total_requests=5000, progress=None,
                         media_policy="all_accessible", discovery_pages=1, workers=1):
    """Keep one in-memory credential while resuming bounded local budgets.

    Remote failures stop immediately. No credential is exported, persisted, or
    reread between passes, and a total budget still prevents unbounded traversal.
    """
    if max_requests < 2 or max_total_requests < 2:
        raise PilotError("연속 수집의 요청 예산은 계정 확인을 포함해 2회 이상이어야 합니다.")
    if not isinstance(workers,int) or isinstance(workers,bool) or not 1<=workers<=5:
        raise PilotError("동시 조회 수는 1~5개로 지정해주세요.")
    started, passes, cursor_restarts = client.request_count, 0, 0
    began=time.perf_counter()
    while True:
        remaining = max_total_requests - (client.request_count - started)
        allowance = min(max_requests, remaining)
        if allowance < 2:
            if passes == 0:
                raise PilotError("계정 확인 뒤 수집할 요청 예산이 부족합니다.")
            result["reason"] = "total_request_budget_reached"
            break
        if hasattr(client, "max_requests"):
            client.max_requests = client.request_count + allowance
        def checkpoint(value):
            if progress:
                progress({**value, "pass": passes + 1, "api_requests": client.request_count - started})
        from .parallel import ParallelAccountCollector
        collector_class = ParallelAccountCollector if workers > 1 else AccountCollector
        extra = {"workers":workers} if workers > 1 else {}
        collector = collector_class(client, store, owner, max_media_pages=max_media_pages,
                                     max_requests=allowance, progress=checkpoint if progress else None,
                                     media_policy=media_policy, discovery_pages=discovery_pages,**extra)
        result = collector.collect_account(start, end, account_id=account_id,
            restart=restart if passes == 0 else False, resume_scope_id=resume_scope_id)
        passes += 1
        cursor_restarts += result["cursor_restarts"]
        resume_scope_id, account_id = result["scope_id"], result["account_id"]
        if progress:
            progress({"pass": passes, "api_requests": client.request_count - started,
                      "catalog_complete": result["catalog_complete"], "media_discovered": result["media_discovered"],
                      "media_completed": result["media_completed"], "media_pending": result["media_pending"],
                      "status": result["status"], "reason": result["reason"]})
        if result["status"] == "complete" or result["media_errors"]:
            break
        if result["reason"] not in {"request_budget_reached", "catalog_page_budget_reached"}:
            break
    result.update(api_requests=client.request_count - started, passes=passes,
                  max_total_requests=max_total_requests, cursor_restarts=cursor_restarts)
    result["elapsed_seconds"]=round(time.perf_counter()-began,3)
    return result
