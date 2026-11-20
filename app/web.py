from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.browse import snapshot_for
from app.config import PREVIEW, REPORT_ZONE, ROOT
from app.modules.m8_batches.handler import chain
from app.modules.m8_batches.service import batch_summary

router = APIRouter()
templates = Jinja2Templates(directory=ROOT / 'app' / 'templates')
MODULES = (
    ('intake', '票源导入'), ('rights', '套票权益'), ('scans', '入场核销'), ('allocation', '权重分账'),
    ('refunds', '退款冲正'), ('reconcile', '场馆勾稽'), ('evidence', '结算凭证'),
)
RAIL_MODULES = MODULES + (('batches', '核销批次核对'),)


def money(amount):
    integer, decimal = divmod(abs(amount), 100)
    return f'{"−" if amount < 0 else ""}{integer:,}.{decimal:02d}'


templates.env.filters['money'] = money


@router.get('/', response_class=HTMLResponse)
def home(request: Request, run: str = '', notice: str = ''):
    snapshot = snapshot_for(request, run) if run else request.app.state.store.latest()
    snapshot = snapshot or request.app.state.preview
    manifest = snapshot['manifest']
    local_time = datetime.fromisoformat(manifest['generated_at']).astimezone(ZoneInfo(REPORT_ZONE))
    return templates.TemplateResponse(request, 'index.html', {
        'snapshot': snapshot, 'manifest': manifest, 'funding': snapshot['funding'],
        'preview': manifest['run_id'] == 'sample', 'modules': MODULES, 'rail_modules': RAIL_MODULES,
        'local_time': local_time.strftime('%m-%d %H:%M:%S'),
        'notice': {'published': '批次已发布', 'reused': '已复用原批次'}.get(notice, ''),
    })


@router.get('/batches', response_class=HTMLResponse)
def batches_page(request: Request, run: str = ''):
    store = request.app.state.store
    is_preview = not run or run == PREVIEW
    if is_preview:
        current_record = {'run_id': PREVIEW, 'sequence': 0, 'state': 'current',
                          'snapshot': request.app.state.preview}
        history = []
        summary = batch_summary(current_record, 'current')
    else:
        current_record, previous = chain(store, run)
        history = store.history(current_record['snapshot']['manifest']['book'])
        summary = batch_summary(current_record, current_record['state'], previous)
    local_time = datetime.fromisoformat(summary['generated_at']).astimezone(ZoneInfo(REPORT_ZONE))
    return templates.TemplateResponse(request, 'batches.html', {
        'summary': summary, 'history': history, 'preview': is_preview,
        'rail_modules': RAIL_MODULES, 'local_time': local_time.strftime('%m-%d %H:%M:%S'),
    })
