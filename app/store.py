import sqlite3, json, os
from datetime import datetime, timezone
from pathlib import Path
from app.errors import DomainError


def connection():
    path = Path(os.environ.get('DB_PATH', 'data/records.db'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    _migrate(db)
    return db


def _migrate(db):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(runs)')}
    for name, ddl in (
        ('activated', 'ALTER TABLE runs ADD COLUMN activated INTEGER NOT NULL DEFAULT 0'),
        ('superseded_by', 'ALTER TABLE runs ADD COLUMN superseded_by TEXT'),
        ('settled_at', 'ALTER TABLE runs ADD COLUMN settled_at TEXT'),
    ):
        if name not in columns:
            db.execute(ddl)
    db.execute('CREATE TABLE IF NOT EXISTS payments (run_id TEXT PRIMARY KEY, receipt TEXT NOT NULL, amount TEXT NOT NULL, total TEXT NOT NULL, created_at TEXT NOT NULL)')


def _sealable(body):
    return bool(body.get('reconcile', {}).get('sealable'))


def _receipt(run_id, body):
    evidence = body.get('archive', {}).get('sha256') or ''
    return 'PAY-' + run_id + '-' + evidence[:12]


def _switch(db, run_id):
    """把可封存批次切为当前结算；旧结算标记为被取代，尾差随之失效但记录保留。"""
    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE runs SET activated=0, superseded_by=? WHERE activated=1 AND id<>?', (run_id, run_id))
    db.execute('UPDATE runs SET activated=1, superseded_by=NULL, settled_at=COALESCE(settled_at,?) WHERE id=?', (now, run_id))


def _pay(db, run_id, body):
    """复核通过后才形成付款记录；同一批重跑或重复复核复用同一回执，不重复付款。"""
    payroll = body.get('reconcile', {}).get('payroll', {})
    now = datetime.now(timezone.utc).isoformat()
    db.execute('INSERT OR IGNORE INTO payments(run_id,receipt,amount,total,created_at) VALUES (?,?,?,?,?)',
               (run_id, _receipt(run_id, body), payroll.get('total', '0'), payroll.get('total', '0'), now))


def save(run_id, body):
    """保存批次与原始证据；只有复核通过才切换结算、形成付款记录。"""
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)', (run_id, json.dumps(body, ensure_ascii=False)))
        # latest 表示最近一次尝试，用于页面回看；是否成为当前结算由 activated 决定。
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (run_id,))
        # 新算一律先以草稿保存；只有复核通过（且对账结平）才切换当前结算。
        # 因此切换失败或复核退回时，旧版结算、旧尾差与付款记录原样保留。
    return get(run_id)


def active_run():
    with connection() as db:
        row = db.execute('SELECT id FROM runs WHERE activated=1 ORDER BY settled_at DESC, rowid DESC LIMIT 1').fetchone()
    return row['id'] if row else None


def get(run_id):
    with connection() as db:
        r = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        payment = db.execute('SELECT * FROM payments WHERE run_id=?', (run_id,)).fetchone() if r else None
    if not r:
        raise DomainError('NOT_FOUND', '批次不存在', 404)
    body = json.loads(r['body'])
    body.update(run_id=r['id'], review_version=r['version'], decision=r['decision'])
    active = bool(r['activated'])
    superseded = r['superseded_by']
    status = 'active' if active else ('superseded' if superseded else 'draft')
    balanced = bool(body.get('reconcile', {}).get('sealable'))
    note = {'active': '可回查',
            'superseded': '旧尾差已失效，原始记录保留供回查',
            'draft': '对账已结平，待复核封存' if balanced else '对账未平，尾差尚未形成封存证据'}[status]
    body['settlement'] = {
        'status': status,
        'label': {'active': '当前结算', 'superseded': '已被新版取代', 'draft': '未封存草稿'}[status],
        'superseded_by': superseded,
        'settled_at': r['settled_at'],
        'residual_valid': active,
        'residual_note': note,
        'payment': None if not payment else {
            'receipt': payment['receipt'], 'amount': payment['amount'],
            'total': payment['total'], 'created_at': payment['created_at'],
        },
        'paid': payment is not None,
    }
    return body


def history():
    with connection() as db:
        rows = db.execute("SELECT id,version,decision,activated,superseded_by FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 WHEN activated=1 THEN 1 ELSE 2 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def review(run_id, version, decision):
    if decision not in ('通过', '退回'):
        raise DomainError('DECISION', '复核状态无效')
    with connection() as db:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND', '批次不存在', 404)
        if row['superseded_by']:
            raise DomainError('STALE', '该结算已被新版取代，请复核当前版本', 409)
        body = json.loads(row['body'])
        if decision == '通过' and not _sealable(body):
            raise DomainError('UNSEALED', '员工应付与批次总额尚未结平，不能封存', 422)
        cur = db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?', (decision, run_id, version))
        if cur.rowcount != 1:
            raise DomainError('STALE', '复核版本已更新，请刷新', 409)
        if decision == '退回' and row['activated']:
            # 已付款结算不允许退回；抛错回滚上面的版本更新。
            raise DomainError('SEALED', '已形成付款的结算不能退回，请提交新批次覆盖', 409)
        if decision == '通过':
            _switch(db, run_id)
            _pay(db, run_id, body)
    return get(run_id)
