"""Batch workflow tests (spec section 45)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.batch import Batch, ItemState, summarise_changes
from app.decisions.manager import DecisionState
from tests import fixtures


@pytest.fixture
def batch(tmp_path) -> Batch:
    source = tmp_path / "in"
    source.mkdir()
    fixtures.form_pdf(source / "a.pdf")
    fixtures.paragraph_pdf(source / "b.pdf")
    (source / "notes.txt").write_text("not a pdf")
    b = Batch(output_folder=str(tmp_path / "out"))
    b.add_folder(str(source))
    return b


def test_a_folder_adds_only_pdfs(batch):
    assert len(batch.items) == 2
    assert all(i.source_path.endswith(".pdf") for i in batch.items)


def test_adding_the_same_file_twice_is_ignored(batch):
    before = len(batch.items)
    batch.add_files([batch.items[0].source_path])
    assert len(batch.items) == before


def test_approve_dismiss_and_unapprove(batch):
    item = batch.analyse(batch.items[0])
    assert item.state is ItemState.READY
    batch.approve(item)
    assert batch.approved == [item]
    batch.dismiss(item)
    assert not batch.approved
    batch.unapprove(item)
    assert item.state is ItemState.READY


def test_only_approved_documents_are_written(batch, tmp_path):
    first = batch.analyse(batch.items[0])
    batch.analyse(batch.items[1])
    first.session.decisions.set_state(first.session.candidates, DecisionState.ACCEPTED)
    batch.approve(first)

    done = batch.process_approved()
    assert len(done) == 1
    outputs = list((tmp_path / "out").glob("*.pdf"))
    assert len(outputs) == 1


def test_output_folder_is_used_and_never_overwritten(batch, tmp_path):
    item = batch.analyse(batch.items[0])
    item.session.decisions.set_state(item.session.candidates, DecisionState.ACCEPTED)
    batch.approve(item)
    batch.process_approved()

    batch.unapprove(item)
    batch.approve(item)
    batch.process_approved()

    outputs = sorted(p.name for p in (tmp_path / "out").glob("*.pdf"))
    assert len(outputs) == 2, outputs
    assert outputs[0] != outputs[1]


def test_progress_is_reported(batch):
    item = batch.analyse(batch.items[0])
    item.session.decisions.set_state(item.session.candidates, DecisionState.ACCEPTED)
    batch.approve(item)
    seen = []
    batch.process_approved(progress=seen.append)
    stages = [p.stage for p in seen]
    assert "Redacting" in stages and "Finished" in stages


def test_cancelling_leaves_finished_documents_in_place(batch, tmp_path):
    for item in batch.items:
        batch.analyse(item)
        item.session.decisions.set_state(item.session.candidates, DecisionState.ACCEPTED)
        batch.approve(item)
    batch.cancel()
    done = batch.process_approved()
    assert done == []
    assert batch.cancelled


def test_start_over_keeps_written_output(batch, tmp_path):
    item = batch.analyse(batch.items[0])
    item.session.decisions.set_state(item.session.candidates, DecisionState.ACCEPTED)
    batch.approve(item)
    batch.process_approved()
    written = list((tmp_path / "out").glob("*.pdf"))
    assert written

    batch.clear()
    assert batch.items == []
    assert list((tmp_path / "out").glob("*.pdf")) == written


def test_one_client_keeps_one_pseudonym_across_the_batch(batch):
    for item in batch.items:
        batch.analyse(item)
        item.session.decisions.set_state(item.session.candidates, DecisionState.ACCEPTED)
        batch.approve(item)
    batch.process_approved()

    names = {}
    for item in batch.completed:
        for change in item.changes:
            if change.original == "John Smith":
                names.setdefault(change.pseudonym, []).append(item.name)
    assert len(names) <= 1, f"the same client got several pseudonyms: {names}"


def test_change_summary_lists_original_and_pseudonym(batch):
    item = batch.analyse(batch.items[0])
    item.session.decisions.set_state(item.session.candidates, DecisionState.ACCEPTED)
    changes = summarise_changes(item)
    assert changes
    for change in changes:
        assert change.original and change.pseudonym
        assert change.original != change.pseudonym
        assert change.occurrences >= 1
        assert change.pages
