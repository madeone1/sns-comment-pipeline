"""Bounded GET fetches; all SQLite operations remain on the caller thread."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .account import AccountCollector, is_global_failure
from .api import GraphError, PilotError, RequestBudgetExceeded, next_cursor, page_data
from .collector import COMMENT_FIELDS, PARENT_FIELDS


class ParallelAccountCollector(AccountCollector):
    def __init__(self,*args,workers=3,**kwargs):
        super().__init__(*args,**kwargs)
        if not isinstance(workers,int) or isinstance(workers,bool) or not 1<=workers<=5:
            raise PilotError("동시 조회 수는 1~5개로 지정해주세요.")
        self.workers=workers

    def _fetch(self,edge,may_restart):
        parent=edge["parent_id"]
        path=f"{parent}/replies" if parent else f"{edge['media_id']}/comments"
        fields=COMMENT_FIELDS if parent else PARENT_FIELDS
        try:
            return self._get(path,fields,edge["next_cursor"]),False
        except GraphError as error:
            if error.code!=100 or edge["next_cursor"] is None or not may_restart:
                raise
            return self._get(path,fields),True

    def _apply(self,scope_id,run_id,account_id,edge,payload,reset,seen,restarted,in_progress):
        key=(edge["media_id"],edge["edge"])
        if reset:
            seen[key]={None}
            restarted.add(key)
            self.cursor_restarts+=1
        else:
            seen.setdefault(key,set()).add(edge["next_cursor"])
        items,following=page_data(payload),next_cursor(payload)
        items,following=self.window_page(scope_id,items,following,edge['parent_id'])
        if following is not None and following in seen[key]:
            raise PilotError("댓글·답글 페이지 커서가 반복됩니다. 미완료로 남깁니다.")
        expansions=[]
        if edge["parent_id"] is None:
            for item in items:
                if not isinstance(item.get("id"),str) or not item["id"].isdigit():
                    raise PilotError("댓글 ID 형식이 올바르지 않습니다.")
                if "replies" in item:
                    value=item["replies"]
                    if not isinstance(value,dict):
                        raise PilotError("답글 확장 응답 형식이 올바르지 않습니다.")
                    page_data(value)
                    next_cursor(value)
                    expansions.append((item["id"],value))
        self.store.save_page(run_id,account_id,edge["media_id"],edge["edge"],items,following,
            self.owner,parent_id=edge["parent_id"],scope_id=scope_id)
        # Save already fetched reply expansions before scheduling more work.
        # Missing expansion remains unknown and receives a real GET request.
        for parent,value in expansions:
            reply=self.store.scope_edge(scope_id,edge["media_id"],"replies:"+parent)
            if (edge["media_id"],reply["edge"]) in in_progress:
                continue
            if not reply["complete"] and reply["next_cursor"] is None:
                self._apply(scope_id,run_id,account_id,reply,value,False,seen,restarted,in_progress)
        self._notify(scope_id,"parallel_comments")

    def _collect_media(self,scope_id,run_id,account_id):
        if self.workers==1:
            return super()._collect_media(scope_id,run_id,account_id)
        active={r["media_id"] for r in self.store.pending_scope_media(scope_id)}
        failed,errors,seen,restarted=set(),[],{},set()
        inflight={}
        budget_hit=False
        fatal=None
        for media_id in list(active):
            if not self.store.pending_scope_edges(scope_id,media_id):
                self.store.finish_scope_media(scope_id,media_id,"complete")
                active.discard(media_id)
            else:
                self.store.finish_scope_media(scope_id,media_id,"running")
        self.store.stage(run_id,"parallel_comments",account_id)
        with ThreadPoolExecutor(max_workers=self.workers,thread_name_prefix="sns-get") as pool:
            while active or inflight:
                if not budget_hit and fatal is None:
                    busy={(e["media_id"],e["edge"]) for e in inflight.values()}
                    for media_id in sorted(active-failed):
                        for edge in self.store.pending_scope_edges(scope_id,media_id):
                            key=(media_id,edge["edge"])
                            if key in busy:
                                continue
                            if len(inflight)>=self.workers:
                                break
                            if self._issued_requests>=self.max_requests:
                                budget_hit=True
                                break
                            future=pool.submit(self._fetch,edge,key not in restarted)
                            inflight[future]=edge
                            busy.add(key)
                        if len(inflight)>=self.workers or budget_hit:
                            break
                if not inflight:
                    break
                done,_=wait(inflight,timeout=0.2,return_when=FIRST_COMPLETED)
                # Establish global stop conditions before processing successes
                # from the same completed batch, then drain admitted GETs.
                outcomes=[]
                for future in done:
                    edge=inflight.pop(future)
                    try:
                        outcomes.append((edge,future.result(),None))
                    except RequestBudgetExceeded as error:
                        budget_hit=True
                        outcomes.append((edge,None,error))
                    except PilotError as error:
                        if is_global_failure(error):
                            fatal=error
                        outcomes.append((edge,None,error))
                unapplied={(e["media_id"],e["edge"]) for e,_,_ in outcomes}
                busy_edges={(e["media_id"],e["edge"]) for e in inflight.values()}
                for edge,value,error in outcomes:
                    media_id=edge["media_id"]
                    if error is None:
                        try:
                            self._apply(scope_id,run_id,account_id,edge,*value,seen,restarted,busy_edges|unapplied)
                        except PilotError as failure:
                            error=failure
                    if error is not None and not isinstance(error,RequestBudgetExceeded):
                        if media_id not in failed:
                            failed.add(media_id)
                            errors.append({"media_id":media_id,"error":str(error)})
                        self.store.finish_scope_media(scope_id,media_id,"failed",str(error))
                    unapplied.discard((media_id,edge["edge"]))
                    still_busy=any(mid==media_id for mid,_ in busy_edges|unapplied)
                    if media_id not in failed and not still_busy and not self.store.pending_scope_edges(scope_id,media_id):
                        self.store.finish_scope_media(scope_id,media_id,"complete")
                        active.discard(media_id)
                active-=failed
        for media_id in active:
            if media_id not in failed:
                self.store.finish_scope_media(scope_id,media_id,"partial")
        if fatal is not None:
            raise fatal
        if budget_hit:
            raise RequestBudgetExceeded("동시 조회의 실행 예산에 도달했습니다. 완료된 응답을 저장하고 재개 위치를 유지했습니다.")
        return errors
