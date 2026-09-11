"""Document-level processing policy over durable, generation-zero extraction runs."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import yaml

from . import runner
from .checkpoint import (
    Checkpoint,
    atomic_bytes,
    digest,
    file_digest,
    read_record,
    write_record,
    writer_lock,
)
from .classifier import classify_signals, merge_classify_thresholds, structural_signals
from .config import load_config
from .processing_config import STAGES, processing_config
from .processing_source import inspect_source
from .replay import _package_code_sha256
from .verify import verify_run


def _page_id(number: int) -> str:
    return f'doc_0001_page_{number:04d}'


def _save(job: dict, root: Path) -> None:
    value = copy.deepcopy(job)
    for page in value['pages']:
        for attempt in page['attempts']:
            attempt.pop('text', None)
    write_record(root / 'job.json', value)


def _publish(job: dict, root: Path) -> dict:
    from .document_report import write_document_report
    _check_source(job)
    _save(job, root)
    write_document_report(job, root)
    return {'job_id': job['job_id'], 'out_dir': str(root), 'status': job['status'],
            'report': str(root / 'report.md'), 'next_action': job['next_action']}


def _check_source(job: dict) -> None:
    source = Path(job['source']['path'])
    if not source.is_file() or file_digest(source) != job['source']['sha256']:
        raise ValueError('Document job source changed or is missing')


def _safe(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Unsafe document job artifact path')
    full = root / path
    if any(item.is_symlink() for item in [full, *full.parents] if item != root.parent):
        raise ValueError('Unsafe document job symlink')
    if not full.resolve().is_relative_to(root):
        raise ValueError('Document job artifact escapes its root')
    return full


def _load(root: Path) -> dict:
    job = read_record(root / 'job.json')
    if job.get('schema_version') != '0.1' or job.get('root') != str(root):
        raise ValueError('Document job identity changed')
    _check_source(job)
    if job.get('config_sha256') != digest(job.get('config')):
        raise ValueError('Document job configuration changed')
    config = job['config']
    expected_policy = processing_config(config, pdf=Path(job['source']['path']).suffix.lower() == '.pdf')
    if job.get('policy') != expected_policy or job.get('limits') != expected_policy['limits']:
        raise ValueError('Document processing policy changed')
    selected = job.get('selected_pages')
    count = job['source']['page_count']
    if (not isinstance(selected, list) or selected != sorted(set(selected))
            or any(type(n) is not int or count is None or not 1 <= n <= count for n in selected)
            or [p['page_number'] for p in job['pages']] != selected
            or any(p['page_id'] != _page_id(p['page_number'])
                   or p['source_sha256'] != job['source']['sha256'] for p in job['pages'])):
        raise ValueError('Document job page inventory changed')
    identifiers = set()
    from .processing_policy import validate_review
    for page in job['pages']:
        history = page.get('review_history', [])
        if not isinstance(history, list) or (history and page['review'] != history[-1]):
            raise ValueError('Active review differs from retained review history')
        for receipt in history:
            validate_review(receipt, page)
    for stage in job['stages']:
        if (stage['stage'] not in STAGES or stage['stage_id'] in identifiers
                or stage['pages'] != sorted(set(stage['pages'])) or not stage['pages']
                or not set(stage['pages']).issubset(selected)
                or stage['run_path'] != f"attempts/{stage['stage_id']}"):
            raise ValueError('Document job attempt plan changed')
        identifiers.add(stage['stage_id'])
        path = _safe(root, stage['config_path'])
        if file_digest(path) != stage['config_sha256']:
            raise ValueError('Document job attempt configuration changed')
        _safe(root, stage['run_path'])
    return job


def _review_decisions(job: dict, review: dict) -> None:
    from .processing_policy import validate_review
    if (not isinstance(review, dict) or set(review) != {'schema_version', 'source_sha256', 'decisions'}
            or review['schema_version'] != '0.1'
            or review['source_sha256'] != job['source']['sha256']
            or not isinstance(review['decisions'], list)):
        raise ValueError('Invalid document review identity or envelope')
    by_id = {p['page_id']: p for p in job['pages']}
    seen = set()
    for decision in review['decisions']:
        if (not isinstance(decision, dict) or decision.get('page_id') not in by_id
                or decision['page_id'] in seen):
            raise ValueError('Review has duplicate or unselected pages')
        seen.add(decision['page_id'])
        bound = {**review, 'decisions': [decision]}
        page = by_id[decision['page_id']]
        validate_review(bound, page)
    for decision in review['decisions']:
        page = by_id[decision['page_id']]
        bound = {**review, 'decisions': [decision]}
        history = page.setdefault('review_history', [])
        if not history or history[-1] != bound:
            history.append(bound)
        page['review'] = bound


def process(*, source: Path, config_path: Path, out_dir: Path, pages: str | None = None,
            adapter_path: Path | None = None, review_path: Path | None = None) -> dict:
    source = source.expanduser().resolve()
    config = load_config(config_path, validate_adapter=False)
    if config.adapter_order is not None:
        raise ValueError('process uses processing stages; run.adapter_order remains for rerun generations')
    if config.max_retries or config.on_page_error != 'stop':
        raise ValueError('process requires zero retries and on_page_error: stop')
    policy = processing_config(config.data, pdf=source.suffix.lower() == '.pdf')
    source_sha = file_digest(source)
    container_error = None
    try:
        count, annotations = inspect_source(source)
    except Exception as exc:
        count, annotations = None, {'status': 'unknown', 'count': None}
        container_error = f'source_container_invalid:{type(exc).__name__}'
    selected = (runner._parse_pages_expression(pages) if pages is not None
                else list(range(1, count + 1)) if count is not None else [])
    if count is not None and (not selected or max(selected) > count):
        raise ValueError('Selected page exceeds the verified source page inventory')
    if container_error:
        selected = []
    root = out_dir.expanduser().resolve()
    from .checkpoint import _sync_directory
    ancestors = []
    ancestor = root
    while not ancestor.exists():
        ancestors.append(ancestor)
        ancestor = ancestor.parent
    root.mkdir(parents=True, exist_ok=False)
    for directory in reversed(ancestors):
        _sync_directory(directory.parent)
    (root / '.job').mkdir()
    (root / 'attempts').mkdir()
    (root / 'partials').mkdir()
    job = {'schema_version': '0.1', 'job_id': f'job_{uuid4().hex}', 'created_at': runner._utc_now(),
           'root': str(root), 'source': {'path': str(source), 'sha256': source_sha,
             'page_count': count, 'annotations': annotations},
           'config': config.data, 'config_sha256': digest(config.data), 'policy': policy,
           'package_sha256': _package_code_sha256(), 'selected_pages': selected, 'pages': [],
           'stages': [], 'status': 'processing', 'halt_reason': None, 'limits': policy['limits'],
           'links': policy['links'], 'usage': {}, 'next_action': 'Process selected source pages.',
           'source_retention': {'capture': 'present', 'preservation': 'unverified',
                                'removal_eligibility': 'not_assessed', 'removed': False}}
    job['pages'] = [dict(page_id=_page_id(n), page_number=n, source_sha256=source_sha,
                        attempts=[], selected_attempt=None, disposition='pending',
                        review_reasons=[], review=None, review_history=[], next_action='local_text') for n in selected]
    if review_path is not None:
        _review_decisions(job, json.loads(review_path.read_text(encoding='utf-8')))
    with writer_lock(root):
        if container_error:
            job['status'] = 'halted'
            job['halt_reason'] = container_error
            job['next_action'] = 'Obtain a valid source container and start a new job.'
            _refresh(job, root)
            return _publish(job, root)
        _refresh(job, root)
        _save(job, root)
        return _continue(job, root, adapter_path)


def _refresh(job: dict, root: Path, *, materialize: bool = True) -> None:
    """Rebuild the job index from validated durable child evidence before scheduling."""
    from .processing_policy import assess_page
    _check_source(job)
    pages = {p['page_id']: p for p in job['pages']}
    for page in pages.values():
        page['attempts'] = []
        page['review_reasons'] = []
    for stage in job['stages']:
        child = _safe(root, stage['run_path'])
        if not child.exists():
            if stage['status'] not in {'planned', 'running'}:
                raise ValueError('Completed child run is missing')
            continue
        if not (child / 'checkpoint.json').exists():
            if stage['status'] == 'completed':
                raise ValueError('Completed child recovery plan is missing')
            stage['status'] = 'halted'
            job.update(status='halted', halt_reason='initialization_incomplete',
                       next_action='Inspect the retained incomplete initialization before starting a new job; no calls were scheduled without a durable plan.')
            continue
        # The shared source was checked above; bind each child's claims to it
        # without rereading the whole input for every historical attempt.
        checkpoint = Checkpoint(child, existing=True, verify_sources=False)
        identity = checkpoint.job
        assert identity is not None
        if identity['config_sha256'] != stage['config_sha256']:
            raise ValueError('Child run configuration disagrees with document job')
        sources = identity['inputs']
        if (len(sources) != 1 or sources[0]['path'] != job['source']['path']
                or sources[0]['sha256'] != job['source']['sha256']
                or sources[0]['page_count'] != job['source']['page_count']
                or set(checkpoint.records) != {_page_id(n) for n in stage['pages']}):
            raise ValueError('Child run source/page identity disagrees with document job')
        if (child / 'manifest.json').exists():
            verification = verify_run(child, check_external_sources=False)
            if verification['status'] != 'pass':
                raise ValueError('Child run verification failed')
        for page_id, record in checkpoint.records.items():
            state = record['state']
            if state == 'pending':
                continue
            outcome = 'outcome_unknown' if state == 'started' else state
            result = record.get('result', record.get('partial_result', {}))
            content = result.get('content', '')
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, allow_nan=False)
            completion = record.get('completion', {})
            provenance = completion.get('provenance', {})
            raw = provenance.get('result', {})
            artifact = f"{stage['run_path']}/{raw['raw_artifact']}" if raw else None
            raw_sha = raw.get('raw_sha256')
            if state == 'failed' and result:
                partial = f"partials/{stage['stage_id']}-{page_id}.txt"
                target = _safe(root, partial)
                encoded = text.encode('utf-8')
                if not target.exists():
                    if not materialize:
                        raise ValueError('Retained partial text is missing')
                    atomic_bytes(target, encoded)
                elif target.read_bytes() != encoded:
                    raise ValueError('Retained partial text changed')
                artifact, raw_sha = partial, file_digest(target)
            signals = structural_signals(text, result_format=result.get('format'),
                                         confidence_detail=result.get('confidence_detail'))
            classification = asdict(classify_signals(signals, merge_classify_thresholds(None),
                pdf_embedded_text_probe=stage['stage'] == 'local_text'
                and Path(job['source']['path']).suffix.lower() == '.pdf'))
            attempt = {'attempt_id': f"{stage['stage_id']}:{page_id}", 'stage': stage['stage'],
                       'run_path': stage['run_path'], 'run_id': identity['run_id'],
                       'page_id': page_id, 'page_number': pages[page_id]['page_number'],
                       'source_sha256': job['source']['sha256'], 'outcome': outcome,
                       'raw_artifact': artifact, 'raw_sha256': raw_sha, 'format': result.get('format'),
                       'text': text, 'warnings': completion.get('quality', {}).get('warnings', result.get('warnings', [])),
                       'classification': classification, 'alignment': completion.get('alignment'),
                       'usage': result.get('usage', {}), 'failure': record.get('error'),
                       'input_evidence': result.get('input_evidence')}
            if (stage['stage'] in {'image', 'second_opinion'} and state == 'completed'
                    and result.get('input_evidence') is None):
                attempt['outcome'] = 'failed'
                attempt['failure'] = {'type': 'EvidenceError', 'code': 'IMAGE_EVIDENCE_REQUIRED',
                                      'http_status': None, 'cost_usd': result.get('usage', {}).get('cost_usd')}
                job.update(status='halted', halt_reason='image_evidence_missing',
                           next_action='Inspect the image adapter: completed image calls require bound input evidence.')
            pages[page_id]['attempts'].append(attempt)
    for page in pages.values():
        page.update(assess_page(page, page['review']))
    attempts = [attempt for page in pages.values() for attempt in page['attempts']]
    paid = [a for a in attempts if a['stage'] in {'image', 'second_opinion'}]
    token_values = [a['usage'].get('tokens') for a in attempts]
    cost_values = [a['usage'].get('cost_usd') for a in attempts]
    paid_costs = [a['usage'].get('cost_usd') for a in paid]
    paid_tokens = [a['usage'].get('tokens') for a in paid]
    known_cost = round(sum(c for c in cost_values if c is not None), 12)
    job['usage'] = {'attempt_pages': len(attempts), 'image_calls': len(paid),
                    'tokens': sum(t for t in token_values if t is not None),
                    'tokens_known': all(t is not None for t in token_values),
                    'cost_usd': known_cost if all(c is not None for c in cost_values) else None,
                    'known_cost_usd': known_cost, 'cost_known': all(c is not None for c in cost_values),
                    'paid_cost_known': all(c is not None for c in paid_costs),
                    'paid_tokens_known': all(t is not None for t in paid_tokens)}


def _budget_reason(job: dict, stage: str, count: int) -> str | None:
    limits, usage = job['limits'], job['usage']
    if limits['max_attempt_pages'] is not None and usage['attempt_pages'] + count > limits['max_attempt_pages']:
        return 'max_attempt_pages'
    if limits['max_tokens'] is not None and usage['tokens'] >= limits['max_tokens']:
        return 'max_tokens'
    if limits['max_cost_usd'] is not None and usage['known_cost_usd'] >= limits['max_cost_usd']:
        return 'max_cost_usd'
    if stage in {'image', 'second_opinion'}:
        if usage['image_calls'] + count > limits['max_image_pages']:
            return 'max_image_pages'
        if limits['max_tokens'] is not None and not usage['paid_tokens_known']:
            return 'unknown_token_usage'
        if limits['max_cost_usd'] is not None and not usage['paid_cost_known']:
            return 'unknown_paid_cost'
    return None


def _plan_stage(job: dict, root: Path, stage: str, numbers: list[int]) -> dict:
    stage_id = stage if stage == 'local_text' else f'{stage}-{numbers[0]:04d}'
    profile = job['policy'][stage]
    options = dict(profile['adapter_options'])
    path = f'attempts/{stage_id}'
    if stage in {'image', 'second_opinion'}:
        options['evidence_dir'] = str(root / path / 'evidence')
    config = copy.deepcopy(job['config'])
    config.pop('processing', None)
    config['taxonomy'] = {'page_types': {'prose': {'default_action': 'transcribe_text', 'prompt': profile['prompt']}}}
    config['run'] = {'adapter': profile['adapter'], 'adapter_options': options,
                     'on_page_error': 'stop', 'retry': {'max_retries': 0}}
    budget = {'max_pages': len(numbers)}
    for key, unit in (('max_tokens', 'tokens'), ('max_cost_usd', 'known_cost_usd')):
        if job['limits'][key] is not None:
            child_key = 'max_usd' if key == 'max_cost_usd' else key
            budget[child_key] = max(0, job['limits'][key] - job['usage'][unit])
    config['run']['budget'] = budget
    config_path = f'.job/{stage_id}.yml'
    encoded = yaml.safe_dump(config, sort_keys=True, allow_unicode=True).encode('utf-8')
    atomic_bytes(root / config_path, encoded)
    item = {'stage_id': stage_id, 'stage': stage, 'pages': numbers, 'run_path': path,
            'config_path': config_path, 'config_sha256': file_digest(root / config_path), 'status': 'planned'}
    job['stages'].append(item)
    _save(job, root)
    return item


def _execute(job: dict, root: Path, stage: dict, adapter_path: Path | None) -> bool:
    stage['status'] = 'running'
    _save(job, root)
    child = _safe(root, stage['run_path'])
    try:
        if child.exists():
            runner.resume(child, adapter_path=adapter_path)
        else:
            runner.run(inputs=[Path(job['source']['path'])], config_path=root / stage['config_path'],
                       out_dir=child, dry_run=False, pages=','.join(map(str, stage['pages'])),
                       adapter_path=adapter_path, resumable=True)
    except (RuntimeError, ValueError, OSError) as exc:
        stage['status'] = 'halted'
        job['status'] = 'halted'
        job['halt_reason'] = f"attempt_stopped:{stage['stage_id']}:{type(exc).__name__}"
        job['next_action'] = 'Inspect the retained failure or uncertain request; reconcile it before starting new work.'
        # Initialization may fail before a durable page plan exists; it made no
        # verified request, and the directory remains evidence rather than reusable output.
        if (child / 'checkpoint.json').exists():
            _refresh(job, root)
        return False
    stage['status'] = 'completed'
    _refresh(job, root)
    _publish(job, root)
    return job['status'] != 'halted'


def _continue(job: dict, root: Path, adapter_path: Path | None) -> dict:
    if job['package_sha256'] != _package_code_sha256():
        raise ValueError('Document processing implementation changed; refusing pending work')
    for stage in job['stages']:
        if stage['status'] in {'planned', 'running'}:
            if not _execute(job, root, stage, adapter_path):
                return _publish(job, root)
    _refresh(job, root)
    for name in STAGES:
        if job['policy'][name] is None:
            continue
        eligible = [p['page_number'] for p in job['pages'] if p['next_action'] == name]
        groups = [eligible] if name == 'local_text' and eligible else [[n] for n in eligible]
        for numbers in groups:
            reason = _budget_reason(job, name, len(numbers))
            if reason:
                job.update(status='halted', halt_reason=f'budget:{reason}',
                           next_action=f'Review retained results and the {reason} budget before starting new work.')
                return _publish(job, root)
            stage = _plan_stage(job, root, name, numbers)
            if not _execute(job, root, stage, adapter_path):
                return _publish(job, root)
    job['status'] = 'completed'
    job['next_action'] = _review_next_action(job)
    return _publish(job, root)


def _review_next_action(job: dict) -> str:
    if any(p['disposition'] == 'source_defect' for p in job['pages']):
        return 'Obtain an alternate source for the damaged pages; do not reconstruct invisible text.'
    unresolved = [p for p in job['pages'] if p['disposition'] not in {'reviewed_text', 'reviewed_blank'}]
    return ('Inspect unresolved source pages and record identity-bound review decisions.'
            if unresolved else 'Review source preservation and publication requirements outside this job.')


def resume_job(job_dir: Path, *, adapter_path: Path | None = None) -> dict:
    root = job_dir.expanduser().resolve()
    with writer_lock(root):
        job = _load(root)
        # Refuse changes in any retained attempt before starting a pending one.
        _refresh(job, root)
        if job['status'] != 'processing':
            return _publish(job, root)
        return _continue(job, root, adapter_path)


def review_job(job_dir: Path, review_path: Path) -> dict:
    root = job_dir.expanduser().resolve()
    with writer_lock(root):
        job = _load(root)
        _refresh(job, root)
        _review_decisions(job, json.loads(review_path.read_text(encoding='utf-8')))
        _refresh(job, root)
        if job['status'] == 'processing':
            job.update(status='halted', halt_reason='review_changed_plan',
                       next_action='Review receipts changed an interrupted plan. Inspect the retained attempts before explicitly starting new work.')
        elif job['status'] == 'completed':
            job['next_action'] = _review_next_action(job)
        return _publish(job, root)


def verify_job(job_dir: Path) -> dict:
    root = job_dir.expanduser().resolve()
    try:
        job = _load(root)
        before = [(p['selected_attempt'], p['disposition'], p['review_reasons']) for p in job['pages']]
        _refresh(job, root, materialize=False)
        if before != [(p['selected_attempt'], p['disposition'], p['review_reasons']) for p in job['pages']]:
            raise ValueError('Job selections disagree with retained evidence')
        from .document_report import build_document_report, verify_document_report
        report = verify_document_report(root)
        if report != build_document_report(job, root):
            raise ValueError('Document report differs from verified job evidence')
        return {'status': 'pass', 'job_id': job['job_id'], 'out_dir': str(root)}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'status': 'fail', 'out_dir': str(root), 'error': str(exc)}
