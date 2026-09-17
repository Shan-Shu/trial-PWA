/* ============================================================================
 * 页面注册表：9 页工作台（规划与检索合并为一条链，减少断裂）
 * 每页一个明确任务；页面模块导出 { title, subtitle, icon, group, mount, refresh }
 * ========================================================================== */
"use strict";

import { dashboardPage } from "./dashboard.js";
import { planretrievePage } from "./planretrieve.js";
import { libraryPage } from "./library.js";
import { knowledgePage } from "./knowledge.js";
import { ontologyPage } from "./ontology.js";
import { experimentPage } from "./experiment.js";
import { writingPage } from "./writing.js";
import { reviewPage } from "./review.js";
import { systemPage } from "./system.js";

/** 分组顺序即侧边栏顺序 */
export const PAGES = [
  { key: "dashboard", group: "总览", page: dashboardPage },
  { key: "planretrieve", group: "研究入口", page: planretrievePage },
  { key: "library", group: "资产", page: libraryPage },
  { key: "knowledge", group: "资产", page: knowledgePage },
  { key: "ontology", group: "资产", page: ontologyPage },
  { key: "experiment", group: "研究", page: experimentPage },
  { key: "writing", group: "研究", page: writingPage },
  { key: "review", group: "运行", page: reviewPage },
  { key: "system", group: "运行", page: systemPage },
];

export const DEFAULT_PAGE = "dashboard";

export function pageOf(key) {
  return (PAGES.find((p) => p.key === key)
    || PAGES.find((p) => p.key === DEFAULT_PAGE)).page;
}
