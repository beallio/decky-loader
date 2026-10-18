use std::{path::PathBuf, sync::Arc};

use anyhow::Context;
use serde_json::{Map, Value, json, value::RawValue};
use tokio::sync::Mutex;

use crate::workers::{PluginRegistry, WorkerError};
use crate::{cef::Cef, plugin_manager::PluginManager, updater::Updater, utilities::Utilities};

#[derive(Debug)]
pub enum RpcError {
    Error {
        name: String,
        message: String,
        traceback: Option<String>,
    },
    Stopped,
}

impl RpcError {
    pub(crate) fn invalid(message: impl Into<String>) -> Self {
        Self::Error {
            name: "ValueError".into(),
            message: message.into(),
            traceback: None,
        }
    }

    pub(crate) fn failure(message: impl Into<String>) -> Self {
        Self::Error {
            name: "Exception".into(),
            message: message.into(),
            traceback: None,
        }
    }

    pub(crate) fn message(&self) -> &str {
        match self {
            Self::Error { message, .. } => message,
            Self::Stopped => "Plugin has been stopped",
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

pub(crate) struct RuntimeConfig {
    pub service_name: String,
    pub cef_url: String,
    pub inject_frontend: bool,
    pub installer: Option<PathBuf>,
    pub listen: String,
    pub worker_mode: String,
    pub github_api: String,
    pub release_repository: String,
    pub testing_download_url: String,
    pub chown_plugin_path: bool,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            service_name: "plugin_loader".into(),
            cef_url: "http://127.0.0.1:8080".into(),
            inject_frontend: false,
            installer: None,
            listen: "127.0.0.1:1339".into(),
            worker_mode: "fork".into(),
            github_api: "https://api.github.com".into(),
            release_repository: "beallio/decky-loader".into(),
            testing_download_url: "https://nightly.link".into(),
            chown_plugin_path: true,
        }
    }
}

pub struct Backend {
    pub registry: Arc<PluginRegistry>,
    pub(crate) homebrew: PathBuf,
    settings_path: PathBuf,
    settings: Mutex<Map<String, Value>>,
    pub(crate) user: String,
    pub(crate) user_home: String,
    pub(crate) version: String,
    pub(crate) sdk_version: String,
    pub(crate) client: reqwest::Client,
    pub(crate) config: RuntimeConfig,
    pub(crate) cef: Cef,
    pub(crate) updater: Arc<Updater>,
    plugin_manager: PluginManager,
    utilities: Utilities,
}

impl Backend {
    pub async fn new(
        registry: Arc<PluginRegistry>,
        homebrew: PathBuf,
        user: String,
        user_home: String,
        version: String,
        sdk_version: String,
    ) -> anyhow::Result<Self> {
        let settings_path = registry
            .plugin_root()
            .parent()
            .unwrap_or(&homebrew)
            .join("settings/loader.json");
        let settings = match tokio::fs::read(&settings_path).await {
            Ok(bytes) => serde_json::from_slice::<Map<String, Value>>(&bytes)
                .context("reading isolated loader settings")?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Map::new(),
            Err(error) => return Err(error.into()),
        };
        let config = RuntimeConfig::default();
        let client = reqwest::Client::builder()
            .user_agent(format!("Decky/{version} (https://decky.xyz)"))
            .no_proxy()
            .no_gzip()
            .no_brotli()
            .no_deflate()
            .build()?;
        let cef = Cef::new(client.clone(), config.cef_url.clone());
        Ok(Self {
            registry,
            homebrew,
            settings_path,
            settings: Mutex::new(settings),
            user,
            user_home,
            version,
            sdk_version,
            client,
            config,
            cef,
            updater: Arc::new(Updater::new()),
            plugin_manager: PluginManager::new(),
            utilities: Utilities::new(),
        })
    }

    pub(crate) fn with_config(mut self, config: RuntimeConfig) -> Self {
        self.cef = Cef::new(self.client.clone(), config.cef_url.clone());
        self.config = config;
        self
    }

    pub(crate) fn emit(&self, event: &str, args: Vec<Value>) {
        self.registry.emit(event, args);
    }

    pub(crate) async fn setting(&self, key: &str, default: Value) -> Value {
        self.settings
            .lock()
            .await
            .get(key)
            .cloned()
            .unwrap_or(default)
    }

    pub(crate) async fn set_setting(&self, key: &str, value: Value) -> Result<(), RpcError> {
        let mut settings = self.settings.lock().await;
        if settings.get(key) == Some(&value) {
            return Ok(());
        }
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
        Ok(())
    }

    pub(crate) async fn initialize_plugin_order(&self) -> Result<(), RpcError> {
        if self.setting("pluginOrder", Value::Null).await.is_null() {
            let plugins = self.registry.list().await;
            let names = plugins
                .as_array()
                .expect("registry plugin list")
                .iter()
                .filter_map(|plugin| plugin.get("name"))
                .cloned()
                .collect();
            self.set_setting("pluginOrder", Value::Array(names)).await?;
        }
        Ok(())
    }

    pub(crate) async fn legacy_utility(&self, method: &str, kwargs: Value) -> Value {
        self.utilities.legacy_call(self, method, kwargs).await
    }

    pub(crate) fn cancel_utilities_setup(&self) {
        self.utilities.cancel_setup();
    }

    pub(crate) async fn shutdown_utilities(&self) -> Result<(), RpcError> {
        self.utilities.shutdown(self).await
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
        if route.starts_with("updater/") {
            return self
                .updater
                .dispatch(self, route, args)
                .await
                .unwrap_or_else(|| Err(missing_route(route)));
        }
        // The plugin manager owns the install and management routes; it returns None for others.
        if let Some(result) = self.plugin_manager.dispatch(self, route, &args).await {
            return result;
        }
        match route {
            "loader/get_plugins" => {
                exact(&args, 0)?;
                Ok(self.registry.list().await)
            }
            "loader/call_plugin_method" | "loader/call_legacy_plugin_method" => {
                let raw = args
                    .iter()
                    .map(serde_json::value::to_raw_value)
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(|error| RpcError::failure(error.to_string()))?;
                let raw: Vec<&RawValue> = raw.iter().map(AsRef::as_ref).collect();
                let legacy = route == "loader/call_legacy_plugin_method";
                let result = self.call_plugin(legacy, &raw).await?;
                serde_json::from_str(&result).map_err(|error| RpcError::failure(error.to_string()))
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
                let default = args.pop().expect("validated default");
                Ok(self.setting(string(&args, 0)?, default).await)
            }
            "utilities/settings/set" => {
                exact(&args, 2)?;
                let value = args.pop().expect("validated setting value");
                let key = string(&args, 0)?;
                self.set_setting(key, value).await?;
                Ok(Value::Null)
            }
            "utilities/get_user_info" => {
                exact(&args, 0)?;
                Ok(json!({"username": self.user, "path": self.user_home}))
            }
            _ if route.starts_with("utilities/") => self
                .utilities
                .dispatch(self, route, args)
                .await
                .unwrap_or_else(|| Err(missing_route(route))),
            _ => Err(missing_route(route)),
        }
    }

    /// Runs `loader/call_plugin_method` (or the legacy route) on unparsed arguments
    /// and returns the route result as JSON text. Only the plugin and method names
    /// are decoded; plugin data passes through as text in both directions.
    pub async fn call_plugin(&self, legacy: bool, args: &[&RawValue]) -> Result<String, RpcError> {
        let name_at = |index: usize| {
            args.get(index)
                .and_then(|raw| serde_json::from_str::<String>(raw.get()).ok())
                .ok_or_else(|| RpcError::invalid(format!("argument {index} must be a string")))
        };
        if !legacy {
            if args.len() < 2 {
                return Err(RpcError::invalid("plugin name and method are required"));
            }
            let (name, method) = (name_at(0)?, name_at(1)?);
            let mut call_args = String::from("[");
            for (index, arg) in args[2..].iter().enumerate() {
                if index > 0 {
                    call_args.push(',');
                }
                call_args.push_str(arg.get());
            }
            call_args.push(']');
            let result = self
                .registry
                .call(&name, &method, &call_args, false)
                .await?;
            return Ok(Box::<str>::from(result).into_string());
        }

        if args.len() != 3 {
            return Err(RpcError::invalid(format!(
                "expected 3 arguments, received {}",
                args.len()
            )));
        }
        let (name, method) = (name_at(0)?, name_at(1)?);
        let kwargs = args[2].get();
        if !kwargs.starts_with('{') {
            return Err(RpcError::invalid(
                "legacy method arguments must be an object",
            ));
        }
        match self.registry.call(&name, &method, kwargs, true).await {
            Ok(result) => Ok(format!(r#"{{"success":true,"result":{}}}"#, result.get())),
            Err(WorkerError::Error(message)) => Ok(format!(
                r#"{{"success":false,"result":{}}}"#,
                Value::String(message)
            )),
            Err(WorkerError::Stopped) => Err(RpcError::Stopped),
        }
    }
}

pub(crate) fn exact(args: &[Value], expected: usize) -> Result<(), RpcError> {
    if args.len() == expected {
        Ok(())
    } else {
        Err(RpcError::invalid(format!(
            "expected {expected} arguments, received {}",
            args.len()
        )))
    }
}

pub(crate) fn string(args: &[Value], index: usize) -> Result<&str, RpcError> {
    args.get(index)
        .and_then(Value::as_str)
        .ok_or_else(|| RpcError::invalid(format!("argument {index} must be a string")))
}

fn missing_route(route: &str) -> RpcError {
    RpcError::Error {
        name: "RouteNotFoundError".into(),
        message: format!("Route {route} does not exist."),
        traceback: None,
    }
}
