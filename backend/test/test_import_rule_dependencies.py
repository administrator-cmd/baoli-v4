"""Excel 导入依赖的身份归一和人工选择回归。"""
import unittest

from app.common.exceptions import BusinessError
from app.domain.imports.rule_dependencies import answer_rule_dependencies, prepare_rule_dependencies


class ImportDependencyTest(unittest.TestCase):
    def setUp(self):
        self.rule = {'candidate_ref': 'R1', 'name': '审核', 'inputs': [], 'source_rule_ref': 'CO-003',
                     'source_rule_refs': ['R2', 'CO-003'], 'source_rule_codes': ['FIDX-004']}
        self.proposals = {
            'rules': [self.rule, {'candidate_ref': 'R2', 'name': '核对'}],
            'calculations': [{'candidate_ref': 'CO-003', 'result_code': 'FIDX-004', 'rule_name': '比率'}],
            'fields': [{'candidate_ref': 'F1', 'name': '合并收入', 'material_ref': 'M1'},
                       {'candidate_ref': 'F2', 'name': '本部收入', 'material_ref': 'M2'}],
        }

    def test_exact_result_keeps_existing_rule_and_deduplicates_calculation(self):
        problems = []
        prepare_rule_dependencies(self.proposals, problems)
        self.assertEqual(problems, [])
        self.assertEqual(self.rule['source_rule_refs'], ['R2'])
        self.assertIsNone(self.rule['source_rule_ref'])
        self.assertEqual(self.rule['source_rule_codes'], ['FIDX-004'])
        self.assertEqual(self.proposals.get('dependency_questions', []), [])

    def test_field_choice_uses_chosen_material_and_keeps_existing_dependencies(self):
        self.rule['dependency_choices'] = [{'term': '收入', 'candidate_refs': ['F1', 'F2']}]
        problems = []
        prepare_rule_dependencies(self.proposals, problems)
        self.assertEqual(problems, [])
        with self.assertRaises(BusinessError):
            answer_rule_dependencies(self.proposals, self.rule, ['CO-003'])
        self.assertEqual(len(self.proposals['dependency_questions']), 1)
        answer_rule_dependencies(self.proposals, self.rule, ['F2'])
        self.assertEqual(self.rule['inputs'], [{'field_ref': 'F2', 'material_ref': 'M2'}])
        self.assertEqual(self.rule['source_rule_codes'], ['FIDX-004'])
        self.assertEqual(self.proposals.get('dependency_questions', []), [])

    def test_same_business_code_requires_choice_between_rules(self):
        self.proposals['rules'].extend({'candidate_ref': ref, 'name': ref, 'rule_code': 'shared'} for ref in ['A', 'B'])
        self.rule['source_rule_codes'].append('shared')
        problems = []
        prepare_rule_dependencies(self.proposals, problems)
        self.assertEqual(problems, [])
        answer_rule_dependencies(self.proposals, self.rule, ['B'])
        self.assertEqual(self.rule['source_rule_refs'], ['R2', 'B'])

    def test_unknown_ref_and_nonunique_calculation_output_cannot_be_autobound(self):
        self.rule['source_rule_codes'] = []
        self.rule['source_rule_refs'].append('FIDX-003')
        self.proposals['rules'][1]['rule_code'] = 'FIDX-004'
        problems = []
        prepare_rule_dependencies(self.proposals, problems)
        self.assertEqual({p['reference'] for p in problems}, {'FIDX-003', 'CO-003'})
        self.assertEqual(self.rule['source_rule_codes'], [])
