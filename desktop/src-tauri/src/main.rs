// PaperOpenClaw 桌面 GUI（Tauri 2）。
//
// 职责：提供原生窗口外壳，启动时拉起后端（backend/runtime/ 下的 onedir 后端，
// 内含 Python 运行时与全部依赖，免安装）。数据目录 backend/data（db/skills/papers）
// 直接可见，用户可把 skill 丢进 backend/data/skills/。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};

/// GUI exe 所在目录（用于定位 backend/ 文件夹）。
fn exe_dir() -> Option<std::path::PathBuf> {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
}

/// 数据目录：显式环境变量优先，否则固定到 <exe>/backend/data。
fn data_dir_default() -> String {
    if let Ok(d) = std::env::var("PAPEROPENCLAW_DATA_DIR") {
        return d;
    }
    if let Some(dir) = exe_dir() {
        return dir.join("backend").join("data").to_string_lossy().into_owned();
    }
    "data".to_string()
}

/// 定位后端：环境变量 → 同目录 backend/runtime/ 下的 onedir 后端 → 回退系统 Python。
fn backend_command() -> Command {
    // 1) 环境变量显式指定的后端可执行文件。
    if let Ok(path) = std::env::var("PAPEROPENCLAW_BACKEND") {
        return Command::new(path);
    }

    // 2) 与 GUI 同目录的 onedir 后端（运行时 + 全部依赖，免装）。
    if let Some(dir) = exe_dir() {
        let candidates = [
            dir.join("backend").join("runtime").join("paperopenclaw-backend.exe"),
            dir.join("backend").join("paperopenclaw-backend").join("paperopenclaw-backend.exe"),
            dir.join("backend").join("runtime").join("paperopenclaw-backend"),
            dir.join("backend").join("paperopenclaw-backend").join("paperopenclaw-backend"),
        ];
        for candidate in candidates {
            if candidate.exists() {
                return Command::new(candidate);
            }
        }
    }

    // 3) 开发回退：python -m paperopenclaw serve。
    let python = std::env::var("PAPEROPENCLAW_PYTHON").unwrap_or_else(|_| "python".to_string());
    let mut command = Command::new(&python);
    command.args(["-m", "paperopenclaw", "serve", "--no-browser"]);
    command
}

/// 后台启动后端；若已在运行（端口被占）则忽略失败。
fn spawn_backend() -> Option<Child> {
    let mut command = backend_command();
    command
        .env("PAPEROPENCLAW_NO_BROWSER", "1")
        .env("PAPEROPENCLAW_DATA_DIR", data_dir_default());

    match command.spawn() {
        Ok(child) => Some(child),
        Err(err) => {
            eprintln!("[PaperOpenClaw] 无法启动后端：{err}");
            None
        }
    }
}

fn main() {
    let _backend = spawn_backend();

    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("PaperOpenClaw 启动失败");
}
