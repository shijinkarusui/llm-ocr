import { useSession } from "@/stores/session";
import { maskedKey } from "@/lib/utils";

export function StatusBar() {
  const baseUrl = useSession((s) => s.baseUrl);
  const model = useSession((s) => s.model);
  const endpoint = useSession((s) => s.endpoint);
  const apiKey = useSession((s) => s.apiKey);

  let host = baseUrl;
  try {
    host = new URL(baseUrl).host;
  } catch {
    /* keep raw */
  }

  return (
    <footer
      aria-label="状态栏"
      className="flex h-6 shrink-0 items-center gap-4 border-t border-border bg-titlebar px-4 text-xs text-muted-foreground"
    >
      <span className="tabular">网关 {host}</span>
      <span className="truncate">模型 {model}</span>
      <span>链路 {endpoint}</span>
      <span className="ml-auto tabular">密钥 {maskedKey(apiKey)}</span>
    </footer>
  );
}
