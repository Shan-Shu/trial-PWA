/* REST 客户端：统一错误处理与 JSON 编解码。 */
"use strict";

export class ApiError extends Error {
  constructor(message, { status = 0, path = "", payload = null, timeout = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.payload = payload;
    this.timeout = timeout;
  }
}

/** 默认超时：模型驱动的写入端点可能要几十秒，但**必须有上限**。
 *
 * 没有超时的后果（实测）：后端卡住时前端无限转圈，用户无法分辨
 * "模型慢"与"已经死了"，只能干等。有超时至少能报出来。
 */
const DEFAULT_TIMEOUT_MS = 120000;

/**
 * 本页会话的 trace：所有请求带上同一个 `X-Trace-Id`，后端中间件沿用，
 * 于是"点击 → 请求 → 作业 → 模型"落在同一条链上（`research-agent-logs --trace`）。
 * 用 sessionStorage 存，一次会话内稳定，刷新页面换新。
 */
const TRACE_KEY = "ra-trace";
export function traceId() {
  try {
    let value = sessionStorage.getItem(TRACE_KEY);
    if (!value) {
      value = `t-${Math.random().toString(16).slice(2, 10)}`;
      sessionStorage.setItem(TRACE_KEY, value);
    }
    return value;
  } catch {
    return "";
  }
}

/**
 * 记一条前端事件到后端统一日志。**只用于诊断**：失败静默忽略，
 * 绝不影响业务；事件名由后端白名单校验。
 */
export function logUiEvent(evt, data) {
  const payload = { evt, data: data || {}, trace: traceId() };
  try {
    fetch("/api/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* 诊断日志失败不能影响业务 */
  }
}

async function request(path, { method = "GET", body, headers, timeoutMs } = {}) {
  const options = { method, headers: { ...(headers || {}) } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const trace = traceId();
  if (trace) options.headers["X-Trace-Id"] = trace;
  const limit = timeoutMs || DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), limit);
  options.signal = controller.signal;

  let resp;
  try {
    resp = await fetch(path, options);
  } catch (err) {
    const aborted = err && (err.name === "AbortError" || controller.signal.aborted);
    throw new ApiError(
      aborted
        ? `请求超时（${Math.round(limit / 1000)}s）: ${path}`
        : `无法连接后端: ${err.message}`,
      { path, timeout: Boolean(aborted) },
    );
  } finally {
    clearTimeout(timer);
  }

  const text = await resp.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!resp.ok) {
    const detail = payload && typeof payload === "object" && payload.error
      ? payload.error
      : typeof payload === "string" ? payload.slice(0, 200) : "";
    throw new ApiError(
      `HTTP ${resp.status} ${path}${detail ? " · " + detail : ""}`,
      { status: resp.status, path, payload },
    );
  }
  return payload;
}

export const api = {
  get: (path) => request(path),
  post: (path, body) => request(path, { method: "POST", body: body ?? {} }),
  del: (path, body) => request(path, { method: "DELETE", body: body ?? {} }),
  request,
};

/** 组装查询串，跳过空值。 */
export function qs(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    if (value === false) return;
    search.set(key, String(value));
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}
