// PaperOpenClaw 桌面 GUI（Tauri 2）。
//
// 职责：提供原生窗口外壳，启动时拉起 Python 后端（FastAPI），前端通过
// http://127.0.0.1:8787 调用它。UI 框架本身不依赖 Python。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};

/// 后台启动 Python 后端；若已在运行（端口被占）则忽略失败。
fn spawn_backend() -> Option<Child> {
    let python = std::env::var("PAPEROPENCLAW_PYTHON").unwrap_or_else(|_| "python".to_string());
    let data_dir = std::env::var("PAPEROPENCLAW_DATA_DIR").ok();

    let mut command = Command::new(&python);
    command
        .args(["-m", "paperopenclaw", "serve", "--no-browser"])
        .env("PAPEROPENCLAW_NO_BROWSER", "1");

    if let Some(dir) = data_dir {
        command.env("PAPEROPENCLAW_DATA_DIR", dir);
    }

    match command.spawn() {
        Ok(child) => Some(child),
        Err(err) => {
            eprintln!("[PaperOpenClaw] 无法启动 Python 后端：{err}");
            None
        }
    }
}

fn main() {
    // 保持子进程存活到程序退出。
    let _backend = spawn_backend();

    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("PaperOpenClaw 启动失败");
}
