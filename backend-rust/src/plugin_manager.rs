use std::{
    collections::{HashMap, HashSet},
    fs::{self, OpenOptions},
    io::{self, Cursor},
    path::{Component, Path, PathBuf},
};

use axum::body::Bytes;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;
use uuid::Uuid;
use zip::ZipArchive;

use crate::{
    backend::{Backend, RpcError},
    platform,
    workers::WorkerError,
};

const SETTINGS_KEYS: [&str; 4] = [
    "frozenPlugins",
    "hiddenPlugins",
    "pluginOrder",
    "disabled_plugins",
];

#[derive(Clone)]
struct InstallRequest {
    artifact: String,
    name: String,
    version: String,
    hash: Value,
    install_type: u64,
}

impl InstallRequest {
    fn to_json(&self) -> Value {
        json!({
            "artifact": self.artifact,
            "name": self.name,
            "version": self.version,
            "hash": self.hash,
            "install_type": self.install_type,
        })
    }
}

enum PendingInstall {
    Single(InstallRequest),
    Batch(Vec<InstallRequest>),
}

#[derive(Clone)]
struct SettingsLists {
    values: HashMap<&'static str, Vec<String>>,
}

impl SettingsLists {
    fn get(&self, key: &'static str) -> &[String] {
        self.values.get(key).map(Vec::as_slice).unwrap_or_default()
    }

    fn get_mut(&mut self, key: &'static str) -> &mut Vec<String> {
        self.values.entry(key).or_default()
    }

    fn to_value(&self, key: &'static str) -> Value {
        Value::Array(self.get(key).iter().cloned().map(Value::String).collect())
    }
}

struct StagedPlugin {
    path: PathBuf,
    homebrew: PathBuf,
}

impl StagedPlugin {
    fn create(plugin_root: &Path, homebrew: &Path) -> Result<Self, RpcError> {
        let path = plugin_root.join(format!(".decky-stage-{}", Uuid::new_v4().simple()));
        fs::create_dir(&path).map_err(|error| {
            RpcError::failure(format!(
                "Could not create plugin staging directory {}: {error}",
                path.display()
            ))
        })?;
        Ok(Self {
            path,
            homebrew: homebrew.to_path_buf(),
        })
    }

    fn directory_name(&self) -> Result<&str, RpcError> {
        self.path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| RpcError::failure("Staging directory name is not valid UTF-8"))
    }
}

impl Drop for StagedPlugin {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
        if let Some(directory) = self.path.file_name() {
            for storage in ["settings", "data", "logs"] {
                let _ = fs::remove_dir_all(self.homebrew.join(storage).join(directory));
            }
        }
    }
}

struct FilesystemReplacement {
    stage: PathBuf,
    target: PathBuf,
    old: Option<(PathBuf, PathBuf)>,
    active: bool,
}

impl FilesystemReplacement {
    fn activate(
        stage: &Path,
        target: &Path,
        old_directory: Option<&Path>,
        backup: &Path,
    ) -> io::Result<Self> {
        let old = if let Some(old_directory) = old_directory.filter(|path| path.exists()) {
            fs::rename(old_directory, backup)?;
            Some((old_directory.to_path_buf(), backup.to_path_buf()))
        } else {
            None
        };

        if target.exists() {
            if let Some((original, _)) = &old {
                if target != original {
                    let error = io::Error::new(
                        io::ErrorKind::AlreadyExists,
                        format!("plugin destination already exists: {}", target.display()),
                    );
                    if let Some((original, backup)) = &old
                        && let Err(restore_error) = fs::rename(backup, original)
                    {
                        return Err(io::Error::other(format!(
                            "{error}; could not restore old plugin directory {}: {restore_error}",
                            original.display()
                        )));
                    }
                    return Err(error);
                }
            } else {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    format!("plugin destination already exists: {}", target.display()),
                ));
            }
        }

        if let Err(error) = fs::rename(stage, target) {
            if let Some((original, backup)) = &old
                && let Err(restore_error) = fs::rename(backup, original)
            {
                return Err(io::Error::other(format!(
                    "{error}; could not restore old plugin directory {}: {restore_error}",
                    original.display()
                )));
            }
            return Err(error);
        }

        Ok(Self {
            stage: stage.to_path_buf(),
            target: target.to_path_buf(),
            old,
            active: true,
        })
    }

    fn rollback(&mut self) -> io::Result<()> {
        let mut first_error = None;
        if self.target.exists()
            && let Err(error) = fs::rename(&self.target, &self.stage)
        {
            first_error = Some(error);
        }
        if let Some((original, backup)) = &self.old
            && backup.exists()
            && let Err(error) = fs::rename(backup, original)
        {
            first_error.get_or_insert(error);
        }
        match first_error {
            Some(error) => Err(error),
            None => {
                self.active = false;
                Ok(())
            }
        }
    }

    fn finish(mut self) -> io::Result<()> {
        self.active = false;
        if let Some((_, backup)) = &self.old
            && backup.exists()
        {
            fs::remove_dir_all(backup)?;
        }
        Ok(())
    }
}

impl Drop for FilesystemReplacement {
    fn drop(&mut self) {
        if self.active
            && let Err(error) = self.rollback()
        {
            eprintln!("Could not roll back interrupted plugin filesystem replacement: {error}");
        }
    }
}

#[derive(Clone)]
struct PluginCandidate {
    name: String,
    requires_root: bool,
}

pub struct PluginManager {
    install_requests: Mutex<HashMap<String, PendingInstall>>,
    operation: Mutex<()>,
}

impl PluginManager {
    pub fn new() -> Self {
        Self {
            install_requests: Mutex::new(HashMap::new()),
            operation: Mutex::new(()),
        }
    }

    pub async fn dispatch(
        &self,
        backend: &Backend,
        route: &str,
        args: Vec<Value>,
    ) -> Option<Result<Value, RpcError>> {
        let result = match route {
            "utilities/install_plugin" => self
                .request_single(backend, &args)
                .await
                .map(|_| Value::Null),
            "utilities/install_plugins" => self
                .request_batch(backend, &args)
                .await
                .map(|_| Value::Null),
            "utilities/confirm_plugin_install" => self.confirm_install(backend, &args).await,
            "utilities/cancel_plugin_install" => self.cancel_install(&args).await,
            "utilities/uninstall_plugin" => self.uninstall_plugin(backend, &args).await,
            "utilities/enable_plugin" => self.enable_plugin(backend, &args).await,
            "utilities/disable_plugin" => self.disable_plugin(backend, &args).await,
            "utilities/set_all_plugins_disabled" => {
                self.set_all_plugins_disabled(backend, &args).await
            }
            _ => return None,
        };
        Some(result)
    }

    async fn request_single(&self, backend: &Backend, args: &[Value]) -> Result<(), RpcError> {
        if args.len() > 5 {
            return Err(RpcError::invalid(format!(
                "expected at most 5 arguments, received {}",
                args.len()
            )));
        }
        let request = InstallRequest {
            artifact: optional_string(args, 0, "")?,
            name: optional_string(args, 1, "No name")?,
            version: optional_string(args, 2, "dev")?,
            hash: args
                .get(3)
                .cloned()
                .unwrap_or_else(|| Value::String(String::new())),
            install_type: optional_install_type(args, 4, 0)?,
        };
        let request_id = Uuid::new_v4().to_string();
        self.install_requests
            .lock()
            .await
            .insert(request_id.clone(), PendingInstall::Single(request.clone()));
        backend.emit(
            "loader/add_plugin_install_prompt",
            vec![
                Value::String(request.name),
                Value::String(request.version),
                Value::String(request_id),
                request.hash,
                Value::from(request.install_type),
            ],
        );
        Ok(())
    }

    async fn request_batch(&self, backend: &Backend, args: &[Value]) -> Result<(), RpcError> {
        if args.len() != 1 {
            return Err(RpcError::invalid(format!(
                "expected 1 argument, received {}",
                args.len()
            )));
        }
        let values = args[0]
            .as_array()
            .ok_or_else(|| RpcError::invalid("argument 0 must be an array of plugin requests"))?;
        let requests = values
            .iter()
            .map(parse_batch_request)
            .collect::<Result<Vec<_>, _>>()?;
        let request_id = Uuid::new_v4().to_string();
        self.install_requests
            .lock()
            .await
            .insert(request_id.clone(), PendingInstall::Batch(requests.clone()));
        backend.emit(
            "loader/add_multiple_plugins_install_prompt",
            vec![
                Value::String(request_id),
                Value::Array(requests.iter().map(InstallRequest::to_json).collect()),
            ],
        );
        Ok(())
    }

    async fn confirm_install(&self, backend: &Backend, args: &[Value]) -> Result<Value, RpcError> {
        exact(args, 1)?;
        let request_id = string(args, 0)?;
        let pending = self
            .install_requests
            .lock()
            .await
            .remove(request_id)
            .ok_or_else(|| {
                RpcError::invalid(format!("Unknown plugin install request {request_id}"))
            })?;
        let _operation = self.operation.lock().await;
        match pending {
            PendingInstall::Single(request) => self.install_one(backend, request).await?,
            PendingInstall::Batch(requests) => {
                for request in requests {
                    self.install_one(backend, request).await?;
                }
            }
        }
        Ok(Value::Null)
    }

    async fn cancel_install(&self, args: &[Value]) -> Result<Value, RpcError> {
        exact(args, 1)?;
        let request_id = string(args, 0)?;
        self.install_requests
            .lock()
            .await
            .remove(request_id)
            .ok_or_else(|| {
                RpcError::invalid(format!("Unknown plugin install request {request_id}"))
            })?;
        Ok(Value::Null)
    }

    async fn install_one(
        &self,
        backend: &Backend,
        request: InstallRequest,
    ) -> Result<(), RpcError> {
        backend.emit(
            "loader/plugin_download_start",
            vec![Value::String(request.name.clone())],
        );
        download_info(backend, 5, "Store.download_progress_info.start");
        let _pause = backend.registry.pause_live_reload();
        let result = self.perform_install(backend, &request).await;
        if let Ok(installed_name) = &result {
            backend.emit(
                "loader/plugin_download_finish",
                vec![Value::String(installed_name.clone())],
            );
        }
        result.map(|_| ())
    }

    async fn perform_install(
        &self,
        backend: &Backend,
        request: &InstallRequest,
    ) -> Result<String, RpcError> {
        let plugin_root = canonical_plugin_root(backend.registry.plugin_root())?;
        let already_installed_by_request_name =
            find_plugin_directory(&plugin_root, &request.name)?.is_some();
        let archive = if let Some(local_path) = request.artifact.strip_prefix("file://") {
            download_info(backend, 10, "Store.download_progress_info.open_zip");
            Bytes::from(tokio::fs::read(local_path).await.map_err(|error| {
                RpcError::failure(format!(
                    "Could not open local plugin archive {local_path}: {error}"
                ))
            })?)
        } else {
            download_info(backend, 10, "Store.download_progress_info.download_zip");
            let download_client = decoding_download_client(backend)?;
            let response = download_client
                .get(&request.artifact)
                .send()
                .await
                .map_err(|error| {
                    RpcError::failure(format!(
                        "Could not download plugin archive {}: {error}",
                        request.artifact
                    ))
                })?;
            if response.status() != reqwest::StatusCode::OK {
                let status = response.status();
                let detail = response.text().await.unwrap_or_default();
                return Err(RpcError::failure(format!(
                    "Could not fetch plugin archive {}: HTTP {status}: {detail}",
                    request.artifact
                )));
            }
            let bytes = response.bytes().await.map_err(|error| {
                RpcError::failure(format!(
                    "Could not read plugin archive {}: {error}",
                    request.artifact
                ))
            })?;
            download_info(backend, 70, "Store.download_progress_info.increment_count");
            increment_install_count(backend, request, already_installed_by_request_name).await;
            bytes
        };

        download_info(backend, 75, "Store.download_progress_info.parse_zip");
        verify_archive_hash(&archive, &request.hash)?;
        let archive_directory = inspect_archive(&archive)?;
        download_info(
            backend,
            80,
            "Store.download_progress_info.uninstalling_previous",
        );
        download_info(
            backend,
            90,
            "Store.download_progress_info.installing_plugin",
        );
        let stage = StagedPlugin::create(&plugin_root, &backend.homebrew)?;
        extract_archive(&archive, &archive_directory, &stage.path)?;
        let candidate = read_plugin_candidate(&stage.path)?;
        if request.version != "dev" && candidate.name != request.name {
            return Err(RpcError::failure(format!(
                "Plugin archive contains {}, expected {}",
                candidate.name, request.name
            )));
        }
        let installed_name = candidate.name.clone();
        if candidate.requires_root && platform::effective_user_ids().0 != 0 {
            // Root plugin files must come from root, not from this account's download.
            drop(stage);
            self.install_root_plugin(backend, request, &archive_directory, &installed_name)
                .await?;
            return Ok(installed_name);
        }
        download_info(backend, 95, "Store.download_progress_info.download_remote");
        download_remote_binaries(backend, &stage.path).await?;
        let stage_name = stage.directory_name()?.to_owned();
        backend
            .registry
            .validate_directory(&stage_name)
            .await
            .map_err(worker_error)?;
        self.commit_staged_plugin(backend, &installed_name, &archive_directory, &stage)
            .await?;
        Ok(installed_name)
    }

    /// Installs or updates a plugin that needs root through the root plugin host.
    /// The host downloads the archive itself and accepts only store URLs and the
    /// store's SHA-256, so this account cannot hand it other code.
    async fn install_root_plugin(
        &self,
        backend: &Backend,
        request: &InstallRequest,
        directory: &str,
        name: &str,
    ) -> Result<(), RpcError> {
        let store_only = || {
            RpcError::failure("Plugins that need root can only be installed from the Decky store")
        };
        let sha256 = request
            .hash
            .as_str()
            .filter(|hash| !hash.is_empty())
            .ok_or_else(store_only)?;
        if !request.artifact.starts_with("https://") {
            return Err(store_only());
        }
        let host = backend.registry.root_host().await.map_err(worker_error)?;
        let _transaction = backend
            .registry
            .begin_install_transaction()
            .map_err(worker_error)?;
        let plugin_root = canonical_plugin_root(backend.registry.plugin_root())?;
        let registered_directory = backend.registry.directory(name).await;
        let was_registered = registered_directory.is_some();
        let old_directory = match registered_directory {
            Some(directory) => Some(checked_plugin_directory(&plugin_root, &directory)?),
            None => find_plugin_directory(&plugin_root, name)?,
        };
        let target = plugin_root.join(directory);
        match &old_directory {
            Some(old) if old != &target => {
                return Err(RpcError::failure(format!(
                    "Plugin {name} now uses the folder {directory}; uninstall it, then install it again"
                )));
            }
            None if target.exists() => {
                return Err(RpcError::failure(format!(
                    "Plugin directory {} is already occupied",
                    target.display()
                )));
            }
            _ => {}
        }
        let was_installed = was_registered || old_directory.is_some();
        let before_settings = if was_registered {
            Some(read_settings_lists(backend, &SETTINGS_KEYS).await?)
        } else if !was_installed {
            Some(read_settings_lists(backend, &["pluginOrder"]).await?)
        } else {
            None
        };
        if was_registered {
            backend
                .registry
                .remove(name, true)
                .await
                .map_err(worker_error)?;
        } else if was_installed {
            backend.emit("loader/unload_plugin", vec![Value::String(name.to_owned())]);
        }

        download_info(
            backend,
            95,
            "Store.download_progress_info.installing_plugin",
        );
        if let Err(error) = host
            .install(directory, name, sha256, &request.artifact)
            .await
        {
            let restore_error = if was_registered {
                restore_registered_plugin(backend, old_directory.as_deref())
                    .await
                    .err()
            } else {
                None
            };
            return Err(RpcError::failure(with_rollback_error(
                format!("Could not install {name}: {error:#}"),
                restore_error,
            )));
        }

        if let Some(before) = &before_settings {
            let mut after = before.clone();
            if was_registered {
                for key in SETTINGS_KEYS {
                    remove_first(after.get_mut(key), name);
                }
            } else {
                after.get_mut("pluginOrder").push(name.to_owned());
            }
            persist_settings_lists(backend, before, &after).await?;
        }
        backend
            .registry
            .import_directory(directory, false, false)
            .await
            .map_err(|error| {
                RpcError::failure(format!("Could not load installed plugin {name}: {error}"))
            })
    }

    async fn commit_staged_plugin(
        &self,
        backend: &Backend,
        name: &str,
        archive_directory: &str,
        stage: &StagedPlugin,
    ) -> Result<(), RpcError> {
        let _transaction = backend
            .registry
            .begin_install_transaction()
            .map_err(worker_error)?;
        let plugin_root = canonical_plugin_root(backend.registry.plugin_root())?;
        let registered_directory = backend.registry.directory(name).await;
        let was_registered = registered_directory.is_some();
        let registered_directory = match registered_directory {
            Some(directory) if directory.is_dir() => {
                Some(checked_plugin_directory(&plugin_root, &directory)?)
            }
            Some(_) => {
                return Err(RpcError::failure(format!(
                    "Registered plugin {name} has no usable directory"
                )));
            }
            None => None,
        };
        let disk_directory = find_plugin_directory(&plugin_root, name)?;
        let old_directory = registered_directory.or(disk_directory);
        let was_installed = was_registered || old_directory.is_some();
        let target = plugin_root.join(archive_directory);
        if target.exists()
            && old_directory
                .as_ref()
                .is_none_or(|old_directory| old_directory != &target)
        {
            return Err(RpcError::failure(format!(
                "Plugin directory {} is already occupied",
                target.display()
            )));
        }

        let before_settings = if was_registered {
            Some(read_settings_lists(backend, &SETTINGS_KEYS).await?)
        } else if !was_installed {
            Some(read_settings_lists(backend, &["pluginOrder"]).await?)
        } else {
            None
        };
        if was_registered {
            backend
                .registry
                .remove(name, true)
                .await
                .map_err(worker_error)?;
        } else if was_installed {
            backend.emit("loader/unload_plugin", vec![Value::String(name.to_owned())]);
        }

        let backup = plugin_root.join(format!(".decky-backup-{}", Uuid::new_v4().simple()));
        let mut replacement = match FilesystemReplacement::activate(
            &stage.path,
            &target,
            old_directory.as_deref(),
            &backup,
        ) {
            Ok(replacement) => replacement,
            Err(error) => {
                let restore_error = if was_registered {
                    restore_registered_plugin(backend, old_directory.as_deref())
                        .await
                        .err()
                } else {
                    None
                };
                return Err(RpcError::failure(with_rollback_error(
                    format!("Could not replace plugin files: {error}"),
                    restore_error,
                )));
            }
        };

        if let Some(before) = &before_settings {
            let mut after = before.clone();
            if was_registered {
                for key in SETTINGS_KEYS {
                    remove_first(after.get_mut(key), name);
                }
            } else if !was_installed {
                after.get_mut("pluginOrder").push(name.to_owned());
            }
            if let Err(error) = persist_settings_lists(backend, before, &after).await {
                let rollback_error = self
                    .rollback_install(
                        backend,
                        &mut replacement,
                        before,
                        was_registered,
                        old_directory.as_deref(),
                    )
                    .await
                    .err();
                return Err(RpcError::failure(with_rollback_error(
                    rpc_error_text(&error),
                    rollback_error,
                )));
            }
        }

        if let Err(error) = backend
            .registry
            .import_directory(archive_directory, false, false)
            .await
        {
            let rollback_error = if let Some(before) = &before_settings {
                self.rollback_install(
                    backend,
                    &mut replacement,
                    before,
                    was_registered,
                    old_directory.as_deref(),
                )
                .await
                .err()
            } else {
                replacement.rollback().err().map(|error| error.to_string())
            };
            return Err(RpcError::failure(with_rollback_error(
                format!("Could not load installed plugin {name}: {error}"),
                rollback_error,
            )));
        }

        replacement.finish().map_err(|error| {
            RpcError::failure(format!(
                "Plugin {name} was installed but its previous directory could not be removed: {error}"
            ))
        })
    }

    async fn rollback_install(
        &self,
        backend: &Backend,
        replacement: &mut FilesystemReplacement,
        before_settings: &SettingsLists,
        was_registered: bool,
        old_directory: Option<&Path>,
    ) -> Result<(), String> {
        let mut failures = Vec::new();
        if let Err(error) = restore_settings_lists(backend, before_settings).await {
            failures.push(rpc_error_text(&error));
        }
        if let Err(error) = replacement.rollback() {
            failures.push(format!("restoring plugin files failed: {error}"));
        }
        if was_registered
            && let Err(error) = restore_registered_plugin(backend, old_directory).await
        {
            failures.push(error);
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(failures.join("; "))
        }
    }

    async fn uninstall_plugin(&self, backend: &Backend, args: &[Value]) -> Result<Value, RpcError> {
        exact(args, 1)?;
        let name = string(args, 0)?;
        let _operation = self.operation.lock().await;
        let _pause = backend.registry.pause_live_reload();
        let plugin_root = canonical_plugin_root(backend.registry.plugin_root())?;
        let registered_directory = backend.registry.directory(name).await;
        let was_registered = registered_directory.is_some();
        let registered_directory = match registered_directory {
            Some(directory) if directory.is_dir() => {
                Some(checked_plugin_directory(&plugin_root, &directory)?)
            }
            Some(_) => None,
            None => None,
        };
        let disk_directory = find_plugin_directory(&plugin_root, name)?;
        let directory = registered_directory
            .or(disk_directory)
            .ok_or_else(|| RpcError::failure(format!("Plugin {name} is not installed")))?;
        let before_settings = if was_registered {
            Some(read_settings_lists(backend, &SETTINGS_KEYS).await?)
        } else {
            None
        };

        if was_registered {
            backend
                .registry
                .remove(name, true)
                .await
                .map_err(worker_error)?;
        } else {
            backend.emit("loader/unload_plugin", vec![Value::String(name.to_owned())]);
        }

        if let Some(before) = &before_settings {
            let mut after = before.clone();
            for key in SETTINGS_KEYS {
                remove_first(after.get_mut(key), name);
            }
            if let Err(error) = persist_settings_lists(backend, before, &after).await {
                let restore_error = restore_registered_plugin(backend, Some(&directory))
                    .await
                    .err();
                return Err(RpcError::failure(with_rollback_error(
                    rpc_error_text(&error),
                    restore_error,
                )));
            }
        }

        if let Err(error) = remove_plugin_directory(backend, &directory).await {
            let settings_error = if let Some(before) = &before_settings {
                restore_settings_lists(backend, before).await.err()
            } else {
                None
            };
            let plugin_error = if was_registered {
                restore_registered_plugin(backend, Some(&directory))
                    .await
                    .err()
            } else {
                None
            };
            let rollback_error = settings_error
                .map(|error| rpc_error_text(&error))
                .into_iter()
                .chain(plugin_error)
                .collect::<Vec<_>>();
            return Err(RpcError::failure(with_rollback_error(
                format!(
                    "Could not remove plugin directory {}: {error}",
                    directory.display()
                ),
                (!rollback_error.is_empty()).then(|| rollback_error.join("; ")),
            )));
        }
        Ok(Value::Null)
    }

    async fn disable_plugin(&self, backend: &Backend, args: &[Value]) -> Result<Value, RpcError> {
        exact(args, 1)?;
        let name = string(args, 0)?;
        let _operation = self.operation.lock().await;
        let before = read_settings_lists(backend, &["disabled_plugins"]).await?;
        if before
            .get("disabled_plugins")
            .iter()
            .any(|value| value == name)
        {
            return Ok(Value::Null);
        }
        let mut after = before.clone();
        after.get_mut("disabled_plugins").push(name.to_owned());
        persist_settings_lists(backend, &before, &after).await?;
        if let Err(error) = backend.registry.set_disabled(name, true).await {
            let restore_error = restore_settings_lists(backend, &before).await.err();
            return Err(RpcError::failure(with_rollback_error(
                error.to_string(),
                restore_error.map(|error| rpc_error_text(&error)),
            )));
        }
        Ok(Value::Null)
    }

    async fn enable_plugin(&self, backend: &Backend, args: &[Value]) -> Result<Value, RpcError> {
        exact(args, 1)?;
        let name = string(args, 0)?;
        let _operation = self.operation.lock().await;
        let plugin_root = canonical_plugin_root(backend.registry.plugin_root())?;
        let registered_directory = backend.registry.directory(name).await;
        let was_registered = registered_directory.is_some();
        let directory = match registered_directory {
            Some(directory) if directory.is_dir() => {
                checked_plugin_directory(&plugin_root, &directory)?
            }
            Some(_) => {
                return Err(RpcError::failure(format!(
                    "Registered plugin {name} has no usable directory"
                )));
            }
            None => find_plugin_directory(&plugin_root, name)?
                .ok_or_else(|| RpcError::failure(format!("Plugin {name} is not installed")))?,
        };
        let folder = directory
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| RpcError::failure("Plugin directory name is not valid UTF-8"))?
            .to_owned();
        let before = read_settings_lists(backend, &["disabled_plugins"]).await?;
        let mut after = before.clone();
        remove_first(after.get_mut("disabled_plugins"), name);
        persist_settings_lists(backend, &before, &after).await?;

        let result = if was_registered {
            backend.registry.set_disabled(name, false).await
        } else {
            backend
                .registry
                .import_directory(&folder, false, false)
                .await
        };
        if let Err(error) = result {
            let restore_error = restore_settings_lists(backend, &before).await.err();
            return Err(RpcError::failure(with_rollback_error(
                error.to_string(),
                restore_error.map(|error| rpc_error_text(&error)),
            )));
        }
        Ok(Value::Null)
    }

    async fn set_all_plugins_disabled(
        &self,
        backend: &Backend,
        args: &[Value],
    ) -> Result<Value, RpcError> {
        exact(args, 0)?;
        let _operation = self.operation.lock().await;
        let before = read_settings_lists(backend, &["disabled_plugins"]).await?;
        let plugins = backend.registry.list().await;
        let mut after = before.clone();
        for plugin in plugins.as_array().into_iter().flatten() {
            if let Some(name) = plugin.get("name").and_then(Value::as_str)
                && !after
                    .get("disabled_plugins")
                    .iter()
                    .any(|disabled| disabled == name)
            {
                after.get_mut("disabled_plugins").push(name.to_owned());
            }
        }
        persist_settings_lists(backend, &before, &after).await?;
        Ok(Value::Null)
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

fn optional_string(args: &[Value], index: usize, default: &str) -> Result<String, RpcError> {
    match args.get(index) {
        None => Ok(default.to_owned()),
        Some(Value::String(value)) => Ok(value.clone()),
        Some(_) => Err(RpcError::invalid(format!(
            "argument {index} must be a string"
        ))),
    }
}

fn optional_install_type(args: &[Value], index: usize, default: u64) -> Result<u64, RpcError> {
    match args.get(index) {
        None => Ok(default),
        Some(value) => parse_install_type(value),
    }
}

fn parse_install_type(value: &Value) -> Result<u64, RpcError> {
    value
        .as_u64()
        .filter(|install_type| *install_type <= 4)
        .ok_or_else(|| RpcError::invalid("install type must be an integer from 0 through 4"))
}

fn parse_batch_request(value: &Value) -> Result<InstallRequest, RpcError> {
    let object = value
        .as_object()
        .ok_or_else(|| RpcError::invalid("each plugin install request must be an object"))?;
    let required_string = |key: &str| {
        object
            .get(key)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| {
                RpcError::invalid(format!("plugin install request requires string {key}"))
            })
    };
    let hash = object
        .get("hash")
        .cloned()
        .unwrap_or_else(|| Value::String(String::new()));
    let install_type = object
        .get("install_type")
        .or_else(|| object.get("installType"))
        .map(parse_install_type)
        .transpose()?
        .unwrap_or(0);
    Ok(InstallRequest {
        artifact: required_string("artifact")?,
        name: required_string("name")?,
        version: required_string("version")?,
        hash,
        install_type,
    })
}

fn download_info(backend: &Backend, percent: u64, translation: &str) {
    backend.emit(
        "loader/plugin_download_info",
        vec![Value::from(percent), Value::String(translation.to_owned())],
    );
}

/// Starts the store install-count POST in the background; the install does not wait for it.
async fn increment_install_count(backend: &Backend, request: &InstallRequest, is_update: bool) {
    let store = backend.setting("store", json!(0)).await;
    let store_url = match store.as_u64().unwrap_or(0) {
        1 => "https://testing.deckbrew.xyz/plugins".to_owned(),
        2 => backend
            .setting("store-url", json!("https://plugins.deckbrew.xyz/plugins"))
            .await
            .as_str()
            .unwrap_or("https://plugins.deckbrew.xyz/plugins")
            .to_owned(),
        _ => "https://plugins.deckbrew.xyz/plugins".to_owned(),
    };
    let url = format!(
        "{}/{}/versions/{}/increment?isUpdate={}",
        store_url.trim_end_matches('/'),
        encode_path_segment(&request.name),
        encode_path_segment(&request.version),
        if is_update { "true" } else { "false" },
    );
    let client = backend.client.clone();
    tokio::spawn(async move { send_install_count(&client, url, INSTALL_COUNT_TIMEOUT).await });
}

// The count is informational. The timeout only bounds the background task.
const INSTALL_COUNT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

async fn send_install_count(client: &reqwest::Client, url: String, timeout: std::time::Duration) {
    match client.post(&url).timeout(timeout).send().await {
        Ok(response) if response.status().is_success() => {}
        Ok(response) => eprintln!(
            "Plugin store did not accept the install count ({})",
            response.status()
        ),
        Err(error) => {
            eprintln!("Could not send the install count: {error}")
        }
    }
}

fn encode_path_segment(segment: &str) -> String {
    let mut result = String::with_capacity(segment.len());
    for byte in segment.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            result.push(byte as char);
        } else {
            result.push('%');
            result.push_str(&format!("{byte:02X}"));
        }
    }
    result
}

fn verify_archive_hash(archive: &[u8], expected: &Value) -> Result<(), RpcError> {
    let expected = match expected {
        Value::String(value) if !value.is_empty() => Some(value.as_str()),
        Value::Bool(false) | Value::Null => None,
        Value::String(_) => None,
        _ => {
            return Err(RpcError::invalid(
                "plugin archive hash must be a SHA-256 string or false",
            ));
        }
    };
    if let Some(expected) = expected {
        let actual = format!("{:x}", Sha256::digest(archive));
        if actual != expected {
            return Err(RpcError::failure(format!(
                "SHA-256 mismatch for plugin archive: expected {expected}, received {actual}"
            )));
        }
    }
    Ok(())
}

fn inspect_archive(archive_bytes: &[u8]) -> Result<String, RpcError> {
    let mut archive = ZipArchive::new(Cursor::new(archive_bytes)).map_err(|error| {
        RpcError::failure(format!("Could not read plugin ZIP archive: {error}"))
    })?;
    let mut plugin_roots = HashSet::new();
    for index in 0..archive.len() {
        let entry = archive.by_index(index).map_err(|error| {
            RpcError::failure(format!("Could not inspect plugin ZIP entry: {error}"))
        })?;
        let entry_name = entry.name().map_err(|error| {
            RpcError::failure(format!("Could not read plugin ZIP entry name: {error}"))
        })?;
        let path = safe_archive_path(entry_name.as_ref())?;
        let components = path.components().collect::<Vec<_>>();
        if components.len() == 2 && components[1].as_os_str() == "plugin.json" && !entry.is_dir() {
            plugin_roots.insert(components[0].as_os_str().to_string_lossy().into_owned());
        }
    }
    if plugin_roots.len() != 1 {
        return Err(RpcError::failure(format!(
            "Plugin ZIP must contain exactly one top-level plugin.json, found {}",
            plugin_roots.len()
        )));
    }
    Ok(plugin_roots.into_iter().next().expect("one plugin root"))
}

fn safe_archive_path(name: &str) -> Result<PathBuf, RpcError> {
    if name.is_empty() || name.starts_with('/') || name.starts_with('\\') || name.contains('\\') {
        return Err(RpcError::failure(format!(
            "Unsafe path in plugin ZIP archive: {name}"
        )));
    }
    let trimmed = name.strip_suffix('/').unwrap_or(name);
    if trimmed.is_empty() {
        return Err(RpcError::failure("Plugin ZIP contains an empty path"));
    }
    let mut path = PathBuf::new();
    for (index, component) in trimmed.split('/').enumerate() {
        if component.is_empty()
            || component == "."
            || component == ".."
            || (index == 0 && component.contains(':'))
        {
            return Err(RpcError::failure(format!(
                "Unsafe path in plugin ZIP archive: {name}"
            )));
        }
        path.push(component);
    }
    if path
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(RpcError::failure(format!(
            "Unsafe path in plugin ZIP archive: {name}"
        )));
    }
    Ok(path)
}

fn extract_archive(
    archive_bytes: &[u8],
    archive_directory: &str,
    stage: &Path,
) -> Result<(), RpcError> {
    let mut archive = ZipArchive::new(Cursor::new(archive_bytes)).map_err(|error| {
        RpcError::failure(format!("Could not read plugin ZIP archive: {error}"))
    })?;
    let mut paths = HashSet::new();
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|error| {
            RpcError::failure(format!("Could not extract plugin ZIP entry: {error}"))
        })?;
        let entry_name = entry.name().map_err(|error| {
            RpcError::failure(format!("Could not read plugin ZIP entry name: {error}"))
        })?;
        let path = safe_archive_path(entry_name.as_ref())?;
        let mut components = path.components();
        let Some(Component::Normal(root)) = components.next() else {
            return Err(RpcError::failure(
                "Plugin ZIP entry has no top-level directory",
            ));
        };
        if root != archive_directory {
            return Err(RpcError::failure(format!(
                "Plugin ZIP entry is outside its plugin directory: {}",
                entry_name
            )));
        }
        let relative = components.as_path();
        if relative.as_os_str().is_empty() {
            if !entry.is_dir() {
                return Err(RpcError::failure(
                    "Plugin ZIP root entry is not a directory",
                ));
            }
            continue;
        }
        if entry
            .unix_mode()
            .is_some_and(|mode| mode & 0o170000 == 0o120000)
        {
            return Err(RpcError::failure(format!(
                "Plugin ZIP contains a symbolic link: {}",
                entry_name
            )));
        }
        let relative_name = relative.to_string_lossy().into_owned();
        if !paths.insert(relative_name.clone()) {
            return Err(RpcError::failure(format!(
                "Plugin ZIP contains duplicate entry: {relative_name}"
            )));
        }
        let output = stage.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(&output).map_err(|error| {
                RpcError::failure(format!("Could not create {}: {error}", output.display()))
            })?;
            continue;
        }
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent).map_err(|error| {
                RpcError::failure(format!("Could not create {}: {error}", parent.display()))
            })?;
        }
        let mut output_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&output)
            .map_err(|error| {
                RpcError::failure(format!("Could not create {}: {error}", output.display()))
            })?;
        io::copy(&mut entry, &mut output_file).map_err(|error| {
            RpcError::failure(format!("Could not extract {}: {error}", output.display()))
        })?;
    }
    Ok(())
}

fn read_plugin_candidate(directory: &Path) -> Result<PluginCandidate, RpcError> {
    let plugin_json = read_json_file(&directory.join("plugin.json"))?;
    let object = plugin_json
        .as_object()
        .ok_or_else(|| RpcError::failure("plugin.json must contain an object"))?;
    let name = object
        .get("name")
        .and_then(Value::as_str)
        .filter(|name| !name.trim().is_empty())
        .ok_or_else(|| RpcError::failure("plugin.json must contain a non-empty name"))?;
    if object.get("author").and_then(Value::as_str).is_none() {
        return Err(RpcError::failure(
            "plugin.json must contain a string author",
        ));
    }
    let flags = object
        .get("flags")
        .and_then(Value::as_array)
        .ok_or_else(|| RpcError::failure("plugin.json flags must be an array"))?;
    if flags.iter().any(|flag| !flag.is_string()) {
        return Err(RpcError::failure("plugin.json flags must contain strings"));
    }
    if object
        .get("api_version")
        .is_some_and(|api_version| api_version.as_u64().is_none())
    {
        return Err(RpcError::failure(
            "plugin.json api_version must be a non-negative integer",
        ));
    }
    if directory.join("package.json").is_file() {
        let package = read_json_file(&directory.join("package.json"))?;
        if package.get("version").and_then(Value::as_str).is_none() {
            return Err(RpcError::failure(
                "package.json must contain a string version",
            ));
        }
    }
    let requires_root = flags.iter().any(|flag| flag.as_str() == Some("root"));
    Ok(PluginCandidate {
        name: name.to_owned(),
        requires_root,
    })
}

/// Removes a plugin folder. Folders that root owns (approved root plugins) are
/// removed by the root plugin host, because this account cannot change them.
async fn remove_plugin_directory(backend: &Backend, directory: &Path) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;

    let owner = fs::symlink_metadata(directory)
        .map_err(|error| error.to_string())?
        .uid();
    if owner == 0 && platform::effective_user_ids().0 != 0 {
        let name = directory
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| "plugin folder name is not valid UTF-8".to_owned())?;
        let host = backend
            .registry
            .root_host()
            .await
            .map_err(|error| error.to_string())?;
        return host
            .uninstall(name)
            .await
            .map_err(|error| format!("{error:#}"));
    }
    tokio::fs::remove_dir_all(directory)
        .await
        .map_err(|error| error.to_string())
}

fn read_json_file(path: &Path) -> Result<Value, RpcError> {
    let bytes = fs::read(path).map_err(|error| {
        RpcError::failure(format!("Could not read {}: {error}", path.display()))
    })?;
    serde_json::from_slice(&bytes)
        .map_err(|error| RpcError::failure(format!("Invalid JSON in {}: {error}", path.display())))
}

fn decoding_download_client(backend: &Backend) -> Result<reqwest::Client, RpcError> {
    reqwest::Client::builder()
        .user_agent(format!("Decky/{} (https://decky.xyz)", backend.version))
        .no_proxy()
        .gzip(true)
        .brotli(true)
        .deflate(true)
        .build()
        .map_err(|error| {
            RpcError::failure(format!(
                "Could not create artifact download client: {error}"
            ))
        })
}

async fn download_remote_binaries(
    backend: &Backend,
    plugin_directory: &Path,
) -> Result<(), RpcError> {
    let package_path = plugin_directory.join("package.json");
    if !package_path.is_file() {
        return Ok(());
    }
    let package = read_json_file(&package_path)?;
    let Some(remote_binaries) = package.get("remote_binary") else {
        return Ok(());
    };
    let remote_binaries = remote_binaries
        .as_array()
        .ok_or_else(|| RpcError::failure("package.json remote_binary must be an array"))?;
    if remote_binaries.is_empty() {
        return Ok(());
    }
    let download_client = decoding_download_client(backend)?;
    let bin_directory = plugin_directory.join("bin");
    fs::create_dir_all(&bin_directory).map_err(|error| {
        RpcError::failure(format!(
            "Could not create {}: {error}",
            bin_directory.display()
        ))
    })?;
    for remote_binary in remote_binaries {
        let object = remote_binary
            .as_object()
            .ok_or_else(|| RpcError::failure("remote_binary entries must be objects"))?;
        let name = object
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| RpcError::failure("remote_binary entry is missing string name"))?;
        let url = object.get("url").and_then(Value::as_str).ok_or_else(|| {
            RpcError::failure(format!("remote_binary {name} is missing string url"))
        })?;
        let expected_hash = object
            .get("sha256hash")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                RpcError::failure(format!("remote_binary {name} is missing string sha256hash"))
            })?;
        validate_binary_name(name)?;
        let response = download_client.get(url).send().await.map_err(|error| {
            RpcError::failure(format!(
                "Could not download remote binary {name} from {url}: {error}"
            ))
        })?;
        if response.status() != reqwest::StatusCode::OK {
            return Err(RpcError::failure(format!(
                "Could not download remote binary {name} from {url}: HTTP {}",
                response.status()
            )));
        }
        let bytes = response.bytes().await.map_err(|error| {
            RpcError::failure(format!(
                "Could not read remote binary {name} from {url}: {error}"
            ))
        })?;
        let actual_hash = format!("{:x}", Sha256::digest(&bytes));
        if actual_hash != expected_hash {
            return Err(RpcError::failure(format!(
                "SHA-256 mismatch for remote binary {name}: expected {expected_hash}, received {actual_hash}"
            )));
        }
        tokio::fs::write(bin_directory.join(name), &bytes)
            .await
            .map_err(|error| {
                RpcError::failure(format!("Could not write remote binary {name}: {error}"))
            })?;
    }
    Ok(())
}

fn validate_binary_name(name: &str) -> Result<(), RpcError> {
    let path = Path::new(name);
    if name.is_empty()
        || path.components().count() != 1
        || !matches!(path.components().next(), Some(Component::Normal(_)))
        || name.contains('/')
        || name.contains('\\')
    {
        return Err(RpcError::failure(format!(
            "Invalid remote binary filename: {name}"
        )));
    }
    Ok(())
}

fn canonical_plugin_root(root: &Path) -> Result<PathBuf, RpcError> {
    fs::canonicalize(root).map_err(|error| {
        RpcError::failure(format!(
            "Could not resolve plugin directory {}: {error}",
            root.display()
        ))
    })
}

fn checked_plugin_directory(plugin_root: &Path, directory: &Path) -> Result<PathBuf, RpcError> {
    let root = fs::canonicalize(plugin_root).map_err(|error| {
        RpcError::failure(format!(
            "Could not resolve plugin directory {}: {error}",
            plugin_root.display()
        ))
    })?;
    let candidate = if directory.is_absolute() {
        directory.to_path_buf()
    } else {
        root.join(directory)
    };
    let metadata = fs::symlink_metadata(&candidate).map_err(|error| {
        RpcError::failure(format!(
            "Could not inspect plugin directory {}: {error}",
            candidate.display()
        ))
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(RpcError::failure(format!(
            "Plugin directory {} is not a real directory",
            candidate.display()
        )));
    }
    let canonical = fs::canonicalize(&candidate).map_err(|error| {
        RpcError::failure(format!(
            "Could not resolve plugin directory {}: {error}",
            candidate.display()
        ))
    })?;
    if canonical.parent() != Some(root.as_path()) {
        return Err(RpcError::failure(format!(
            "Plugin directory {} is not a direct child of {}",
            canonical.display(),
            root.display()
        )));
    }
    Ok(canonical)
}

fn find_plugin_directory(plugin_root: &Path, name: &str) -> Result<Option<PathBuf>, RpcError> {
    let directories = fs::read_dir(plugin_root).map_err(|error| {
        RpcError::failure(format!(
            "Could not read plugin directory {}: {error}",
            plugin_root.display()
        ))
    })?;
    for entry in directories {
        let entry = entry.map_err(|error| RpcError::failure(error.to_string()))?;
        if !entry
            .file_type()
            .map_err(|error| RpcError::failure(error.to_string()))?
            .is_dir()
        {
            continue;
        }
        if entry.file_name().to_str().is_some_and(|name| {
            name.starts_with(".decky-stage-") || name.starts_with(".decky-backup-")
        }) {
            continue;
        }
        let directory = entry.path();
        let plugin_json = directory.join("plugin.json");
        if read_json_file(&plugin_json)
            .ok()
            .and_then(|value| value.get("name").and_then(Value::as_str).map(str::to_owned))
            .is_some_and(|installed_name| installed_name == name)
        {
            return Ok(Some(directory));
        }
    }
    Ok(None)
}

async fn read_settings_lists(
    backend: &Backend,
    keys: &[&'static str],
) -> Result<SettingsLists, RpcError> {
    let mut values = HashMap::with_capacity(keys.len());
    for &key in keys {
        let setting = backend.setting(key, json!([])).await;
        let array = setting
            .as_array()
            .ok_or_else(|| RpcError::failure(format!("Setting {key} must be an array")))?;
        let mut names = Vec::with_capacity(array.len());
        for value in array {
            names.push(
                value
                    .as_str()
                    .ok_or_else(|| {
                        RpcError::failure(format!("Setting {key} must contain strings"))
                    })?
                    .to_owned(),
            );
        }
        values.insert(key, names);
    }
    Ok(SettingsLists { values })
}

async fn persist_settings_lists(
    backend: &Backend,
    before: &SettingsLists,
    after: &SettingsLists,
) -> Result<(), RpcError> {
    let mut changed: Vec<&'static str> = Vec::new();
    for key in SETTINGS_KEYS {
        if !before.values.contains_key(key) || !after.values.contains_key(key) {
            continue;
        }
        if before.get(key) == after.get(key) {
            continue;
        }
        if let Err(error) = backend.set_setting(key, after.to_value(key)).await {
            let mut rollback_errors = Vec::new();
            for changed_key in changed.into_iter().rev() {
                if let Err(rollback_error) = backend
                    .set_setting(changed_key, before.to_value(changed_key))
                    .await
                {
                    rollback_errors.push(rpc_error_text(&rollback_error));
                }
            }
            if rollback_errors.is_empty() {
                return Err(error);
            }
            return Err(RpcError::failure(with_rollback_error(
                rpc_error_text(&error),
                Some(rollback_errors.join("; ")),
            )));
        }
        changed.push(key);
    }
    Ok(())
}

async fn restore_settings_lists(
    backend: &Backend,
    settings: &SettingsLists,
) -> Result<(), RpcError> {
    for key in SETTINGS_KEYS {
        if !settings.values.contains_key(key) {
            continue;
        }
        backend.set_setting(key, settings.to_value(key)).await?;
    }
    Ok(())
}

fn remove_first(values: &mut Vec<String>, needle: &str) {
    if let Some(index) = values.iter().position(|value| value == needle) {
        values.remove(index);
    }
}

async fn restore_registered_plugin(
    backend: &Backend,
    directory: Option<&Path>,
) -> Result<(), String> {
    let directory = directory.ok_or_else(|| "original plugin directory is missing".to_owned())?;
    let folder = directory
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| "original plugin directory name is not valid UTF-8".to_owned())?;
    backend
        .registry
        .import_directory(folder, false, false)
        .await
        .map_err(|error| error.to_string())
}

fn rpc_error_text(error: &RpcError) -> String {
    match error {
        RpcError::Error { name, message, .. } => format!("{name}: {message}"),
        RpcError::Stopped => "Plugin manager stopped".to_owned(),
    }
}

fn worker_error(error: WorkerError) -> RpcError {
    match error {
        WorkerError::Error(message) => RpcError::failure(message),
        WorkerError::Stopped => RpcError::Stopped,
    }
}

fn with_rollback_error(error: String, rollback_error: Option<String>) -> String {
    match rollback_error {
        Some(rollback_error) => format!("{error}; rollback also failed: {rollback_error}"),
        None => error,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn install_count_gives_up_when_the_store_never_answers() {
        // Accept the connection but never reply, like a stalled store.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (socket, _) = listener.accept().await.unwrap();
            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
            drop(socket);
        });
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let started = std::time::Instant::now();
        send_install_count(
            &client,
            format!("http://{address}/plugin/versions/1/increment?isUpdate=false"),
            std::time::Duration::from_millis(200),
        )
        .await;
        assert!(started.elapsed() < std::time::Duration::from_secs(5));
        server.abort();
    }

    #[test]
    fn archive_paths_reject_escape_and_absolute_forms() {
        for path in [
            "../outside",
            "plugin/../../outside",
            "/tmp/file",
            "C:/escape",
            "plugin\\file",
        ] {
            assert!(safe_archive_path(path).is_err(), "accepted {path:?}");
        }
        assert_eq!(
            safe_archive_path("plugin/dist/index.js").unwrap(),
            PathBuf::from("plugin/dist/index.js")
        );
    }

    #[test]
    fn replacing_live_directory_can_roll_back_without_losing_old_files() {
        let root =
            std::env::temp_dir().join(format!("decky-plugin-replacement-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let old = root.join("plugin");
        let stage = root.join(".stage");
        let target = root.join("plugin");
        let backup = root.join(".backup");
        fs::create_dir(&old).unwrap();
        fs::write(old.join("version"), "old").unwrap();
        fs::create_dir(&stage).unwrap();
        fs::write(stage.join("version"), "new").unwrap();

        let mut replacement =
            FilesystemReplacement::activate(&stage, &target, Some(&old), &backup).unwrap();
        assert_eq!(fs::read_to_string(target.join("version")).unwrap(), "new");
        replacement.rollback().unwrap();
        assert_eq!(fs::read_to_string(old.join("version")).unwrap(), "old");
        assert_eq!(fs::read_to_string(stage.join("version")).unwrap(), "new");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn settings_state_transitions_preserve_source_disable_semantics() {
        let mut settings = SettingsLists {
            values: SETTINGS_KEYS
                .into_iter()
                .map(|key| (key, vec!["already-disabled".to_owned()]))
                .collect(),
        };
        settings
            .get_mut("disabled_plugins")
            .push("enabled".to_owned());
        remove_first(settings.get_mut("disabled_plugins"), "enabled");
        assert_eq!(settings.get("disabled_plugins"), ["already-disabled"]);

        for plugin in ["already-disabled", "enabled"] {
            if !settings
                .get("disabled_plugins")
                .iter()
                .any(|name| name == plugin)
            {
                settings.get_mut("disabled_plugins").push(plugin.to_owned());
            }
        }
        assert_eq!(
            settings.get("disabled_plugins"),
            ["already-disabled", "enabled"]
        );
    }
}
