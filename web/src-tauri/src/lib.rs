// llm-ocr Tauri v2 shell: tray + native menu + single-instance + window-state.
// Engine calls go through the serve sidecar (serve.exe, PyInstaller onefile)
// over HTTP 127.0.0.1:21139; Rust exposes no custom commands (all business
// logic stays in Python src/). Dev mode: spawn `python ../../serve.py`.
//
// Sidecar observability: the release binary is built with
// `windows_subsystem = "windows"`, so eprintln! alone reaches nobody.  A failed
// sidecar therefore used to be invisible — the only symptom was the frontend's
// generic 15-second timeout.  This module now keeps the sidecar's output stream,
// remembers its exit code, and pushes a concrete reason to the in-app log console
// (Tauri event "shell-error") plus a log file under the app log directory.
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime};

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, RunEvent, WindowEvent,
};
use tauri_plugin_shell::process::CommandEvent;

const SIDECAR_PORT: u16 = 21139;
/// How many sidecar stdout/stderr lines to keep for a crash report.
const LOG_TAIL_LINES: usize = 50;
/// Give the sidecar this long to answer /api/health before complaining.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(20);
/// A Tauri event with no listener attached is dropped, and the sidecar is spawned
/// from `setup()` — possibly before the webview finished loading.  Replay the
/// report a few times; the frontend ignores repeated ids.
const REPORT_REEMITS: u32 = 5;
const REPORT_REEMIT_GAP: Duration = Duration::from_secs(3);

// ---------------------------------------------------------------------------
// Sidecar process tree (P1a)
//
// serve.exe is a PyInstaller **onefile** build: the process Tauri spawns is a
// bootloader that unpacks itself and then runs the real engine as a *child*.
// That grandchild is the one listening on 127.0.0.1:21139 and holding
// %TEMP%\llm-ocr-serve-21139.lock, so ending only the bootloader — everything
// `CommandChild::kill()` can reach — leaves a zombie engine behind: the port
// stays bound, the lock stays held, and the next launch loses the lock race and
// exits (code 2).  The user then never gets an engine back, and the UI has no
// way to recover on its own.
//
// A Windows job object created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE fixes
// that at the root: the sidecar is adopted into the job right after spawn, every
// descendant it creates inherits the job, and the kernel kills the whole tree
// when the last handle to the job closes — which happens when *this* process
// dies, however it dies, including a hard `taskkill /F` of web.exe where no Rust
// code of ours runs at all.  `kill_sidecar` additionally terminates the job
// explicitly so a normal tray quit does not depend on process teardown order.
//
// The six Win32 calls needed are declared here instead of pulling in the
// `windows` crate: no crate in this graph enables its JobObjects features today,
// so turning them on would invalidate the fingerprint of `windows` and rebuild
// it plus tao / wry / tauri and every plugin above them.  This keeps the change
// inside this crate; the winnt.h struct layouts are asserted at compile time.
#[cfg(windows)]
mod proctree {
    use std::ffi::c_void;

    /// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (winnt.h).
    const KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
    /// JOBOBJECTINFOCLASS::JobObjectExtendedLimitInformation (winnt.h).
    const EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9;
    /// PROCESS_SET_QUOTA | PROCESS_TERMINATE: what AssignProcessToJobObject needs.
    const ASSIGN_ACCESS: u32 = 0x0100 | 0x0001;

    #[repr(C)]
    #[derive(Default)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    #[derive(Default)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        // Six counters, not five: leaving this one out makes the struct 8 bytes
        // short and SetInformationJobObject fails with ERROR_BAD_LENGTH (24).
        other_transfer_count: u64,
    }

    #[repr(C)]
    #[derive(Default)]
    struct ExtendedLimitInformation {
        basic_limit_information: BasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    // Guard the hand-written layouts against winnt.h drifts: a wrong size makes
    // SetInformationJobObject read past the struct and silently do nothing.
    // (64-bit only — the shell ships x86_64 only.)
    #[cfg(target_pointer_width = "64")]
    const _: () = {
        assert!(std::mem::size_of::<BasicLimitInformation>() == 64);
        assert!(std::mem::size_of::<IoCounters>() == 48);
        assert!(std::mem::size_of::<ExtendedLimitInformation>() == 144);
    };

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *mut c_void, name: *const u16) -> *mut c_void;
        fn SetInformationJobObject(
            job: *mut c_void,
            class: i32,
            info: *mut c_void,
            size: u32,
        ) -> i32;
        fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
        fn TerminateJobObject(job: *mut c_void, exit_code: u32) -> i32;
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn CloseHandle(handle: *mut c_void) -> i32;
        fn GetLastError() -> u32;
    }

    /// A job object owning a process tree.  Dropping it closes the last handle,
    /// which is what trips KILL_ON_JOB_CLOSE — so the tree dies with us.
    pub struct ProcessTree {
        job: *mut c_void,
    }

    // SAFETY: a job handle is a kernel handle with no thread affinity, and it is
    // only ever used through &self by the (thread-safe) calls above.
    unsafe impl Send for ProcessTree {}
    unsafe impl Sync for ProcessTree {}

    impl ProcessTree {
        /// Create the kill-on-close job and adopt `pid` into it.
        ///
        /// `Err` carries the reason: when this fails the caller must fall back to
        /// killing by parent pid, because a job-less sidecar can still orphan its
        /// own engine.
        pub fn adopt(pid: u32) -> Result<ProcessTree, String> {
            let job = unsafe { CreateJobObjectW(std::ptr::null_mut(), std::ptr::null()) };
            if job.is_null() {
                return Err(format!(
                    "CreateJobObject failed (win32 error {})",
                    unsafe { GetLastError() }
                ));
            }
            let tree = ProcessTree { job };

            let mut limits = ExtendedLimitInformation::default();
            limits.basic_limit_information.limit_flags = KILL_ON_JOB_CLOSE;
            let sized = std::mem::size_of::<ExtendedLimitInformation>() as u32;
            let ok = unsafe {
                SetInformationJobObject(
                    tree.job,
                    EXTENDED_LIMIT_INFORMATION_CLASS,
                    &mut limits as *mut ExtendedLimitInformation as *mut c_void,
                    sized,
                )
            };
            if ok == 0 {
                return Err(format!(
                    "SetInformationJobObject failed (win32 error {})",
                    unsafe { GetLastError() }
                ));
            }

            let process = unsafe { OpenProcess(ASSIGN_ACCESS, 0, pid) };
            if process.is_null() {
                return Err(format!(
                    "OpenProcess(pid {pid}) failed (win32 error {})",
                    unsafe { GetLastError() }
                ));
            }
            let assigned = unsafe { AssignProcessToJobObject(tree.job, process) };
            let assign_error = if assigned == 0 {
                unsafe { GetLastError() }
            } else {
                0
            };
            unsafe { CloseHandle(process) };
            if assigned == 0 {
                return Err(format!(
                    "AssignProcessToJobObject(pid {pid}) failed (win32 error {assign_error})"
                ));
            }
            Ok(tree)
        }

        /// Kill every process in the job right now.  Closing the handle would do
        /// the same thing; this just makes the timing explicit on the exit path.
        pub fn terminate(&self) {
            unsafe { TerminateJobObject(self.job, 1) };
        }
    }

    impl Drop for ProcessTree {
        fn drop(&mut self) {
            unsafe { CloseHandle(self.job) };
        }
    }
}

#[cfg(not(windows))]
mod proctree {
    /// Job objects are Windows-only; elsewhere killing stays single-process.
    pub struct ProcessTree;

    impl ProcessTree {
        pub fn adopt(_pid: u32) -> Result<ProcessTree, String> {
            Err("job objects are only available on Windows".to_string())
        }

        pub fn terminate(&self) {}
    }
}

/// Last-resort tree kill for a sidecar that could not be adopted into a job
/// object (or when the job could not be created at all): `taskkill /T` walks the
/// child list itself.  It cannot cover the case where this process is killed
/// outright — nothing runs then — which is exactly why the job object, not this,
/// is the primary mechanism.
fn taskkill_tree(pid: u32) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let _ = std::process::Command::new("taskkill")
            .args(["/F", "/T", "/PID", &pid.to_string()])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
    #[cfg(not(windows))]
    let _ = pid;
}

/// The running sidecar: the process Tauri's shell plugin tracks, plus the job
/// object that owns its whole tree (bootloader *and* the engine it unpacks).
#[derive(Default)]
struct SidecarInner {
    child: Option<tauri_plugin_shell::process::CommandChild>,
    tree: Option<proctree::ProcessTree>,
}

struct SidecarState(pub Mutex<SidecarInner>);

#[derive(Clone)]
struct FailureReport {
    id: u64,
    headline: String,
    body: String,
}

/// Diagnostics for the sidecar: recent output, exit code, and the last report so
/// it can be replayed to a frontend that attached its listener late.
#[derive(Default)]
struct SidecarDiagInner {
    lines: VecDeque<String>,
    exit_code: Option<Option<i32>>,
    spawn_error: Option<String>,
    /// The failure already reported for the *current* sidecar process.  Gating
    /// the side effects on this is what makes "one process, one report" hold on
    /// every call path (spawn failure and watchdog alike); `begin_run` clears it.
    reported: Option<FailureReport>,
}

struct SidecarDiag(Mutex<SidecarDiagInner>);

impl SidecarDiagInner {
    /// A sidecar process was just spawned: forget what was recorded for the
    /// previous one, so its output and exit code cannot be attributed to the new
    /// process and its failure report cannot swallow the new one's (P4).
    fn begin_run(&mut self) {
        self.lines.clear();
        self.exit_code = None;
        self.spawn_error = None;
        self.reported = None;
    }
}

fn push_log_line(app: &tauri::AppHandle, line: &str) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }
    let line: String = line.chars().take(400).collect();
    if let Some(diag) = app.try_state::<SidecarDiag>() {
        let mut d = diag.0.lock().unwrap();
        while d.lines.len() >= LOG_TAIL_LINES {
            d.lines.pop_front();
        }
        d.lines.push_back(line);
    }
}

fn set_exit_code(app: &tauri::AppHandle, code: Option<i32>) {
    if let Some(diag) = app.try_state::<SidecarDiag>() {
        diag.0.lock().unwrap().exit_code = Some(code);
    }
}

fn set_spawn_error(app: &tauri::AppHandle, reason: &str) {
    if let Some(diag) = app.try_state::<SidecarDiag>() {
        diag.0.lock().unwrap().spawn_error = Some(reason.to_string());
    }
}

fn exit_code(app: &tauri::AppHandle) -> Option<Option<i32>> {
    app.try_state::<SidecarDiag>().and_then(|d| d.0.lock().unwrap().exit_code)
}

fn spawn_error(app: &tauri::AppHandle) -> Option<String> {
    app.try_state::<SidecarDiag>()
        .and_then(|d| d.0.lock().unwrap().spawn_error.clone())
}

fn log_file_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    let dir = app.path().app_log_dir().ok()?;
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir.join("sidecar.log"))
}

/// Dependency-free probe of serve.py's /api/health (no HTTP client crate).
fn health_ok(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(700)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1500)));
    let request =
        format!("GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut raw = Vec::new();
    let _ = (&mut stream).take(8192).read_to_end(&mut raw);
    let text = String::from_utf8_lossy(&raw);
    // A foreign program squatting on the port must not pass for our engine.
    (text.starts_with("HTTP/1.1 200") || text.starts_with("HTTP/1.0 200"))
        && text.contains("\"ok\"")
        && text.contains("true")
}

fn emit_report(app: &tauri::AppHandle, report: &FailureReport) {
    if let Some(win) = app.get_webview_window("main") {
        let payload = serde_json::json!({
            "id": report.id,
            "message": report.headline,
            "detail": report.body,
        });
        let _ = win.emit("shell-error", payload);
    }
}

/// Make a sidecar failure diagnosable: write a log file, then surface the same
/// text in the in-app log console.
///
/// Reported at most once per sidecar process (P4).  The old guard only
/// deduplicated the *text*: the log write and the event emission below ran on
/// every call, so any second caller reaching this function appended a duplicate
/// block to sidecar.log and pushed another round of `shell-error` events (the
/// spawn-failure branch and the watchdog both report through here).  Returning
/// before any I/O makes the guard cover every call path; `begin_run` clears the
/// flag for a restarted sidecar, so a new process can still report its failure.
fn report_failure(app: &tauri::AppHandle, headline: &str, reason: &str) {
    let report = {
        let diag = app.state::<SidecarDiag>();
        let mut d = diag.0.lock().unwrap();
        if d.reported.is_some() {
            return;
        }
        let exit = match d.exit_code {
            Some(Some(code)) => format!("退出码 {code}"),
            Some(None) => "被信号终止".to_string(),
            None => "进程未退出".to_string(),
        };
        let tail = if d.lines.is_empty() {
            "(sidecar 没有输出)".to_string()
        } else {
            d.lines.iter().cloned().collect::<Vec<_>>().join("\n")
        };
        let path = log_file_path(app)
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "(日志目录不可用)".to_string());
        let body = format!(
            "原因: {reason}\n子进程: {exit}\nsidecar 输出末尾 {} 行:\n{tail}\n日志文件: {path}",
            d.lines.len()
        );
        let id = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|t| t.as_millis() as u64)
            .unwrap_or(0);
        let fresh = FailureReport {
            id,
            headline: headline.to_string(),
            body,
        };
        d.reported = Some(fresh.clone());
        fresh
    };

    if let Some(path) = log_file_path(app) {
        let block = format!(
            "\n===== llm-ocr 引擎启动失败 =====\n{}\n{}\n",
            report.headline, report.body
        );
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
        {
            let _ = f.write_all(block.as_bytes());
        }
    }

    // Visible in a dev run's console; in a release build this goes nowhere, which
    // is exactly why the event and the log file exist.
    eprintln!("[shell] {}: {}", report.headline, report.body);

    emit_report(app, &report);
    let replay_app = app.clone();
    std::thread::spawn(move || {
        for _ in 0..REPORT_REEMITS {
            std::thread::sleep(REPORT_REEMIT_GAP);
            emit_report(&replay_app, &report);
        }
    });
}

/// If the engine never answers, say why.  Without this the only thing the user
/// ever sees is the frontend's generic "bridge not ready after 15s" line.
fn watchdog_missing_engine(app: tauri::AppHandle) {
    let started = Instant::now();
    loop {
        std::thread::sleep(Duration::from_millis(500));
        if health_ok(SIDECAR_PORT) {
            return; // engine is answering; nothing to report
        }
        if let Some(reason) = spawn_error(&app) {
            report_failure(&app, "引擎进程无法启动", &reason);
            return;
        }
        if let Some(code) = exit_code(&app) {
            // Give a freshly spawned process a moment to bind before declaring it gone.
            if started.elapsed() >= Duration::from_secs(3) {
                let shown = code.map(|c| c.to_string()).unwrap_or_else(|| "信号".into());
                report_failure(
                    &app,
                    &format!("引擎进程已退出（{shown}），127.0.0.1:{SIDECAR_PORT} 无响应"),
                    "serve.exe 启动后即退出：端口被占用、资源缺失或已有实例持锁。",
                );
                return;
            }
        }
        if started.elapsed() >= HEALTH_TIMEOUT {
            report_failure(
                &app,
                &format!(
                    "启动 {} 秒后 127.0.0.1:{SIDECAR_PORT}/api/health 仍不可达",
                    HEALTH_TIMEOUT.as_secs()
                ),
                "子进程没有监听端口，或该端口被别的程序接管。",
            );
            return;
        }
    }
}

fn spawn_sidecar(app: &tauri::AppHandle) {
    use tauri_plugin_shell::ShellExt;
    // A fresh sidecar process starts a fresh reporting epoch (P4).
    if let Some(diag) = app.try_state::<SidecarDiag>() {
        diag.0.lock().unwrap().begin_run();
    }
    let scope = app.shell().sidecar("serve").unwrap();
    // Release build: bundled sidecar. Dev build (`tauri dev`): sidecar file
    // is absent, fall back to the repo python script (dev-only).
    let first = scope.args(["--port", "21139"]).spawn();
    let attempt = match first {
        Ok(pair) => Ok(pair),
        Err(e) => {
            #[cfg(debug_assertions)]
            {
                let fallback = app
                    .shell()
                    .command("python")
                    .args(["../../serve.py", "--port", "21139"])
                    .spawn();
                match fallback {
                    Ok(pair) => Ok(pair),
                    Err(e2) => Err(format!(
                        "sidecar spawn failed: {e}; dev fallback (python ../../serve.py) failed: {e2}"
                    )),
                }
            }
            #[cfg(not(debug_assertions))]
            {
                Err(format!("sidecar spawn failed: {e}"))
            }
        }
    };

    match attempt {
        Ok((events, child)) => {
            // Adopt the bootloader into the kill-on-close job *before* it can
            // start the real engine — everything it spawns later inherits the
            // job.  It unpacks a ~45 MB onefile archive first, so this lands
            // microseconds into a multi-second startup.
            let pid = child.pid();
            let tree = match proctree::ProcessTree::adopt(pid) {
                Ok(tree) => Some(tree),
                Err(reason) => {
                    push_log_line(
                        app,
                        &format!(
                            "[shell] cannot adopt sidecar pid {pid} into a job object ({reason}); \
                             falling back to taskkill /T on exit"
                        ),
                    );
                    None
                }
            };
            if let Some(state) = app.try_state::<SidecarState>() {
                let mut running = state.0.lock().unwrap();
                running.child = Some(child);
                running.tree = tree;
            }
            // Keep the event stream: the old code wrote `Ok((_, child))`, which
            // dropped every line the sidecar prints, its own log() included.
            let pump_app = app.clone();
            tauri::async_runtime::spawn(async move {
                let mut events = events;
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                            push_log_line(&pump_app, &String::from_utf8_lossy(&bytes));
                        }
                        CommandEvent::Error(err) => {
                            push_log_line(&pump_app, &format!("[shell] {err}"));
                        }
                        CommandEvent::Terminated(payload) => {
                            set_exit_code(&pump_app, payload.code);
                        }
                        _ => {}
                    }
                }
            });
            let watchdog_app = app.clone();
            std::thread::spawn(move || watchdog_missing_engine(watchdog_app));
        }
        Err(reason) => {
            push_log_line(app, &format!("[shell] {reason}"));
            set_spawn_error(app, &reason);
            report_failure(app, "引擎进程无法启动", &reason);
        }
    }
}

fn kill_sidecar(app: &tauri::AppHandle) {
    let mut child = None;
    let mut tree = None;
    if let Some(state) = app.try_state::<SidecarState>() {
        let mut running = state.0.lock().unwrap();
        child = running.child.take();
        tree = running.tree.take();
    }

    // Terminating the job reaches the bootloader *and* the engine it unpacked
    // (P1a); `child.kill()` alone only reaches the bootloader, which is what
    // used to strand a zombie engine on port 21139.  When there is no job (it
    // could not be created or the sidecar could not be adopted) fall back to
    // killing by parent pid, which is the best a job-less shell can do.
    match (&tree, &child) {
        (Some(tree), _) => tree.terminate(),
        (None, Some(child)) => taskkill_tree(child.pid()),
        (None, None) => {}
    }
    if let Some(child) = child {
        let _ = child.kill();
    }
    // `tree` is dropped here: closing the last handle is itself fatal to the job.
}

// ---------------------------------------------------------------------------
// %TEMP%\_MEI* sweep (P1b)
//
// serve.exe is a PyInstaller *onefile* build, so its bootloader unpacks itself
// into `%TEMP%\_MEI<random>\` (~90 MB) and deletes that directory again on its
// way out.  `kill_sidecar` ends the process with TerminateJobObject (or
// `taskkill /T`), which gives the bootloader no chance to run that cleanup, so
// every quit used to strand one directory: an affected machine had collected
// 116 of them, 9.96 GB.
//
// The shell is the only party that can fix this.  It is the one that kills the
// tree, it is running both *before* the next unpack (when any `_MEI*` is debris
// by construction) and *after* the previous kill, and it owns the app log a
// failure report can go to.  serve.py cannot do it: by the time its own code
// runs its `_MEI` directory already exists and cannot be removed while the
// engine lives, and it is killed with TerminateProcess, so exit-time code of
// its own never runs either.
//
// Safety rules, most important first:
//   1. Only ever delete a directory that is *ours*.  `_MEI` is PyInstaller's
//      prefix, not this application's, and a machine can be running somebody
//      else's PyInstaller program right now.  A candidate must contain
//      MEI_MARKER, a data file this project's own serve.spec bundles, before it
//      is considered at all -- so another program's unpack directory is not
//      merely protected, it is never a candidate.  This matters because "just
//      try to delete it and ignore errors" is *not* sufficient protection on
//      Windows: a live onefile directory is only partly locked and
//      remove_dir_all stops at the first locked entry, having already deleted
//      whatever came before it (measured on a live engine: 44 files, 3.37 MB
//      gone before the first failure).
//   2. Never delete a directory that is in use.  Even for our own debris, every
//      error is ignored and nothing is forced through: the startup sweep scans
//      *before* our sidecar can unpack anything, the exit sweep runs *after* the
//      process tree is dead, and a directory whose files are still held simply
//      survives to be retried on the next launch.
//   3. Never race a program that is starting up.  Directories whose *creation
//      time* is younger than MEI_MIN_AGE are skipped: that is the one window in
//      which a PyInstaller program's files are not open yet.
//   4. Startup must not get slower.  Only the scan (one `read_dir`, plus one
//      `stat` per `_MEI*` entry) sits on the startup path; the deletions run on
//      a detached thread.
//   5. Quitting must not hang.  The exit sweep runs under a hard time budget, so
//      a backlog of a hundred directories cannot stall the exit path.

/// Minimum age for a `_MEI*` directory to count as debris.
///
/// 90 s is chosen against two measured ends.  It has to be far longer than the
/// window in which a *concurrent* PyInstaller program is vulnerable (its
/// `_MEI*` directory exists but nothing inside it is open yet) -- that window is
/// the extraction itself, seconds: the sidecar here goes from spawn to
/// `/api/health` in ~2.6 s, so 90 s is a ~35x margin, and the ownership rule (1)
/// sits in front of it anyway.  It has to be short enough that the normal
/// quit-then-relaunch cycle reclaims the previous run's directory; at 90 s a
/// user who starts the app again a minute and a half later gets it back, where
/// the task's upper bound of 120 s would have made them wait another launch.
const MEI_MIN_AGE: Duration = Duration::from_secs(90);
/// What the exit sweep may spend deleting before it leaves the rest to the next
/// startup sweep (which runs off the critical path).
const MEI_EXIT_BUDGET: Duration = Duration::from_millis(1500);
/// Attempts per directory on the exit path: TerminateJobObject only *starts*
/// process teardown, so the killed engine can keep its files locked for a few
/// more milliseconds.
const MEI_EXIT_ATTEMPTS: u32 = 3;
const MEI_RETRY_GAP: Duration = Duration::from_millis(150);
/// Proof of ownership, relative to the unpack directory's root: a data file that
/// `serve.spec` bundles out of `prompts/` and no other program can have.  Only a
/// directory containing it is ever considered for deletion, which is what keeps
/// a foreign PyInstaller program out of reach no matter how old its directory is
/// (see rule 1 above).  If serve.spec ever stops bundling this file the sweep
/// stops deleting anything -- the safe direction -- and says so in the log as
/// `skip_foreign`.
const MEI_MARKER_DIR: &str = "prompts";
const MEI_MARKER_FILE: &str = "ocr_system.md";

/// A `_MEI*` directory old enough to be a leftover.
struct MeiDebris {
    path: PathBuf,
    created: SystemTime,
}

/// What one scan saw; also what the sweep log line is built from.
#[derive(Default)]
struct MeiScan {
    /// `_MEI*` directories that are directories (not symlinks).
    seen: usize,
    /// Skipped by the MEI_MIN_AGE guard.
    recent: usize,
    /// Skipped because the filesystem gave no creation time (never guessed).
    undated: usize,
    /// Skipped because they are not ours (MEI_MARKER missing).
    foreign: usize,
    debris: Vec<MeiDebris>,
}

/// `_MEI` followed by a non-empty run of alphanumerics is exactly what the
/// PyInstaller bootloader generates (`_MEI0000122c2` was the shape observed on
/// this machine).  Requiring that shape keeps unrelated directories which merely
/// start with `_MEI` out of reach.
fn is_mei_dir_name(name: &str) -> bool {
    match name.strip_prefix("_MEI") {
        Some(rest) => !rest.is_empty() && rest.chars().all(|c| c.is_ascii_alphanumeric()),
        None => false,
    }
}

/// Is this unpack directory one of ours?  One `stat` on a file only this
/// project's bundle contains (rule 1); anything else -- another PyInstaller
/// program's directory, or junk that merely borrowed the prefix -- answers no
/// and is left alone.
fn looks_like_ours(dir: &std::path::Path) -> bool {
    dir.join(MEI_MARKER_DIR).join(MEI_MARKER_FILE).is_file()
}

/// The cheap, synchronous half: list `%TEMP%` and decide what is debris.  One
/// `read_dir`, one `stat` per `_MEI*` entry for the ownership marker, one
/// metadata call for the age -- no recursion into the candidates, no deletion,
/// nothing that scales with their contents.
fn scan_mei_dirs() -> MeiScan {
    let mut scan = MeiScan::default();
    let entries = match std::fs::read_dir(std::env::temp_dir()) {
        Ok(entries) => entries,
        // Unreadable %TEMP% (or none): nothing to clean, nothing to report.
        Err(_) => return scan,
    };
    let now = SystemTime::now();
    for entry in entries.flatten() {
        if !is_mei_dir_name(&entry.file_name().to_string_lossy()) {
            continue;
        }
        // A reparse point named `_MEI...` could lead anywhere; this sweep has no
        // business near one.
        match entry.file_type() {
            Ok(kind) if kind.is_dir() && !kind.is_symlink() => {}
            _ => continue,
        }
        scan.seen += 1;
        // Rule 1 first: a directory without our marker belongs to some other
        // program (or to nobody we can identify), so it is not ours to delete --
        // not now, not when it is old, never.
        if !looks_like_ours(&entry.path()) {
            scan.foreign += 1;
            continue;
        }
        // Age comes from the directory's *creation* time -- `Metadata::created`,
        // i.e. the NTFS/exFAT creation stamp written when the bootloader made the
        // directory (mtime is useless here: extraction keeps touching it).  A
        // directory we cannot date is left alone rather than guessed at.
        let created = match entry.metadata().and_then(|m| m.created()) {
            Ok(created) => created,
            Err(_) => {
                scan.undated += 1;
                continue;
            }
        };
        match now.duration_since(created) {
            Ok(age) if age >= MEI_MIN_AGE => scan.debris.push(MeiDebris {
                path: entry.path(),
                created,
            }),
            // Younger than the guard, or stamped in the future (clock skew):
            // either way a PyInstaller program may be unpacking into it now.
            _ => scan.recent += 1,
        }
    }
    // Newest first: on the exit path the directory that was just killed is also
    // the youngest piece of debris, so it is the one the budget reaches first.
    scan.debris.sort_by(|a, b| b.created.cmp(&a.created));
    scan
}

/// Delete the debris.  Every error is dropped on purpose: a `_MEI*` directory
/// that refuses to be deleted is one somebody is still using, and leaving it
/// alone is the point of rule 1 above.  Anything skipped here is retried on the
/// next startup.
fn remove_mei_dirs(
    app: &tauri::AppHandle,
    tag: &str,
    scan: &MeiScan,
    budget: Option<Duration>,
    attempts: u32,
) {
    let started = Instant::now();
    let mut removed = 0usize;
    let mut failed = 0usize;
    for debris in &scan.debris {
        // The budget never skips the first candidate: on the exit path that one
        // is the directory left by the kill that was just issued.
        if removed + failed > 0 {
            if let Some(budget) = budget {
                if started.elapsed() >= budget {
                    break;
                }
            }
        }
        let mut deleted = false;
        for attempt in 0..attempts.max(1) {
            if attempt > 0 {
                std::thread::sleep(MEI_RETRY_GAP);
            }
            if std::fs::remove_dir_all(&debris.path).is_ok() {
                deleted = true;
                break;
            }
        }
        if deleted {
            removed += 1;
        } else {
            failed += 1;
        }
    }
    let elapsed_ms = started.elapsed().as_millis();
    append_sweep_log(
        app,
        &format!(
            "t={} tag={} temp={} mei_seen={} debris={} skip_recent={} skip_undated={} skip_foreign={} removed={} failed={} elapsed_ms={}\n",
            epoch_ms(),
            tag,
            std::env::temp_dir().display(),
            scan.seen,
            scan.debris.len(),
            scan.recent,
            scan.undated,
            scan.foreign,
            removed,
            failed,
            elapsed_ms,
        ),
    );
}

fn epoch_ms() -> u128 {
    SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// One line per sweep in `<app log dir>\mei-sweep.log`.  A release build has no
/// console (`windows_subsystem = "windows"`), and "swept nothing because there
/// was nothing" has to stay distinguishable from "swept nothing because every
/// removal failed" -- both for the user diagnosing a full disk and for anyone
/// verifying this feature.
fn append_sweep_log(app: &tauri::AppHandle, line: &str) {
    if let Some(path) = log_file_path(app).map(|p| p.with_file_name("mei-sweep.log")) {
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
        {
            let _ = f.write_all(line.as_bytes());
        }
    }
}

/// Startup sweep, called from `setup()` immediately *before* `spawn_sidecar()`
/// while our own sidecar has not unpacked yet -- so every `_MEI*` the scan can
/// see is debris by construction.  The scan is synchronous (microseconds); the
/// deletions go to a detached thread, so even a 116-directory backlog costs the
/// launch nothing.
fn sweep_mei_dirs_before_sidecar(app: &tauri::AppHandle) {
    let scan = scan_mei_dirs();
    let app = app.clone();
    std::thread::spawn(move || remove_mei_dirs(&app, "startup", &scan, None, 1));
}

/// Exit sweep, called right after `kill_sidecar()`.  The bootloader was killed
/// before it could delete its own unpack directory, and by now its files are
/// (or are about to become) free.  Bounded by MEI_EXIT_BUDGET so a backlog can
/// never make quitting feel stuck; whatever it does not reach is picked up by
/// the next startup sweep.
fn sweep_mei_dirs_after_sidecar(app: &tauri::AppHandle) {
    let scan = scan_mei_dirs();
    remove_mei_dirs(
        app,
        "exit",
        &scan,
        Some(MEI_EXIT_BUDGET),
        MEI_EXIT_ATTEMPTS,
    );
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(SidecarState(Mutex::new(SidecarInner::default())))
        .manage(SidecarDiag(Mutex::new(SidecarDiagInner::default())))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.unminimize();
                let _ = win.show();
                let _ = win.set_focus();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::new().build())
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // Engine bridge first: frontend fetches 127.0.0.1:21139 on load.
            // Sweep before spawning: our own sidecar has not unpacked yet, so
            // every `_MEI*` still in %TEMP% is debris from an earlier kill (P1b).
            sweep_mei_dirs_before_sidecar(app.handle());
            spawn_sidecar(app.handle());

            // Native menu: 显示主窗口 / 退出
            let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("llm-ocr · 语音学教程 OCR")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(win) = app.get_webview_window("main") {
                            let _ = win.show();
                            let _ = win.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(win) = app.get_webview_window("main") {
                            let visible = win.is_visible().unwrap_or(true);
                            if visible {
                                let _ = win.hide();
                            } else {
                                let _ = win.show();
                                let _ = win.set_focus();
                            }
                        }
                    }
                })
                .build(app)?;

            // First paint: show the window (tauri.conf visible=false avoids white flash)
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.show();
            }
            // Notify frontend that the shell is ready
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.emit("shell-ready", ());
            }
            Ok(())
        })
        .on_window_event(|win, event| {
            // Close to tray: hide instead of destroy; quit via tray menu.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if win.label() == "main" {
                    api.prevent_close();
                    let _ = win.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                kill_sidecar(app);
                // The kill skipped the bootloader's own cleanup, and its files
                // are free now that the process is gone: delete the directory
                // here (P1b).  Bounded by MEI_EXIT_BUDGET.
                sweep_mei_dirs_after_sidecar(app);
            }
        });
}
