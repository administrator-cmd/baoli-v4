// 在 Node 中执行组件的实际 setup 和 Vue 响应式逻辑；所有接口均为内存替身。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { parse, compileScript } from "@vue/compiler-sfc";
import * as vue from "vue";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const flush = async () => {
  for (let index = 0; index < 6; index++) await vue.nextTick();
};
const operation = (status, reused = false) => ({ operation_no: "op1", operation_type: "RULEBOOK_REVISE", status, reused });
const document = () => ({ title: "原文", summary: "", requirements: [], issues: [], sheets: [] });
const job = (revision = 1) => ({ job_no: "job1", revision, document: document(), document_supported: true,
  can_edit: true, can_generate: false, can_retry: false, status: "EDITABLE", latest_operation: null });

function setup(t, relative = "src/views/documents/RulebookGenerationDetailView.vue", props = {}) {
  const scope = vue.effectScope();
  t.after(() => scope.stop());
  const tracked = vue.ref(null);
  let handlers;
  let sequence = 0;
  const failure = vue.ref(null);
  const route = vue.reactive({ params: { jobNo: "job1" }, query: {} });
  const calls = [];
  const api = {
    getGenerationJob: async target => { calls.push(target); return job(2); },
    listGenerationExports: async () => [], listGenerationRevisions: async () => [],
    listGenerationMessages: async () => ({ items: [] }),
    createGenerationExport: async () => ({ export_no: "export1", revision: 1 }),
    downloadGenerationExportFile: async () => {},
  };
  const imports = { listTemplates: async () => [] };
  const never = name => { throw new Error(`测试不允许未模拟接口：${name}`); };
  const strictApi = new Proxy(api, { get: (target, name) => target[name] ?? (() => never(name)) });
  const replacements = {
    vue: { ...vue, onMounted: () => {}, onUnmounted: () => {} },
    "vue-router": { useRoute: () => route, useRouter: () => ({ push: async () => {}, replace: async () => {} }),
      onBeforeRouteLeave: () => {}, onBeforeRouteUpdate: () => {} },
    "element-plus": { ElMessage: { success() {}, error() {}, warning() {} }, ElMessageBox: { confirm: async () => {} } },
    "@/api/rulebookGenerations": strictApi,
    "@/api/imports": imports,
    "@/api/http": { ApiError: class ApiError extends Error {},
      isOperationActive: status => ["PENDING", "RUNNING"].includes(status), operationIdempotencyKey: () => `key${++sequence}` },
    "@/constants/labels": { formatDate: value => value },
    "@/composables/useMediaQuery": { useMediaQuery: () => vue.ref(false) },
    "@/composables/useRequestRunner": { useRequestRunner: () => ({ failure,
      clear: () => { failure.value = null; }, setFailure: error => { failure.value = error; } }) },
    "@/composables/useOperationTracker": { useOperationTracker: () => ({ operation: tracked, watching: vue.ref(false),
      track: (value, next) => { tracked.value = value; handlers = next; }, requery() {}, reset: () => { tracked.value = null; } }) },
    "@/utils/ids": { newId: () => `id${++sequence}` },
  };
  function load(filename) {
    let source = fs.readFileSync(filename, "utf8");
    if (filename.endsWith(".vue")) source = compileScript(parse(source).descriptor, { id: "regression" }).content;
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
    const module = { exports: {} };
    const requireStub = name => {
      if (name in replacements) return replacements[name];
      if (name.endsWith(".vue")) return { default: {} };
      if (name.startsWith("@/")) return load(path.join(root, "src", `${name.slice(2)}.ts`));
      throw new Error(`未模拟的依赖：${name}`);
    };
    vm.runInNewContext(compiled, { module, exports: module.exports, require: requireStub, structuredClone }, { filename });
    return module.exports;
  }
  const component = load(path.join(root, relative)).default;
  const reactiveProps = vue.reactive(props);
  const state = scope.run(() => component.setup(reactiveProps, { expose() {}, emit() {} }));
  return { state, props: reactiveProps, api, imports, calls, route, failure,
    documentUtils: load(path.join(root, "src/utils/generationDocument.ts")),
    finish: async value => { tracked.value = value; await handlers.onFinished(value); } };
}

test("批次与目标规则只显示原值，不被当作历史记录的自身编号", t => {
  const { documentUtils: utils } = setup(t);
  const sheet = { columns: [
    { column_id: "batch", name: "批次ID", code_role: "GROUP" },
    { column_id: "target", name: "规则ID", is_business_code: true, code_role: "REFERENCE" },
  ] };
  const row = { cells: { batch: "BATCH-001", target: "RULE-001" }, business_code: "" };
  assert.equal(utils.businessCodeColumn(sheet), undefined);
  assert.equal(utils.entryBusinessCode(sheet, row), "");
  assert.equal(utils.entryCellValue(sheet, row, "batch"), "BATCH-001");
  assert.equal(utils.entryCellValue(sheet, row, "target"), "RULE-001");
  assert.equal(utils.cloneValue(sheet).columns[1].code_role, "REFERENCE");
});

test("未标注编号语义的旧文档仍优先使用原编号", t => {
  const { documentUtils: utils } = setup(t);
  const sheet = { columns: [
    { column_id: "extra", name: "编制编号" }, { column_id: "own", name: "材料规则ID" },
  ] };
  const row = { cells: { extra: "OLD-001", own: "SOURCE-001" }, business_code: "OLD-001" };
  assert.equal(utils.entryBusinessCode(sheet, row), "SOURCE-001");
});

test("跨表关联能区分相同的表内编号", t => {
  const { documentUtils: utils } = setup(t);
  const sheet = { name: "当前要求", business_code_scope: "SHEET", columns: [
    { column_id: "own", name: "规则ID", code_role: "IDENTITY" },
  ] };
  const row = { cells: { own: "001" }, business_code: "001" };
  assert.equal(utils.entryReferenceLabel(sheet, row), "当前要求 · 001");
  assert.equal(utils.entryReferenceLabel({ ...sheet, name: "另一组要求" }, row), "另一组要求 · 001");
  assert.equal(utils.entryBusinessCode(sheet, row), "001");
});

for (const output of ["download", "import"]) {
  test(`后台完成与${output}重叠后自动补查新版本`, async t => {
    const h = setup(t);
    const gate = deferred();
    h.api.createGenerationExport = () => gate.promise;
    h.state.applyJob(job(), true);
    h.state.loading.value = false;
    h.state.trackOperation(operation("RUNNING"));
    const producing = output === "download" ? h.state.downloadRevision(1) : h.state.useRevisionInScenario(1);
    await h.finish(operation("SUCCEEDED"));
    assert.equal(h.calls.length, 0);
    gate.resolve({ export_no: "export1", revision: 1 });
    await producing;
    await flush();
    assert.equal(h.calls.length, 1);
    assert.equal(h.state.job.value.revision, 2);
    assert.equal(h.state.draftRevision.value, 2);
  });
}

test("延后刷新保留未保存草稿及其旧修订基准", async t => {
  const h = setup(t);
  h.state.applyJob(job(), true);
  h.state.loading.value = false;
  h.state.draft.value.title = "未保存内容";
  h.state.dirty.value = true;
  h.state.exporting.value = true;
  await h.state.loadJob();
  h.state.exporting.value = false;
  await flush();
  assert.equal(h.state.job.value.revision, 2);
  assert.equal(h.state.draft.value.title, "未保存内容");
  assert.equal(h.state.draftRevision.value, 1);
  assert.equal(h.state.dirty.value, true);
});

for (const retry of [false, true]) {
  test(`编制${retry ? "重试" : "开始"}响应丢失后首次点击可重新受理`, async t => {
    const h = setup(t);
    const original = { ...job(0), document: null, can_generate: !retry, can_retry: retry, status: retry ? "FAILED" : "NOT_GENERATED" };
    h.state.applyJob(original, true);
    h.state.loading.value = false;
    const keys = [];
    const submit = async (_, key) => {
      keys.push(key);
      if (keys.length === 1) throw new Error("受理响应丢失");
      return operation(keys.length === 2 ? "FAILED" : "RUNNING", keys.length === 2);
    };
    h.api.startGeneration = submit;
    h.api.retryGeneration = async (...args) => {
      const accepted = await submit(...args);
      return { job: { ...original, latest_operation: accepted }, operation: accepted, reused: accepted.reused };
    };
    await h.state.generate();
    await h.state.generate();
    assert.equal(keys.length, 3);
    assert.equal(keys[0], keys[1]);
    assert.notEqual(keys[1], keys[2]);
    assert.equal(h.state.displayOperation.value.status, "RUNNING");
  });
}

test("助手响应丢失后的重试不会把旧 FAILED 当新受理", async t => {
  const h = setup(t);
  h.state.applyJob(job(), true);
  h.state.loading.value = false;
  h.state.assistantInstruction.value = "修改这一条";
  const keys = [];
  h.api.sendGenerationMessage = async (_, request) => {
    keys.push(request.idempotencyKey);
    if (keys.length === 1) throw new Error("受理响应丢失");
    return operation(keys.length === 2 ? "FAILED" : "RUNNING", keys.length === 2);
  };
  assert.equal(await h.state.sendInstruction("修改这一条"), false);
  assert.equal(h.state.assistantInstruction.value, "修改这一条");
  assert.equal(await h.state.sendInstruction("修改这一条"), true);
  assert.equal(keys.length, 3);
  assert.equal(keys[0], keys[1]);
  assert.notEqual(keys[1], keys[2]);
  assert.equal(h.state.displayOperation.value.status, "RUNNING");
});

test("新待补充项恢复自动筛选，手动选择仍保留", async t => {
  const h = setup(t, "src/components/generation/GenerationIssuesPanel.vue", {
    document: { ...document(), issues: [{ issue_id: "q1", status: "RESOLVED" }] }, submitAnswer: async () => true,
  });
  assert.equal(h.state.filter.value, "RESOLVED");
  h.props.document = { ...document(), issues: [{ issue_id: "q1", status: "OPEN" }] };
  await flush();
  assert.equal(h.state.filter.value, "OPEN");
  h.state.filterChosen.value = true;
  h.state.filter.value = "RESOLVED";
  h.props.document = { ...document(), issues: [{ issue_id: "q2", status: "OPEN" }] };
  await flush();
  assert.equal(h.state.filter.value, "RESOLVED");
});

test("保存刷新保留工作表、搜索和页码，关闭旧编辑副本", async t => {
  const current = { ...document(), sheets: ["s1", "s2"].map(sheet_id => ({ sheet_id, columns: [],
    rows: Array.from({ length: 50 }, (_, index) => ({ row_id: `${sheet_id}-${index}`, cells: { body: "内容" } })) })) };
  const h = setup(t, "src/components/generation/GenerationWorkbookPanel.vue", { document: current, refreshToken: 0 });
  h.state.selectedSheet.value = "s2";
  h.state.search.value = "内容";
  await flush();
  h.state.page.value = 2;
  h.state.entryDialog.value = true;
  h.state.editingRow.value = current.sheets[1].rows[0];
  h.props.document = structuredClone(current);
  h.props.refreshToken = 1;
  await flush();
  assert.equal(h.state.selectedSheet.value, "s2");
  assert.equal(h.state.search.value, "内容");
  assert.equal(h.state.page.value, 2);
  assert.equal(h.state.entryDialog.value, false);
  assert.equal(h.state.editingRow.value, null);
});
