"""组装图的服务实现；各步骤直接绑定其职责模块，不提供旧模块兼容导出。"""

from . import aggregate, context, extraction_batch, lifecycle, model, results, tools


class AuditRunner:
    """满足 AuditNodeRunner 协议，供固定任务图与单条规则执行器调用。"""

    graph_load_task = staticmethod(lifecycle.graph_load_task)
    graph_execute_current_node = staticmethod(lifecycle.graph_execute_current_node)
    graph_finalize_node_execution = staticmethod(lifecycle.graph_finalize_node_execution)
    graph_advance_workflow = staticmethod(lifecycle.graph_advance_workflow)
    graph_save_node_result = staticmethod(lifecycle.graph_save_node_result)
    graph_prepare_materials = staticmethod(context.graph_prepare_materials)
    graph_load_evaluation = staticmethod(context.graph_load_evaluation)
    graph_execute_extraction_batch = staticmethod(extraction_batch.graph_execute_extraction_batch)
    graph_load_rule_instance = staticmethod(context.graph_load_rule_instance)
    graph_prepare_agent_context = staticmethod(context.graph_prepare_agent_context)
    graph_build_aggregate_verdict = staticmethod(aggregate.graph_build_aggregate_verdict)
    graph_call_model = staticmethod(model.graph_call_model)
    graph_execute_tool_calls = staticmethod(tools.graph_execute_tool_calls)
    graph_append_tool_results = staticmethod(tools.graph_append_tool_results)
    graph_produce_candidate_verdict = staticmethod(results.graph_produce_candidate_verdict)
    graph_save_evaluation_result = staticmethod(results.graph_save_evaluation_result)


audit_runner = AuditRunner()
