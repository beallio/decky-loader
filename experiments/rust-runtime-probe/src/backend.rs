use std::{path::PathBuf, sync::Arc};

use anyhow::Context;
use serde_json::{Map, Value, json};
use tokio::sync::Mutex;

use crate::workers::{PluginRegistry, WorkerError};

pub enum RpcError {
    Error {
        name: String,
        message: String,
        traceback: Option<String>,
    },
    Stopped,
}

impl RpcError {
    fn invalid(message: impl Into<String>) -> Self {
        Self::Error {
            name: "ValueError".into(),
            message: message.into(),
            traceback: None,
        }
    }

    fn failure(message: impl Into<String>) -> Self {
        Self::Error {
            name: "Exception".into(),
            message: message.into(),
            traceback: None,
        }
    }
}

impl From<WorkerError> for RpcError {
    fn from(value: WorkerError) -> Self {
        match value {
            WorkerError::Error(message) => Self::failure(message),
            WorkerError::Stopped => Self::Stopped,
        }
    }
}

pub struct Backend {
    pub registry: Arc<PluginRegistry>,
    homebrew: PathBuf,
    settings_path: PathBuf,
    settings: Mutex<Map<String, Value>>,
    user: String,
    user_home: String,
    version: String,
}

impl Backend {
    pub async fn new(
        registry: Arc<PluginRegistry>,
        homebrew: PathBuf,
        user: String,
        user_home: String,
        version: String,
    ) -> anyhow::Result<Self> {
        let settings_path = homebrew.join("settings/loader.json");
        let settings = match tokio::fs::read(&settings_path).await {
            Ok(bytes) => serde_json::from_slice::<Map<String, Value>>(&bytes)
                .context("reading isolated loader settings")?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Map::new(),
            Err(error) => return Err(error.into()),
        };
        Ok(Self {
            registry,
            homebrew,
            settings_path,
            settings: Mutex::new(settings),
            user,
            user_home,
            version,
        })
    }

    pub fn loader_version(&self) -> &str {
        &self.version
    }

    pub async fn plugin_directory(&self, name: &str) -> Option<PathBuf> {
        self.registry.directory(name).await
    }

    pub async fn plugin_data_directory(&self, name: &str) -> Option<PathBuf> {
        let directory = self.registry.directory(name).await?;
        Some(self.homebrew.join("data").join(directory.file_name()?))
    }

    pub async fn dispatch(&self, route: &str, mut args: Vec<Value>) -> Result<Value, RpcError> {
        match route {
            "loader/get_plugins" => {
                exact(&args, 0)?;
                Ok(self.registry.list().await)
            }
            "loader/call_plugin_method" => {
                if args.len() < 2 {
                    return Err(RpcError::invalid("plugin name and method are required"));
                }
                let mut header = args.drain(..2);
                let name = header.next().expect("validated plugin name");
                let method = header.next().expect("validated method name");
                drop(header);
                let name = name
                    .as_str()
                    .ok_or_else(|| RpcError::invalid("argument 0 must be a string"))?;
                let method = method
                    .as_str()
                    .ok_or_else(|| RpcError::invalid("argument 1 must be a string"))?;
                self.registry
                    .call(name, method, Value::Array(args), false)
                    .await
                    .map_err(Into::into)
            }
            "loader/call_legacy_plugin_method" => {
                exact(&args, 3)?;
                let kwargs = args.pop().expect("validated legacy arguments");
                let name = string(&args, 0)?;
                let method = string(&args, 1)?;
                if !kwargs.is_object() {
                    return Err(RpcError::invalid(
                        "legacy method arguments must be an object",
                    ));
                }
                match self.registry.call(name, method, kwargs, true).await {
                    Ok(result) => {
                        let mut response = json!({"success": true});
                        response["result"] = result;
                        Ok(response)
                    }
                    Err(WorkerError::Error(message)) => {
                        let mut response = json!({"success": false});
                        response["result"] = Value::String(message);
                        Ok(response)
                    }
                    Err(WorkerError::Stopped) => Err(RpcError::Stopped),
                }
            }
            "loader/reload_plugin" => {
                exact(&args, 1)?;
                self.registry.reload(string(&args, 0)?).await?;
                Ok(Value::Null)
            }
            "utilities/ping" => {
                exact(&args, 0)?;
                Ok(json!("pong"))
            }
            "utilities/settings/get" => {
                exact(&args, 2)?;
                let settings = self.settings.lock().await;
                Ok(settings.get(string(&args, 0)?).unwrap_or(&args[1]).clone())
            }
            "utilities/settings/set" => {
                exact(&args, 2)?;
                let value = args.pop().expect("validated setting value");
                let key = string(&args, 0)?;
                let mut settings = self.settings.lock().await;
                let previous = settings.insert(key.to_owned(), value);
                let commit = async {
                    let data = serde_json::to_vec_pretty(&*settings)
                        .map_err(|error| RpcError::failure(error.to_string()))?;
                    let parent = self.settings_path.parent().expect("settings parent");
                    tokio::fs::create_dir_all(parent)
                        .await
                        .map_err(|error| RpcError::failure(error.to_string()))?;
                    let temporary = self.settings_path.with_extension("json.new");
                    tokio::fs::write(&temporary, data)
                        .await
                        .map_err(|error| RpcError::failure(error.to_string()))?;
                    tokio::fs::rename(&temporary, &self.settings_path)
                        .await
                        .map_err(|error| RpcError::failure(error.to_string()))
                }
                .await;
                if let Err(error) = commit {
                    if let Some(previous) = previous {
                        settings.insert(key.to_owned(), previous);
                    } else {
                        settings.remove(key);
                    }
                    return Err(error);
                }
                Ok(Value::Null)
            }
            "utilities/get_user_info" => {
                exact(&args, 0)?;
                Ok(json!({"username": self.user, "path": self.user_home}))
            }
            "updater/get_version_info" => {
                exact(&args, 0)?;
                // Report the packaged SDK/source baseline, not a Rust release.
                // This private probe has no release updater or administrative update routes.
                Ok(json!({
                    "current": self.version,
                    "remote": null,
                    "all": [],
                    "updatable": false,
                }))
            }
            _ => Err(RpcError::Error {
                name: "RouteNotFoundError".into(),
                message: format!("Route {route} does not exist."),
                traceback: None,
            }),
        }
    }
}

fn exact(args: &[Value], expected: usize) -> Result<(), RpcError> {
    if args.len() == expected {
        Ok(())
    } else {
        Err(RpcError::invalid(format!(
            "expected {expected} arguments, received {}",
            args.len()
        )))
    }
}

fn string(args: &[Value], index: usize) -> Result<&str, RpcError> {
    args.get(index)
        .and_then(Value::as_str)
        .ok_or_else(|| RpcError::invalid(format!("argument {index} must be a string")))
}
