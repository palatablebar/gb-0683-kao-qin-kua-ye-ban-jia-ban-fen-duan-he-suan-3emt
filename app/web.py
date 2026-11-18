import base64
from flask import request, render_template, jsonify
from app.errors import DomainError
from app.config import TITLE, MODULES, SHORT
from app.workflow import run, load
from app import store


def payload():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise DomainError('PAYLOAD', '请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'], str):
        raise DomainError('CSV', 'CSV内容必须是文本')
    sample = data.get('sample', request.args.get('sample', 'dirty.csv'))
    content = data.get('csv')
    return (content.encode() if isinstance(content, str) else load(sample)), data.get('parameters', {})


def api(stage):
    content, parameters = payload()
    return jsonify(run(content, parameters, stage))


def _basis():
    """页面回看：最近提交的批次；若无，用当前已封存结算；再无则现算脏样例。"""
    latest = store.history()
    if latest:
        return latest[0]['id']
    active = store.active_run()
    return active


def page(stage, template):
    history = store.history()
    run_id = _basis()
    state = store.get(run_id) if run_id else run(load('dirty.csv'), {})
    if 'sample' in request.args:
        source = load(request.args['sample'])
    elif state.get('source_b64'):
        source = base64.b64decode(state['source_b64'])
    else:
        source = load('dirty.csv')
    data = run(source, state.get('manifest', {}).get('parameters', {}), stage)
    active_id = store.active_run()
    settlement = state.get('settlement', {})
    if active_id and active_id != state.get('run_id'):
        settlement = dict(settlement or {}, current_elsewhere=active_id)
    return render_template(template, title=TITLE, short=SHORT, modules=MODULES, current=stage,
                           data=data, run=state, history=history, settlement=settlement,
                           active_id=active_id)
