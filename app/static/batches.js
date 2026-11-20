const $ = selector => document.querySelector(selector);
const run = document.body.dataset.run;
const view = $('#batch-view');
const status = $('#batch-status');
const search = $('#batch-filter');
const back = $('#batch-back');
const next = $('#batch-next');
const limit = 20;
let offset = 0;
let request;

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
const numberFormat = new Intl.NumberFormat('zh-CN');
const money = cents => {
  const digits = Math.abs(cents);
  return `${cents < 0 ? '−' : ''}${numberFormat.format(Math.floor(digits / 100))}.${String(digits % 100).padStart(2, '0')}`;
};

const COLUMNS = {
  changes: [
    ['state_label', '处置', 'state'], ['ticket', '票码'], ['show', '场次'],
    ['previous_scan_id', '原核销单号'], ['current_scan_id', '现核销单号'],
    ['scanned_at', '入场时间', 'time'], ['line', '源行', 'row'],
    ['payable_before_cents', '原应收 ¥', 'money'], ['payable_after_cents', '现应收 ¥', 'money'],
    ['payable_delta_cents', '应收变动 ¥', 'money'],
  ],
  isolated: [
    ['key', '核销单号'], ['code', '原因码'], ['reason', '拒收原因'],
    ['line', '源行', 'row'], ['raw', '原值', 'raw'],
  ],
};

function detailsCell(row) {
  const cell = document.createElement('td');
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = '查看';
  const code = document.createElement('code');
  code.className = 'raw';
  code.textContent = JSON.stringify(row.raw, null, 2);
  details.append(summary, code);
  cell.append(details);
  return cell;
}

function displayCell(row, key, type) {
  const cell = document.createElement('td');
  if (['money', 'row'].includes(type)) cell.className = 'num';
  if (type === 'raw') return detailsCell(row);
  const text = document.createElement('span');
  const value = row[key];
  if (type === 'time') {
    text.textContent = dateFormatter.format(new Date(value));
  } else if (type === 'money') {
    text.textContent = value === null || value === undefined ? '—' : money(value);
  } else if (type === 'state') {
    text.textContent = value || '—';
    text.className = `state-tag state-tag--${row.state}`;
  } else {
    text.textContent = value === null || value === undefined || value === '' ? '—' : value;
  }
  text.title = text.textContent;
  cell.append(text);
  return cell;
}

function render(columns, rows, message, error = false) {
  const header = document.createElement('tr');
  for (const [, label, type] of columns) {
    const heading = document.createElement('th');
    heading.scope = 'col';
    heading.textContent = label;
    if (['money', 'row'].includes(type)) heading.className = 'num';
    header.append(heading);
  }
  $('#batch-columns').replaceChildren(header);
  const body = $('#batch-rows');
  body.replaceChildren();
  if (!rows.length) {
    const empty = body.insertRow().insertCell();
    empty.colSpan = columns.length;
    empty.className = error ? 'empty error' : 'empty';
    empty.textContent = message;
    return;
  }
  for (const row of rows) {
    const tr = body.insertRow();
    tr.classList.add(`row--${row.state || 'isolated'}`);
    for (const [key, , type] of columns) tr.append(displayCell(row, key, type || 'text'));
  }
}

async function refresh() {
  request?.abort();
  const controller = new AbortController();
  request = controller;
  const columns = COLUMNS[view.value];
  const query = new URLSearchParams({view: view.value, q: search.value, offset, limit});
  status.textContent = '读取中…';
  render(columns, [], '读取批次明细…');
  back.disabled = next.disabled = true;
  try {
    const response = await fetch(`/api/batches/${encodeURIComponent(run)}/rows?${query}`, {signal: controller.signal});
    if (!response.ok) throw new Error('读取失败，请重新筛选。');
    const data = await response.json();
    if (request !== controller) return;
    render(columns, data.items, '无匹配记录，请调整筛选。');
    status.textContent = `${data.total} 行${data.items.length ? ` · ${offset + 1}–${offset + data.items.length}` : ''}`;
    back.disabled = offset === 0;
    next.disabled = offset + limit >= data.total;
  } catch (error) {
    if (error.name === 'AbortError' || request !== controller) return;
    status.textContent = '读取失败';
    render(columns, [], '读取失败，请点击筛选重试。', true);
  }
}

async function fillHistory() {
  const cells = document.querySelectorAll('[data-summary]');
  if (!cells.length) return;
  const runIds = [...new Set([...cells].map(cell => cell.dataset.summary))];
  const summaries = new Map();
  await Promise.all(runIds.map(async id => {
    const response = await fetch(`/api/batches/${encodeURIComponent(id)}`);
    if (response.ok) summaries.set(id, await response.json());
  }));
  cells.forEach(cell => {
    const data = summaries.get(cell.dataset.summary);
    if (!data) return;
    const field = cell.dataset.field;
    if (field === 'payable_cents') cell.textContent = money(data.payable_cents);
    else if (field === 'scan_count') cell.textContent = numberFormat.format(data.scan_count);
    else cell.textContent = data.diff_counts[field];
  });
}

$('#batch-filters').addEventListener('submit', event => { event.preventDefault(); offset = 0; refresh(); });
view.addEventListener('change', () => { offset = 0; refresh(); });
back.addEventListener('click', () => { offset = Math.max(0, offset - limit); refresh(); });
next.addEventListener('click', () => { offset += limit; refresh(); });
refresh();
fillHistory();
