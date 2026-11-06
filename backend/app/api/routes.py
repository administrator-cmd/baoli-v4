"""HTTP 路由汇总：把各业务子包的路由挂到同一个前缀下。

各子包只导出自己的 router 变量，汇总关系集中在这里，子包之间互不引用。
"""

from fastapi import APIRouter

from app.modules.audit.artifact import controller as audit_documents
from app.modules.audit.execution import controller as audit_executions
from app.modules.audit.execution import history_controller as audit_history
from app.modules.audit.field import controller as audit_fields
from app.modules.audit.report import controller as audit_report
from app.modules.audit.report import report_controller as report_reports
from app.modules.audit.report import risk_controller as report_risks
from app.modules.audit.report_template import controller as report_templates
from app.modules.audit.task import controller as audit_tasks
from app.modules.imports.generation import controller as rulebook_generations
from app.modules.imports.job import controller as import_jobs
from app.modules.material.document import controller as material_documents
from app.modules.plan.config import controller as plan_configuration
from app.modules.plan.plan import controller as plan_lifecycle
from app.modules.plan.plan import task_controller as plan_tasks
from app.modules.scenario.draft import calculations_controller as scenario_calculations
from app.modules.scenario.draft import fields_controller as scenario_fields
from app.modules.scenario.draft import materials_controller as scenario_materials
from app.modules.scenario.draft import rules_controller as scenario_rules
from app.modules.scenario.draft import sections_controller as scenario_sections
from app.modules.scenario.template import configuration_controller as scenario_configuration
from app.modules.scenario.template import controller as scenario_lifecycle
from app.modules.scenario.template import publishing_controller as scenario_publishing
from app.modules.system.console import catalogs_controller as system_catalogs
from app.modules.system.console import operations_controller as system_operations
from app.modules.system.console import status_controller as system_status
from app.modules.system.operation import controller as system_logs

ROUTERS: tuple[APIRouter, ...] = (
    system_status.router,
    system_catalogs.router,
    system_logs.router,
    system_operations.router,
    import_jobs.router,
    rulebook_generations.router,
    scenario_lifecycle.router,
    scenario_configuration.router,
    scenario_materials.router,
    scenario_fields.router,
    scenario_rules.router,
    scenario_calculations.router,
    scenario_sections.router,
    scenario_publishing.router,
    plan_lifecycle.router,
    plan_configuration.router,
    plan_tasks.router,
    audit_tasks.router,
    audit_executions.router,
    audit_documents.router,
    audit_report.router,
    audit_fields.router,
    audit_history.executions_router,
    material_documents.router,
    report_reports.router,
    report_risks.risks_router,
    report_templates.router,
)
