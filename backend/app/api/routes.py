"""HTTP 路由汇总：把各业务子包的路由挂到同一个前缀下。

各子包只导出自己的 router 变量，汇总关系集中在这里，子包之间互不引用。
"""

from fastapi import APIRouter

from .audit import documents as audit_documents
from .audit import executions as audit_executions
from .audit import history as audit_history
from .audit import results as audit_results
from .audit import tasks as audit_tasks
from .imports import jobs as import_jobs
from .material import documents as material_documents
from .plan import configuration as plan_configuration
from .plan import lifecycle as plan_lifecycle
from .plan import tasks as plan_tasks
from .report import reports as report_reports
from .report import risks as report_risks
from .scenario import calculations as scenario_calculations
from .scenario import configuration as scenario_configuration
from .scenario import fields as scenario_fields
from .scenario import lifecycle as scenario_lifecycle
from .scenario import materials as scenario_materials
from .scenario import publishing as scenario_publishing
from .scenario import rules as scenario_rules
from .scenario import sections as scenario_sections
from .system import catalogs as system_catalogs
from .system import logs as system_logs
from .system import operations as system_operations
from .system import status as system_status

ROUTERS: tuple[APIRouter, ...] = (
    system_status.router,
    system_catalogs.router,
    system_logs.router,
    system_operations.router,
    import_jobs.router,
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
    audit_results.router,
    audit_history.executions_router,
    material_documents.router,
    report_reports.router,
    report_risks.risks_router,
)
