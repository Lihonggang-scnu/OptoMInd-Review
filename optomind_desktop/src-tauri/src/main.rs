// OptoMind desktop shell (commit 2): Python sidecar lifecycle.
//
// Security contract (mirrors optomind_ui/server.py):
//   * the sidecar binds 127.0.0.1 ONLY -- never 0.0.0.0, never a tunnel;
//   * the shell talks to http://127.0.0.1:<picked-port> exclusively;
//   * no orphan Python may survive window close (terminate -> kill).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{Manager, RunEvent, WindowEvent};

struct SidecarState {
    child: Mutex<Option<Child>>,
    port: Mutex<u16>,
}

fn pick_free_port() -> u16 {
    // Never hardcode a fixed port: grab an ephemeral free port from the OS.
    // If a racer steals it afterwards, uvicorn exits loudly and the crash
    // surface explains it -- silent failure is not an option.
    TcpListener::bind("127.0.0.1:0")
        .expect("no free loopback port available")
        .local_addr()
        .expect("loopback addr")
        .port()
}

fn locate_python(exe_dir: &Path) -> Option<PathBuf> {
    // Priority: explicit override > embedded > repo .venv > PATH python.
    if let Ok(p) = std::env::var("OPTOMIND_PYTHON") {
        if !p.trim().is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    let mut candidates = vec![exe_dir.join("python").join("python.exe")];
    let mut cursor: Option<&Path> = Some(exe_dir);
    for _ in 0..5 {
        match cursor {
            Some(dir) => {
                candidates.push(dir.join(".venv").join("Scripts").join("python.exe"));
                cursor = dir.parent();
            }
            None => break,
        }
    }
    for candidate in candidates {
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    for name in ["python.exe", "python"] {
        if let Ok(output) = Command::new(name).arg("--version").output() {
            if output.status.success() {
                return Some(PathBuf::from(name));
            }
        }
    }
    None
}

fn locate_repo_root(start: &Path) -> Option<PathBuf> {
    // Dev-mode resolution: find the directory containing optomind_ui/server.py.
    let mut cursor: Option<&Path> = start.parent();
    for _ in 0..6 {
        match cursor {
            Some(dir) => {
                if dir.join("optomind_ui").join("server.py").is_file() {
                    return Some(dir.to_path_buf());
                }
                cursor = dir.parent();
            }
            None => break,
        }
    }
    None
}

fn probe_ready(port: u16) -> bool {
    // Minimal HTTP GET against the loopback sidecar; avoids pulling an HTTP
    // client crate into the shell (keeps the binary small).
    let mut stream = match TcpStream::connect(("127.0.0.1", port)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    stream
        .set_read_timeout(Some(Duration::from_millis(1500)))
        .ok();
    let request = format!(
        "GET /api/preflight HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 256];
    let n = stream.read(&mut head).unwrap_or(0);
    let text = String::from_utf8_lossy(&head[..n]);
    text.starts_with("HTTP/") && text.contains(" 200")
}

#[cfg(windows)]
use std::os::windows::process::CommandExt;

fn kill_tree(pid: u32) {
    // Graceful pass first (WM_CLOSE where applicable), then force after a
    // bounded grace period. Either way the tree must be gone before exit.
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T"])
        .creation_flags(0x0800_0000)
        .output();
    std::thread::sleep(Duration::from_secs(3));
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(0x0800_0000)
        .output();
}

fn spawn_sidecar(
    python: &Path,
    repo_root: Option<&Path>,
    port: u16,
) -> Result<Child, String> {
    let mut command = Command::new(python);
    command
        .arg("-m")
        .arg("optomind_ui.server")
        .arg("--port")
        .arg(port.to_string())
        .stdin(Stdio::null());
    let log_path = std::env::temp_dir().join("optomind-sidecar.log");
    if let Ok(log) = std::fs::File::create(&log_path) {
        command.stdout(log.try_clone().expect("log handle"));
        command.stderr(log);
    }
    if let Some(root) = repo_root {
        command.current_dir(root);
        command.env("PYTHONPATH", root);
    }
    #[cfg(windows)]
    command.creation_flags(0x0800_0000);
    command.spawn().map_err(|e| {
        format!(
            "无法启动 Python sidecar：{e}。日志：{log}",
            log = log_path.display()
        )
    })
}

fn show_error_on(window: &tauri::WebviewWindow, title: &str, detail: &str) {
    let safe_detail = detail.replace('\\', "/").replace('\'', "");
    let js = format!(
        "document.body.innerHTML = '<div style=\"font-family:system-ui;padding:28px;color:#e2e8f0;background:#0f172a;height:100vh;box-sizing:border-box\">' +
        '<h2 style=\"margin:0 0 12px;font-size:20px\">{t}</h2>' +
        '<p style=\"line-height:1.8;white-space:pre-wrap;font-size:14px\">{d}</p>' +
        '<button onclick=\"location.reload()\" style=\"margin-top:14px;padding:9px 20px;border-radius:8px;border:0;background:#0ea5e9;color:#04283c;font-weight:600;cursor:pointer\">重试</button></div>';",
        t = title,
        d = safe_detail
    );
    let _ = window.eval(&js);
}

fn boot_sidecar(app: &tauri::AppHandle) {
    let state: tauri::State<SidecarState> = app.state();
    let port = pick_free_port();
    if let Some(splash) = app.get_webview_window("splash") {
        let js_port = format!("window.__PORT={port};");
        for _ in 0..10 {
            if splash.eval(&js_port).is_ok() {
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    let python = match locate_python(&exe_dir) {
        Some(p) => p,
        None => {
            if let Some(splash) = app.get_webview_window("splash") {
                show_error_on(
                    &splash,
                    "未找到 Python",
                    "OptoMind 需要 Python 3.11+ 才能运行本地服务。\n\n请安装 Python 后点击重试，或设置环境变量 OPTOMIND_PYTHON 指向 python.exe。",
                );
            }
            return;
        }
    };
    let repo_root = locate_repo_root(&exe_dir);
    eprintln!("[sidecar] python={:?} repo={:?} port={}", python, repo_root, port);
    match spawn_sidecar(&python, repo_root.as_deref(), port) {
        Ok(child) => {
            eprintln!("[sidecar] spawned pid={}", child.id());
            *state.child.lock().unwrap() = Some(child);
            *state.port.lock().unwrap() = port;
            let handle = app.clone();
            std::thread::spawn(move || {
                // Readiness gate: poll /api/preflight up to ~40s before
                // swapping the splash screen for the real UI.
                let deadline = std::time::Instant::now() + Duration::from_secs(40);
                let mut ready = false;
                while std::time::Instant::now() < deadline {
                    if probe_ready(port) {
                        ready = true;
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(400));
                }
                let main_window = handle.get_webview_window("main");
                let splash = handle.get_webview_window("splash");
                if !ready {
                    if let Some(splash_window) = splash {
                        show_error_on(
                            &splash_window,
                            "本地服务启动超时",
                            "sidecar 在 40 秒内未就绪。常见原因：安全软件拦截回环端口，或 pip 依赖缺失。\n日志：%TEMP%\\optomind-sidecar.log\n点击重试会重新拉起 sidecar。",
                        );
                    }
                    return;
                }
                if let Some(win) = main_window {
                    let url: tauri::Url =
                        format!("http://127.0.0.1:{port}/").parse().expect("loopback url");
                    let _ = win.navigate(url);
                    let _ = win.show();
                    let _ = win.set_focus();
                }
                if let Some(splash_window) = splash {
                    let _ = splash_window.close();
                }
                // Crash watch: sidecar dying mid-session must be visible,
                // never a silent white window.
                let watch_handle = handle.clone();
                std::thread::spawn(move || loop {
                    std::thread::sleep(Duration::from_secs(2));
                    let state: tauri::State<SidecarState> = watch_handle.state();
                    let mut guard = state.child.lock().unwrap();
                    match guard.as_mut() {
                        Some(child) => match child.try_wait() {
                            Ok(Some(_status)) => {
                                *guard = None;
                                drop(guard);
                                if let Some(win) = watch_handle.get_webview_window("main") {
                                    let js = concat!(
                                        "if(!window.__optomindCrash){window.__optomindCrash=1;",
                                        "const bar=document.createElement('div');",
                                        "bar.style.cssText='position:fixed;inset:auto 0 0 0;z-index:99999;padding:16px 22px;background:#7f1d1d;color:#fecaca;font-family:system-ui;font-size:14px;display:flex;gap:16px;align-items:center';",
                                        "bar.textContent='本地服务已退出。日志：%TEMP%\\optomind-sidecar.log';",
                                        "const btn=document.createElement('button');",
                                        "btn.textContent='重启服务';",
                                        "btn.style.cssText='padding:7px 16px;border-radius:8px;border:0;background:#fecaca;color:#7f1d1d;font-weight:700;cursor:pointer';",
                                        "btn.onclick=()=>window.__TAURI__.core.invoke('restart_sidecar');",
                                        "bar.appendChild(btn);document.body.appendChild(bar);}"
                                    );
                                    let _ = win.eval(js);
                                }
                                break;
                            }
                            Ok(None) => {}
                            Err(_) => break,
                        },
                        None => break,
                    }
                });
            });
        }
        Err(message) => {
            if let Some(splash_window) = app.get_webview_window("splash") {
                show_error_on(&splash_window, "sidecar 启动失败", &message);
            }
        }
    }
}

#[tauri::command]
fn restart_sidecar(app: tauri::AppHandle) {
    let state: tauri::State<SidecarState> = app.state();
    if let Some(child) = state.child.lock().unwrap().as_ref() {
        kill_tree(child.id());
    }
    *state.child.lock().unwrap() = None;
    boot_sidecar(&app);
}

fn main() {
    eprintln!("[boot] entering main");
    tauri::Builder::default()
        .manage(SidecarState {
            child: Mutex::new(None),
            port: Mutex::new(0),
        })
        .invoke_handler(tauri::generate_handler![restart_sidecar])
        .setup(|app| {
            // Windows come from tauri.conf.json (created BEFORE build(), so
            // the event loop never sees a zero-window app and auto-exits).
            // The splash page tolerates late __PORT injection via polling.
            eprintln!("[boot] setup begin, spawning sidecar");
            eprintln!("[boot] windows at setup: {:?}", app.webview_windows().keys().collect::<Vec<_>>());
            boot_sidecar(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Only the MAIN window drives app lifetime; the splash screen is
            // closed BY the app itself after the sidecar is ready.
            if window.label() == "main" {
                if let WindowEvent::CloseRequested { .. } = event {
                    let _ = window.app_handle().exit(0);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("OptoMind desktop shell failed to build")
        .run(|app, event| {
            match &event {
                RunEvent::ExitRequested { code, .. } => {
                    eprintln!("[evt] exit_requested code={code:?}");
                }
                RunEvent::WindowEvent { label, event, .. } => {
                    eprintln!("[evt] window {label} {:?}", event);
                }
                _ => {}
            }
            if let RunEvent::Exit = event {
                eprintln!("[evt] EXIT cleanup");
                let state: tauri::State<SidecarState> = app.state();
                if let Some(child) = state.child.lock().unwrap().as_ref() {
                    kill_tree(child.id());
                }
                *state.child.lock().unwrap() = None;
            }
        });
}
