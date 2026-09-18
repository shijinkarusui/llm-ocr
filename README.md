# llm-ocr

**English** · [简体中文](README.zh-CN.md)

> Vision-LLM OCR toolchain for scanned Chinese phonetics textbooks: per-page
> Markdown transcription (IPA-safe) + raster-backed searchable dual-layer PDF,
> driven by any OpenAI-compatible gateway. Desktop app (Tauri 2 + React 19,
> engine sidecar over HTTP) and CLI share one engine. API key lives in
> memory/env only — never in git.

---

## 0. What it is

- **Input**: scanned images / scanned PDF (each page rasterized to PNG).
- **Output A**: high-fidelity Markdown — whole book `book.md` + per-page
  `pages/page_*.md` (Chinese punctuation normalized, IPA kept verbatim,
  diacritics emitted as Unicode combining marks).
- **Output B**: dual-layer searchable PDF `book_searchable.pdf` — original
  raster as background (same page size), plus an invisible searchable text
  layer. The old garbled text layer is never reused.
- **Model**: any OpenAI-compatible `POST /v1/*` gateway —
  `chat/completions`, `responses`, `messages` all supported, arbitrary
  official fields pass straight through (`**kwargs` / `--extra-json`).
- **Key hygiene**: `LLM_OCR_KEY` (also `LLM_OCR_API_KEY`/`OCTOPUS_API_KEY`),
  `.env` autoload, never in git (`.gitignore` enforces it).

## 1. Architecture

```
                          +-------------------+
                          |   prompts/*.md    |  OCR contract
                          | ocr_system.md     |  (transcription rules,
                          | notation_spec.md  |   Unicode combining map)
                          +----+------+-------+
                               |      |
              +----------------+      +-----------------+
              |                                       |
     +--------v--------+                   +----------v--------+
     |  DESKTOP APP    |  HTTP bridge      |       CLI         |
     |  web/ (Tauri)   |<----------------->|  src/cli.py       |
     |  React 5 views  |  serve.py :21139  |  models/probe/    |
     |  + log console  |  key in memory    |  ocr/batch/       |
     |  + status bar   |  job poll/cancel  |  config/--tui     |
     |                 |                   |                   |
     |                 |  thin dispatch:   |  cli.py -> per-   |
     |                 |  bridge -> engine |  module mains,    |
     |                 |                   |  one resolve path |
     +--------+--------+                   +----------+--------+
              |                                       |
              +-------------------+-------------------+
                                  |
                    +-------------v--------------+
                    |      CORE ENGINE (src/)    |
                    |                            |
                    | config.py  GlobalConfig{   |
                    |  base_url,model,key,       |
                    |  timeout} vs RunConfig{    |
                    |  endpoint,detail,          |
                    |  concurrency,dpi,retries,  |
                    |  system,extra}             |
                    |  Priority: CLI > env >     |
                    |  DEFAULT; key in memory    |
                    |  only, write_env() never   |
                    |  persists plaintext        |
                    |                            |
                    | llm_client.py  TRANSPORT:  |
                    |  generic_request(endpoint, |
                    |  body) low-level + typed   |
                    |  chat_completions /        |
                    |  responses_create /        |
                    |  anthropic_messages +      |
                    |  *_vision (+_url) helpers; |
                    |  auto_vision() 1+1 route;  |
                    |  HttpError fail-fast 4xx,  |
                    |  retry 429/5xx/timeout;    |
                    |  _resolve_endpoint() anti  |
                    |  /v1/v1 double-write       |
                    +-------------+--------------+
                                  |
            +---------------------+----------------------+
            |                     |                      |
 +----------v---------+ +---------v--------+ +-----------v---------+
 | ocr_page.py        | | batch_plan.py    | | make_searchable.py  |
 | SINGLE PAGE:       | | BATCH:           | | DUAL-LAYER PDF:     |
 | image / pdf-page / | | ThreadPool 1-20, | | raster = background |
 | image-url -> PNG   | | adaptive halve/  | | (same size), text   |
 | -> *_vision()      | | recover, dry-run | | layer invisible;    |
 | -> page MD         | | plan, usage.jsonl| | boxes reliable ->   |
 |                    | | append (resume), | | render_mode=3 exact |
 |                    | | skipped logged   | | else fallback dual  |
 +----------+---------+ +---------+--------+ | copy (full-hidden   |
            |                     |          | + per-line spread)   |
 +----------v---------+ +---------v--------+ +-----------v---------+
 | check_notation.py  | | postprocess.py   | | verify_searchable.py|
| GATE: banned       | | page/book cleaner | | VERIFY: per-page    |
 | ascii/lost marks/  | | punct, footers,  | | keyword hits,       |
 | no LaTeX residue   | | protect math) +  | | copyable chars,     |
 | -> 0 issues gate   | | merge_pages +    | | compare_md diff     |
 |                    | | polish_with_llm  | |                     |
 +----------+---------+ +---------+--------+ +-----------+---------+
            |                     |                      |
            +---------------------+----------------------+
                                  |
                    +-------------v--------------+
                    | render.py (PyMuPDF/fitz) |
                    | render_page(pdf,pno,dpi) |
                    | -> opaque PNG bytes      |
                    +--------------------------+
```

### Layer responsibilities

| Layer | Files | Owns | Never touches |
|---|---|---|---|
| Contract | `prompts/ocr_system.md`, `prompts/notation_spec.md` | Transcription rules, Unicode combining map, IPA keep-list | Code, keys |
| Config | `src/config.py` + `.env.example` | `GlobalConfig` vs `RunConfig`, CLI>env>DEFAULT, dotenv first-import, key masking | Transport, rendering |
| Transport | `src/llm_client.py` | 3 ingresses, passthrough `**kwargs`, `auto_vision()` 1+1, retries, endpoint normalize | OCR prompts, PDF layout |
| Page/Batch | `src/ocr_page.py`, `src/batch_plan.py`, `src/book_id.py` | Single shot, thread pool + adaptive concurrency, `usage.jsonl` resume, per-book folder + whole-book merge, `book.json` identity | Gateway internals |
| Render/Search | `src/render.py`, `src/make_searchable.py` | 200dpi raster, raster-bg + hidden text, box/exact vs fallback | LLM params |
| Quality | `src/check_notation.py`, `src/postprocess.py`, `src/verify_searchable.py` | Notation gate, CJK cleanup + merge, keyword/copyable verify | Secrets |
| UI | `web/` (Tauri 2 + React 19) + `serve.py` bridge | 5 views, typed `lib/engine.ts`, tray/single-instance/native dialogs | Engine logic |
| CLI | `src/cli.py`, `src/cli_common.py`, `src/interactive.py` | One flag source (`add_llm_args`), subcommands, TUI shim | UI widgets |

### Data flow (one page)

```
PDF page --render.py(200dpi)--> PNG bytes
  --> llm_client.*_vision(PNG, ocr_system.md, detail=high, **extra)
        chat:      messages[].content[{text},{image_url:{url:data|https,detail}}]
        responses: input[{role:user,content:[{input_text},{input_image}]}] (must wrap message)
        messages:  content[{text},{image:{source:{base64|url}}}]
  --> page Markdown
  --> check_notation.py gate (0 issues)
  --> <output dir>/<pdf stem>/pages/page_NNNN.md
      (+ usage.jsonl row: endpoint/extra/tokens)
  --> markdown_cleaner.clean_single_page (single-page exit / before batch write)
  --> markdown_cleaner.clean_merged_book + merge_pages (end of batch)
      --> <output dir>/<pdf stem>/<pdf stem>-ocr.md
  --> make_searchable(raster + md [+boxes]) --> book_searchable.pdf
  --> verify_searchable(keywords) --> hits / copyable chars
```

### Concurrency & resume

- `batch_plan.py`: `ThreadPoolExecutor` 1–20 (CLI default 4, lib 1; >8 warns,
  >20 rejects), semaphore adaptive halving on 429/5xx with recovery,
  cross-process file lock, `usage.jsonl` append-only — reruns skip
  `status=success` pages automatically; `skipped` is logged too. Both the log
  and the page files live in the per-book folder `<output dir>/<pdf stem>/`
  (§5 batch step), so resuming a range keeps writing into the same book.
- Retries: per-leg, 4xx fail fast (never retried), 429/5xx/timeout/`OSError`
  retried; `auto` mode = responses single-try → chat single-try on
  429/5xx/timeout/`OSError` only (1+1, no 9x explosion).

## 2. Quickstart

```bash
pip install -r requirements.txt        # python-dotenv + PyMuPDF
cp .env.example .env                   # fill in LLM_OCR_KEY (never commit .env)
python serve.py                        # engine bridge (:21139)
cd web && pnpm dev                     # frontend (:1420)
# or integrated: pnpm tauri dev (Rust spawns the serve sidecar)
python -m src.cli models               # list gateway models
python -m src.cli probe --image tests/cand_165.png
```

Single page / batch:

```bash
python src/ocr_page.py --image tests/cand_165.png --output out/cand_165.md
python src/ocr_page.py --image tests/cand_165.png --output out/a.md \
  --endpoint chat --detail high \
  --extra-json '{"temperature":0.2,"reasoning_effort":"low"}'
python src/batch_plan.py --dry-run --endpoint responses
python src/batch_plan.py --pdf book.pdf --output-dir out --endpoint chat
# -> out/book/pages/page_0000.md ... and out/book/book-ocr.md (whole book)
```

SDK-style (bypass CLI, same engine):

```python
from llm_client import generic_request
payload, usage = generic_request(
    {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
    endpoint="chat",   # chat | responses | messages (or a full /v1/... path)
)
```

### Portable build

Releases ship as a **portable folder** (`llm-ocr-<version>-portable/`) — there is
no installer. Unzip and run `llm-ocr.exe`; the engine sidecar and its Python
runtime sit next to it, so **no Python is needed on the target machine** and
nothing is written outside the folder. Then open the **Connection** view and
fill in the gateway URL, model and API key.

Rebuild it with `pwsh -File build-portable.ps1` (see §8).

## 3. Configuration (src/config.py)

- `GlobalConfig`: `base_url / model / key / timeout` (where to talk).
- `RunConfig`: `endpoint / detail / concurrency / dpi / retries / system /
  extra` (how to talk).
- Priority: `CLI flag > env > DEFAULT`; `dpi/retries` are CLI-only (no env
  fallback); `LLM_OCR_PORT` is deprecated (warns and is ignored — `base_url`
  is the only endpoint knob).
- CLI defaults to `endpoint=responses` / concurrency 4; the library defaults
  to `chat` / concurrency 1 (`ns.is_cli` distinguishes them).
- Key hygiene: read only from `api_key / --key-stdin / LLM_OCR_KEY /
  LLM_OCR_API_KEY / OCTOPUS_API_KEY / OCTOPUS_KEY`; `check()` prints a masked
  length only.

`.env.example` doubles as the contract: `LLM_OCR_BASE_URL` must be the `/v1`
root (not `/v1/chat/completions`); on Octopus the model is a group name (the
`OC/` prefix is required — a bare name returns 400); other official fields go
through `--extra-json`.

## 4. Transport (src/llm_client.py)

Three API layers, from raw to convenient:

1. `generic_request(endpoint, body, **kw)` — you build any legal JSON; it only
   injects `model/base_url/key` before POSTing. `endpoint` accepts an alias or
   a full `/v1/...` path.
2. Typed entry points — `chat_completions / responses_create /
   anthropic_messages` (all pass `**kwargs` straight into the body).
3. Vision helpers — `chat_vision / responses_vision / anthropic_vision`
   (local PNG is base64'd first) and `chat_vision_url /
   responses_vision_url / anthropic_vision_url` (remote https fetched by the
   gateway, no base64); `detail: high|low|auto` (`high` recommended for IPA).
4. `auto_vision()` — responses single try, falling back to chat single try
   **only** on 429/5xx/timeout/OSError (1+1); 400/401/403/404 raise directly.

 Reliability: `HttpError` separates retryable from fail-fast; POST timeout 120 s
(`--timeout`), `GET /v1/models` fixed at 15 s single try; `Retry-After` capped
at 60 s; `_resolve_endpoint()` prevents `/v1/v1` double-writes and upgrades a
bare `host:port` to `http://` (with https-443 detection).

## 5. OCR pipeline (page / batch / postprocess / dual-layer / verify)

1. **Render** (`render.py`): `render_page(pdf, pno_0based, dpi=200) -> PNG
   bytes`; 200 dpi recommended (an IPA page is ~1184x1788, ~1.1 MB base64;
   300 dpi is ~2.4 MB and easily over the limit). Arguments are validated
   (type / positive / in range).
2. **Single page** (`ocr_page.py`): sources `image` / `pdf-page` /
   `image-url`; live `--endpoint/--detail/--extra-json`; emits page Markdown.
3. **Batch** (`batch_plan.py`): thread pool + adaptive concurrency (halve on
   429/5xx, recover on success), `--dry-run` to plan first, append-only
   `usage.jsonl` (endpoint/extra/tokens; reruns skip successes, skipped pages
   are logged), page ranges, 2 retries per failure. The `--output-dir` you pass
   gets one folder per book (named after the PDF, extension dropped), so
   several books never mix:

   ```
   <output dir>/<pdf stem>/
   ├── book.json              identity: absolute source path + size + page count
   │                          + pages_done + artifacts (never credentials)
   ├── <pdf stem>-ocr.md      whole-book Markdown: every pages/*.md merged
   │                          by postprocess.merge_pages at the end of a run
   ├── pages/page_0000.md ... one file per page (0-based in the file name)
   └── usage.jsonl            resume log (status=success pages are skipped)
   ```

   `book.json` answers "whose page 0 is this?": it binds the folder to one PDF
   (absolute path, size, mtime, page count, pages present on disk, artifacts),
   so a later step can verify it is using the right book instead of guessing.
   Re-running the same book updates it in place (idempotent: `created` and
   unknown fields survive, `updated` and `pages_done` refresh); credential-shaped
   keys and values are stripped on every read *and* write.

   The merge is not a plain concatenation: single-page exits and batch writes first
   run `clean_single_page`; `merge_pages` cleans each existing page again, then
   `clean_merged_book` normalizes the whole-book page blocks. It never invents
   missing pages; empty pages keep only their canonical boundaries.
4. **Notation gate** (`check_notation.py`): the machine form of
   `prompts/notation_spec.md` — rejects ASCII substitutes like `[t_w]`/`[tw]`/
   `[kh]`, LaTeX residue (`\underset`/`\overset`/`$` delimiters), split or
   stray combining marks, unbalanced brackets. Target: 0 issues.
5. **Post-process** (`markdown_cleaner.py` + `postprocess.py`): CJK punctuation, footers,
   code fences, LaTeX/formulas, IPA, and combining marks are protected before cleanup;
   local-image replacement never enters protected regions. `merge_pages(pages, title)`
   merges only pages that exist on disk and emits fixed page blocks:
   ```markdown
   ---
   第 X 页

   page body

   ---
   ```
   It emits no page index and never invents missing-page or empty-page placeholder text;
   legacy HTML page markers are converted to the same format. `batch_plan.run_batch`
   calls it at the end of every batch, with the PDF stem as `title`, writing
   `<output dir>/<pdf stem>/<pdf stem>-ocr.md`; `polish_with_llm` passes through the
   same three protocols.
6. **Dual layer** (`make_searchable.py`): the page raster is the full-page
   background (size unchanged, `maxdiff=0`, no recompression); when boxes are
   reliable (`reliable != false`) it writes them exactly with
   `render_mode=3`, otherwise it falls back to a dual copy (a 0.5 pt
   fully-hidden copy + 8 pt spread per line) so text stays searchable and
   copyable.
   The bridge resolves the book first (`src/book_id.py`): pointing at a book
   folder uses its own `book.json` (and fills in the source PDF, so you need not
   pick it again); pointing at a parent directory requires exactly one book to
   match the chosen PDF (same path, or same size + page count when the file was
   moved); no match or several matches is a hard refusal that lists the
   candidates — never a silent pick. A dir without `book.json` still works as
   before and gets its identity file written on the first successful build. The
   dual-layer PDF lands inside the book folder, then its name is recorded in
   `artifacts.searchable_pdf`.
7. **Verify** (`verify_searchable.py`): `verify(pdf, keywords)` reports
   per-page keyword hits plus total copyable characters; `compare_md` diffs
   two Markdown files with difflib.

## 6. Desktop app (Tauri 2 + React 19 + serve.py bridge)

- Architecture: Tauri shell (tray / single instance / window memory / native
  dialogs) + React frontend (5 views: Connection, Single, Batch, Dual-layer,
  Params — plus a log console and a status bar) + `serve.py` engine bridge
  (stdlib `http.server`, `127.0.0.1:21139`, job-based long tasks).
- The key lives in frontend memory only and reaches the bridge through the
  request body/header; logs print `sk-**** len=N`.
- Development: `python serve.py` (bridge) + `cd web && pnpm dev` (frontend,
  :1420); integrated: `pnpm tauri dev`.
- Contract: `tests/test_serve_contract.py` (health/prompt/dry-run/notation,
  fully offline). See `spec-new-ui.md` §3 for the API contract table.
- **Production builds must use `pnpm tauri build`.** A bare
  `cargo build --release` omits the `tauri/custom-protocol` feature and yields
  a dev-mode binary (loads `localhost:1420`, assets not embedded). Quick
  check: the string `theme-init.js` is present in a production `web.exe`.

## 7. CLI (src/cli.py + cli_common.py)

`python -m src.cli <models|probe|ocr|batch|config> [--tui]`;
`add_llm_args()` is the single flag source (`--base-url/--model/--api-key/
--key-stdin/--endpoint/--detail/--extra-json/--timeout/--system`, plus
`--concurrency` for batch); `--key-stdin` is tri-state (api_key wins + warns /
TTY errors / pipe reads all); `--api-key` on the command line warns (env or a
pipe is preferred).

## 8. Packaging

Releases are **portable only**; `bundle.targets` is `[]`, so Tauri produces no
NSIS/MSI artifacts. `build-portable.ps1` runs the whole chain and is the only
supported way to cut a release:

```
PyInstaller onedir  ->  refresh externalBin  ->  pnpm tauri build --no-bundle
                    ->  sidecar smoke test   ->  assemble  ->  zip
```

`serve.spec` (PyInstaller **onedir**, `console=False`): `pathex` covers root and
`src/`; `datas` carries `prompts/*.md` plus `tests/cand_165.png` (resolved in
the frozen app through `sys._MEIPASS` via `serve._res_file`); `hiddenimports`
lists every engine module — including `book_id`, which `serve.py` imports lazily
inside a function, so it must be listed explicitly even though the static module
graph usually finds it.

The result is:

```
llm-ocr-<version>-portable/
├── llm-ocr.exe     Tauri shell (frontend assets embedded in the exe)
├── serve.exe       engine sidecar
└── _internal/      Python runtime and support files
```

`.gitignore` excludes `dist-portable/`.

**Why onedir and not onefile.** A onefile sidecar re-extracts itself into
`%TEMP%\_MEI*` (about 90 MB) on every launch. Because the shell terminates the
sidecar with a Job Object, the bootloader never gets to clean up, so every run
leaked one directory — one machine had accumulated 116 of them, 9.96 GB. With
onedir there is no extraction at all and the leak is gone at the root. The shell
still sweeps stale `%TEMP%\_MEI*` directories on startup and exit, but that is
now only housekeeping for leftovers from older versions; it touches only
directories carrying this project's own marker file, so foreign PyInstaller
directories are never candidates.

**Never ship a bare `cargo build --release`.** It omits the
`tauri/custom-protocol` feature and yields a dev-mode binary that loads
`localhost:1420` with the frontend not embedded. `pnpm tauri build` (with or
without `--no-bundle`) enables it. Quick check: a production `llm-ocr.exe`
contains the string `theme-init.js`.

The Job Object still matters with onedir: the sidecar is now a single process,
but `kill_sidecar()` only runs on `RunEvent::ExitRequested`, so a force-killed
shell would otherwise leave the engine holding port 21139. `KILL_ON_JOB_CLOSE`
covers that case.

## 9. Security & limits

- The key never enters the repository (`.gitignore`: `.env / out/ /
  __pycache__ / dist/ / build*`); do not put secrets in `--extra-json`.
- `out/` holds local run artifacts (`out/<pdf stem>/` per book: pages/,
  usage.jsonl, `<pdf stem>-ocr.md`, dual-layer PDF) and is not published with
  the repo.
- `tests/` sample images are small debug fixtures; bring your own book PDF
  (`OCR_PDF_PATH` or `--pdf`).
- `src/conn_test.py` is a placeholder with no business logic.

## 10. Changelog

### 0.7.0 — dual-layer PDF evolution & full-pipeline experience upgrade

- **Automatic bookmark outline (TOC Tree) generation for dual-layer PDFs.** Extracts section headers (`#` to `######`) from OCR markdown outputs and injects a complete hierarchical outline tree into the searchable PDF.
- **Logical page numbers & Page Labels mapping.** Intelligently detects physical folio numbers on pages and compiles page label rules (`set_page_labels`), ensuring the PDF viewer navigation folio matches the actual printed book pages.
- **Equation & atomic block topology protection.** Blocks math and formula blocks (`$$...$$`, `is_equation`) from being wrapped or split across characters, preserving atomic bounding and preventing distorted text alignments.
- **Robust multi-hundred-page processing.** Hardened geometry gates, punctuation normalization, and batch error tolerance for large books, with rich progress and telemetry feedback across the desktop UI.

### 0.6.3 — per-book output folders, identity binding, portable releases

- **Each book gets its own folder.** Batch output now lands in
  `<outdir>/<pdf stem>/`, holding `pages/`, `usage.jsonl`, the merged
  `<pdf stem>-ocr.md` and the dual-layer PDF. Two books can no longer be
  confused — which was the real failure mode: a `page_0000.md` on its own does
  not say which book it came from.
- **`book.json` binds OCR results to their source PDF.** Written by the batch
  job (absolute source path, page count, size, mtime, pages done, artifacts),
  and it deliberately records **no** base URL and no credential of any kind. The
  dual-layer step reads it: point at a book folder and the source PDF is filled
  in for you; point at a parent directory and your chosen PDF is matched against
  the books underneath. Ambiguous or mismatched input **fails loudly with the
  candidate list** rather than silently using the wrong book. Directories
  written before this version keep working and gain a `book.json` the first time
  a dual-layer build succeeds in them.
- **The whole-book Markdown is actually produced.** `postprocess.merge_pages`
  existed but was never called anywhere; batch now merges every page present on
  disk into `<pdf stem>-ocr.md`, so resuming across several runs still
  accumulates one complete book. Both READMEs now describe what the code does
  instead of a flow that was never wired up.
- **Portable releases, no installers.** A portable folder plus zip replaces the
  NSIS/MSI artifacts — see §8.
- **The sidecar moved to onedir**, which removes the per-run `%TEMP%`
  extraction entirely — see §8.
- The batch view reports the merged Markdown path when a run finishes. The
  dual-layer view's verification-keyword field now defaults to empty and
  explains what the keywords actually do.

### 0.6.2 — engine connectivity, lifecycle and installer fixes

Also in this build: batch output now lands in a per-book folder
(`<output dir>/<pdf stem>/` with `pages/`, `usage.jsonl`, an identity file
`book.json` and the merged `<pdf stem>-ocr.md`) — `merge_pages` used to be
documented but never called, so a finished batch left only loose page files, and
two books OCRed side by side were indistinguishable.

Everything below was found by running the **packaged** build; none of it
reproduces under `pnpm tauri dev`, because the Vite proxy masks the address
resolution and the sidecar is started by hand.

- **Engine URL was unreachable in production builds.** `ENGINE_BASE` decided
  "browser vs Tauri" from `location.protocol.startsWith("http")`, but Tauri 2
  on Windows serves the app from `http://tauri.localhost` — protocol `http:`
  too. Every `/api/*` call therefore went to the app's own asset server and
  came back as `index.html` (`Unexpected token '<'`). Now detected via
  `__TAURI_INTERNALS__`. The same wrong test in `pick.ts` disabled native file
  dialogs, so selected paths arrived as bare filenames.
- **A zombie sidecar made the second launch unusable.** `kill_sidecar()` only
  terminated the PyInstaller bootloader; the real child survived, kept the
  port and the lock, and — with its stdout pipe closed — answered every
  request with 0 bytes. The app had no way to recover. The shell now puts the
  sidecar in a Job Object with `KILL_ON_JOB_CLOSE`, and the sidecar's logging
  is guarded so a detached process degrades instead of going silent.
- **Lock handling.** A lock left behind by a hard kill used to block the next
  start forever; it now records the holder's PID and start time and is
  reclaimed only when the holder is provably gone. Reclamation is atomic
  (unique temp name + `os.replace` with the handle held), closing a race where
  a loser could delete the winner's lock.
- **Port conflicts are loud.** The bridge asserted `SO_EXCLUSIVEADDRUSE` on
  Windows; previously a second process could silently bind the same port and
  steal connections. A conflicting bind now exits with code 3 and a clear
  message.
- **Failures are visible.** Sidecar output is retained and, when the engine
  cannot start, the concrete reason (exit code plus the tail of its stderr)
  appears in the in-app run log and in
  `%LOCALAPPDATA%\cn.lxm.llmocr\logs\sidecar.log`, instead of only a 15-second
  timeout.
- **Diagnostics.** A non-JSON response now reports HTTP status, URL,
  content-type and the body prefix rather than a bare parse error. The CSP
  gained `connect-src` for the bridge and Tauri IPC.
- **Installer is Simplified Chinese** (`nsis.languages = ["SimpChinese"]`,
  MSI `zh-CN`).
- **`%TEMP%` no longer grows without bound.** Every run used to leak the
  sidecar's ~93 MB extraction directory; startup and exit now sweep
  directories that carry this project's marker (aged 90 s or more).
