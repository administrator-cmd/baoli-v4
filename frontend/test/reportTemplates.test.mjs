// 执行实际 Vue setup，所有请求均为内存替身，不启动浏览器或服务。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { compileScript, parse } from "@vue/compiler-sfc";
import * as vue from "vue";

const profile = () => ({ version: "1.0", document_overview: "审核报告", sections: [{ name: "结论", purpose: "概述" }],
  structure: {}, expression: {}, formatting_conventions: [] });
const template = (style = null) => ({ template_no: "template1", name: "原名称", style_profile: style,
  status: style ? "READY" : "FAILED", analysis_error: "分析未完成", latest_operation: null });

function setup(t) {
  const source = fs.readFileSync(new URL("../src/views/reports/ReportTemplateListView.vue", import.meta.url), "utf8");
  const script = compileScript(parse(source).descriptor, { id: "report-template-review" }).content;
  const compiled = ts.transpileModule(script, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const scope = vue.effectScope();
  t.after(() => scope.stop());
  const saves = [];
  const api = {
    listReportTemplates: async () => ({ items: [] }),
    updateReportTemplate: async (id, payload) => { saves.push({ id, payload }); return {}; },
  };
  const fail = name => { throw new Error(`未模拟调用：${name}`); };
  const modules = {
    vue: { ...vue, onMounted() {}, onUnmounted() {} },
    "element-plus": { ElMessage: { success() {}, error() {}, warning() {}, info() {} } },
    "@/api/reportTemplates": new Proxy(api, { get: (target, name) => target[name] ?? (() => fail(name)) }),
    "@/api/http": {
      isOperationActive: status => ["PENDING", "RUNNING"].includes(status),
      operationIdempotencyKey: () => "key",
      getOperation: () => fail("getOperation"), watchOperation: () => fail("watchOperation"),
    },
    "@/constants/labels": { formatDate: value => value },
  };
  const module = { exports: {} };
  vm.runInNewContext(compiled, {
    module, exports: module.exports, Error,
    require: name => name in modules ? modules[name] : fail(name),
  });
  const state = scope.run(() => module.exports.default.setup({}, { expose() {} }));
  return { state, api, saves };
}

test("首次分析失败的模板可以打开弹窗并单独改名", async t => {
  const h = setup(t);
  h.state.openEdit(template());
  assert.equal(h.state.editVisible.value, true);
  assert.equal(h.state.profileText.value, "");
  h.state.editName.value = " 新名称 ";
  await h.state.submitEdit();
  assert.equal(h.saves.length, 1);
  assert.equal(h.saves[0].payload.name, "新名称");
  assert.equal(h.saves[0].payload.style_profile, null);
  assert.equal(h.state.editVisible.value, false);
});

test("有效档案仍随编辑保存，服务端拒绝时保留用户输入", async t => {
  const h = setup(t);
  h.state.openEdit(template(profile()));
  h.state.editName.value = "手工名称";
  h.api.updateReportTemplate = async (_, payload) => {
    assert.equal(payload.style_profile.document_overview, "审核报告");
    throw new Error("风格档案超过 8 KiB");
  };
  await h.state.submitEdit();
  assert.equal(h.state.editVisible.value, true);
  assert.equal(h.state.editName.value, "手工名称");
  assert.equal(JSON.parse(h.state.profileText.value).sections[0].name, "结论");
  assert.equal(h.state.profileError.value, "风格档案超过 8 KiB");
});

test("编辑已有档案不能用 null 或数组静默变成只改名", async t => {
  const h = setup(t);
  for (const json of ["null", "[]", "3", '"text"', "{invalid"]) {
    h.state.openEdit(template(profile()));
    h.state.profileText.value = json;
    await h.state.submitEdit();
    assert.equal(h.saves.length, 0);
    assert.ok(h.state.profileError.value);
    assert.equal(h.state.editVisible.value, true);
  }
});

test("正在分析的模板仍不能通过单独改名绕过编辑互斥", async t => {
  const h = setup(t);
  const row = { ...template(), latest_operation: { operation_type: "REPORT_TEMPLATE_ANALYZE", status: "RUNNING" } };
  h.state.items.value = [row];
  h.state.openEdit(row);
  h.state.editName.value = "新名称";
  await h.state.submitEdit();
  assert.equal(h.saves.length, 0);
  assert.equal(h.state.editLocked.value, true);
});

test("有效档案与名称可以正常保存并刷新列表", async t => {
  const h = setup(t);
  h.state.openEdit(template(profile()));
  h.state.editName.value = "新名称";
  const edited = { ...profile(), document_overview: "财报审核报告" };
  h.state.profileText.value = JSON.stringify(edited);
  await h.state.submitEdit();
  assert.equal(h.saves.length, 1);
  assert.equal(h.saves[0].payload.style_profile.document_overview, "财报审核报告");
  assert.equal(h.state.profileError.value, null);
  assert.equal(h.state.editVisible.value, false);
});
