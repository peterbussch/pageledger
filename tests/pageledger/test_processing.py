"""A document job owns cross-attempt recovery, budgets and evidence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_image_evidence import image_descriptor

from pageledger import runner
from pageledger.adapters import ExtractionResult, TextAdapter
from pageledger.checkpoint import Checkpoint, file_digest, read_record
from pageledger.processing import process, resume_job, review_job, verify_job

TEXT = 'The synthetic document contains complete readable prose for this source page. ' * 8


class StageAdapter(TextAdapter):
    calls = []
    stage = 'local_text'
    failure = None
    defective = set()

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        self.calls.append((self.stage, page_number))
        if self.failure and self.stage == 'image':
            raise self.failure
        warnings = ['coverage_defect'] if page_number in self.defective else []
        evidence = (image_descriptor(Path(self.evidence_dir).parent, source, page_number, prompt)
                    if self.stage in {'image', 'second_opinion'} else None)
        return ExtractionResult(TEXT, 'text', 1.0,
                                'gemini-test-returned' if evidence else 'synthetic', warnings,
                                {'pages': 1, 'tokens': 10, 'cost_usd': None}, input_evidence=evidence)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    source.write_text(TEXT + '\f' + TEXT + '\f' + TEXT)
    config = tmp_path / 'config.yml'
    data = {'schema_version': '0.1', 'run': {'adapter': 'text'},
            'processing': {stage: {'adapter': stage} for stage in
                           ('local_text', 'local_ocr', 'image')},
            'taxonomy': {'page_types': {'prose': {'default_action': 'transcribe_text'}}}}
    data['processing']['limits'] = {'max_image_pages': 2, 'max_attempt_pages': 10}
    config.write_text(yaml.safe_dump(data))
    shared = {'calls': [], 'defective': {2, 3}, 'failure': None}
    def adapter(name, *args, **kwargs):
        value = StageAdapter()
        value.calls = shared['calls']
        object.__setattr__(value, 'stage', name)
        object.__setattr__(value, 'failure', shared['failure'])
        object.__setattr__(value, 'defective', shared['defective'])
        object.__setattr__(value, 'evidence_dir', args[0].get('evidence_dir') if args else None)
        return value
    monkeypatch.setattr(runner, 'load_adapter', adapter)
    return source, config, tmp_path / 'job', shared


def launch(setup, **kwargs):
    source, config, out, _ = setup
    return process(source=source, config_path=config, out_dir=out, **kwargs)


def test_serial_escalation_preserves_exact_denominator_and_review_holds(setup):
    result = launch(setup, pages='2-3')
    assert result['status'] == 'completed'
    job = read_record(setup[2] / 'job.json')
    assert job['source']['page_count'] == 3
    assert job['selected_pages'] == [2, 3]
    assert setup[3]['calls'] == [('local_text', 2), ('local_text', 3),
                                ('local_ocr', 2), ('local_ocr', 3), ('image', 2), ('image', 3)]
    assert all(page['disposition'] == 'coverage_defect' for page in job['pages'])
    assert all(len(page['attempts']) == 3 for page in job['pages'])
    assert verify_job(setup[2])['status'] == 'pass'
    assert (setup[2] / 'report.md').is_file()


def test_child_completion_before_job_commit_is_adopted_without_repeating(setup, monkeypatch):
    from pageledger import processing
    original = processing._refresh
    def stop(job, root):
        if any((root / stage['run_path'] / 'manifest.json').exists() for stage in job['stages']):
            raise KeyboardInterrupt
        return original(job, root)
    with monkeypatch.context() as patch:
        patch.setattr(processing, '_refresh', stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    resume_job(setup[2])
    assert setup[3]['calls'].count(('local_text', 1)) == 1
    assert len(setup[3]['calls']) == 7


def test_started_image_outcome_halts_all_later_calls(setup):
    setup[3]['failure'] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        launch(setup)
    before = list(setup[3]['calls'])
    result = resume_job(setup[2])
    assert result['status'] == 'halted'
    assert setup[3]['calls'] == before
    assert read_record(setup[2] / 'job.json')['pages'][1]['disposition'] == 'outcome_unknown'


def test_shared_image_budget_stops_queue(setup):
    data = yaml.safe_load(setup[1].read_text())
    data['processing']['limits']['max_image_pages'] = 1
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)['status'] == 'halted'
    assert [call for call in setup[3]['calls'] if call[0] == 'image'] == [('image', 2)]
    assert read_record(setup[2] / 'job.json')['usage']['image_calls'] == 1


def test_source_mutation_stops_resume_before_calls(setup, monkeypatch):
    original = Checkpoint.save
    def stop(self, page_id, record):
        original(self, page_id, record)
        if record['state'] == 'response':
            raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, 'save', stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    before = list(setup[3]['calls'])
    setup[0].write_text('changed')
    with pytest.raises(ValueError, match='source'):
        resume_job(setup[2])
    assert setup[3]['calls'] == before


def test_bound_reviewed_blank_and_source_defect_skip_all_engines(setup, tmp_path):
    review = tmp_path / 'review.json'
    review.write_text(json.dumps({'schema_version': '0.1', 'source_sha256': file_digest(setup[0]),
        'decisions': [dict(page_id=f'doc_0001_page_{n:04d}', page_number=n,
            disposition=disposition, selected_attempt=None, output_sha256=None,
            reviewer='synthetic reviewer', reason='Visual inspection of source page',
            reviewed_at='2026-09-11T12:00:00Z') for n, disposition in
            [(1, 'reviewed_blank'), (2, 'source_defect'), (3, 'illustration')]]}))
    result = launch(setup, review_path=review)
    assert result['status'] == 'completed'
    assert setup[3]['calls'] == []
    assert verify_job(setup[2])['status'] == 'pass'
    review_job(setup[2], review)
    assert setup[3]['calls'] == []


def test_tampered_selected_output_fails_verification(setup):
    launch(setup)
    job = read_record(setup[2] / 'job.json')
    selected = job['pages'][0]['attempts'][0]['raw_artifact']
    (setup[2] / selected).write_text('changed')
    assert verify_job(setup[2])['status'] == 'fail'
    with pytest.raises(ValueError):
        resume_job(setup[2])


def test_invalid_pdf_gets_failed_container_report_without_invented_pages(setup):
    pdf = setup[0].with_suffix('.pdf')
    pdf.write_bytes(b'%PDF-invalid synthetic container')
    result = process(source=pdf, config_path=setup[1], out_dir=setup[2])
    assert result['status'] == 'halted'
    job = read_record(setup[2] / 'job.json')
    assert job['source']['page_count'] is None
    assert job['pages'] == []
    assert setup[3]['calls'] == []
    assert 'source_container_invalid' in job['halt_reason']


def test_unknown_paid_cost_stops_before_second_image(setup):
    data = yaml.safe_load(setup[1].read_text())
    data['processing']['limits']['max_cost_usd'] = 5
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)['status'] == 'halted'
    job = read_record(setup[2] / 'job.json')
    assert job['halt_reason'] == 'budget:unknown_paid_cost'
    assert job['usage']['cost_usd'] is None
    assert job['usage']['image_calls'] == 1
    assert verify_job(setup[2])['status'] == 'pass'


def test_token_budget_accumulates_across_local_and_ocr(setup):
    data = yaml.safe_load(setup[1].read_text())
    data['processing']['limits']['max_tokens'] = 35
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)['status'] == 'halted'
    assert setup[3]['calls'] == [('local_text', 1), ('local_text', 2), ('local_text', 3), ('local_ocr', 2)]
    job = read_record(setup[2] / 'job.json')
    assert job['usage']['tokens'] == 40
    assert verify_job(setup[2])['status'] == 'pass'


def test_partial_image_failure_is_retained_never_selected_or_retried(setup, monkeypatch):
    from pageledger.adapters import AdapterFailure
    original = StageAdapter.extract
    def clipped(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.stage == 'image':
            raise AdapterFailure('MODEL_OUTPUT_TRUNCATED', partial_result=result)
        return result
    monkeypatch.setattr(StageAdapter, 'extract', clipped)
    assert launch(setup)['status'] == 'halted'
    job = read_record(setup[2] / 'job.json')
    page = job['pages'][1]
    assert page['disposition'] == 'provider_failure'
    assert page['selected_attempt'].startswith('local_text:')
    partial = page['attempts'][-1]
    assert partial['failure']['code'] == 'MODEL_OUTPUT_TRUNCATED'
    assert (setup[2] / partial['raw_artifact']).read_text() == TEXT
    calls = list(setup[3]['calls'])
    assert resume_job(setup[2])['status'] == 'halted'
    assert setup[3]['calls'] == calls
    assert verify_job(setup[2])['status'] == 'pass'
    (setup[2] / partial['raw_artifact']).unlink()
    assert verify_job(setup[2])['status'] == 'fail'
    assert not (setup[2] / partial['raw_artifact']).exists()


def test_forged_internally_consistent_report_is_not_authoritative_over_job(setup):
    from pageledger.document_report import render_document_report
    launch(setup)
    path = setup[2] / 'document.json'
    report = json.loads(path.read_text())
    report['source_retention']['preservation'] = 'preserved'
    path.write_text(json.dumps(report))
    (setup[2] / 'report.md').write_text(render_document_report(report))
    assert verify_job(setup[2])['status'] == 'fail'


def test_verify_job_rejects_selected_output_without_its_attempt(setup):
    launch(setup, pages='1')
    path = setup[2] / 'document.json'
    report = json.loads(path.read_text())
    assert report['pages'][0]['selected_output'] is not None
    report['pages'][0]['attempts'] = []
    path.write_text(json.dumps(report))

    result = verify_job(setup[2])

    assert result['status'] == 'fail'
    assert 'selected output attempt is missing' in result['error']


def test_verify_job_accepts_a_legacy_report_without_format_marker(setup):
    from pageledger.document_report import render_document_report

    launch(setup)
    path = setup[2] / 'document.json'
    report = json.loads(path.read_text())
    report.pop('report_format')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (setup[2] / 'report.md').write_text(render_document_report(report))

    assert verify_job(setup[2])['status'] == 'pass'


def test_read_only_source_inspection_counts_annotations_without_exposing_contents(tmp_path):
    pytest.importorskip('pypdf')
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

    from pageledger.processing_source import inspect_source
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    page[NameObject('/Annots')] = ArrayObject([writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Text'),
        NameObject('/Contents'): TextStringObject('Synthetic private annotation, never report its body'),
    })) for _ in range(19)])
    source = tmp_path / 'annotations.pdf'
    writer.write(source)
    before = file_digest(source)
    assert inspect_source(source) == (1, {'status': 'present', 'count': 19})
    assert file_digest(source) == before


def test_declared_pdf_count_mismatch_fails_without_reindexing(tmp_path):
    pytest.importorskip('pypdf')
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, NumberObject

    from pageledger.processing_source import inspect_source
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer._pages.get_object()[NameObject('/Count')] = NumberObject(130)
    source = tmp_path / 'miscount.pdf'
    writer.write(source)
    with pytest.raises(ValueError, match='Declared PDF page count'):
        inspect_source(source)


def test_process_cli_and_resume_job_dispatch(setup, capsys):
    from pageledger.cli import main
    setup[3]['defective'] = set()
    assert main(['process', str(setup[0]), '--config', str(setup[1]), '--out', str(setup[2]), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'
    assert main(['resume', str(setup[2]), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['job_id']
    assert main(['verify-job', str(setup[2]), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'pass'
    assert setup[3]['calls'] == [('local_text', 1), ('local_text', 2), ('local_text', 3)]


@pytest.mark.parametrize('failure', [False, True])
def test_job_and_document_artifacts_validate_against_schemas(setup, failure):
    from jsonschema import validate
    if failure:
        setup[3]['failure'] = RuntimeError('synthetic provider unavailable')
    launch(setup)
    schemas = Path(__file__).resolve().parents[2] / 'schemas'
    for name in ('job', 'document'):
        validate(json.loads((setup[2] / f'{name}.json').read_text()),
                 json.loads((schemas / f'{name}.schema.json').read_text()))


def test_dollar_cap_is_enforced_within_local_batch(setup, monkeypatch):
    from dataclasses import replace
    original = StageAdapter.extract
    def billed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return replace(result, usage={**result.usage, 'cost_usd': 2.0})
    monkeypatch.setattr(StageAdapter, 'extract', billed)
    data = yaml.safe_load(setup[1].read_text())
    data['processing']['limits']['max_cost_usd'] = 1.0
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)['status'] == 'halted'
    assert setup[3]['calls'] == [('local_text', 1)]
    assert read_record(setup[2] / 'job.json')['usage']['known_cost_usd'] == 2.0


def test_initialization_interruption_produces_explicit_halt_without_calls(setup, monkeypatch):
    def stop(*args):
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, 'initialize', stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    assert resume_job(setup[2])['status'] == 'halted'
    assert setup[3]['calls'] == []
    assert read_record(setup[2] / 'job.json')['halt_reason'] == 'initialization_incomplete'
    assert verify_job(setup[2])['status'] == 'pass'


def test_replacing_review_retains_history_without_calls(setup, tmp_path):
    launch(setup)
    job = read_record(setup[2] / 'job.json')
    chosen = job['pages'][0]['attempts'][0]
    receipt = {'schema_version': '0.1', 'source_sha256': job['source']['sha256'],
               'decisions': [dict(page_id=chosen['page_id'], page_number=1, disposition='reviewed_text',
                                  selected_attempt=chosen['attempt_id'], output_sha256=chosen['raw_sha256'],
                                  reason='Checked against source', reviewer='Synthetic reviewer',
                                  reviewed_at='2026-09-11T18:00:00Z')]}
    review = tmp_path / 'decisions.json'
    review.write_text(json.dumps(receipt))
    review_job(setup[2], review)
    receipt['decisions'][0]['reason'] = 'Rechecked source and notes'
    review.write_text(json.dumps(receipt))
    review_job(setup[2], review)
    review_job(setup[2], review)
    page = read_record(setup[2] / 'job.json')['pages'][0]
    assert len(page['review_history']) == 2
    assert page['review'] == page['review_history'][-1]
    assert len(setup[3]['calls']) == 7
    assert verify_job(setup[2])['status'] == 'pass'


def test_image_configuration_cannot_silently_skip_disabled_ocr(setup):
    data = yaml.safe_load(setup[1].read_text())
    data['processing']['local_ocr'] = None
    setup[1].write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match='local_ocr'):
        launch(setup)
    assert not setup[2].exists()


def test_native_non_token_usage_does_not_block_known_image_token_budget(setup, monkeypatch):
    from dataclasses import replace
    original = StageAdapter.extract
    def tokens(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return replace(result, usage={**result.usage, 'tokens': 10 if self.stage == 'image' else None})
    monkeypatch.setattr(StageAdapter, 'extract', tokens)
    data = yaml.safe_load(setup[1].read_text())
    data['processing']['limits']['max_tokens'] = 100
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)['status'] == 'completed'
    job = read_record(setup[2] / 'job.json')
    assert job['usage']['image_calls'] == 2
    assert job['usage']['tokens'] == 20
    assert job['usage']['tokens_known'] is False
    assert job['usage']['paid_tokens_known'] is True


def test_review_during_interruption_cannot_execute_an_obsolete_pending_plan(setup, monkeypatch, tmp_path):
    def stop(**kwargs):
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(runner, 'run', stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    receipt = {'schema_version': '0.1', 'source_sha256': file_digest(setup[0]), 'decisions': [
        dict(page_id='doc_0001_page_0001', page_number=1, disposition='source_defect',
             selected_attempt=None, output_sha256=None, reviewer='Synthetic reviewer',
             reason='Missing source strokes', reviewed_at='2026-09-11T18:00:00Z')]}
    review = tmp_path / 'review.json'
    review.write_text(json.dumps(receipt))
    assert review_job(setup[2], review)['status'] == 'halted'
    assert resume_job(setup[2])['status'] == 'halted'
    assert setup[3]['calls'] == []


@pytest.mark.parametrize('limit,value', [('max_tokens', 10), ('max_cost_usd', 1.0)])
def test_reaching_exact_budget_cap_stops_before_next_local_request(setup, monkeypatch, limit, value):
    from dataclasses import replace
    original = StageAdapter.extract
    def billed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return replace(result, usage={**result.usage, 'cost_usd': 1.0})
    monkeypatch.setattr(StageAdapter, 'extract', billed)
    config = yaml.safe_load(setup[1].read_text())
    config['processing']['limits'][limit] = value
    setup[1].write_text(yaml.safe_dump(config))
    assert launch(setup)['status'] == 'halted'
    assert setup[3]['calls'] == [('local_text', 1)]
    assert verify_job(setup[2])['status'] == 'pass'


def test_refresh_checks_shared_source_once_for_all_child_runs(setup, monkeypatch):
    from pageledger.processing import _refresh
    launch(setup)
    job = read_record(setup[2] / 'job.json')
    original = Path.open
    reads = []
    def counted(path, *args, **kwargs):
        if path == setup[0]:
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', counted)
    _refresh(job, setup[2], materialize=False)
    assert len(reads) == 1


@pytest.mark.parametrize('completed', [False, True])
def test_resume_rejects_child_using_a_different_config(setup, monkeypatch, tmp_path, completed):
    def stop(*args, **kwargs):
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(runner, 'run', stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    job = read_record(setup[2] / 'job.json')
    stage = job['stages'][0]
    alternate = tmp_path / 'alternate.yml'
    config = yaml.safe_load((setup[2] / stage['config_path']).read_text())
    config['run']['adapter'] = 'alternate_engine'
    alternate.write_text(yaml.safe_dump(config))
    kwargs = dict(inputs=[setup[0]], config_path=alternate,
                  out_dir=setup[2] / stage['run_path'], dry_run=False,
                  pages=','.join(map(str, stage['pages'])), resumable=True)
    if completed:
        runner.run(**kwargs)
    else:
        with monkeypatch.context() as patch:
            patch.setattr(Checkpoint, 'extract', stop)
            with pytest.raises(KeyboardInterrupt):
                runner.run(**kwargs)
    before = list(setup[3]['calls'])
    with pytest.raises(ValueError, match='configuration'):
        resume_job(setup[2])
    assert setup[3]['calls'] == before
