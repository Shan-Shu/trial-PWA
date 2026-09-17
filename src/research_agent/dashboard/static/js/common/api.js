/* REST 客户端：统一错误处理与 JSON 编解码。 */
"use strict";

export class ApiError extends Error {
  constructor(message, { status = 0, path = "", payload = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.payload = payload;
  }
}

async function request(path, { method = "GET", body, headers } = {}) {
  const options = { method, headers: { ...(headers || {}) } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, options);
  } catch (err) {
    throw new ApiError(`无法连接后端: ${err.message}`, { path });
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
