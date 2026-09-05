from types import SimpleNamespace

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo


class CharacterTokenHandler:
    prompt_tokens = 10

    @staticmethod
    def count_tokens(text):
        return len(text)


class Provider:
    def __init__(self, files):
        self.files = files

    def get_diff_files(self):
        return self.files


def _file(path, patch, edit_type=EDIT_TYPE.MODIFIED):
    return FilePatchInfo("old", "new", patch, path, edit_type=edit_type)


def _build(monkeypatch, files, *, limit=260, max_chunks=20, numbered=False):
    from pr_agent.algo import review_chunking

    monkeypatch.setattr(review_chunking, "get_max_tokens", lambda model: limit)
    return review_chunking.build_review_chunk_plan(
        Provider(files),
        CharacterTokenHandler(),
        "model",
        add_line_numbers=numbered,
        max_chunks=max_chunks,
        output_buffer_tokens=20,
        metadata_tokens=10,
    )


def test_small_diff_is_one_complete_chunk(monkeypatch):
    from pr_agent.algo.review_chunking import coverage_for_results

    patch = "@@ -1,2 +1,2 @@\n old\n-bad\n+good"
    plan = _build(monkeypatch, [_file("src/a.py", patch)])

    assert plan.status == "ready"
    assert len(plan.units) == len(plan.chunks) == 1
    assert "src/a.py" in plan.chunks[0].text
    assert patch in plan.chunks[0].raw_text
    coverage = coverage_for_results(plan, (plan.chunks[0].chunk_id,), ())
    assert coverage.status == "complete"
    assert coverage.missing_unit_ids == ()


def test_multi_file_plan_preserves_order_and_covers_every_hunk_once(monkeypatch):
    files = [
        _file("src/a.py", "@@ -1 +1 @@\n-a\n+" + "a" * 80),
        _file("src/b.py", "@@ -1 +1 @@\n-b\n+" + "b" * 80),
        _file("src/c.py", "@@ -1 +1 @@\n-c\n+" + "c" * 80),
    ]

    plan = _build(monkeypatch, files, limit=180)

    assert plan.status == "ready"
    assert len(plan.chunks) == 3
    assert [unit.filename for unit in plan.units] == ["src/a.py", "src/b.py", "src/c.py"]
    owned = [unit_id for chunk in plan.chunks for unit_id in chunk.unit_ids]
    assert owned == [unit.unit_id for unit in plan.units]
    assert len(owned) == len(set(owned))


def test_multiple_hunks_are_semantic_units(monkeypatch):
    patch = "@@ -1 +1 @@\n-old1\n+new1\n@@ -10 +10 @@\n-old2\n+new2"

    plan = _build(monkeypatch, [_file("src/a.py", patch)], limit=180)

    assert plan.status == "ready"
    assert len(plan.units) == 2
    assert [unit.hunk_index for unit in plan.units] == [0, 1]
    assert all(unit.part_count == 1 for unit in plan.units)
    assert "@@ -1 +1 @@" in plan.units[0].raw_text
    assert "@@ -10 +10 @@" in plan.units[1].raw_text


def test_single_huge_hunk_splits_on_complete_diff_lines(monkeypatch):
    body = "\n".join(f"+line-{index}-{'x' * 18}" for index in range(12))
    patch = f"@@ -1,0 +1,12 @@\n{body}"

    plan = _build(monkeypatch, [_file("src/large.py", patch)], limit=190)

    assert plan.status == "ready"
    assert len(plan.units) > 1
    parent_ids = {unit.parent_unit_id for unit in plan.units}
    assert len(parent_ids) == 1
    assert [unit.part_index for unit in plan.units] == list(range(1, len(plan.units) + 1))
    assert {unit.part_count for unit in plan.units} == {len(plan.units)}
    reconstructed = []
    for unit in plan.units:
        lines = unit.raw_hunk.splitlines()
        assert lines[0] == "@@ -1,0 +1,12 @@"
        reconstructed.extend(lines[1:])
    assert reconstructed == body.splitlines()


def test_deleted_file_patch_remains_reviewable(monkeypatch):
    plan = _build(
        monkeypatch,
        [_file("src/removed.py", "@@ -1,2 +0,0 @@\n-old\n-code", EDIT_TYPE.DELETED)],
    )

    assert plan.status == "ready"
    assert len(plan.units) == 1
    assert plan.units[0].edit_type == "deleted"
    assert "-old" in plan.units[0].raw_text


def test_missing_patch_is_reported_as_unreviewable(monkeypatch):
    plan = _build(monkeypatch, [_file("binary.dat", "")])

    assert plan.status == "empty"
    assert plan.units == ()
    assert plan.unreviewable_files == (("binary.dat", "missing_patch"),)


def test_plan_hash_is_deterministic_and_content_sensitive(monkeypatch):
    first = _build(monkeypatch, [_file("a.py", "@@ -1 +1 @@\n-a\n+b")])
    second = _build(monkeypatch, [_file("a.py", "@@ -1 +1 @@\n-a\n+b")])
    changed = _build(monkeypatch, [_file("a.py", "@@ -1 +1 @@\n-a\n+c")])

    assert first.plan_hash == second.plan_hash
    assert first.plan_hash != changed.plan_hash
    assert first.units[0].unit_id == second.units[0].unit_id
    assert first.units[0].unit_id != changed.units[0].unit_id


def test_capacity_exhaustion_never_pretends_to_be_complete(monkeypatch):
    from pr_agent.algo.review_chunking import coverage_for_results

    files = [
        _file("a.py", "@@ -1 +1 @@\n-a\n+" + "a" * 80),
        _file("b.py", "@@ -1 +1 @@\n-b\n+" + "b" * 80),
    ]
    plan = _build(monkeypatch, files, limit=180, max_chunks=1)

    assert plan.status == "capacity_exceeded"
    assert plan.unplanned_unit_ids
    coverage = coverage_for_results(plan, tuple(chunk.chunk_id for chunk in plan.chunks), ())
    assert coverage.status == "partial"
    assert coverage.missing_unit_ids == plan.unplanned_unit_ids


def test_one_diff_line_larger_than_budget_is_rejected_without_clipping(monkeypatch):
    plan = _build(monkeypatch, [_file("a.py", "@@ -1 +1 @@\n+" + "x" * 400)], limit=150)

    assert plan.status == "unit_too_large"
    assert plan.chunks == ()
    assert len(plan.units) == 1
    assert plan.error


def test_numbered_and_raw_chunk_views_have_same_ownership(monkeypatch):
    plan = _build(
        monkeypatch,
        [_file("a.py", "@@ -2 +2 @@\n-old\n+new")],
        numbered=True,
    )

    assert plan.status == "ready"
    assert "__new hunk__" in plan.chunks[0].text
    assert "@@ -2 +2 @@" in plan.chunks[0].raw_text
    assert plan.chunks[0].unit_ids == (plan.units[0].unit_id,)
