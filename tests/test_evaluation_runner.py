"""Tests for evaluation runner helpers."""
import json
from pathlib import Path
import pytest


def test_eval_record_has_version_field():
    """EvalRecord must carry the target version string."""
    from evaluation.models import EvalRecord
    fields = {f.name for f in EvalRecord.__dataclass_fields__.values()}
    assert "version" in fields, "EvalRecord missing 'version' field"


def test_eval_record_has_category_field():
    """EvalRecord must carry the scenario category string."""
    from evaluation.models import EvalRecord
    fields = {f.name for f in EvalRecord.__dataclass_fields__.values()}
    assert "category" in fields, "EvalRecord missing 'category' field"


def test_eval_record_version_defaults_to_empty():
    import dataclasses
    from evaluation.models import EvalRecord
    defaults = {f.name: f.default for f in dataclasses.fields(EvalRecord)}
    assert defaults.get("version") == ""


def test_read_category_returns_value_from_metadata_yaml(tmp_path):
    """_read_category reads 'category: debugging' from metadata.yaml."""
    from evaluation.runner import _read_category
    (tmp_path / "metadata.yaml").write_text("category: debugging\n")
    assert _read_category(tmp_path) == "debugging"


def test_read_category_strips_inline_comment(tmp_path):
    from evaluation.runner import _read_category
    (tmp_path / "metadata.yaml").write_text("category: deployment  # new in v2\n")
    assert _read_category(tmp_path) == "deployment"


def test_read_category_returns_empty_when_no_file(tmp_path):
    from evaluation.runner import _read_category
    assert _read_category(tmp_path) == ""


def test_load_scenario_includes_category(tmp_path):
    """load_scenario() includes 'category' key in returned dict."""
    from evaluation.runner import load_scenario
    (tmp_path / "query.md").write_text("What is wrong with the pod?")
    (tmp_path / "metadata.yaml").write_text("category: debugging\n")
    sc = load_scenario(tmp_path)
    assert sc["category"] == "debugging"


def test_load_scenario_category_empty_when_no_metadata(tmp_path):
    from evaluation.runner import load_scenario
    (tmp_path / "query.md").write_text("List all pods")
    sc = load_scenario(tmp_path)
    assert sc["category"] == ""


def test_list_scenarios_filters_by_category(tmp_path, monkeypatch):
    """list_scenarios(category_filter={'debugging'}) excludes other categories."""
    from evaluation import runner as runner_mod
    from evaluation.runner import list_scenarios

    s1 = tmp_path / "01-crashloop"
    s1.mkdir()
    (s1 / "query.md").write_text("q")
    (s1 / "metadata.yaml").write_text("category: debugging\n")

    s2 = tmp_path / "41-deploy-nginx"
    s2.mkdir()
    (s2 / "query.md").write_text("q")
    (s2 / "metadata.yaml").write_text("category: deployment\n")

    monkeypatch.setattr(runner_mod, "SCENARIOS_DIR", tmp_path)
    result = list_scenarios(category_filter={"debugging"})
    assert len(result) == 1
    assert result[0].name == "01-crashloop"
