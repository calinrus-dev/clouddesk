#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use base64::Engine;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{path::PathBuf, time::Duration};
use tauri::State;
use tauri_plugin_opener::OpenerExt;
use tokio::sync::Mutex;

#[derive(Deserialize, Clone)]
struct Credentials {
    url: String,
    token: String,
}

struct Bridge {
    credentials: Mutex<Option<Credentials>>,
    client: reqwest::Client,
    root: PathBuf,
}

fn allowed_operation(operation: &str) -> bool {
    matches!(
        operation,
        "status"
            | "login"
            | "logout"
            | "list"
            | "stat"
            | "quota"
            | "mkdir"
            | "move"
            | "delete"
            | "upload"
            | "download"
            | "jobs"
            | "job"
            | "cancel"
            | "pause"
            | "resume"
            | "preview"
            | "volumes"
            | "configure"
            | "copy"
    )
}

#[tauri::command]
async fn image_preview(
    job_id: String,
    bridge: State<'_, Bridge>,
) -> Result<Option<String>, String> {
    let job = bridge_rpc("job".into(), json!({"id": job_id}), bridge).await?;
    if job.get("state").and_then(Value::as_str) != Some("completed")
        || job.get("kind").and_then(Value::as_str) != Some("download")
    {
        return Err("La vista previa todavía no está disponible".into());
    }
    let path = job
        .get("local")
        .and_then(Value::as_str)
        .ok_or("Archivo inválido")?;
    let extension = PathBuf::from(path)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let mime = match extension.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "gif" => "image/gif",
        _ => return Ok(None),
    };
    let file = std::fs::metadata(path).map_err(|_| "No se encuentra la vista previa")?;
    if file.len() > 20 * 1024 * 1024 {
        return Err("La vista previa de imagen admite hasta 20 MB".into());
    }
    let bytes = std::fs::read(path).map_err(|_| "No se pudo leer la imagen")?;
    Ok(Some(format!(
        "data:{};base64,{}",
        mime,
        base64::engine::general_purpose::STANDARD.encode(bytes)
    )))
}

#[tauri::command]
async fn bridge_rpc(
    operation: String,
    args: Value,
    bridge: State<'_, Bridge>,
) -> Result<Value, String> {
    if !allowed_operation(&operation) || !args.is_object() {
        return Err("Petición local inválida".into());
    }
    let mut cached = bridge.credentials.lock().await;
    if cached.is_none() {
        let process = tokio::process::Command::new(bridge.root.join(".venv/bin/python"))
            .args(["-m", "bridge", "credential"])
            .current_dir(&bridge.root)
            .kill_on_drop(true)
            .output();
        let output = tokio::time::timeout(Duration::from_secs(25), process)
            .await
            .map_err(|_| "El servicio local tarda demasiado en arrancar")?
            .map_err(|_| "No se encuentra el entorno Python. Ejecuta scripts/setup.sh")?;
        let value: Value = serde_json::from_slice(&output.stdout)
            .map_err(|_| "No se pudo iniciar el servicio. Revisa el llavero del escritorio")?;
        if let Some(error) = value.get("error") {
            return Err(error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("Desbloquea el llavero")
                .to_owned());
        }
        let credentials: Credentials =
            serde_json::from_value(value).map_err(|_| "Configuración local inválida")?;
        if credentials.url != "http://127.0.0.1:47823" {
            return Err("Dirección del servicio local inválida".into());
        }
        *cached = Some(credentials);
    }
    let credentials = cached.as_ref().unwrap().clone();
    drop(cached);
    let response = bridge
        .client
        .post(format!("{}/rpc", credentials.url))
        .bearer_auth(&credentials.token)
        .json(&json!({"operation": operation, "args": args}))
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(_) => {
            *bridge.credentials.lock().await = None;
            return Err(
                "El servicio local no responde. No repitas una escritura sin comprobarla".into(),
            );
        }
    };
    let value: Value = response
        .json()
        .await
        .map_err(|_| "Respuesta local inválida")?;
    if let Some(error) = value.get("error") {
        return Err(error
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("La operación falló")
            .to_owned());
    }
    value
        .get("result")
        .cloned()
        .ok_or_else(|| "Respuesta local incompleta".to_owned())
}

#[tauri::command]
async fn open_preview(
    job_id: String,
    app: tauri::AppHandle,
    bridge: State<'_, Bridge>,
) -> Result<(), String> {
    let job = bridge_rpc("job".into(), json!({"id": job_id}), bridge).await?;
    if job.get("state").and_then(Value::as_str) != Some("completed")
        || job.get("kind").and_then(Value::as_str) != Some("download")
    {
        return Err("La vista previa todavía no está disponible".into());
    }
    let path = job
        .get("local")
        .and_then(Value::as_str)
        .ok_or("Archivo de vista previa inválido")?;
    // The path comes from an authenticated, completed download, never arbitrary web content.
    let file = PathBuf::from(path);
    let extension = file
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if !matches!(
        extension.as_str(),
        "png"
            | "jpg"
            | "jpeg"
            | "webp"
            | "gif"
            | "pdf"
            | "txt"
            | "md"
            | "csv"
            | "mp3"
            | "mp4"
            | "ogg"
            | "wav"
    ) {
        return Err("Descarga este tipo de archivo para abrirlo manualmente".into());
    }
    app.opener()
        .open_path(path, None::<&str>)
        .map_err(|_| "No se pudo abrir el visor del escritorio".into())
}

fn main() {
    let root = std::env::var_os("O2_DESKTOP_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .to_path_buf()
        });
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(330))
        .build()
        .expect("No se pudo crear el cliente local");
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Bridge {
            credentials: Mutex::new(None),
            client,
            root,
        })
        .invoke_handler(tauri::generate_handler![
            bridge_rpc,
            open_preview,
            image_preview
        ])
        .run(tauri::generate_context!())
        .expect("No se pudo iniciar O2 Cloud Desktop");
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_file_operations_are_exposed() {
        assert!(allowed_operation("move"));
        assert!(allowed_operation("preview"));
        assert!(!allowed_operation("credential"));
        assert!(!allowed_operation("delete-permanent"));
    }
}
