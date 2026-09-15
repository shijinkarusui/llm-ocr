/** Backend error_code -> Chinese guidance. Frontend-only, no backend dependency.
 *  Unknown codes fall through to the raw message so new codes stay visible. */

export interface BackendEnvelope {
  error?: string;
  error_code?: string;
  hint_cn?: string;
  next_action?: string;
  http_status?: number;
  elapsed_ms?: number;
}

const ZH: Record<string, string> = {
  UNAUTHORIZED: "密钥无效或无权限：检查密钥是否填错、过期或被轮换后重试。",
  MODEL_NOT_FOUND: "模型不存在：Octopus 网关请补 OC/ 前缀，或拉取模型列表核对拼写。",
  TIMEOUT: "请求超时：网络慢或网关排队，可稍后重试，或适当调大超时秒。",
  GATEWAY_5XX: "网关/上游 5xx：服务端不稳定，稍后重试即可，批量任务可断点续跑。",
  RATE_LIMITED: "触发限流(429)：并发过高或配额耗尽，请降并发后继续跑。",
  BAD_EXTRA_JSON: "透传 JSON 非法：顶层必须是对象，且以 --extra-json 为准，请修正后重试。",
  EMPTY_OUTPUT: "模型返回空：可调大 max_output_tokens，或把 reasoning 从 medium 降到 low 后重试。",
  BAD_GATEWAY_HTML: "网关返回了 HTML 而非 JSON：多为网关/代理错误页，请检查网关地址与网关状态。",
  MISSING_PAGES: "输出目录缺页：先回批量把缺的页补跑出来，再构建双层 PDF。",
  UNKNOWN: "未知错误：重试仍失败请带上 http_status 与运行日志报修。",
};

/** Map any thrown error / backend envelope to a Chinese one-liner. */
export function toZh(e: unknown): string {
  if (e instanceof DOMException && e.name === "AbortError") return "已取消。";
  const msg = e instanceof Error ? e.message : String(e ?? "");
  // Backend envelope often surfaces as JSON in message or as data.error.
  const m = msg.match(/"error_code"\s*:\s*"([A-Z_0-9]+)"/);
  const code = m?.[1];
  if (code && ZH[code]) {
    const hint = msg.match(/"hint_cn"\s*:\s*"([^"]*)"/)?.[1];
    return hint || ZH[code];
  }
  if (/HTTP 401|Unauthorized|Authentication failed/i.test(msg)) return ZH.UNAUTHORIZED;
  if (/model not found/i.test(msg)) return ZH.MODEL_NOT_FOUND;
  if (/429|rate limit|限流/i.test(msg)) return ZH.RATE_LIMITED;
  // Non-JSON gateway page: check the HTML shape BEFORE the generic HTTP 5xx
  // branch, so an HTML error page keeps its actionable hint.
  if (/响应不是 JSON|Unexpected token '<'|content-type/i.test(msg)) return ZH.BAD_GATEWAY_HTML;
  if (/timed? ?out|超时|AbortError/i.test(msg) && !/已取消/.test(msg)) return ZH.TIMEOUT;
  if (/HTTP 5\d\d/.test(msg)) return ZH.GATEWAY_5XX;
  if (/透传 JSON|extra-json/i.test(msg)) return ZH.BAD_EXTRA_JSON;
  if (/空输出|EMPTY_OUTPUT|empty/i.test(msg) && /输出|empty/i.test(msg)) return ZH.EMPTY_OUTPUT;
  return msg || "未知错误。";
}
