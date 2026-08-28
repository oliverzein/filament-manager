# tests/test_template_index.py
"""Tests for the template index loader."""
from fm.presets.template_index import (
    load_index,
    templates_for_type,
    templates_for_type_and_vendor,
    all_vendors_for_type,
    preview_for_template,
)


def test_load_index_has_templates():
    idx = load_index()
    assert "templates" in idx
    assert len(idx["templates"]) > 300


def test_templates_for_type_petg():
    result = templates_for_type("PETG")
    assert len(result) > 0
    names = [t["name"] for t in result]
    assert "Bambu PETG Translucent" in names
    # Generic first
    assert result[0]["vendor"] == "Generic"


def test_templates_for_type_and_vendor_bambu():
    result = templates_for_type_and_vendor("PETG", "Bambu")
    assert len(result) > 0
    assert all("bambu" in t["vendor"].lower() for t in result)


def test_templates_for_type_and_vendor_empty():
    result = templates_for_type_and_vendor("PETG", "")
    # Empty vendor = no filter, should return all PETG templates
    assert len(result) == len(templates_for_type("PETG"))


def test_all_vendors_for_type_pla():
    vendors = all_vendors_for_type("PLA")
    assert "Generic" in vendors
    assert vendors[0] == "Generic"
    assert "Bambu Lab" in vendors


def test_preview_for_template_bambu_petg():
    preview = preview_for_template("Bambu PETG Translucent")
    assert preview is not None
    assert preview["filament_flow_ratio"] == "0.95"
    assert preview["fan_max_speed"] == "30"


def test_preview_for_template_nonexistent():
    preview = preview_for_template("Nonexistent Template")
    assert preview is None
