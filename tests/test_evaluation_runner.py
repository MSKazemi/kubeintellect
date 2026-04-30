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
