/** Minimal offline runner for web/src/lib/errors.ts toZh() — no vitest/jest needed.
 *
 *  Run:  node --experimental-strip-types web/src/lib/errors.test.ts
 *  (Node 24 strips types natively; the test imports ./errors.ts directly.)
 *  tsc:  covered by the project `tsc --noEmit` (no node types required).
 */
import { toZh } from "./errors.ts";

function eq(a: string, b: string): void {
  if (a !== b) throw new Error(`eq failed:\n  actual:   ${JSON.stringify(a)}\n  expected: ${JSON.stringify(b)}`);
}

function match(s: string, re: RegExp): void {
  if (!re.test(s)) throw new Error(`match failed: ${JSON.stringify(s)} !~ ${String(re)}`);
}

let n = 0;
function check(name: string, fn: () => void): void {
  fn();
  n += 1;
  console.log(`ok ${n} - ${name}`);
}

// Envelope codes surface as JSON inside Error.message (see engine.ts req()).
check("UNAUTHORIZED envelope prefers backend hint_cn", () => {
  const e = new Error(JSON.stringify({ error_code: "UNAUTHORIZED", hint_cn: "后端中文", error: "x" }) + " 后端中文");
  eq(toZh(e), "后端中文");
});
check("RATE_LIMITED falls back to ZH table without hint", () => {
  const e = new Error(JSON.stringify({ error_code: "RATE_LIMITED" }));
  match(toZh(e), /429/);
});
check("EMPTY_OUTPUT matched by code or text", () => {
  match(toZh(new Error(JSON.stringify({ error_code: "EMPTY_OUTPUT" }))), /max_output_tokens|返回空/);
  match(toZh(new Error("empty OCR output")), /max_output_tokens|返回空/);
});
check("AbortError maps to 已取消", () => {
  eq(toZh(new DOMException("aborted", "AbortError")), "已取消。");
});
check("HTTP 401 text maps to UNAUTHORIZED zh", () => {
  match(toZh(new Error("HTTP 401: Unauthorized")), /密钥无效/);
});
check("HTML gateway page maps to BAD_GATEWAY_HTML zh", () => {
  match(toZh(new Error("响应不是 JSON: HTTP 502 body=\"<html>\"")), /HTML/);
});
check("MISSING_PAGES envelope maps to resume hint", () => {
  const e = new Error(JSON.stringify({ error_code: "MISSING_PAGES", hint_cn: "缺 2 页" }) + " 缺 2 页");
  eq(toZh(e), "缺 2 页");
});
check("unknown message passes through", () => {
  eq(toZh(new Error("weird-bug-123")), "weird-bug-123");
});

console.log(`\nerrors.test: ${n} passed`);
