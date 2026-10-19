"""The 0.8 ladder: a second literal engine (layout_ocr) and a local reader (local_image)."""

import dataclasses
from pathlib import Path

import pytest
import yaml
from test_escalation import _attempt, _page
from test_image_evidence import image_descriptor
from test_processing import StageAdapter, setup  # noqa: F401  (setup is a fixture)

from pageledger import runner
from pageledger.adapters import AdapterFailure
from pageledger.checkpoint import read_record
from pageledger.processing import process, resume_job, verify_job
from pageledger.processing_config import ladder, processing_config
from pageledger.processing_policy import assess_page

GENERATIVE = ["ocr", "page_image", "generative"]
FULL = ("local_text", "local_ocr", "layout_ocr", "local_image", "image", "second_opinion")


def _config(**processing):
    return {"processing": {"local_ocr": {"adapter": "rapidocr"}, **processing}}


def test_the_new_stages_appear_in_the_policy_only_when_configured():
    old = processing_config(_config(), pdf=True)
    assert "layout_ocr" not in old and "local_image" not in old
    assert "max_local_image_pages" not in old["limits"]
    assert ladder(old) == ("local_text", "local_ocr", "image", "second_opinion")
    new = processing_config(
        _config(
            layout_ocr={"adapter": "indra"},
            local_image={"adapter": "vision"},
            image={"adapter": "vision"},
            limits={"max_image_pages": 2, "max_local_image_pages": 10},
        ),
        pdf=True,
    )
    assert ladder(new) == FULL
    assert new["limits"]["max_local_image_pages"] == 10


@pytest.mark.parametrize(
    "processing,message",
    [
        ({"local_image": {"adapter": "vision"}}, "positive max_local_image_pages"),
        ({"limits": {"max_local_image_pages": 3}}, "needs a local_image stage"),
        (
            {"local_ocr": None, "layout_ocr": {"adapter": "indra"}},
            "layout_ocr requires an explicit local_ocr stage",
        ),
    ],
)
def test_meaningless_ladder_configs_are_refused(processing, message):
    with pytest.raises(ValueError, match=message):
        processing_config(_config(**processing), pdf=True)


def test_a_local_reader_alone_can_be_contested():
    policy = processing_config(
        _config(
            local_image={"adapter": "vision"},
            limits={"max_local_image_pages": 4},
            contest={"rules": "ru-print-0.2"},
        ),
        pdf=True,
    )
    assert policy["contest"] == {"rules": "ru-print-0.2"}


def test_a_held_ocr_page_climbs_to_the_second_literal_engine_first():
    ocr = _attempt("a2", "local_ocr", "x", warnings=["coverage_defect"])
    result = assess_page(_page(_attempt("a1", "local_text", " "), ocr), stages=FULL)
    assert result["next_action"] == "layout_ocr"


def test_a_failed_local_reader_hands_the_page_to_the_next_stage():
    failed = {**_attempt("a4", "local_image", ""), "outcome": "failed", "raw_artifact": None}
    held = _attempt("a3", "layout_ocr", "x", warnings=["coverage_defect"])
    page = _page(_attempt("a1", "local_text", " "), _attempt("a2", "local_ocr", " "), held, failed)
    result = assess_page(page, stages=FULL)
    assert result["next_action"] == "image"
    assert result["disposition"] != "provider_failure"
    # With nowhere left to go, the page waits for review, and says why.
    stops = assess_page(page, stages=FULL[:4])
    assert stops["next_action"] == "review" and "provider_failure" in stops["review_reasons"]


def test_always_does_not_send_a_clean_local_reading_on_to_the_paid_reader():
    ocr = _attempt("a2", "local_ocr", "clean words here")
    local = _attempt("a3", "local_image", "clean words here", adapter_capabilities=GENERATIVE)
    comparison = {
        "left_attempt": "a2",
        "right_attempt": "a3",
        "agreement_ratio": 1.0,
        "spans": [],
        "number_differences": [],
    }
    page = _page(_attempt("a1", "local_text", " "), ocr, local, comparisons=[comparison])
    stays = assess_page(page, escalate_on=("hold", "always", "disagreement"), stages=FULL)
    assert stays["next_action"] == "review"
    before = assess_page(
        _page(_attempt("a1", "local_text", " "), ocr), escalate_on=("hold", "always"), stages=FULL
    )
    assert before["next_action"] == "layout_ocr"


def test_a_local_reading_that_disagrees_climbs_even_when_contested():
    ocr = _attempt("a2", "local_ocr", "Embassy 1119 16th")
    local = _attempt("a3", "local_image", "Embassy 1125 16th", adapter_capabilities=GENERATIVE)
    comparison = {
        "left_attempt": "a2",
        "right_attempt": "a3",
        "agreement_ratio": 0.67,
        "spans": [],
        "number_differences": [{"number": "1119", "side": "left", "context": "Embassy 1119"}],
    }

    def contest(base, witness):
        return {"record": {}, "sha256": "0" * 64, "open": 1, "decided": [], "edition": None}

    page = _page(_attempt("a1", "local_text", " "), ocr, local, comparisons=[comparison])
    result = assess_page(
        page, escalate_on=("hold", "always", "disagreement"), stages=FULL, contest=contest
    )
    assert result["next_action"] == "image"
    assert "contested" in result["review_reasons"]
    assert result["triggers"][-1] == {
        "trigger": "disagreement",
        "stage": "local_image",
        "attempt": "a3",
        "reasons": ["numeric_disagreement"],
    }


def test_the_paid_reader_is_the_contested_base_over_the_local_one():
    ocr = _attempt("a2", "local_ocr", "words")
    local = _attempt("a3", "local_image", "local words", adapter_capabilities=GENERATIVE)
    paid = _attempt("a4", "image", "paid words", adapter_capabilities=GENERATIVE)
    seen = []

    def contest(base, witness):
        seen.append((base["attempt_id"], witness["attempt_id"]))
        return {"record": {}, "sha256": "0" * 64, "open": 0, "decided": [], "edition": None}

    page = _page(_attempt("a1", "local_text", " "), ocr, local, paid)
    result = assess_page(page, stages=FULL, contest=contest)
    assert result["selected_attempt"] == "a4"
    assert seen == [("a4", "a2")]


# A whole job on the ladder, with the stub adapters of the processing tests.


class LocalReader(StageAdapter):
    fail = False

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        if self.fail:
            self.calls.append((self.stage, page_number))
            raise AdapterFailure("MODEL_TIMEOUT")
        result = super().extract(
            source, page_id=page_id, page_number=page_number, action=action, prompt=prompt
        )
        evidence = image_descriptor(Path(self.evidence_dir).parent, source, page_number, prompt)
        return dataclasses.replace(result, input_evidence=evidence)


def _ladder_job(setup, monkeypatch, *, fail=False, max_local=5):  # noqa: F811
    source, config, out, shared = setup
    data = yaml.safe_load(config.read_text())
    data["processing"]["layout_ocr"] = {"adapter": "layout_ocr"}
    data["processing"]["local_image"] = {"adapter": "local_image"}
    data["processing"]["limits"].update(max_local_image_pages=max_local, max_attempt_pages=20)
    config.write_text(yaml.safe_dump(data))
    stub = runner.load_adapter

    def adapter(name, *args, **kwargs):
        value = stub(name, *args, **kwargs)
        if name != "local_image":
            return value
        reader = LocalReader()
        for field in ("calls", "stage", "defective", "evidence_dir", "warn", "texts"):
            object.__setattr__(reader, field, getattr(value, field))
        object.__setattr__(reader, "capabilities", ("generative",))
        object.__setattr__(reader, "fail", fail)
        return reader

    monkeypatch.setattr(runner, "load_adapter", adapter)
    return process(source=source, config_path=config, out_dir=out, pages="2-3")


def test_a_held_page_climbs_every_rung_of_the_ladder(setup, monkeypatch):  # noqa: F811
    result = _ladder_job(setup, monkeypatch)
    assert result["status"] == "completed"
    stages = [stage for stage, _ in setup[3]["calls"]]
    assert stages == [
        "local_text",
        "local_text",
        "local_ocr",
        "local_ocr",
        "layout_ocr",
        "layout_ocr",
        "local_image",
        "local_image",
        "image",
        "image",
    ]
    job = read_record(setup[2] / "job.json")
    assert job["usage"]["local_image_calls"] == 2 and job["usage"]["image_calls"] == 2
    assert verify_job(setup[2])["status"] == "pass"


def test_a_failing_local_reader_does_not_halt_the_job(setup, monkeypatch):  # noqa: F811
    result = _ladder_job(setup, monkeypatch, fail=True)
    assert result["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    for page in job["pages"]:
        outcomes = [(a["stage"], a["outcome"]) for a in page["attempts"]]
        assert ("local_image", "failed") in outcomes and outcomes[-1] == ("image", "completed")
    assert verify_job(setup[2])["status"] == "pass"


def test_the_local_reader_has_its_own_budget(setup, monkeypatch):  # noqa: F811
    result = _ladder_job(setup, monkeypatch, max_local=1)
    assert result["status"] == "paused_budget"
    job = read_record(setup[2] / "job.json")
    assert job["halt_reason"] == "budget:max_local_image_pages"
    resumed = resume_job(setup[2], raise_limits={"max_local_image_pages": 2})
    assert resumed["status"] == "completed"
    assert verify_job(setup[2])["status"] == "pass"
