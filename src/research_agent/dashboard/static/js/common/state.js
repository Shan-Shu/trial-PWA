/* 跨模块共享状态与订阅（新增 tab 用；旧 app.js 仍用自己的 state 对象）。 */
"use strict";

const listeners = new Map();

export const store = {
  /** 文献库筛选条件（library tab 与详情面板共用） */
  libraryFilters: {
    q: "",
    status: "",
    source: "",
    decision: "",
    tag: "",
    folder_id: "",
    favorite: false,
    low_signal: "",
    year_mode: "",
    sort_by: "quality",
    sort_desc: true,
    limit: 50,
    offset: 0,
  },
  /** 上次文献库查询结果（供"导出选中"等操作复用） */
  libraryResult: null,
  /** 当前选中的文献 key 集合 */
  librarySelection: new Set(),
  /** 当前打开的文献 key（详情面板） */
  currentPaperKey: null,
  /** 写作台当前项目 */
  writingProjectId: null,
};

export function on(event, handler) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(handler);
  return () => listeners.get(event)?.delete(handler);
}

export function emit(event, payload) {
  (listeners.get(event) || []).forEach((handler) => {
    try {
      handler(payload);
    } catch (err) {
      console.error(`[store] 订阅者异常 event=${event}`, err);
    }
  });
}

/** 更新筛选条件并广播 */
export function setFilters(patch) {
  Object.assign(store.libraryFilters, patch || {});
  emit("filters", store.libraryFilters);
}

export function toggleSelection(key, on) {
  const set = store.librarySelection;
  const next = on === undefined ? !set.has(key) : Boolean(on);
  if (next) set.add(key);
  else set.delete(key);
  emit("selection", set);
  return next;
}

export function clearSelection() {
  store.librarySelection.clear();
  emit("selection", store.librarySelection);
}

export function selectedKeys() {
  return [...store.librarySelection];
}
