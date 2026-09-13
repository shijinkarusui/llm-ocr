// llm-ocr Tauri v2 shell: tray + native menu + single-instance + window-state.
// Engine calls go through the serve sidecar (serve.exe, PyInstaller onefile)
// over HTTP 127.0.0.1:21139; Rust exposes no custom commands (all business
// logic stays in Python src/). Dev mode: spawn `python ../../serve.py`.
use std::sync::Mutex;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, RunEvent, WindowEvent,
};

struct SidecarState(pub Mutex<Option<tauri_plugin_shell::process::CommandChild>>);

fn spawn_sidecar(app: &tauri::AppHandle) {
    use tauri_plugin_shell::ShellExt;
    let scope = app.shell().sidecar("serve").unwrap();
    // Release build: bundled sidecar. Dev build (`tauri dev`): sidecar file
    // is absent, fall back to the repo python script (dev-only).
    let child = match scope.args(["--port", "21139"]).spawn() {
        Ok((_, child)) => Some(child),
        Err(_) => {
            #[cfg(debug_assertions)]
            {
                let fallback = app
                    .shell()
                    .command("python")
                    .args(["../../serve.py", "--port", "21139"])
                    .spawn();
                match fallback {
                    Ok((_, child)) => Some(child),
                    Err(e) => {
                        eprintln!("[shell] dev fallback spawn failed: {e}");
                        None
                    }
                }
            }
            #[cfg(not(debug_assertions))]
            {
                eprintln!("[shell] sidecar spawn failed");
                None
            }
        }
    };
    if let Some(state) = app.try_state::<SidecarState>() {
        *state.0.lock().unwrap() = child;
    }
}

fn kill_sidecar(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<SidecarState>() {
        if let Some(child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(SidecarState(Mutex::new(None)))
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
            }
        });
}
