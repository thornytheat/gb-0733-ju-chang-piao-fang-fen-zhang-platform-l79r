import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.m8_batches.service import STATE_LABELS, scan_diff, stale_reason, submitted_scan_ids
from app.sources import sample_sources


def upload(client, files, book='batch-book'):
    return client.post('/api/runs', data={'book': book},
                       files=[('files', (name, payload, 'text/csv')) for name, payload in files.items()])


def test_stale_scan_snapshot_is_rejected_and_current_settlement_survives(client, upload, files):
    """旧表晚到：缺已登记核销且无新记录，拒绝且不改动当前批次。"""
    first = upload(files)
    assert first.status_code == 201
    run1 = first.json()['run_id']
    lines = files['scans.csv'].splitlines(keepends=True)
    stale = {**files, 'scans.csv': b''.join(ln for ln in lines if not ln.startswith(b'S110-B'))}
    rejected = upload(stale)
    assert (rejected.status_code, rejected.json()['code']) == (409, 'STALE_SCAN_SNAPSHOT')
    assert rejected.json()['detail']['missing_scan_ids'] == ['S110-B']
    assert rejected.json()['detail']['introduced_scan_ids'] == []
    # 当前完整账、应收与采信核销都没有被冲掉
    assert client.get('/api/batches').json()['items'][0]['run_id'] == run1
    head = client.get(f'/api/batches/{run1}').json()
    assert head['payable_cents'] == 135546 and head['scan_count'] == 7
    with sqlite3.connect(client.app.state.store.path) as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone() == (1,)


def test_snapshot_with_new_scan_id_is_accepted_even_when_it_drops_old_ones(client, upload, files):
    """带回新流水号即视为更新的完整账，缺失项按正常替代处理，不得误判为旧表。"""
    upload(files)
    lines = files['scans.csv'].splitlines(keepends=True)
    early = ('S115-E-EARLY,00115,E-MATINEE,2026-11-19T15:01:00+08:00,海棠二门\n').encode()
    newer = {**files, 'scans.csv': files['scans.csv'] + early}
    result = upload(newer)
    assert result.status_code == 201 and result.json()['reused'] is False
    run2 = result.json()['run_id']
    detail = client.get(f'/api/batches/{run2}').json()
    replaced = [row for row in detail['diff'] if row['state'] == 'replaced']
    assert [(row['ticket'], row['previous_scan_id'], row['current_scan_id']) for row in replaced] == [
        ('00115', 'S115-E', 'S115-E-EARLY')]
    assert detail['diff_counts'] == {'kept': 6, 'replaced': 1, 'added': 0, 'lost': 0}


def test_resending_identical_snapshot_creates_no_new_record_and_keeps_head(client, upload, files):
    first = upload(files)
    run1 = first.json()['run_id']
    again = upload(files)
    assert again.status_code == 200 and again.json()['reused'] is True
    assert again.json()['run_id'] == run1
    rerun = client.post(f'/api/runs/{run1}/rerun')
    assert rerun.status_code == 200 and rerun.json()['manifest'] == first.json()['manifest']
    history = client.get('/api/batches', params={'run': run1}).json()
    assert history['total'] == 1 and history['items'][0]['state'] == 'current'


def test_history_keeps_older_run_and_marks_latest_current_with_first_batch_all_added(client, upload, files):
    first = upload(files)
    run1 = first.json()['run_id']
    lines = files['scans.csv'].splitlines(keepends=True)
    early = ('S115-E-EARLY,00115,E-MATINEE,2026-11-19T15:01:00+08:00,海棠二门\n').encode()
    run2 = upload({**files, 'scans.csv': files['scans.csv'] + early}).json()['run_id']
    items = client.get('/api/batches', params={'run': run2}).json()['items']
    assert [(item['sequence'], item['state']) for item in items] == [(2, 'current'), (1, 'history')]
    first_detail = client.get(f'/api/batches/{run1}').json()
    # 首版没有前置批次，全部采信核销都解释为新增，不存在冲掉
    assert first_detail['diff_counts'] == {'kept': 0, 'replaced': 0, 'added': 7, 'lost': 0}
    # 历史批次仍可按编号读取，首页只呈现当前账（run2）
    assert client.get('/api/batches/' + run1).status_code == 200
    assert client.get('/api/batches').json()['items'][0]['run_id'] == run2
    assert run1 not in client.get('/').text


def test_batch_rows_endpoint_pages_changes_and_explains_late_isolated_scans(client, upload, files):
    run1 = upload(files).json()['run_id']
    page = client.get(f'/api/batches/{run1}/rows', params={'view': 'changes', 'limit': 3})
    assert page.status_code == 200 and page.json()['total'] == 7 and len(page.json()['items']) == 3
    late = client.get(f'/api/batches/{run1}/rows', params={'view': 'isolated', 'q': 'SCAN_REPEAT'})
    keys = {item['key']: item for item in late.json()['items']}
    assert set(keys) == {'S107-B-LATE', 'S117-Y'}
    assert keys['S107-B-LATE']['reason'] == '该票场组合已有更早的有效核销'
    assert keys['S107-B-LATE']['detail']['retained_scan'] == 'S107-B'
    bad_view = client.get(f'/api/batches/{run1}/rows', params={'view': 'nope'})
    assert (bad_view.status_code, bad_view.json()['code']) == (422, 'VIEW_UNKNOWN')
    missing = client.get('/api/batches/missing-run')
    assert (missing.status_code, missing.json()['code']) == (404, 'RUN_NOT_FOUND')


def test_batches_web_page_renders_for_preview_and_published_book(client, upload, files):
    run1 = upload(files).json()['run_id']
    preview = client.get('/batches')
    assert preview.status_code == 200 and '核销批次核对' in preview.text and '最终场馆应收' in preview.text
    published = client.get('/batches', params={'run': run1})
    assert published.status_code == 200 and run1 in published.text
    assert 'data-run="' + run1 + '"' in published.text
    home = client.get('/', params={'run': run1}).text
    assert '/batches?run=' + run1 in home


def test_legacy_database_without_state_column_migrates_and_keeps_working(tmp_path, files):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE publications (book TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE runs (id TEXT PRIMARY KEY, book TEXT NOT NULL, payload TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE);
        CREATE TABLE run_inputs (run_id TEXT NOT NULL, source TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY (run_id, source));
        """)
    with TestClient(create_app(path)) as client:
        response = upload(client, files)
        assert response.status_code == 201
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT state FROM runs").fetchone() == ('current',)
            assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='run_book_state'").fetchone()
        # 重新初始化须幂等
        client.app.state.store.initialize()


def test_scan_diff_and_stale_helpers_operate_on_registered_ids_including_isolated():
    files = sample_sources()
    ids = submitted_scan_ids(files['scans.csv'])
    # S-CLASH 虽被隔离，仍代表场馆登记过的流水号，旧表判断必须计入
    assert {'S-CLASH', 'S110-B', 'S107-B-LATE'} <= ids
    head = {'manifest': {'run_id': 'run_head'}}
    head_sources = {'scans.csv': files['scans.csv']}
    dropped = {'scans.csv': files['scans.csv'].replace(b'S110-B,00110', b'X-DROP,00110')}
    # 用一条全新流水号替换：有新信息，不算旧表
    assert stale_reason(head, head_sources, dropped) is None
    only_missing = {'scans.csv': b'\n'.join(
        line for line in files['scans.csv'].splitlines() if not line.startswith(b'S110-B')) + b'\n'}
    reason = stale_reason(head, head_sources, only_missing)
    assert reason == {'missing_scan_ids': ['S110-B'], 'introduced_scan_ids': [], 'current_run': 'run_head'}
    assert set(STATE_LABELS) == {'kept', 'replaced', 'added', 'lost'}
    rows, counts = scan_diff(None, {'scans': [], 'funding': {'ledger': []}})
    assert rows == [] and counts == {'kept': 0, 'replaced': 0, 'added': 0, 'lost': 0}
