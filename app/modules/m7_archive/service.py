from app.modules.m7_archive.export_out import checksum, verify

def execute(state):
    payroll = state.get('reconcile', {}).get('payroll', {})
    state['archive'] = {
        'evidence_count': len(state.get('calculations', [])) + len(state.get('quarantine', []))
        + len(state.get('payables', [])) + len(state.get('residuals', []))
        + len(state.get('batch', {}).get('adjustments', [])),
        'ready': state.get('reconcile', {}).get('sealable', False),
        'payroll_settled': payroll.get('settled', False),
        'residual_count': payroll.get('residual_count', 0),
    }
    state['archive']['sha256'] = checksum(state)
    state['archive']['verified'] = verify(state)
    return state
