"""Unit tests for the flat worker recipe's response.json_schema.

The schema is the contract between the worker session and the ingest
pipeline. These tests pin its shape so changes are deliberate.
"""
from __future__ import annotations

import pathlib

import jsonschema
import pytest
import yaml

RECIPE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "prompts" / "recipes" / "worker.yaml"
)


def _load_schema() -> dict:
    with RECIPE_PATH.open() as f:
        recipe = yaml.safe_load(f)
    return recipe["response"]["json_schema"]


def test_schema_is_valid_json_schema():
    """The schema itself must be a well-formed Draft-7 JSON Schema —
    a malformed schema fails at goose recipe load time, before any
    model call, which is the most expensive way to catch typos."""
    jsonschema.Draft7Validator.check_schema(_load_schema())


def test_no_finding_with_empty_findings_validates():
    """A clean no-finding investigation — the most common shape."""
    schema = _load_schema()
    obj = {
        "verdict": "no-finding",
        "anchor_file": "/workspace/api/permissions.py",
        "task_id": 15,
        "summary": "Investigated DRF permission classes; no real-boundary sinks found.",
        "findings": [],
    }
    jsonschema.validate(obj, schema)


def test_finding_with_one_well_formed_entry_validates():
    schema = _load_schema()
    obj = {
        "verdict": "finding",
        "anchor_file": "/workspace/api/views.py",
        "task_id": 18,
        "summary": "Found one DRF ViewSet missing permission_classes.",
        "findings": [
            {
                "finding_type": "missing authorization",
                "cwe": "CWE-287",
                "severity": "high",
                "title": "EventViewSet exposes CRUD without auth gate",
                "sink_location": "api/views.py:295",
                "sink_category": "authorization",
                "summary": "EventViewSet exposes Event CRUD operations without permission_classes.",
                "source_to_sink": "DRF router → EventViewSet (api/views.py:295) → Event.objects.* (no gate)",
                "why_insufficient": "No permission_classes set, no project-wide DEFAULT_PERMISSION_CLASSES, no base-class gate.",
            }
        ],
    }
    jsonschema.validate(obj, schema)


def test_finding_with_bare_int_cwe_rejected():
    """The schema's cwe pattern is the canonical defense against the
    parser-hostile shapes the markdown frontmatter pipeline used to
    silently accept (e.g. `cwe: 287` becoming the int 287)."""
    schema = _load_schema()
    obj = {
        "verdict": "finding",
        "anchor_file": "/workspace/api/views.py",
        "task_id": 18,
        "summary": "test",
        "findings": [
            {
                "finding_type": "missing auth",
                "cwe": 287,  # bare int — must reject (not even a string)
                "severity": "high",
                "title": "test",
                "sink_location": "api/views.py:1",
                "sink_category": "authorization",
                "why_insufficient": "test",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(obj, schema)


def test_finding_with_string_cwe_missing_prefix_rejected():
    schema = _load_schema()
    obj = {
        "verdict": "finding",
        "anchor_file": "/workspace/x.py",
        "task_id": 1,
        "summary": "test",
        "findings": [
            {
                "finding_type": "missing auth",
                "cwe": "287",  # missing "CWE-" prefix
                "severity": "high",
                "title": "test",
                "sink_location": "x.py:1",
                "sink_category": "authorization",
                "why_insufficient": "test",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(obj, schema)


def test_finding_with_abbreviated_severity_rejected():
    schema = _load_schema()
    obj = {
        "verdict": "finding",
        "anchor_file": "/workspace/x.py",
        "task_id": 1,
        "summary": "test",
        "findings": [
            {
                "finding_type": "missing auth",
                "cwe": "CWE-287",
                "severity": "med",  # not in enum (must be info|low|medium|high|critical)
                "title": "test",
                "sink_location": "x.py:1",
                "sink_category": "authorization",
                "why_insufficient": "test",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(obj, schema)


def test_finding_with_unknown_sink_category_rejected():
    schema = _load_schema()
    obj = {
        "verdict": "finding",
        "anchor_file": "/workspace/x.py",
        "task_id": 1,
        "summary": "test",
        "findings": [
            {
                "finding_type": "missing auth",
                "cwe": "CWE-287",
                "severity": "high",
                "title": "test",
                "sink_location": "x.py:1",
                "sink_category": "other",  # not in nine-category enum
                "why_insufficient": "test",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(obj, schema)


def test_walked_back_verdict_rejected():
    """walked-back-finding was an earlier verdict; the worker's vocabulary
    is just finding | no-finding."""
    schema = _load_schema()
    obj = {
        "verdict": "walked-back-finding",
        "anchor_file": "/workspace/x.py",
        "task_id": 1,
        "summary": "test",
        "findings": [],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(obj, schema)
