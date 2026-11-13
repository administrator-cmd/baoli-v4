"""配置更新边界回归：不访问数据库、文件或外部服务。"""

import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.plan.config.model import PlanField, PlanRule
from app.services.shared import configuration as config_store


class ConfigurationUpdateTests(unittest.TestCase):
    def setUp(self):
        self.owner = SimpleNamespace(config_json={})
        self.dirty = self.enterContext(patch.object(config_store, "flag_modified"))
        self.field = config_store.ConfigItem(
            self.owner,
            "fields",
            {"id": 1, "field_key": "f1", "name": "原字段", "unit": "元", "custom": {"note": "保留"}},
            PlanField,
        )

    def test_partial_edit_preserves_identity_and_business_metadata(self):
        config_store.update_field(self.field, name="新字段", unit=None)
        self.assertEqual(self.field.name, "新字段")
        self.assertIsNone(self.field.unit)
        self.assertEqual(self.field.field_key, "f1")
        self.assertEqual(self.field.data["custom"], {"note": "保留"})
        self.dirty.assert_called_once_with(self.owner, "config_json")

    def test_wrong_section_and_unknown_fields_do_not_partially_apply(self):
        original = deepcopy(self.field.data)
        for update in (
            lambda: config_store.update_rule(self.field, name="错误集合"),
            lambda: config_store.update_field(self.field, **{"name": "不应保存", "naem": "拼错"}),
            lambda: config_store.update_field(self.field, **{"field_key": "另一个身份"}),
        ):
            with self.assertRaises(ValueError):
                update()
            self.assertEqual(self.field.data, original)
        self.dirty.assert_not_called()

    def test_implicit_assignment_cannot_bypass_the_update_boundary(self):
        with self.assertRaises(FrozenInstanceError):
            self.field.name = "绕过入口"
        self.assertEqual(self.field.name, "原字段")
        self.dirty.assert_not_called()

    def test_nested_update_does_not_retain_the_callers_mutable_object(self):
        schema = {"properties": {"amount": {"type": "number"}}}
        config_store.update_field(self.field, structure_schema_json=schema)
        schema["properties"].clear()
        self.assertEqual(self.field.structure_schema_json["properties"], {"amount": {"type": "number"}})

    def test_extensions_preserve_existing_metadata_and_cannot_overwrite_fixed_fields(self):
        config_store.update_extensions(
            self.field, {"enabled": False, "custom": {"note": "更新"}, "model": {"name": "业务元数据"}}
        )
        original = deepcopy(self.field.data)
        with self.assertRaises(ValueError):
            config_store.update_extensions(self.field, {"custom": {}, "name": "覆盖固定字段"})
        self.assertEqual(self.field.data, original)
        self.assertFalse(self.field.enabled)
        self.assertEqual(self.field.data["custom"], {"note": "更新"})
        self.assertEqual(self.field.model, {"name": "业务元数据"})

    def test_generated_rule_sync_is_separate_from_ordinary_rule_editing(self):
        rule = config_store.ConfigItem(self.owner, "rules", {"rule_key": "old", "custom": "保留"}, PlanRule)
        with self.assertRaises(ValueError):
            config_store.update_rule(rule, **{"rule_key": "new"})
        config_store.update_generated_rule(rule, rule_key="new", origin_json={"source": "compiled"})
        self.assertEqual(rule.rule_key, "new")
        self.assertEqual(rule.origin_json, {"source": "compiled"})
        self.assertEqual(rule.custom, "保留")


if __name__ == "__main__":
    unittest.main()
