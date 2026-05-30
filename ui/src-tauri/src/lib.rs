use std::process::Command;

#[tauri::command]
fn run_script(script_name: &str, args: Vec<&str>) -> Result<String, String> {
    let script_path = format!("../scripts/{}", script_name);
    
    let mut cmd = Command::new("pwsh");
    cmd.arg("-NoProfile")
       .arg("-File")
       .arg(&script_path);
       
    for arg in args {
        cmd.arg(arg);
    }
    
    match cmd.output() {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout).to_string();
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            if output.status.success() {
                Ok(stdout)
            } else {
                Err(format!("Error: {}\n{}", stderr, stdout))
            }
        }
        Err(e) => Err(format!("Failed to execute script: {}", e)),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![run_script])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
