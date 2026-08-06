"""
P7.3 configuration accessibility proofs.

These tests walk the entire ``app.core.config_spec`` surface and prove the
TUI Configuration panel exposes it all: every spec gets exactly one form
row, rows render in stable display-group order, secret rows are masked and
never editable, and every editable field's current value passes the P7.2
mutation layer's dry-run validation (the exact gate ``save_config`` runs
before writing anything).
"""

from app.core import config_spec
from app.services import config_mutation
from app.ui.data import ServiceFacade
from tests.ui_fakes import FakeRelay, FakeStore


def _form():
    return ServiceFacade(
        relay_instance=FakeRelay(), store=FakeStore()
    ).config_form()


def test_form_covers_every_registry_setting():
    form = {f.env or f.attr: f for f in _form()}
    assert len(form) == len(config_spec.SPECS)

    for spec in config_spec.SPECS:
        key = spec.env or spec.attr
        field = form[key]
        assert field.attr == spec.attr
        assert field.group == config_spec.tui_group_for(spec)
        assert field.kind == config_spec.tui_kind_for(spec)
        assert field.editable is config_spec.tui_editable_for(spec)
        assert field.secret is spec.secret


def test_form_rows_render_in_stable_group_order():
    fields = _form()

    # The form preserves registry order 1:1.
    assert [f.attr for f in fields] == [s.attr for s in config_spec.SPECS]

    # The screen renders DISPLAY_GROUPS in order; within every group the
    # registry order is preserved and every spec appears exactly once.
    by_group: dict[str, list[str]] = {}
    for field in fields:
        by_group.setdefault(field.group, []).append(field.attr)

    registry_index = {s.attr: i for i, s in enumerate(config_spec.SPECS)}
    seen: list[str] = []
    for group in config_spec.DISPLAY_GROUPS:
        attrs = by_group[group]
        indices = [registry_index[attr] for attr in attrs]
        assert indices == sorted(indices), group
        seen.extend(attrs)

    assert len(seen) == len(config_spec.SPECS)
    assert len(set(seen)) == len(config_spec.SPECS)


def test_secret_rows_are_masked_and_never_editable():
    for field in _form():
        if not field.secret:
            continue
        assert not field.editable
        assert field.value == "(unset)" or "*" in field.value


def test_editable_rows_are_env_backed_and_not_secret():
    for field in _form():
        if not field.editable:
            continue
        assert field.env
        assert not field.secret


def test_every_editable_value_passes_the_mutation_gate():
    fields = {f.env: f.value for f in _form() if f.editable}
    assert len(fields) >= 90  # full surface, not a token subset

    for env, value in fields.items():
        report = config_mutation.set_setting(
            env, value, reload=False, dry_run=True
        )
        assert report["dry_run"] is True, env
        assert "old" in report and "new" in report, env


def test_secret_envs_are_refused_by_the_save_path():
    facade = ServiceFacade(relay_instance=FakeRelay(), store=FakeStore())

    for spec in config_spec.SPECS:
        if not spec.secret:
            continue
        report = facade.save_config({spec.env: "should-not-write"})
        assert report["saved"] is False, spec.env
        assert not facade._store.env_writes, spec.env
