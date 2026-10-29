import { createApp } from "vue";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";

// Element Plus 样式在前，项目样式在后：项目样式只做布局与少量覆盖。
import "element-plus/dist/index.css";
import "./style.css";

import App from "./App.vue";
import { loadStatusVocabulary } from "./constants/statusVocabulary";
import router from "./router";

// 状态文案以后端词表为准；读取失败时页面会显示原始状态码，这里只记录失败原因。
void loadStatusVocabulary().catch((error) => {
  console.error("审核状态词表读取失败，页面将显示原始状态码", error);
});

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount("#app");
