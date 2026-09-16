import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Sidebar } from "@/components/Sidebar";
import { LogConsole } from "@/components/LogConsole";
import { StatusBar } from "@/components/StatusBar";
import { ConnectView } from "@/features/ConnectView";
import { SingleView } from "@/features/SingleView";
import { BatchView } from "@/features/BatchView";
import { SearchableView } from "@/features/SearchableView";
import { ParamsView } from "@/features/ParamsView";
import { useSession } from "@/stores/session";
import { useBridgeReady } from "@/hooks/useBridgeReady";
import "./index.css";

const queryClient = new QueryClient();

function Views() {
  const view = useSession((s) => s.view);
  return (
    <>
      <div className={view === "connect" ? "block" : "hidden"}>
        <ConnectView />
      </div>
      <div className={view === "single" ? "block" : "hidden"}>
        <SingleView />
      </div>
      <div className={view === "batch" ? "block" : "hidden"}>
        <BatchView />
      </div>
      <div className={view === "searchable" ? "block" : "hidden"}>
        <SearchableView />
      </div>
      <div className={view === "params" ? "block" : "hidden"}>
        <ParamsView />
      </div>
    </>
  );
}

function App() {
  useBridgeReady();
  const dark = useSession((s) => s.dark);
  const themeMode = useSession((s) => s.themeMode);


  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  useEffect(() => {
    /* 只有「跟随系统」时才听系统变化，手动覆盖不会被抢回 */
    if (themeMode !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => useSession.getState().set({ dark: media.matches });
    apply();   /* 首屏就要跟随系统：原来只监听变化、不读初值，暗色主题永远不生效 */
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [themeMode]);

  return (
    <QueryClientProvider client={queryClient}>
      <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
        <div className="flex min-h-0 flex-1">
          <Sidebar />
          <main className="min-w-0 flex-1 overflow-y-auto" aria-label="主内容">
            <Views />
          </main>
        </div>
        <LogConsole />
        <StatusBar />
      </div>
    </QueryClientProvider>
  );
}

export default App;
