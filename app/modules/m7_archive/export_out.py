import io, csv, json, hashlib


def _row(writer, kind, rid, line, business_id, code, content):
    writer.writerow([kind, rid, line, business_id, code, json.dumps(content, ensure_ascii=False, sort_keys=True)])


def bundle(state, include_settlement=True):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['记录类型', '批次', '来源行', '业务ID', '原因码', '内容'])
    rid = state.get('run_id', 'preview')
    _row(writer, '运行清单', rid, '', '', '', state.get('manifest', {}))
    current = state.get('settlement') or {}
    if include_settlement and current.get('status'):
        _row(writer, '结算状态', rid, '', '', current.get('status', ''),
             {'label': current.get('label', ''), 'residual_valid': current.get('residual_valid'),
              'superseded_by': current.get('superseded_by'), 'payment': current.get('payment')})
    _row(writer, '对账', rid, '', '', '', state.get('reconcile', {}))
    for row in state.get('calculations', []):
        _row(writer, '计算', rid, row.get('line', ''), row.get('id', ''), row.get('code', ''), row)
    for row in state.get('payables', []):
        _row(writer, '员工应付', rid, row.get('carrier_line', ''), row.get('employee', ''), 'PAYABLE', row)
    for row in state.get('residuals', []):
        _row(writer, '尾差', rid, row.get('line', ''), row.get('employee', ''), row.get('level', ''), row)
    for row in state.get('batch', {}).get('adjustments', []):
        _row(writer, '尾差', rid, row.get('line', ''), row.get('employee', ''), row.get('level', ''), row)
    if state.get('batch'):
        _row(writer, '批次总额', rid, '', '', 'BATCH', state['batch'])
    for row in state.get('quarantine', []):
        _row(writer, '隔离', rid, row['line'], row.get('id', ''), row['code'], row.get('raw', {}))
    return '﻿' + out.getvalue()


def checksum(state):
    # 封存校验覆盖计算证据，不含随复核/付款变化的结算状态行。
    return hashlib.sha256(bundle(state, include_settlement=False).encode('utf-8')).hexdigest()


def verify(state):
    stored = state.get('archive', {}).get('sha256')
    return bool(stored) and stored == checksum(state)
