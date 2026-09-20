import { createApp } from "vue";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";

// Element Plus 样式在前，项目样式在后：项目样式只做布局与少量覆盖。
import "element-plus/dist/index.css";
import "./style.css";

import App from "./App.vue";
import router from "./router";

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount("#app");
