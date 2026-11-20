import csv
import io

from app.config import SOURCE_COLUMNS
from app.contracts import key
from app.errors import Problem, RowError
from app.modules.m1_intake.reader import decode
from app.reasons import REASONS

SCAN_SOURCE = 'scans.csv'

# 每次提交后，相对上一版完整账，单张已占票场（票码+场次）的处置状态。
KEPT = 'kept'          # 保留：上一版采信的核销在本版仍是同一条
REPLACED = 'replaced'  # 替代：同票场由另一条（通常业务时间更早的）核销占票
ADDED = 'added'        # 新增：上一版未占票场，本版新采信
LOST = 'lost'          # 冲掉：本版完整账不再采信该票场（迟到旧表会呈现此项）
STATES = (KEPT, REPLACED, ADDED, LOST)

STATE_LABELS = {
    KEPT: '保留', REPLACED: '替代', ADDED: '新增', LOST: '冲掉',
}


def submitted_scan_ids(data):
    """从原始 scans.csv 抽取本快照登记过的全部核销流水号。

    只读流水号列，不做业务校验：隔离行（冲突、越窗等）同样代表场馆“登记过”
    这条核销，因此判断旧快照时不能只看采信结果。
    """
    text, _ = decode(data, SCAN_SOURCE)
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    header = None
    identifiers = set()
    try:
        for fields in reader:
            if not any(field.strip() for field in fields):
                continue
            if header is None:
                header = tuple(field.strip().lstrip('\ufeff') for field in fields)
                if set(header) != set(SOURCE_COLUMNS[SCAN_SOURCE]):
                    raise Problem(422, 'FILE_COLUMNS', {'source': SCAN_SOURCE})
                index = header.index('scan_id')
                continue
            if tuple(field.strip().lstrip('\ufeff') for field in fields) == header:
                continue
            if len(fields) != len(header):
                continue
            raw_id = fields[index].strip()
            if not raw_id:
                continue
            try:
                identifiers.add(key(raw_id))
            except RowError:
                identifiers.add(raw_id)
    except csv.Error as exc:
        raise Problem(422, 'FILE_CSV', {'source': SCAN_SOURCE}) from exc
    if header is None:
        raise Problem(422, 'FILE_BLANK', {'source': SCAN_SOURCE})
    return identifiers


def stale_reason(head_snapshot, head_sources, sources):
    """判断提交的完整核销表是否比当前账册更旧。

    旧表特征：当前账册已知的核销流水号在本快照里整批缺失，且本快照没有带回
    任何当前账册没见过的新流水号（即没有更新的信息）。若有新流水号，说明这是
    一份更新的完整账，缺失项属于正常的替代/作废，不得按旧表拦截。
    """
    if head_snapshot is None:
        return None
    current_ids = submitted_scan_ids(head_sources[SCAN_SOURCE])
    incoming_ids = submitted_scan_ids(sources[SCAN_SOURCE])
    missing = sorted(current_ids - incoming_ids)
    introduced = sorted(incoming_ids - current_ids)
    if missing and not introduced:
        return {'missing_scan_ids': missing, 'introduced_scan_ids': introduced,
                'current_run': head_snapshot['manifest']['run_id']}
    return None


def _scan_index(snapshot):
    return {(row['ticket'], row['show']): row for row in snapshot['scans']}


def _ledger_index(snapshot):
    return {(row['ticket'], row['show']): row for row in snapshot['funding']['ledger']}


def scan_diff(previous, current):
    """逐票场比较两版完整账的采信核销，输出保留/替代/新增/冲掉。

    应收影响取自结算账：场馆应收只认正常场次的有效核销，因此每条占票变化都能
    对应到该票场应收（分）的变动；迟到旧表冲掉的记录显示为负向影响。
    """
    before = _scan_index(previous) if previous else {}
    after = _scan_index(current)
    ledger_before = _ledger_index(previous) if previous else {}
    ledger_after = _ledger_index(current)
    rows = []
    for pair in sorted(set(before) | set(after)):
        old, new = before.get(pair), after.get(pair)
        if old and new:
            state = KEPT if old['scan_id'] == new['scan_id'] else REPLACED
            entry = new
        elif new:
            state, entry = ADDED, new
        else:
            state, entry = LOST, old
        payable_before = ledger_before.get(pair, {}).get('payable_cents', 0)
        payable_after = ledger_after.get(pair, {}).get('payable_cents', 0)
        rows.append({
            'state': state, 'state_label': STATE_LABELS[state],
            'ticket': pair[0], 'show': pair[1],
            'scan_id': entry['scan_id'], 'scanned_at': entry['scanned_at'],
            'gate': entry['gate'], 'line': entry['line'],
            'previous_scan_id': old['scan_id'] if old else '',
            'previous_line': old['line'] if old else None,
            'current_scan_id': new['scan_id'] if new else '',
            'payable_before_cents': payable_before, 'payable_after_cents': payable_after,
            'payable_delta_cents': payable_after - payable_before,
        })
    counts = {state: sum(1 for row in rows if row['state'] == state) for state in STATES}
    return rows, counts


def isolated_scans(snapshot):
    """本快照里核销表的拒收记录，附中文原因，解释迟到/重复/越窗为何不占票。"""
    rows = []
    for row in snapshot['quarantine']:
        if row['source'] != SCAN_SOURCE:
            continue
        rows.append({**row, 'reason': REASONS[row['code']]})
    return sorted(rows, key=lambda row: (row['line'], row['key']))


def batch_summary(run, state, previous_run=None, include_rows=True):
    snapshot = run['snapshot']
    previous = previous_run['snapshot'] if previous_run else None
    diff, counts = scan_diff(previous, snapshot)
    manifest = snapshot['manifest']
    totals = snapshot['funding']['totals']
    isolated = isolated_scans(snapshot)
    summary = {
        'run_id': manifest['run_id'], 'book': manifest['book'], 'state': state,
        'sequence': run['sequence'], 'generated_at': manifest['generated_at'],
        'rule_version': manifest['rule_version'], 'previous_run': manifest['previous_run'],
        'reused': state == 'reused',
        'scan_count': totals['scan_count'], 'payable_cents': totals['payable_cents'],
        'unsettled_cents': totals['unsettled_cents'], 'refunded_cents': totals['refunded_cents'],
        'net_cents': totals['net_cents'], 'isolated_scan_count': len(isolated),
        'diff_counts': counts,
    }
    if include_rows:
        summary['diff'] = diff
        summary['isolated_scans'] = isolated
    return summary
