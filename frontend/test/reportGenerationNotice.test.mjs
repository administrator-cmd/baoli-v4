// 渲染实际 Vue 组件，不启动浏览器或调用服务。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { compileScript, parse } from "@vue/compiler-sfc";
import * as vue from "vue";
import { renderToString } from "@vue/server-renderer";

const source = fs.readFileSync(new URL("../src/components/audit/ReportGenerationNotice.vue", import.meta.url), "utf8");
const script = compileScript(parse(source).descriptor, { id: "report-generation-notice", inlineTemplate: true }).content;
const compiled = ts.transpileModule(script, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const module = { exports: {} };
vm.runInNewContext(compiled, { module, exports: module.exports, require: () => vue });
const Notice = module.exports.default;

async function render(generation) {
  const app = vue.createSSRApp({ render: () => vue.h(Notice, { generation }) });
  app.component("el-alert", vue.defineComponent({
    props: ["title", "description"],
    setup: props => () => vue.h("div", { role: "alert" }, [props.title, props.description]),
  }));
  return renderToString(app);
}

test("旧档案降级明确显示实际生成方式和原选模板", async () => {
  const html = await render({ mode: "RULES", requested_mode: "TEMPLATE", template_name: "合同报告",
    fallback_reason: "模板档案无效，本次按规则生成" });
  assert.match(html, /role="alert"/);
  assert.match(html, /模板档案无效，本次按规则生成/);
  assert.match(html, /原选模板：合同报告/);
});

test("正常模板模式显示模板名称且不报警", async () => {
  const html = await render({ mode: "TEMPLATE", template_name: "合同报告" });
  assert.match(html, /生成方式：仿照模板/);
  assert.match(html, /合同报告/);
  assert.doesNotMatch(html, /role="alert"/);
});

test("普通规则模式不显示降级警告或旧模板名称", async () => {
  const html = await render({ mode: "RULES", template_name: "旧名称" });
  assert.match(html, /生成方式：按规则生成/);
  assert.doesNotMatch(html, /role="alert"|旧名称/);
});

test("历史报告缺少生成元数据时不推断生成方式", async () => {
  for (const generation of [null, undefined]) {
    assert.doesNotMatch(await render(generation), /生成方式|role="alert"|按规则生成|仿照模板/);
  }
});
