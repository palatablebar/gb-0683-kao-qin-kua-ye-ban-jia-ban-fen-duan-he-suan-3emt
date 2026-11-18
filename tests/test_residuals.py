from copy import deepcopy
from decimal import Decimal

import pytest

from app import store
from app.errors import DomainError
from app.workflow import run

HEADER = ('id,employee,shift_day,version,punch,at,shift_start,shift_end,'
          'break_start,break_end,hourly_rate,paid,note\n')


def csv_bytes(employees=('E101', 'E102'), rate='10.01', minutes=30):
    """两名员工同日各一小段，精确额 5.005 元，逐段落分会各进 1 分。"""
    end_h, end_m = divmod(9 * 60 + minutes, 60)
    rows = []
    for index, employee in enumerate(employees, 1):
        common = ('%s,2026-10-02,1,%%s,%%s,09:00,12:00,11:00,12:00,%s,0,x\n'
                  % (employee, rate))
        rows.append('%05d,' % index + common % ('IN', '2026-10-02T09:00:00+08:00'))
        rows.append('%05d,' % (index + 100) + common % ('OUT', '2026-10-02T%02d:%02d:00+08:00' % (end_h, end_m)))
    return (HEADER + ''.join(rows)).encode()


@pytest.fixture
def residual_state():
    return run(csv_bytes(), {}, 7)


def test_piece_precision_is_preserved(residual_state):
    # 裁决：先保留片段精度，片段金额不因批次补差被改写
    for row in residual_state['calculations']:
        assert row['exact_amount'] == '5.005'
        assert Decimal(row['amount']) == Decimal('5.01')


def test_employee_payable_rounds_once_from_exact_total(residual_state):
    for payable in residual_state['payables']:
        assert payable['exact_total'] == '5.005'
        assert payable['payable'] == '5.01'
        assert payable['residual'] == '0.005'


def test_batch_total_rounds_once_and_is_exact(residual_state):
    batch = residual_state['batch']
    # 两段精确合计 10.010，批次总额一次落分 10.01；逐段落分之和为 10.02
    assert batch['exact_total'] == '10.01'
    assert batch['total'] == '10.01'
    assert batch['sum_payables'] == '10.02'
    assert batch['residual'] == '-0.01'
    assert batch['residual_cents'] == -1
    assert Decimal(batch['sum_final']) == Decimal('10.01')


def test_batch_residual_is_attributed_to_named_piece(residual_state):
    batch = residual_state['batch']
    assert len(batch['adjustments']) == 1
    adjustment = batch['adjustments'][0]
    assert adjustment['level'] == 'BATCH'
    assert adjustment['employee'] == 'E101'
    assert adjustment['piece'].startswith('E101/2026-10-02|')
    assert adjustment['cents'] == -1
    assert '最大余数法' in adjustment['reason']
    entry = next(p for p in residual_state['payables'] if p['employee'] == 'E101')
    assert entry['final_payable'] == '5.00' and entry['batch_adjustment_cents'] == -1
    other = next(p for p in residual_state['payables'] if p['employee'] == 'E102')
    assert other['final_payable'] == '5.01' and other['batch_adjustment_cents'] == 0


def test_reconcile_includes_residuals_and_balances(residual_state):
    payroll = residual_state['reconcile']['payroll']
    assert payroll['settled'] is True
    assert payroll['attribution_complete'] is True
    assert payroll['residual_count'] == 2 + 1  # 2 条员工层 + 1 条批次层
    assert payroll['batch_cents'] == payroll['residual_cents'] == -1
    assert payroll['difference'] == '0.00'


def test_each_piece_standalone_is_explainable(residual_state):
    # 同一员工多段时，任一片段的精确额与倍率只取决于自身工时与费率
    calc = dict(residual_state['calculations'][0])
    assert Decimal(calc['exact_amount']) == Decimal(calc['minutes']) * Decimal(calc['multiplier']) * Decimal(calc['rate']) / 60


def test_export_contains_residual_and_piece_evidence(residual_state):
    from app.modules.m7_archive.export_out import bundle, checksum
    text = bundle(residual_state)
    assert '员工应付' in text and '尾差' in text and '批次总额' in text
    assert residual_state['batch']['adjustments'][0]['reason'] in text
    assert checksum(residual_state) == residual_state['archive']['sha256']
    assert residual_state['archive']['verified'] is True


def test_same_run_never_pays_twice(residual_state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(residual_state['run_id'], residual_state)
    store.review(residual_state['run_id'], 1, '通过')
    store.review(residual_state['run_id'], 2, '通过')
    store.save(residual_state['run_id'], residual_state)
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM payments').fetchone()[0] == 1
    assert store.get(residual_state['run_id'])['settlement']['payment']['total'] == '10.01'


def test_new_rule_or_source_invalidates_old_residual(residual_state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    old = deepcopy(residual_state)
    store.save(old['run_id'], old)
    store.review(old['run_id'], 1, '通过')

    changed = run(csv_bytes(rate='10.02'), {}, 7)
    assert changed['run_id'] != old['run_id']  # 输入字节变化 → 新幂等键
    store.save(changed['run_id'], changed)
    assert store.get(old['run_id'])['settlement']['status'] == 'active'  # 新算未复核，不切旧结算
    store.review(changed['run_id'], 1, '通过')

    superseded = store.get(old['run_id'])
    assert superseded['settlement']['status'] == 'superseded'
    assert superseded['settlement']['residual_valid'] is False
    # 旧批次记录、原始片段与当时尾差全部保留可回查
    assert superseded['source_b64']
    assert superseded['calculations'] == old['calculations']
    assert superseded['batch']['adjustments'] == old['batch']['adjustments']
    assert store.get(changed['run_id'])['settlement']['status'] == 'active'
    with pytest.raises(DomainError) as error:
        store.review(old['run_id'], superseded['review_version'], '通过')
    assert error.value.status == 409


def test_failed_switch_keeps_previous_settlement(residual_state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(residual_state['run_id'], residual_state)
    store.review(residual_state['run_id'], 1, '通过')
    active = store.active_run()

    unbalanced = deepcopy(residual_state)
    unbalanced['run_id'] = 'broken-run'
    unbalanced['reconcile']['sealable'] = False
    unbalanced['reconcile']['payroll']['settled'] = False
    store.save('broken-run', unbalanced)
    with pytest.raises(DomainError) as error:
        store.review('broken-run', 1, '通过')
    assert error.value.code == 'UNSEALED'
    assert store.active_run() == active
    assert store.get(active)['settlement']['status'] == 'active'


def test_switch_rolls_back_on_failure(residual_state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(residual_state['run_id'], residual_state)
    store.review(residual_state['run_id'], 1, '通过')
    active = store.active_run()

    moved = deepcopy(residual_state)
    moved['run_id'] = 'next-run'
    store.save('next-run', moved)

    def boom(db, run_id, body):
        raise RuntimeError('settlement write failed')

    monkeypatch.setattr(store, '_pay', boom)
    with pytest.raises(RuntimeError):
        store.review('next-run', 1, '通过')
    assert store.active_run() == active
    assert store.get(active)['settlement']['status'] == 'active'
    assert store.get('next-run')['settlement']['status'] == 'draft'


def test_review_rejects_unsettled_batch(residual_state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    broken = deepcopy(residual_state)
    broken['run_id'] = 'unsettled'
    broken['reconcile']['sealable'] = False
    store.save('unsettled', broken)
    with pytest.raises(DomainError) as error:
        store.review('unsettled', 1, '通过')
    assert error.value.code == 'UNSEALED'
    assert store.get('unsettled')['settlement']['status'] == 'draft'


def test_export_endpoint_blocks_unsettled_batch(client):
    from app.workflow import load
    submitted = client.post('/api/runs', json={'csv': load('dirty.csv').decode('utf-8-sig'), 'parameters': {}}).get_json()
    assert client.get('/api/archive/' + submitted['run_id'] + '/export.csv').status_code == 200
    # 未复核封存的批次即使行数对账通过，也不能作为封存证据导出
    from app import store as runtime_store
    body = runtime_store.get(submitted['run_id'])
    body['reconcile']['sealable'] = False
    runtime_store.save('unsealed-export', body)
    assert client.get('/api/archive/unsealed-export/export.csv').status_code == 422


def test_same_employee_multiple_rates_tail_belongs_to_last_piece():
    from app.modules.m5_calculate.rules import price, settle
    # 同一员工同日两段不同倍率：假日段 + 跨夜后的工作日段，费率带分角，精确额非整分
    segments = [
        {'id': 'E200/2026-10-01', 'employee': 'E200', 'shift_day': '2026-10-01',
         'calendar_day': '2026-10-01', 'start': '2026-10-01T22:00:00+08:00',
         'end': '2026-10-02T00:00:00+08:00', 'minutes': 7, 'rate': '13.37', 'line': 2},
        {'id': 'E200/2026-10-01', 'employee': 'E200', 'shift_day': '2026-10-01',
         'calendar_day': '2026-10-02', 'start': '2026-10-02T00:00:00+08:00',
         'end': '2026-10-02T00:01:00+08:00', 'minutes': 1, 'rate': '13.37', 'line': 2},
    ]
    calculations = price(segments, {'2026-10-01'})
    assert [r['code'] for r in calculations] == ['HOLIDAY', 'REGULAR']
    payables, residuals = settle(calculations)
    entry = payables[0]
    # 尾差只落到该员工最后一条计薪片段，片段本身金额不被改写
    assert entry['carrier_piece'] == calculations[-1]['piece_id']
    for row in calculations:
        expected = Decimal(row['minutes']) * Decimal(row['rate']) * Decimal(row['multiplier']) / 60
        assert Decimal(row['exact_amount']) == expected.quantize(Decimal('0.0000000001'))
    # 员工应付 = 精确合计一次落分，而不是逐段落分相加
    exact_sum = sum(Decimal(r['exact_amount']) for r in calculations)
    assert Decimal(entry['payable']) == exact_sum.quantize(Decimal('0.01'))
    assert residuals[0]['piece'] == entry['carrier_piece']
    assert Decimal(residuals[0]['amount']) == Decimal(entry['residual'])


def test_batch_residual_can_top_up_employee():
    state = run(csv_bytes(rate='10.008'), {}, 7)
    # 两段精确各 5.004 → 员工各落分 5.00，合计 10.00；批次总额 10.01，需补 1 分
    assert [(p['employee'], p['payable'], p['final_payable']) for p in state['payables']] == [
        ('E101', '5.00', '5.01'), ('E102', '5.00', '5.00')]
    adjustment = state['batch']['adjustments'][0]
    assert adjustment['cents'] == 1 and adjustment['employee'] == 'E101'
    assert '补足' in adjustment['reason']
    assert state['reconcile']['payroll']['settled'] is True
    assert state['reconcile']['payroll']['sum_final'] == state['reconcile']['payroll']['total'] == '10.01'


def test_empty_payroll_is_not_sealable():
    # 全批隔离、无任何可计薪片段时，不能因 0 == 0 被误判为可封存
    state = run((HEADER + 'x,bad\n').encode(), {}, 7)
    assert state['payables'] == []
    assert state['batch']['balanced'] is False
    assert state['reconcile']['payroll']['settled'] is False
    assert state['reconcile']['sealable'] is False
