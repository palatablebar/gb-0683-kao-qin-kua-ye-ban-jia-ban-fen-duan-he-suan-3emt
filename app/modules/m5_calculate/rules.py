from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from collections import OrderedDict, defaultdict

CENT = Decimal('0.01')
SHOW = Decimal('0.000001')  # 展示用：余差保留 6 位，计算与比较始终用全精度 Decimal


def _round2(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _plain(value):
    """不带指数、不丢精度的十进制文本。"""
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _money(value):
    """金额列固定两位小数；仅用于展示，不参与应付计算。"""
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), 'f')


def _show(value):
    return _plain(value.quantize(SHOW, rounding=ROUND_HALF_UP))


def price(segments, holidays):
    """按片段计算精确金额；片段金额不落分，落分只发生在员工应付与批次总额。"""
    normal_used = defaultdict(int)
    out = []
    for s in sorted(segments, key=lambda r: (r['start'], r['employee'])):
        d = date.fromisoformat(s['calendar_day'])
        key = (s['employee'], s['calendar_day'])
        minutes = s['minutes']
        rate = Decimal(str(s['rate']))
        if s['calendar_day'] in holidays:
            parts = [(minutes, Decimal(3), 'HOLIDAY')]
        elif d.weekday() >= 5:
            parts = [(minutes, Decimal(2), 'REST')]
        else:
            regular = min(minutes, max(0, 480 - normal_used[key]))
            normal_used[key] += minutes
            parts = [(regular, Decimal(1), 'REGULAR'), (minutes - regular, Decimal('1.5'), 'OVERTIME')]
        for count, multiplier, code in parts:
            if count:
                exact = rate * count * multiplier / 60
                item = dict(s)
                item.update(
                    minutes=count,
                    multiplier=str(multiplier),
                    code=code,
                    amount=_money(exact),
                    exact_amount=_plain(exact.quantize(Decimal('0.0000000001'), rounding=ROUND_HALF_UP)),
                    piece_id='%s|%s|%s' % (s['id'], s['start'], code),
                )
                out.append(item)
    return out


def _carrier(rows):
    """尾差承载片段：该员工最后一条计薪片段（按开始时刻，同刻按片段ID）。"""
    return sorted(rows, key=lambda r: (r['start'], r['piece_id']))[-1]


def settle(calculations):
    """先保留片段精确额，再按员工汇总后一次性落分；尾差归属到具体片段。"""
    groups = OrderedDict()
    for row in calculations:
        groups.setdefault(row['employee'], []).append(row)
    payables, residuals = [], []
    for employee in sorted(groups):
        rows = groups[employee]
        exact_total = sum((Decimal(r['exact_amount']) for r in rows), Decimal(0))
        payable = _round2(exact_total)
        residual = payable - exact_total
        carrier = _carrier(rows)
        payables.append({
            'employee': employee,
            'piece_count': len(rows),
            'minutes': sum(r['minutes'] for r in rows),
            'exact_total': _plain(exact_total),
            'payable': _money(payable),
            'residual': _show(residual),
            'final_payable': _money(payable),
            'batch_adjustment_cents': 0,
            'final_residual': _show(residual),
            'carrier_piece': carrier['piece_id'],
            'carrier_line': carrier.get('line', ''),
            'carrier_code': carrier['code'],
            'carrier_exact_amount': carrier['exact_amount'],
            'carrier_shift_day': carrier.get('shift_day', ''),
        })
        if residual:
            residuals.append({
                'level': 'EMPLOYEE',
                'employee': employee,
                'piece': carrier['piece_id'],
                'line': carrier.get('line', ''),
                'code': carrier['code'],
                'cents': 0,
                'amount': _show(residual),
                'reason': '片段精确额合计 %s，员工应付一次落分为 %s，余差 %s 由该员工最后一条计薪片段承载'
                % (_plain(exact_total), _plain(payable), _show(residual)),
            })
    return payables, residuals


def distribute(payables, exact_grand):
    """批次总额一次落分；与员工应付合计的差额按最大余数法逐分归属到具名员工。

    优先抵消“落分方向与差额相反”的员工余差，同值按员工编码排序，保证可回查、可重放。
    """
    total = _round2(exact_grand)
    base = sum((Decimal(p['payable']) for p in payables), Decimal(0))
    difference = total - base
    need = int(difference / CENT) if difference else 0
    exact = {p['employee']: Decimal(p['exact_total']) for p in payables}
    current = {p['employee']: Decimal(p['payable']) for p in payables}
    residual = {e: current[e] - exact[e] for e in current}

    if need > 0:
        preferred = [e for e in current if residual[e] < 0]
    elif need < 0:
        preferred = [e for e in current if residual[e] > 0]
    else:
        preferred = []
    neutral = [e for e in current if e not in preferred]
    preferred.sort(key=lambda e: (-abs(residual[e]), e))
    neutral.sort(key=lambda e: (-abs(residual[e]), e))
    order = preferred + neutral

    cents = defaultdict(int)
    sign = 1 if need > 0 else -1
    if order:
        for index in range(abs(need)):
            cents[order[index % len(order)]] += sign

    by_employee = {p['employee']: p for p in payables}
    adjustments = []
    for employee in sorted(cents):
        delta = Decimal(cents[employee]) / 100
        entry = by_employee[employee]
        entry['final_payable'] = _money(current[employee] + delta)
        entry['batch_adjustment_cents'] = cents[employee]
        entry['final_residual'] = _show(current[employee] + delta - exact[employee])
        adjustments.append({
            'level': 'BATCH',
            'employee': employee,
            'piece': entry['carrier_piece'],
            'line': entry['carrier_line'],
            'code': entry['carrier_code'],
            'cents': cents[employee],
            'amount': _money(delta),
            'reason': '批次精确总额 %s 一次落分为 %s，员工应付合计 %s，差 %s；按最大余数法%s %d 分'
            % (_plain(exact_grand), _plain(total), _plain(base), _plain(difference),
               '补足' if cents[employee] > 0 else '扣减', abs(cents[employee])),
        })

    final_sum = sum((Decimal(p['final_payable']) for p in payables), Decimal(0))
    return {
        'exact_total': _plain(exact_grand),
        'total': _money(total),
        'sum_payables': _money(base),
        'residual': _money(difference),
        'residual_cents': need,
        'adjustments': adjustments,
        'adjusted_cents': sum(cents.values()),
        'sum_final': _money(final_sum),
        'balanced': bool(payables) and final_sum == total and abs(need) == sum(abs(v) for v in cents.values()),
    }


def apply(state):
    holidays = set(state['parameters'].get('holidays', ['2026-10-01']))
    calculations = price(state['segments'], holidays)
    exact_grand = sum((Decimal(r['exact_amount']) for r in calculations), Decimal(0))
    payables, residuals = settle(calculations)
    batch = distribute(payables, exact_grand)
    paid = sum((Decimal(str(r['paid'])) for r in state['rows']), Decimal(0))
    state['calculations'] = calculations
    state['payables'] = payables
    state['residuals'] = residuals
    state['batch'] = batch
    state['business'] = {
        'total': batch['sum_final'],
        'exact_total': batch['exact_total'],
        'paid': _money(paid),
        'difference': _money(Decimal(batch['sum_final']) - paid),
        'minutes': sum(r['minutes'] for r in calculations),
        'unit': 'CNY',
        'payable_count': len(payables),
        'residual_count': len(residuals) + len(batch['adjustments']),
        'employee_residual_total': _show(sum((Decimal(r['amount']) for r in residuals), Decimal(0))),
        'batch_adjustment_total': _money(sum((Decimal(r['amount']) for r in batch['adjustments']), Decimal(0))),
    }
    return state
