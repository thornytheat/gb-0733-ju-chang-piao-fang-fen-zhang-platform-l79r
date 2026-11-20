import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.browse import Browse
from app.config import PREVIEW
from app.modules.m8_batches.service import batch_summary

router = APIRouter(prefix='/api/batches')


class BatchBrowse(Browse):
    def __init__(self, q: Annotated[str, Query(max_length=64)] = '',
                 show: Annotated[str, Query(max_length=64)] = '',
                 view: Annotated[str, Query(max_length=20)] = 'changes',
                 offset: Annotated[int, Query(ge=0)] = 0,
                 limit: Annotated[int, Query(ge=1, le=200)] = 50):
        super().__init__(q, show, view, offset, limit)


def preview_record(request):
    return {'run_id': PREVIEW, 'sequence': 0, 'state': 'current', 'snapshot': request.app.state.preview}


def chain(store, run_id):
    """返回当前批次记录及其账册内上一版（按发布序号）。"""
    record = store.record(run_id)
    with store.connect() as db:
        row = db.execute('SELECT id, sequence, state, payload FROM runs WHERE book = ? AND sequence < ? '
                         'ORDER BY sequence DESC LIMIT 1',
                         (record['snapshot']['manifest']['book'], record['sequence'])).fetchone()
    previous = ({'run_id': row[0], 'sequence': row[1], 'state': row[2], 'snapshot': json.loads(row[3])}
                if row else None)
    return record, previous


def summarize(request, store, run_id):
    if run_id == PREVIEW:
        return batch_summary(preview_record(request), 'current')
    record, previous = chain(store, run_id)
    return batch_summary(record, record['state'], previous)


@router.get('')
def batches(request: Request, run: str = ''):
    """账册内全部提交批次（含历史），按发布序号倒序；默认取当前账册。"""
    store = request.app.state.store
    if run == PREVIEW:
        return {'items': [batch_summary(preview_record(request), 'current', include_rows=False)], 'total': 1}
    snapshot = store.get(run) if run else store.latest()
    if snapshot is None:
        return {'items': [batch_summary(preview_record(request), 'current', include_rows=False)], 'total': 1}
    records = store.history(snapshot['manifest']['book'])
    items = [batch_summary(record, record['state'],
                           records[index + 1] if index + 1 < len(records) else None,
                           include_rows=False)
             for index, record in enumerate(records)]
    return {'items': items, 'total': len(items)}


@router.get('/{run_id}')
def batch_detail(request: Request, run_id: str):
    return summarize(request, request.app.state.store, run_id)


@router.get('/{run_id}/rows')
def batch_rows(request: Request, run_id: str, browse: BatchBrowse = Depends()):
    """批次核对明细：changes 为保留/替代/新增/冲掉，isolated 为核销拒收记录。"""
    summary = summarize(request, request.app.state.store, run_id)
    rows = browse.choose({'changes': summary['diff'], 'isolated': summary['isolated_scans']})
    return browse.page(rows, run_id=run_id, diff_counts=summary['diff_counts'])
