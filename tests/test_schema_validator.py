"""Tests for schema_validator — node input/output contract enforcement."""

import pytest

from symphony.sop import schema_validator as sv


class TestExtractPayload:
    def test_parses_raw_json_output(self):
        r = {"status": "completed", "output": '{"a": 1}', "provider": "pi"}
        assert sv.extract_payload(r) == {"a": 1}

    def test_parses_fenced_json_output(self):
        r = {"output": "Here you go:\n```json\n{\"a\": 2}\n```\nDone."}
        assert sv.extract_payload(r) == {"a": 2}

    def test_parses_embedded_object(self):
        r = {"output": "prefix {\"a\": 3, \"b\": [1,2]} suffix"}
        assert sv.extract_payload(r) == {"a": 3, "b": [1, 2]}

    def test_falls_back_to_raw_text(self):
        r = {"output": "no json here"}
        assert sv.extract_payload(r) == "no json here"

    def test_prefers_structured_data_field(self):
        r = {"output": "ignored", "data": {"x": 9}}
        assert sv.extract_payload(r) == {"x": 9}


class TestValidate:
    schema = {
        "type": "object",
        "required": ["changed_files"],
        "properties": {"changed_files": {"type": "array"}},
    }

    def test_empty_schema_always_passes(self):
        assert sv.validate({"anything": 1}, {}).ok

    def test_valid_payload_passes(self):
        assert sv.validate({"changed_files": ["a.py"]}, self.schema).ok

    def test_missing_required_fails_with_message(self):
        vr = sv.validate({}, self.schema)
        assert not vr.ok
        assert any("changed_files" in e for e in vr.errors)

    def test_wrong_type_fails(self):
        vr = sv.validate({"changed_files": "not-an-array"}, self.schema)
        assert not vr.ok


class TestValidateOutput:
    def test_output_recovered_from_text_and_validated(self):
        schema = {"type": "object", "required": ["ok"]}
        result = {"output": '{"ok": true}', "provider": "pi"}
        vr = sv.validate_output("n", result, schema)
        assert vr.ok and vr.payload == {"ok": True}

    def test_output_free_text_fails_object_schema(self):
        schema = {"type": "object", "required": ["ok"]}
        result = {"output": "I could not do it", "provider": "pi"}
        vr = sv.validate_output("n", result, schema)
        assert not vr.ok


class TestValidateInput:
    def test_upstream_output_flattened_and_validated(self):
        # B depends on A; A produced {"files": [...]}.
        schema = {
            "type": "object",
            "required": ["files"],
            "properties": {"files": {"type": "array"}},
        }
        node_input = {"analyze": {"output": '{"files": ["a.py"]}', "provider": "pi"}}
        vr = sv.validate_input("review", node_input, schema)
        assert vr.ok

    def test_upstream_missing_field_fails(self):
        schema = {"type": "object", "required": ["files"]}
        node_input = {"analyze": {"output": '{"other": 1}', "provider": "pi"}}
        vr = sv.validate_input("review", node_input, schema)
        assert not vr.ok
