import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.errors import Problem
from app.manifest import canonical, input_manifest, run_identifier
from app.modules.m8_batches.service import stale_reason
from app.pipeline import compute

SCHEMA = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')
INDEXES = (
    'CREATE INDEX IF NOT EXISTS run_head ON runs(book, sequence DESC)',
    "CREATE INDEX IF NOT EXISTS run_book_state ON runs(book, state, sequence DESC)",
)


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA foreign_keys = ON')
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode = WAL')
            db.executescript(SCHEMA)
            # 旧账册的 runs 表没有 state 列：先补列再建依赖该列的索引。
            columns = {row[1] for row in db.execute('PRAGMA table_info(runs)')}
            if 'state' not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN state TEXT NOT NULL DEFAULT 'current'")
            for statement in INDEXES:
                db.execute(statement)

    def latest(self, book=None):
        with closing(self.connect()) as db:
            if book is None:
                row = db.execute("SELECT payload FROM runs WHERE state = 'current' "
                                 'ORDER BY sequence DESC LIMIT 1').fetchone()
            else:
                row = db.execute("SELECT payload FROM runs WHERE book = ? AND state = 'current' "
                                 'ORDER BY sequence DESC LIMIT 1', (book,)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, run_id):
        return self.record(run_id)['snapshot']

    def record(self, run_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT id, sequence, state, payload FROM runs WHERE id = ?', (run_id,)).fetchone()
        if not row:
            raise Problem(404, 'RUN_NOT_FOUND', {'run_id': run_id})
        return {'run_id': row[0], 'sequence': row[1], 'state': row[2], 'snapshot': json.loads(row[3])}

    def history(self, book):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id, sequence, state, payload FROM runs WHERE book = ? ORDER BY sequence DESC',
                              (book,)).fetchall()
        return [{'run_id': row[0], 'sequence': row[1], 'state': row[2], 'snapshot': json.loads(row[3])}
                for row in rows]

    def _inputs(self, db, run_id):
        rows = db.execute('SELECT source, data FROM run_inputs WHERE run_id = ?', (run_id,)).fetchall()
        if not rows:
            raise Problem(404, 'RUN_NOT_FOUND', {'run_id': run_id})
        return dict(rows)

    def inputs(self, run_id):
        with closing(self.connect()) as db:
            return self._inputs(db, run_id)

    def commit(self, book, sources):
        inputs = input_manifest(sources)
        identifier = run_identifier(book, inputs)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload FROM runs WHERE id = ?', (identifier,)).fetchone()
            if existing:
                return json.loads(existing[0]), True
            origin = db.execute('SELECT payload FROM publications WHERE book = ?', (book,)).fetchone()
            head_row = db.execute('SELECT id, payload FROM runs WHERE book = ? ORDER BY sequence DESC LIMIT 1',
                                  (book,)).fetchone()
            if head_row:
                head = json.loads(head_row[1])
                # 旧快照拦截发生在任何写入之前：失败重传不会改动当前账册与历史批次。
                stale = stale_reason(head, self._inputs(db, head_row[0]), sources)
                if stale:
                    raise Problem(409, 'STALE_SCAN_SNAPSHOT', stale)
            else:
                head = None
            snapshot = compute(book, identifier, sources, json.loads(origin[0]) if origin else None, head)
            if origin is None:
                db.execute('INSERT INTO publications VALUES (?, ?)', (book, canonical(snapshot['publication'])))
            if head_row:
                db.execute("UPDATE runs SET state = 'history' WHERE id = ?", (head_row[0],))
            db.execute("INSERT INTO runs (id, book, payload, sequence, state) "
                       'SELECT ?, ?, ?, COALESCE(MAX(sequence), 0) + 1, ? FROM runs',
                       (identifier, book, canonical(snapshot), 'current'))
            db.executemany('INSERT INTO run_inputs VALUES (?, ?, ?)',
                           [(identifier, name, data) for name, data in sources.items()])
        return snapshot, False
