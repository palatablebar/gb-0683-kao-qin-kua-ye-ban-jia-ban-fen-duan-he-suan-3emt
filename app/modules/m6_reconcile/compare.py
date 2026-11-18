from decimal import Decimal, ROUND_HALF_UP


def _money(value):
    return format(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')


def payroll(state):
    """尾差对账：员工最终应付合计必须与批次总额（已落分）一致，且尾差可回查到片段。"""
    payables = state.get('payables', [])
    batch = state.get('batch', {})
    employee_residuals = state.get('residuals', [])
    adjustments = batch.get('adjustments', [])
    total = Decimal(str(batch.get('total', '0')))
    exact_total = Decimal(str(batch.get('exact_total', '0')))
    sum_payables = sum((Decimal(p['payable']) for p in payables), Decimal(0))
    sum_final = sum((Decimal(p['final_payable']) for p in payables), Decimal(0))
    sum_exact = sum((Decimal(p['exact_total']) for p in payables), Decimal(0))
    attributed = all(a.get('employee') and a.get('piece') for a in list(employee_residuals) + list(adjustments))
    paid = Decimal(str(state.get('business', {}).get('paid', '0')))
    difference = sum_final - total
    batch_cents = sum(a.get('cents', 0) for a in adjustments)
    return {
        'exact_total': format(exact_total, 'f'),
        'total': _money(total),
        'sum_payables': _money(sum_payables),
        'sum_final': _money(sum_final),
        'sum_exact': format(sum_exact, 'f'),
        'difference': _money(difference),
        'batch_residual': _money(total - sum_payables),
        'batch_cents': batch_cents,
        'residual_cents': batch.get('residual_cents', 0),
        'employee_residuals': len(employee_residuals),
        'batch_adjustments': len(adjustments),
        'residual_count': len(employee_residuals) + len(adjustments),
        'attribution_complete': attributed,
        'settled': bool(payables) and difference == 0 and batch.get('balanced') is True
        and attributed and batch_cents == batch.get('residual_cents', 0),
        'paid': _money(paid),
        'unpaid_difference': _money(total - paid),
        'employee_breakdown': [
            {
                'employee': p['employee'],
                'minutes': p['minutes'],
                'pieces': p['piece_count'],
                'exact_total': p['exact_total'],
                'payable': p['payable'],
                'residual': p['residual'],
                'batch_adjustment_cents': p.get('batch_adjustment_cents', 0),
                'final_payable': p['final_payable'],
                'carrier_piece': p['carrier_piece'],
            }
            for p in payables
        ],
    }


def balance(state):
    accepted = len(state['rows'])
    isolated = len(state['quarantine'])
    accounted = accepted + isolated + state['blank_count'] + state['header_count'] + state['duplicate_count']
    rows_balanced = accounted == state['input_count']
    money = payroll(state)
    return {
        'input': state['input_count'],
        'accepted': accepted,
        'isolated': isolated,
        'blank': state['blank_count'],
        'headers': state['header_count'],
        'duplicates': state['duplicate_count'],
        'accounted': accounted,
        'balanced': rows_balanced,
        'business': state.get('business', {}),
        'payroll': money,
        'sealable': rows_balanced and money['settled'],
    }
