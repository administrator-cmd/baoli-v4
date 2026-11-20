"""规则依赖按本次工作簿的对象身份绑定。业务歧义保存在导入提案中。"""
from collections import defaultdict

from app.common.exceptions import BusinessError


def _append_unique(items, value):
    if value not in items:
        items.append(value)


def prepare_rule_dependencies(proposals, problems):
    objects = {item['candidate_ref']: (group, item) for group in ('fields', 'rules', 'calculations')
               for item in proposals[group]}
    by_code = defaultdict(set)
    for ref, (group, item) in objects.items():
        if group == 'rules':
            codes = [item.get('rule_code') or item.get('source_id')]
        elif group == 'calculations':
            codes = [item.get('rule_code'), item['result_code']]
        else:
            continue
        for code in filter(None, codes):
            by_code[code].add(ref)

    questions = []
    for rule in proposals['rules']:
        owner = rule['candidate_ref']
        refs = []
        codes = []
        bound = {i['field_ref'] for i in rule.get('inputs', [])}
        choices = rule.pop('dependency_choices', [])

        def bind(reference, *, code_slot=False):
            # 两种命名空间可以同名，先按来源属性的契约解析。
            targets = by_code.get(reference) if code_slot else None
            if not targets:
                targets = {reference} if reference in objects else by_code.get(reference, set())
            if len(targets) > 1:
                choices.append({'term': reference, 'candidate_refs': sorted(targets)})
                return
            if not targets:
                problems.append({'owner': owner, 'kind': 'rule.dependency.unknown', 'reference': reference})
                return
            target = next(iter(targets))
            group, item = objects[target]
            if group == 'rules' and target != owner:
                _append_unique(refs, target)
            elif group == 'calculations' and by_code[item['result_code']] == {target}:
                _append_unique(codes, item['result_code'])
            else:
                problems.append({'owner': owner, 'kind': 'rule.dependency.invalid_target', 'reference': reference})
                return
            bound.add(target)

        for ref in dict.fromkeys([*(rule.get('source_rule_refs') or []), rule.get('source_rule_ref')]):
            if ref is not None:
                bind(ref)
        for code in rule.get('source_rule_codes') or []:
            bind(code, code_slot=True)
        rule['source_rule_refs'] = refs
        rule['source_rule_ref'] = rule.get('source_rule_ref') if rule.get('source_rule_ref') in refs else None
        rule['source_rule_codes'] = codes
        for choice in choices:
            candidates = list(dict.fromkeys(choice['candidate_refs']))
            options = []
            for ref in candidates:
                if ref not in objects or ref == owner or ref in bound:
                    break
                group, item = objects[ref]
                if group == 'calculations' and by_code[item['result_code']] != {ref}:
                    break
                origin = item.get('origin') or {}
                options.append({'ref': ref, 'group': group, 'name': item.get('rule_name') or item['name'],
                                'sheet_name': origin.get('sheet_name'), 'row_no': origin.get('row_no')})
            if len(options) != len(candidates) or len(options) < 2 or not choice['term'].strip():
                problems.append({'owner': owner, 'kind': 'rule.dependency.choice', 'reference': candidates})
                continue
            question = {'owner_ref': owner, 'term': choice['term'], 'options': options}
            _append_unique(questions, question)
    if questions:
        proposals['dependency_questions'] = questions


def answer_rule_dependencies(proposals, rule, answers):
    owner = rule['candidate_ref']
    pending = proposals.get('dependency_questions') or []
    questions = [q for q in pending if q['owner_ref'] == owner]
    if not questions or len(answers) != len(questions):
        raise BusinessError('IMPORT_DEPENDENCY_ANSWER_INVALID', '请逐项选择这条规则的引用对象')
    for q, ref in zip(questions, answers):
        option = next((o for o in q['options'] if o['ref'] == ref), None)
        if option is None:
            raise BusinessError('IMPORT_DEPENDENCY_ANSWER_INVALID', '请选择当前问题列出的对象')
        group = option['group']
        item = next(i for i in proposals[group] if i['candidate_ref'] == ref)
        if group == 'fields':
            if not any(i['field_ref'] == ref for i in rule['inputs']):
                rule['inputs'].append({'field_ref': ref, 'material_ref': item['material_ref']})
        elif group == 'rules':
            _append_unique(rule['source_rule_refs'], ref)
        else:
            _append_unique(rule['source_rule_codes'], item['result_code'])
    proposals['dependency_questions'] = [q for q in pending if q['owner_ref'] != owner]
