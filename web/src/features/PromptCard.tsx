import { useState } from "react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useSession } from "@/stores/session";
import { useLog } from "@/stores/log";
import { promptText } from "@/lib/engine";

export function PromptCard() {
  const value = useSession((x) => x.ocrPrompt);
  const set = useSession((x) => x.set);
  const emit = useLog((x) => x.emit);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const customized = value.trim().length > 0;

  async function loadDefault() {
    setBusy(true);
    try {
      const r = await promptText();
      set({ ocrPrompt: r.prompt });
      setNote("已载入服务端默认提示词，可在此基础上修改");
      emit("已载入默认提示词", "ok");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setNote(`载入失败：${msg}`);
      emit(`载入默认提示词失败：${msg}`, "err");
    } finally {
      setBusy(false);
    }
  }

  function clearCustom() {
    set({ ocrPrompt: "" });
    setNote("已清除，改用服务端默认的 prompts/ocr_system.md");
    emit("已清除自定义提示词", "muted");
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>OCR 提示词</CardTitle>
        <CardDescription>
          留空即用服务端默认的 prompts/ocr_system.md；一旦填写，单页 OCR 与批量任务都会改用它。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p role="status" className="text-[13px]">
          <span className={customized ? "font-medium text-foreground" : "text-muted-foreground"}>
            {customized
              ? `正在使用自定义提示词（${value.length} 字）`
              : "正在使用服务端默认提示词"}
          </span>
        </p>
        <Textarea
          rows={10}
          value={value}
          onChange={(e) => set({ ocrPrompt: e.target.value })}
          placeholder="留空 = 使用默认提示词；也可点下方「载入默认提示词」取一份可编辑的副本"
          aria-label="自定义 OCR 提示词"
          aria-describedby="prompt-card-note"
        />
        {note && (
          <p id="prompt-card-note" role="status" className="text-xs leading-5 text-muted-foreground">
            {note}
          </p>
        )}
      </CardContent>
      <CardFooter>
        <Button variant="outline" onClick={loadDefault} disabled={busy}>
          {busy ? "载入中…" : "载入默认提示词"}
        </Button>
        <Button className="ml-2" variant="outline" onClick={clearCustom} disabled={!customized}>
          清除自定义
        </Button>
      </CardFooter>
    </Card>
  );
}
