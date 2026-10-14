use std::{
    collections::{HashMap, HashSet},
    ffi::OsStr,
    fs, io,
    os::fd::{AsFd, OwnedFd},
    os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, PermissionsExt},
        process::{CommandExt, ExitStatusExt},
    },
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
    sync::{
        Arc, Weak,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use serde_json::{Value, json};
use tokio::{
    io::unix::AsyncFd,
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
    process::{Child, Command},
    sync::{Mutex, Notify, broadcast, mpsc, oneshot, watch},
    task::JoinHandle,
    time::{sleep, timeout},
};
use uuid::Uuid;

use crate::{
    launcher::{ForkLauncher, LauncherEnvironment, LauncherStartError},
    platform,
};

const WORKER_READY_TIMEOUT: Duration = Duration::from_secs(30);
const WORKER_GRACEFUL_TIMEOUT: Duration = Duration::from_secs(5);
const SOCKET_RETRY_INTERVAL: Duration = Duration::from_millis(50);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkerError {
    Error(String),
    Stopped,
}

impl std::fmt::Display for WorkerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Error(message) => f.write_str(message),
            Self::Stopped => f.write_str("Plugin has been stopped"),
        }
    }
}

impl std::error::Error for WorkerError {}

#[derive(Clone)]
struct PluginMetadata {
    name: String,
    directory: String,
    version: Option<Value>,
    load_type: u8,
    passive: bool,
    disabled: bool,
    requires_root: bool,
    debug: bool,
}

struct PluginRuntime {
    worker: Option<WorkerProcess>,
    start_error: Option<String>,
}

struct PluginEntry {
    metadata: PluginMetadata,
    disabled: AtomicBool,
    runtime: Mutex<PluginRuntime>,
}

pub struct PluginRegistry {
    plugins: tokio::sync::RwLock<HashMap<String, Arc<PluginEntry>>>,
    mutations: Mutex<()>,
    worker_executable: PathBuf,
    worker_mode: WorkerMode,
    worker_bootstrap: WorkerBootstrap,
    fork_launcher: Option<Arc<ForkLauncher>>,
    plugin_root: PathBuf,
    loader_settings_path: PathBuf,
    homebrew: PathBuf,
    user: String,
    user_ids: (u32, u32),
    auth_token: String,
    server_host: String,
    server_port: u16,
    chown_plugin_path: bool,
    socket_directory: PathBuf,
    events: broadcast::Sender<Value>,
    shutdown: watch::Receiver<bool>,
    shutting_down: AtomicBool,
    live_reload_task: Mutex<Option<JoinHandle<()>>>,
    live_reload_paused: AtomicUsize,
    live_reload_resync: AtomicBool,
    live_reload_notify: Arc<Notify>,
    active_install_transactions: AtomicUsize,
    install_transactions_changed: Notify,
}

pub struct LiveReloadPause<'a> {
    paused: &'a AtomicUsize,
    resync: &'a AtomicBool,
    notify: &'a Notify,
}

impl Drop for LiveReloadPause<'_> {
    fn drop(&mut self) {
        self.paused.fetch_sub(1, Ordering::AcqRel);
        self.resync.store(true, Ordering::Release);
        self.notify.notify_one();
    }
}

pub struct InstallTransaction<'a> {
    active: &'a AtomicUsize,
    changed: &'a Notify,
}

impl Drop for InstallTransaction<'_> {
    fn drop(&mut self) {
        if self.active.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.changed.notify_one();
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerMode {
    Exec,
    Fork,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerBootstrap {
    Full,
    Lean,
}

impl WorkerBootstrap {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Full => "full",
            Self::Lean => "lean",
        }
    }
}

pub struct WorkerConfiguration {
    pub executable: PathBuf,
    pub mode: WorkerMode,
    pub bootstrap: WorkerBootstrap,
    pub auth_token: String,
    pub server_host: String,
    pub server_port: u16,
    pub chown_plugin_path: bool,
}

struct WorkerLaunchConfig<'a> {
    worker_executable: &'a Path,
    worker_mode: WorkerMode,
    worker_bootstrap: WorkerBootstrap,
    fork_launcher: Option<&'a Arc<ForkLauncher>>,
    plugin_root: &'a Path,
    homebrew: &'a Path,
    user: &'a str,
    auth_token: &'a str,
    server_host: &'a str,
    server_port: u16,
    chown_plugin_path: bool,
    socket_directory: &'a Path,
}

struct WorkerProcess {
    handle: WorkerHandle,
    stop: Option<mpsc::UnboundedSender<()>>,
    monitor: Option<JoinHandle<()>>,
}

impl Drop for WorkerProcess {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
    }
}
struct ProcessGroupGuard {
    pid: u32,
}

impl Drop for ProcessGroupGuard {
    fn drop(&mut self) {
        send_group_signal(self.pid, libc::SIGKILL);
    }
}

enum WorkerSupervisor {
    Direct {
        child: Child,
        pid: u32,
        group: ProcessGroupGuard,
    },
    Fork {
        launcher: Arc<ForkLauncher>,
        id: String,
        pid: u32,
        exit: AsyncFd<OwnedFd>,
        group: ProcessGroupGuard,
    },
}

impl WorkerSupervisor {
    async fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        match self {
            Self::Direct { child, pid, .. } => match child.try_wait()? {
                Some(status) => {
                    send_group_signal(*pid, libc::SIGKILL);
                    Ok(Some(status))
                }
                None => Ok(None),
            },
            Self::Fork {
                launcher, id, pid, ..
            } => {
                let status = launcher
                    .child_status(id)
                    .await
                    .map_err(|error| io::Error::other(error.to_string()))?;
                let Some(status) = status else {
                    return Ok(None);
                };
                send_group_signal(*pid, libc::SIGKILL);
                launcher
                    .forget_child(id, *pid)
                    .await
                    .map_err(|error| io::Error::other(error.to_string()))?;
                Ok(Some(ExitStatus::from_raw(status)))
            }
        }
    }

    async fn abort_startup(&mut self, socket_path: &Path) {
        match self {
            Self::Direct { child, pid, .. } => terminate_and_reap(child, *pid).await,
            Self::Fork {
                launcher, id, pid, ..
            } => terminate_fork_child(launcher, id, *pid).await,
        }
        let _ = fs::remove_file(socket_path);
    }

    fn spawn_reaper(self, stop: mpsc::UnboundedReceiver<()>) -> JoinHandle<io::Result<ExitStatus>> {
        match self {
            Self::Direct { child, pid, group } => tokio::spawn(reap_child(child, stop, pid, group)),
            Self::Fork {
                launcher,
                id,
                pid,
                exit,
                group,
            } => tokio::spawn(reap_fork_child(stop, launcher, id, pid, exit, group)),
        }
    }
}

enum WorkerLaunchError {
    Failed(String),
    Interrupted,
}

#[derive(Clone)]
struct WorkerHandle {
    writer: Arc<Mutex<tokio::net::unix::OwnedWriteHalf>>,
    state: Arc<Mutex<PendingState>>,
}

struct PendingState {
    alive: bool,
    stopping: bool,
    failure: Option<String>,
    requests: HashMap<String, oneshot::Sender<std::result::Result<Value, WorkerError>>>,
}

impl PluginRegistry {
    pub async fn start(
        worker: WorkerConfiguration,
        plugin_root: PathBuf,
        homebrew: PathBuf,
        user: String,
        user_ids: (u32, u32),
        events: broadcast::Sender<Value>,
        mut shutdown: watch::Receiver<bool>,
    ) -> Result<Self> {
        let WorkerConfiguration {
            executable: worker_executable,
            mode: worker_mode,
            bootstrap: worker_bootstrap,
            auth_token,
            server_host,
            server_port,
            chown_plugin_path,
        } = worker;
        let loader_settings_path = plugin_root
            .parent()
            .unwrap_or(&homebrew)
            .join("settings/loader.json");
        let disabled = read_disabled_plugins(&loader_settings_path);
        let metadata = discover_plugins(&plugin_root, &disabled)?;
        let socket_directory = create_socket_directory(user_ids)?;
        let fork_launcher = if worker_mode == WorkerMode::Fork {
            match ForkLauncher::spawn(
                &worker_executable,
                worker_bootstrap.as_str(),
                &plugin_root,
                &homebrew,
                &user,
                &mut shutdown,
                LauncherEnvironment {
                    auth_token: &auth_token,
                    server_host: &server_host,
                    server_port,
                    chown_plugin_path,
                },
            )
            .await
            {
                Ok(launcher) => Some(Arc::new(launcher)),
                Err(LauncherStartError::Failed(error)) => {
                    let _ = fs::remove_dir_all(&socket_directory);
                    return Err(anyhow::anyhow!(error));
                }
                Err(LauncherStartError::Interrupted) => {
                    let _ = fs::remove_dir_all(&socket_directory);
                    return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
                }
            }
        } else {
            None
        };
        let launch_config = WorkerLaunchConfig {
            worker_executable: &worker_executable,
            worker_mode,
            worker_bootstrap,
            fork_launcher: fork_launcher.as_ref(),
            plugin_root: &plugin_root,
            homebrew: &homebrew,
            user: &user,
            auth_token: &auth_token,
            server_host: &server_host,
            server_port,
            chown_plugin_path,
            socket_directory: &socket_directory,
        };
        let mut plugins = HashMap::with_capacity(metadata.len());

        for plugin in metadata.into_values() {
            if shutdown_requested(&shutdown) {
                cleanup_plugins(&plugins, &socket_directory, fork_launcher.as_ref()).await;
                return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
            }

            let entry = Arc::new(PluginEntry {
                disabled: AtomicBool::new(plugin.disabled),
                metadata: plugin,
                runtime: Mutex::new(PluginRuntime {
                    worker: None,
                    start_error: None,
                }),
            });

            if !entry.metadata.passive && !entry.disabled.load(Ordering::Relaxed) {
                let mut runtime = entry.runtime.lock().await;
                let plugin_directory = plugin_root.join(&entry.metadata.directory);
                match set_plugin_permissions_inner(
                    &plugin_directory,
                    entry.metadata.requires_root,
                    user_ids,
                    chown_plugin_path,
                ) {
                    Err(error) => runtime.start_error = Some(error.to_string()),
                    Ok(()) => match launch_worker(
                        &launch_config,
                        &entry.metadata,
                        events.clone(),
                        &mut shutdown,
                    )
                    .await
                    {
                        Ok(worker) => runtime.worker = Some(worker),
                        Err(WorkerLaunchError::Failed(error)) => runtime.start_error = Some(error),
                        Err(WorkerLaunchError::Interrupted) => {
                            drop(runtime);
                            cleanup_plugins(&plugins, &socket_directory, fork_launcher.as_ref())
                                .await;
                            return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
                        }
                    },
                }
            }

            plugins.insert(entry.metadata.name.clone(), entry);
        }

        if shutdown_requested(&shutdown) {
            cleanup_plugins(&plugins, &socket_directory, fork_launcher.as_ref()).await;
            return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
        }

        Ok(Self {
            plugins: tokio::sync::RwLock::new(plugins),
            mutations: Mutex::new(()),
            worker_executable,
            worker_mode,
            worker_bootstrap,
            fork_launcher,
            plugin_root,
            loader_settings_path,
            homebrew,
            user,
            user_ids,
            auth_token,
            server_host,
            server_port,
            chown_plugin_path,
            socket_directory,
            events,
            shutdown,
            shutting_down: AtomicBool::new(false),
            live_reload_task: Mutex::new(None),
            live_reload_paused: AtomicUsize::new(0),
            live_reload_resync: AtomicBool::new(false),
            live_reload_notify: Arc::new(Notify::new()),
            active_install_transactions: AtomicUsize::new(0),
            install_transactions_changed: Notify::new(),
        })
    }

    pub fn pause_live_reload(&self) -> LiveReloadPause<'_> {
        self.live_reload_paused.fetch_add(1, Ordering::AcqRel);
        LiveReloadPause {
            paused: &self.live_reload_paused,
            resync: &self.live_reload_resync,
            notify: &self.live_reload_notify,
        }
    }

    pub fn plugin_root(&self) -> &Path {
        &self.plugin_root
    }

    pub fn essential_failure(&self) -> Option<watch::Receiver<bool>> {
        self.fork_launcher
            .as_ref()
            .map(|launcher| launcher.failure())
    }

    pub fn emit(&self, event: &str, args: Vec<Value>) {
        let mut payload = json!({"type": 5, "event": event});
        payload["args"] = Value::Array(args);
        let _ = self.events.send(payload);
    }

    pub fn begin_install_transaction(
        &self,
    ) -> std::result::Result<InstallTransaction<'_>, WorkerError> {
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&self.shutdown) {
            return Err(WorkerError::Stopped);
        }
        self.active_install_transactions
            .fetch_add(1, Ordering::AcqRel);
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&self.shutdown) {
            if self
                .active_install_transactions
                .fetch_sub(1, Ordering::AcqRel)
                == 1
            {
                self.install_transactions_changed.notify_one();
            }
            return Err(WorkerError::Stopped);
        }
        Ok(InstallTransaction {
            active: &self.active_install_transactions,
            changed: &self.install_transactions_changed,
        })
    }

    pub async fn list(&self) -> Value {
        let mut entries: Vec<_> = self.plugins.read().await.values().cloned().collect();
        entries.sort_unstable_by(|left, right| left.metadata.name.cmp(&right.metadata.name));

        let mut result = Vec::with_capacity(entries.len());
        for entry in entries {
            let runtime = entry.runtime.lock().await;
            let worker_failure = if let Some(worker) = &runtime.worker {
                worker.handle.state.lock().await.failure.clone()
            } else {
                None
            };
            let error = runtime.start_error.as_ref().or(worker_failure.as_ref());
            let mut item = json!({
                "name": entry.metadata.name,
                "version": entry.metadata.version,
                "load_type": entry.metadata.load_type,
                "disabled": entry.disabled.load(Ordering::Relaxed),
            });
            if let Some(error) = error {
                item["error"] = Value::String(error.clone());
            }
            result.push(item);
        }

        Value::Array(result)
    }

    pub async fn directory(&self, name: &str) -> Option<PathBuf> {
        self.plugins
            .read()
            .await
            .get(name)
            .map(|entry| self.plugin_root.join(&entry.metadata.directory))
    }

    pub async fn call(
        &self,
        name: &str,
        method: &str,
        args: Value,
        legacy: bool,
    ) -> std::result::Result<Value, WorkerError> {
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(WorkerError::Stopped);
        }

        let entry = self
            .plugins
            .read()
            .await
            .get(name)
            .cloned()
            .ok_or_else(|| WorkerError::Error(format!("Plugin {name} not found")))?;
        if method.starts_with('_') {
            return Err(WorkerError::Error(format!(
                "Plugin {} tried to call private method {method}",
                display_name(&entry.metadata)
            )));
        }
        if entry.metadata.passive {
            return Err(WorkerError::Error(
                "This plugin is passive (aka does not implement main.py)".to_owned(),
            ));
        }
        if entry.disabled.load(Ordering::Acquire) {
            return Err(WorkerError::Error("This plugin is disabled".to_owned()));
        }

        let handle = {
            let runtime = entry.runtime.lock().await;
            if let Some(error) = &runtime.start_error {
                return Err(WorkerError::Error(error.clone()));
            }
            let Some(worker) = &runtime.worker else {
                return Err(WorkerError::Stopped);
            };
            worker.handle.clone()
        };

        let id = Uuid::new_v4().to_string();
        let mut request = json!({
            "type": 0,
            "method": method,
            "id": id,
        });
        request["args"] = args;
        if legacy {
            request["legacy"] = Value::Bool(true);
        }
        let mut bytes = serde_json::to_vec(&request).map_err(|error| {
            WorkerError::Error(format!("Failed to serialize plugin request: {error}"))
        })?;
        bytes.push(b'\n');

        let (sender, receiver) = oneshot::channel();
        {
            let mut state = handle.state.lock().await;
            if !state.alive {
                return Err(state
                    .failure
                    .as_ref()
                    .map(|failure| WorkerError::Error(failure.clone()))
                    .unwrap_or(WorkerError::Stopped));
            }
            state.requests.insert(id.clone(), sender);
        }

        let write_result = {
            let mut writer = handle.writer.lock().await;
            let alive = handle.state.lock().await.alive;
            if !alive {
                drop(writer);
                return receiver.await.unwrap_or(Err(WorkerError::Stopped));
            }
            writer.write_all(&bytes).await
        };
        if let Err(error) = write_result {
            mark_worker_dead(
                &handle.state,
                Some(format!("Worker IPC write failed: {error}")),
            )
            .await;
            return receiver.await.unwrap_or(Err(WorkerError::Stopped));
        }

        receiver.await.unwrap_or(Err(WorkerError::Stopped))
    }

    pub async fn import_directory(
        &self,
        directory: &str,
        refresh: bool,
        batch: bool,
    ) -> std::result::Result<(), WorkerError> {
        let _mutation = self.mutations.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(WorkerError::Stopped);
        }
        if refresh && self.live_reload_paused.load(Ordering::Acquire) > 0 {
            self.live_reload_resync.store(true, Ordering::Release);
            return Ok(());
        }
        let directory_path = resolve_plugin_directory(&self.plugin_root, directory)?;
        let disabled_plugins = read_disabled_plugins(&self.loader_settings_path);
        let mut metadata = read_plugin_metadata(&directory_path, &disabled_plugins)?;
        let previous = self.plugins.read().await.get(&metadata.name).cloned();
        if refresh && previous.is_some() && !metadata.debug {
            return Ok(());
        }
        metadata.disabled = disabled_plugins
            .iter()
            .any(|disabled| disabled == &metadata.name);
        if !metadata.passive && !metadata.disabled {
            self.set_plugin_permissions(&directory_path, metadata.requires_root)?;
        }

        let (previous_was_running, previous_error) = if let Some(previous) = &previous {
            let mut runtime = previous.runtime.lock().await;
            let worker = runtime.worker.take();
            let was_running = worker.is_some();
            let previous_error = runtime.start_error.clone();
            drop(runtime);
            if let Some(mut worker) = worker {
                worker.stop(false).await;
            }
            (was_running, previous_error)
        } else {
            (false, None)
        };

        let mut shutdown = self.shutdown.clone();
        let worker = match self.launch_plugin_worker(&metadata, &mut shutdown).await {
            Ok(worker) => worker,
            Err(WorkerError::Error(error)) => {
                if let Some(previous) = &previous
                    && let Some(restore_error) = self
                        .restore_previous_entry(previous, previous_was_running, previous_error)
                        .await
                {
                    return Err(WorkerError::Error(format!(
                        "{error}; previous plugin restart failed: {restore_error}"
                    )));
                }
                return Err(WorkerError::Error(error));
            }
            Err(WorkerError::Stopped) => return Err(WorkerError::Stopped),
        };
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            if let Some(mut worker) = worker {
                worker.stop(false).await;
            }
            return Err(WorkerError::Stopped);
        }

        let entry = Arc::new(PluginEntry {
            disabled: AtomicBool::new(metadata.disabled),
            metadata: metadata.clone(),
            runtime: Mutex::new(PluginRuntime {
                worker,
                start_error: None,
            }),
        });
        self.plugins
            .write()
            .await
            .insert(metadata.name.clone(), entry);

        if !metadata.disabled && !batch {
            self.emit(
                "loader/import_plugin",
                vec![
                    Value::String(metadata.name),
                    metadata.version.unwrap_or(Value::Null),
                    Value::from(metadata.load_type),
                    Value::Bool(true),
                    Value::from(15_000),
                ],
            );
        }
        Ok(())
    }

    async fn launch_plugin_worker(
        &self,
        metadata: &PluginMetadata,
        shutdown: &mut watch::Receiver<bool>,
    ) -> std::result::Result<Option<WorkerProcess>, WorkerError> {
        if metadata.passive || metadata.disabled {
            return Ok(None);
        }
        let config = self.launch_config();
        match launch_worker(&config, metadata, self.events.clone(), shutdown).await {
            Ok(worker) => Ok(Some(worker)),
            Err(WorkerLaunchError::Failed(error)) => Err(WorkerError::Error(error)),
            Err(WorkerLaunchError::Interrupted) => Err(WorkerError::Stopped),
        }
    }

    async fn restore_previous_entry(
        &self,
        entry: &Arc<PluginEntry>,
        was_running: bool,
        previous_error: Option<String>,
    ) -> Option<String> {
        let mut worker = None;
        let mut start_error = previous_error;
        let mut restart_error = None;
        if was_running
            && !self.shutting_down.load(Ordering::Acquire)
            && !shutdown_requested(&self.shutdown)
        {
            let mut shutdown = self.shutdown.clone();
            match self
                .launch_plugin_worker(&entry.metadata, &mut shutdown)
                .await
            {
                Ok(restarted) => {
                    worker = restarted;
                    start_error = None;
                }
                Err(error) => {
                    let message = error.to_string();
                    restart_error = Some(message.clone());
                    start_error = Some(message);
                }
            }
        }
        let mut runtime = entry.runtime.lock().await;
        runtime.worker = worker;
        runtime.start_error = start_error;
        restart_error
    }

    pub async fn remove(
        &self,
        name: &str,
        uninstall: bool,
    ) -> std::result::Result<(), WorkerError> {
        let _mutation = self.mutations.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(WorkerError::Stopped);
        }
        let entry = self
            .plugins
            .write()
            .await
            .remove(name)
            .ok_or_else(|| WorkerError::Error(format!("Plugin {name} not found")))?;
        self.emit("loader/unload_plugin", vec![Value::String(name.to_owned())]);
        let mut runtime = entry.runtime.lock().await;
        if let Some(mut worker) = runtime.worker.take() {
            worker.stop(uninstall).await;
        }
        Ok(())
    }

    pub async fn set_disabled(
        &self,
        name: &str,
        disabled: bool,
    ) -> std::result::Result<(), WorkerError> {
        let _mutation = self.mutations.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(WorkerError::Stopped);
        }
        let entry = self
            .plugins
            .read()
            .await
            .get(name)
            .cloned()
            .ok_or_else(|| WorkerError::Error(format!("Plugin {name} not found")))?;
        let was_disabled = entry.disabled.load(Ordering::Acquire);
        if disabled && was_disabled {
            return Ok(());
        }

        let mut metadata = entry.metadata.clone();
        metadata.disabled = disabled;
        if !disabled && !metadata.passive {
            let directory = self.plugin_root.join(&metadata.directory);
            self.set_plugin_permissions(&directory, metadata.requires_root)?;
        }

        let (previous_was_running, previous_error, previous_worker) = {
            let mut runtime = entry.runtime.lock().await;
            let worker = runtime.worker.take();
            (worker.is_some(), runtime.start_error.clone(), worker)
        };
        if let Some(mut worker) = previous_worker {
            worker.stop(false).await;
        }
        if disabled {
            entry.disabled.store(true, Ordering::Release);
            let mut runtime = entry.runtime.lock().await;
            runtime.start_error = None;
            drop(runtime);
            self.emit(
                "loader/disable_plugin",
                vec![Value::String(name.to_owned())],
            );
            return Ok(());
        }

        let mut shutdown = self.shutdown.clone();
        let worker = match self.launch_plugin_worker(&metadata, &mut shutdown).await {
            Ok(worker) => worker,
            Err(WorkerError::Error(error)) => {
                entry.disabled.store(was_disabled, Ordering::Release);
                if let Some(restore_error) = self
                    .restore_previous_entry(&entry, previous_was_running, previous_error)
                    .await
                {
                    return Err(WorkerError::Error(format!(
                        "{error}; previous plugin restart failed: {restore_error}"
                    )));
                }
                return Err(WorkerError::Error(error));
            }
            Err(WorkerError::Stopped) => {
                entry.disabled.store(was_disabled, Ordering::Release);
                return Err(WorkerError::Stopped);
            }
        };
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            if let Some(mut worker) = worker {
                worker.stop(false).await;
            }
            entry.disabled.store(was_disabled, Ordering::Release);
            return Err(WorkerError::Stopped);
        }

        entry.disabled.store(false, Ordering::Release);
        let mut runtime = entry.runtime.lock().await;
        runtime.worker = worker;
        runtime.start_error = None;
        drop(runtime);
        self.emit(
            "loader/import_plugin",
            vec![
                Value::String(metadata.name),
                metadata.version.unwrap_or(Value::Null),
                Value::from(metadata.load_type),
                Value::Bool(true),
                Value::from(15_000),
            ],
        );
        Ok(())
    }

    pub async fn reload(&self, name: &str) -> std::result::Result<(), WorkerError> {
        let _mutation = self.mutations.lock().await;
        let mut shutdown = self.shutdown.clone();
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            return Err(WorkerError::Stopped);
        }

        let entry = self
            .plugins
            .read()
            .await
            .get(name)
            .cloned()
            .ok_or_else(|| WorkerError::Error(format!("Plugin {name} not found")))?;
        let disabled = read_disabled_plugins(&self.loader_settings_path)
            .iter()
            .any(|entry| entry == name);
        let mut metadata = entry.metadata.clone();
        metadata.disabled = disabled;
        if !metadata.disabled && !metadata.passive {
            let directory = self.plugin_root.join(&metadata.directory);
            self.set_plugin_permissions(&directory, metadata.requires_root)?;
        }

        let (previous_was_running, previous_error, previous_worker) = {
            let mut runtime = entry.runtime.lock().await;
            let worker = runtime.worker.take();
            (worker.is_some(), runtime.start_error.clone(), worker)
        };
        if let Some(mut worker) = previous_worker {
            worker.stop(false).await;
        }
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            return Err(WorkerError::Stopped);
        }
        if metadata.disabled {
            entry.disabled.store(true, Ordering::Release);
            let mut runtime = entry.runtime.lock().await;
            runtime.start_error = None;
            return Ok(());
        }

        let worker = match self.launch_plugin_worker(&metadata, &mut shutdown).await {
            Ok(worker) => worker,
            Err(WorkerError::Error(error)) => {
                if let Some(restore_error) = self
                    .restore_previous_entry(&entry, previous_was_running, previous_error)
                    .await
                {
                    return Err(WorkerError::Error(format!(
                        "{error}; previous plugin restart failed: {restore_error}"
                    )));
                }
                return Err(WorkerError::Error(error));
            }
            Err(WorkerError::Stopped) => return Err(WorkerError::Stopped),
        };
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            if let Some(mut worker) = worker {
                worker.stop(false).await;
            }
            return Err(WorkerError::Stopped);
        }
        entry.disabled.store(false, Ordering::Release);
        let mut runtime = entry.runtime.lock().await;
        runtime.worker = worker;
        runtime.start_error = None;
        drop(runtime);
        self.emit(
            "loader/import_plugin",
            vec![
                Value::String(metadata.name),
                metadata.version.unwrap_or(Value::Null),
                Value::from(metadata.load_type),
                Value::Bool(false),
            ],
        );
        Ok(())
    }

    pub async fn start_live_reload(self: &Arc<Self>) -> std::result::Result<(), WorkerError> {
        if std::env::var("LIVE_RELOAD").is_ok_and(|value| value != "1") {
            return Ok(());
        }
        let mut task = self.live_reload_task.lock().await;
        if task.as_ref().is_some_and(|task| !task.is_finished()) {
            return Ok(());
        }
        let root = self.plugin_root.clone();
        let weak = Arc::downgrade(self);
        let shutdown = self.shutdown.clone();
        let live_reload_notify = Arc::clone(&self.live_reload_notify);
        *task = Some(tokio::spawn(live_reload_loop(
            root,
            weak,
            shutdown,
            live_reload_notify,
        )));
        Ok(())
    }

    pub async fn shutdown(&self) {
        if self.shutting_down.swap(true, Ordering::AcqRel) {
            return;
        }
        let live_reload_task = self.live_reload_task.lock().await.take();
        if let Some(mut task) = live_reload_task
            && timeout(Duration::from_secs(40), &mut task).await.is_err()
        {
            task.abort();
            let _ = task.await;
        }
        loop {
            let changed = self.install_transactions_changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if self.active_install_transactions.load(Ordering::Acquire) == 0 {
                break;
            }
            changed.await;
        }
        let _mutation = self.mutations.lock().await;
        let plugins = self.plugins.read().await.clone();
        cleanup_plugins(
            &plugins,
            &self.socket_directory,
            self.fork_launcher.as_ref(),
        )
        .await;
    }

    pub fn set_plugin_permissions(
        &self,
        directory: &Path,
        requires_root: bool,
    ) -> std::result::Result<(), WorkerError> {
        let directory = directory
            .to_str()
            .ok_or_else(|| WorkerError::Error("Plugin path is not valid UTF-8".to_owned()))?;
        let directory = resolve_plugin_directory(&self.plugin_root, directory)?;
        set_plugin_permissions_inner(
            &directory,
            requires_root,
            self.user_ids,
            self.chown_plugin_path,
        )
    }

    pub async fn validate_directory(
        &self,
        directory: &str,
    ) -> std::result::Result<(), WorkerError> {
        let _mutation = self.mutations.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(WorkerError::Stopped);
        }
        let directory_path = resolve_plugin_directory(&self.plugin_root, directory)?;
        let metadata = read_plugin_metadata(&directory_path, &[])?;
        self.set_plugin_permissions(&directory_path, metadata.requires_root)?;
        Ok(())
    }

    fn launch_config(&self) -> WorkerLaunchConfig<'_> {
        WorkerLaunchConfig {
            worker_executable: &self.worker_executable,
            worker_mode: self.worker_mode,
            worker_bootstrap: self.worker_bootstrap,
            fork_launcher: self.fork_launcher.as_ref(),
            plugin_root: &self.plugin_root,
            homebrew: &self.homebrew,
            user: &self.user,
            auth_token: &self.auth_token,
            server_host: &self.server_host,
            server_port: self.server_port,
            chown_plugin_path: self.chown_plugin_path,
            socket_directory: &self.socket_directory,
        }
    }
}

fn shutdown_requested(shutdown: &watch::Receiver<bool>) -> bool {
    *shutdown.borrow()
}

async fn cleanup_plugins(
    plugins: &HashMap<String, Arc<PluginEntry>>,
    socket_directory: &Path,
    fork_launcher: Option<&Arc<ForkLauncher>>,
) {
    let entries: Vec<_> = plugins.values().cloned().collect();
    let stops = entries.into_iter().map(|entry| async move {
        let mut runtime = entry.runtime.lock().await;
        if let Some(mut worker) = runtime.worker.take() {
            worker.stop(false).await;
        }
    });
    futures_util::future::join_all(stops).await;
    let _ = fs::remove_dir_all(socket_directory);
    if let Some(launcher) = fork_launcher {
        launcher.shutdown().await;
    }
}

fn display_name(plugin: &PluginMetadata) -> String {
    match &plugin.version {
        Some(Value::String(version)) if !version.is_empty() => {
            format!("{} (v{version})", plugin.name)
        }
        Some(Value::Number(version)) => format!("{} (v{version})", plugin.name),
        _ => plugin.name.clone(),
    }
}

fn discover_plugins(
    plugin_root: &Path,
    disabled: &[String],
) -> Result<HashMap<String, PluginMetadata>> {
    let mut directories = fs::read_dir(plugin_root)
        .with_context(|| format!("Failed to read plugin root {}", plugin_root.display()))?
        .filter_map(std::result::Result::ok)
        .filter(|entry| entry.path().is_dir())
        .collect::<Vec<_>>();
    directories.sort_unstable_by_key(|entry| entry.file_name());

    let mut plugins = HashMap::with_capacity(directories.len());
    for directory in directories {
        let directory_name = directory.file_name().to_string_lossy().into_owned();
        let directory_path = directory.path();
        if is_managed_plugin_directory(plugin_root, &directory_path) {
            continue;
        }
        let plugin_json_path = directory_path.join("plugin.json");
        if !plugin_json_path.is_file() {
            continue;
        }
        let metadata_json = match read_json(&plugin_json_path) {
            Ok(metadata) => metadata,
            Err(_) => continue,
        };
        let Some(name) = metadata_json.get("name").and_then(Value::as_str) else {
            continue;
        };
        if metadata_json.get("author").is_none() || metadata_json.get("flags").is_none() {
            continue;
        }
        let flags = metadata_json.get("flags");
        let requires_root = match flags {
            Some(Value::Array(flags)) => flags.iter().any(|flag| flag.as_str() == Some("root")),
            Some(Value::String(flags)) => flags.contains("root"),
            Some(Value::Object(flags)) => flags.contains_key("root"),
            _ => false,
        };
        let debug = match flags {
            Some(Value::Array(flags)) => flags.iter().any(|flag| flag.as_str() == Some("debug")),
            Some(Value::String(flags)) => flags.contains("debug"),
            Some(Value::Object(flags)) => flags.contains_key("debug"),
            _ => false,
        };

        let package_path = directory_path.join("package.json");
        let (version, load_type) = if package_path.is_file() {
            let package = match read_json(&package_path) {
                Ok(package) => package,
                Err(_) => continue,
            };
            let Some(version) = package.get("version") else {
                continue;
            };
            let load_type = if package.get("type").and_then(Value::as_str) == Some("module") {
                1
            } else {
                0
            };
            (Some(version.clone()), load_type)
        } else {
            (None, 0)
        };

        let plugin = PluginMetadata {
            name: name.to_owned(),
            directory: directory_name,
            version,
            load_type,
            passive: !directory_path.join("main.py").is_file(),
            disabled: disabled.iter().any(|disabled| disabled == name),
            requires_root,
            debug,
        };
        plugins.insert(plugin.name.clone(), plugin);
    }

    Ok(plugins)
}

fn resolve_plugin_directory(
    plugin_root: &Path,
    requested: &str,
) -> std::result::Result<PathBuf, WorkerError> {
    let root = fs::canonicalize(plugin_root).map_err(|error| {
        WorkerError::Error(format!(
            "Could not resolve plugin root {}: {error}",
            plugin_root.display()
        ))
    })?;
    let requested = Path::new(requested);
    let candidate = if requested.is_absolute() {
        requested.to_path_buf()
    } else {
        root.join(requested)
    };
    let directory = fs::canonicalize(&candidate).map_err(|error| {
        WorkerError::Error(format!(
            "Plugin directory {} is not accessible: {error}",
            candidate.display()
        ))
    })?;
    if directory == root || directory.parent() != Some(root.as_path()) || !directory.is_dir() {
        return Err(WorkerError::Error(format!(
            "Plugin directory must be a direct child of {}: {}",
            root.display(),
            directory.display()
        )));
    }
    Ok(directory)
}

fn read_plugin_metadata(
    directory: &Path,
    disabled_plugins: &[String],
) -> std::result::Result<PluginMetadata, WorkerError> {
    let plugin_json_path = directory.join("plugin.json");
    let metadata_json = read_json(&plugin_json_path).map_err(|error| {
        WorkerError::Error(format!(
            "Could not read plugin metadata {}: {error}",
            plugin_json_path.display()
        ))
    })?;
    let name = metadata_json
        .get("name")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .ok_or_else(|| {
            WorkerError::Error(format!(
                "Plugin metadata has no valid name: {}",
                plugin_json_path.display()
            ))
        })?;
    if metadata_json.get("author").is_none() || metadata_json.get("flags").is_none() {
        return Err(WorkerError::Error(format!(
            "Plugin metadata is missing author or flags: {}",
            plugin_json_path.display()
        )));
    }
    let flags = metadata_json.get("flags");
    let requires_root = match flags {
        Some(Value::Array(flags)) => flags.iter().any(|flag| flag.as_str() == Some("root")),
        Some(Value::String(flags)) => flags.contains("root"),
        Some(Value::Object(flags)) => flags.contains_key("root"),
        _ => false,
    };
    let debug = match flags {
        Some(Value::Array(flags)) => flags.iter().any(|flag| flag.as_str() == Some("debug")),
        Some(Value::String(flags)) => flags.contains("debug"),
        Some(Value::Object(flags)) => flags.contains_key("debug"),
        _ => false,
    };
    let package_path = directory.join("package.json");
    let (version, load_type) = if package_path.is_file() {
        let package = read_json(&package_path).map_err(|error| {
            WorkerError::Error(format!(
                "Could not read package metadata {}: {error}",
                package_path.display()
            ))
        })?;
        let version = package.get("version").ok_or_else(|| {
            WorkerError::Error(format!(
                "Package metadata has no version: {}",
                package_path.display()
            ))
        })?;
        (
            Some(version.clone()),
            u8::from(package.get("type").and_then(Value::as_str) == Some("module")),
        )
    } else {
        (None, 0)
    };
    let directory_name = directory
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| WorkerError::Error("Plugin directory name is not valid UTF-8".to_owned()))?
        .to_owned();
    Ok(PluginMetadata {
        name: name.to_owned(),
        directory: directory_name,
        version,
        load_type,
        passive: !directory.join("main.py").is_file(),
        disabled: disabled_plugins.iter().any(|disabled| disabled == name),
        requires_root,
        debug,
    })
}

fn set_plugin_permissions_inner(
    directory: &Path,
    requires_root: bool,
    host_user_ids: (u32, u32),
    enabled: bool,
) -> std::result::Result<(), WorkerError> {
    if !enabled {
        return Ok(());
    }
    let metadata = fs::symlink_metadata(directory).map_err(|error| {
        WorkerError::Error(format!(
            "Could not inspect plugin directory {}: {error}",
            directory.display()
        ))
    })?;
    if !metadata.is_dir() {
        return Err(WorkerError::Error(format!(
            "Plugin path is not a directory: {}",
            directory.display()
        )));
    }
    let effective_user_ids = platform::effective_user_ids();
    let tree_user_ids = if requires_root {
        effective_user_ids
    } else {
        host_user_ids
    };
    chown_tree(directory, tree_user_ids).map_err(|error| {
        WorkerError::Error(format!(
            "Could not chown plugin directory {}: {error}",
            directory.display()
        ))
    })?;
    chown_path(directory, effective_user_ids).map_err(|error| {
        WorkerError::Error(format!(
            "Could not chown plugin directory {}: {error}",
            directory.display()
        ))
    })?;
    chmod_tree(directory).map_err(|error| {
        WorkerError::Error(format!(
            "Could not chmod plugin directory {}: {error}",
            directory.display()
        ))
    })?;
    let plugin_json = directory.join("plugin.json");
    chown_path(&plugin_json, effective_user_ids).map_err(|error| {
        WorkerError::Error(format!(
            "Could not chown plugin metadata {}: {error}",
            plugin_json.display()
        ))
    })?;
    fs::set_permissions(&plugin_json, fs::Permissions::from_mode(0o755)).map_err(|error| {
        WorkerError::Error(format!(
            "Could not chmod plugin metadata {}: {error}",
            plugin_json.display()
        ))
    })
}

fn chown_tree(path: &Path, owner: (u32, u32)) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Ok(());
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? {
            chown_tree(&entry?.path(), owner)?;
        }
    }
    chown_path(path, owner)
}

fn chown_path(path: &Path, owner: (u32, u32)) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || (metadata.uid() == owner.0 && metadata.gid() == owner.1)
    {
        return Ok(());
    }
    platform::chown_link(path, owner)
}

fn chmod_tree(path: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Ok(());
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? {
            chmod_tree(&entry?.path())?;
        }
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o755))
}

fn read_json(path: &Path) -> io::Result<Value> {
    let bytes = fs::read(path)?;
    serde_json::from_slice(&bytes)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn read_disabled_plugins(settings_path: &Path) -> Vec<String> {
    let Ok(settings) = read_json(settings_path) else {
        return Vec::new();
    };
    settings
        .get("disabled_plugins")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

struct InotifyEvent {
    watch_descriptor: i32,
    mask: u32,
    name: Option<PathBuf>,
}

struct InotifyWatcher {
    fd: AsyncFd<OwnedFd>,
    plugin_root: PathBuf,
    watched_directories: HashMap<i32, PathBuf>,
}

impl InotifyWatcher {
    fn new(plugin_root: &Path) -> io::Result<Self> {
        let plugin_root = fs::canonicalize(plugin_root)?;
        let mut watcher = Self {
            fd: AsyncFd::new(platform::inotify_init()?)?,
            plugin_root,
            watched_directories: HashMap::new(),
        };
        watcher.rebuild()?;
        Ok(watcher)
    }

    fn add_watch(&mut self, directory: &Path) -> io::Result<()> {
        let mask = libc::IN_CREATE
            | libc::IN_CLOSE_WRITE
            | libc::IN_MOVED_TO
            | libc::IN_DELETE
            | libc::IN_MOVED_FROM
            | libc::IN_DELETE_SELF
            | libc::IN_MOVE_SELF
            | libc::IN_ONLYDIR
            | libc::IN_DONT_FOLLOW;
        let watch_descriptor =
            platform::inotify_add_watch(self.fd.get_ref().as_fd(), directory, mask)?;
        self.watched_directories
            .insert(watch_descriptor, directory.to_path_buf());
        Ok(())
    }

    fn add_tree(&mut self, directory: &Path) -> io::Result<()> {
        let mut pending = vec![directory.to_path_buf()];
        while let Some(directory) = pending.pop() {
            if is_managed_plugin_directory(&self.plugin_root, &directory) {
                continue;
            }
            let metadata = match fs::symlink_metadata(&directory) {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            };
            if !metadata.is_dir() {
                continue;
            }
            match self.add_watch(&directory) {
                Ok(()) => {}
                Err(error)
                    if directory != self.plugin_root
                        && matches!(
                            error.kind(),
                            io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied
                        ) =>
                {
                    continue;
                }
                Err(error) => return Err(error),
            }
            let entries = match fs::read_dir(&directory) {
                Ok(entries) => entries,
                Err(error)
                    if directory != self.plugin_root
                        && matches!(
                            error.kind(),
                            io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied
                        ) =>
                {
                    continue;
                }
                Err(error) => return Err(error),
            };
            for entry in entries {
                let entry = match entry {
                    Ok(entry) => entry,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                    Err(error) => return Err(error),
                };
                if entry.file_type()?.is_dir() {
                    pending.push(entry.path());
                }
            }
        }
        Ok(())
    }

    fn rebuild(&mut self) -> io::Result<()> {
        self.discard_pending_events()?;
        for watch_descriptor in self.watched_directories.keys().copied().collect::<Vec<_>>() {
            match platform::inotify_remove_watch(self.fd.get_ref().as_fd(), watch_descriptor) {
                Ok(()) => {}
                Err(error) if error.raw_os_error() == Some(libc::EINVAL) => {}
                Err(error) => return Err(error),
            }
        }
        self.watched_directories.clear();
        self.add_tree(&self.plugin_root.clone())?;
        self.discard_pending_events()
    }

    fn discard_pending_events(&self) -> io::Result<()> {
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            match platform::read_fd(self.fd.get_ref().as_fd(), &mut buffer) {
                Ok(0) => return Ok(()),
                Ok(_) => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(()),
                Err(error) => return Err(error),
            }
        }
    }

    async fn read_events(&self) -> io::Result<Vec<InotifyEvent>> {
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let mut readiness = self.fd.readable().await?;
            match readiness.try_io(|inner| platform::read_fd(inner.get_ref().as_fd(), &mut buffer))
            {
                Ok(Ok(count)) => return parse_inotify_events(&buffer[..count]),
                Ok(Err(error)) => return Err(error),
                Err(_) => continue,
            }
        }
    }

    fn affected_plugins(&mut self, events: Vec<InotifyEvent>) -> io::Result<Vec<String>> {
        let mut changed = HashSet::new();
        let mut rebuild = false;
        for event in events {
            if event.mask & libc::IN_Q_OVERFLOW != 0 {
                rebuild = true;
                continue;
            }
            if event.mask & libc::IN_IGNORED != 0 {
                self.watched_directories.remove(&event.watch_descriptor);
                continue;
            }
            let Some(directory) = self
                .watched_directories
                .get(&event.watch_descriptor)
                .cloned()
            else {
                continue;
            };
            let path = event
                .name
                .map_or_else(|| directory.clone(), |name| directory.join(name));
            let is_directory = event.mask & libc::IN_ISDIR != 0;
            if event.mask & (libc::IN_DELETE_SELF | libc::IN_MOVE_SELF) != 0
                && path != self.plugin_root
            {
                rebuild = true;
            }
            if is_directory && event.mask & (libc::IN_DELETE | libc::IN_MOVED_FROM) != 0 {
                rebuild = true;
            }
            if is_directory && event.mask & (libc::IN_CREATE | libc::IN_MOVED_TO) != 0 {
                self.add_tree(&path)?;
            }
            if !is_directory
                && event.mask
                    & (libc::IN_CLOSE_WRITE
                        | libc::IN_MOVED_TO
                        | libc::IN_DELETE
                        | libc::IN_MOVED_FROM)
                    != 0
                && let Some(plugin) = changed_plugin_directory(&self.plugin_root, &path)
            {
                changed.insert(plugin);
            }
        }
        if rebuild {
            self.rebuild()?;
        }
        let mut changed = changed.into_iter().collect::<Vec<_>>();
        changed.sort_unstable();
        Ok(changed)
    }
}
const INOTIFY_EVENT_HEADER_SIZE: usize =
    std::mem::size_of::<i32>() + 3 * std::mem::size_of::<u32>();

fn parse_inotify_events(buffer: &[u8]) -> io::Result<Vec<InotifyEvent>> {
    let mut offset = 0;
    let mut events = Vec::new();
    while offset < buffer.len() {
        let header_end = offset
            .checked_add(INOTIFY_EVENT_HEADER_SIZE)
            .filter(|end| *end <= buffer.len())
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "truncated inotify event header")
            })?;
        let header = &buffer[offset..header_end];
        let watch_descriptor = i32::from_ne_bytes(
            header[0..4]
                .try_into()
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid inotify wd"))?,
        );
        let mask = u32::from_ne_bytes(
            header[4..8]
                .try_into()
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid inotify mask"))?,
        );
        let _cookie =
            u32::from_ne_bytes(header[8..12].try_into().map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "invalid inotify cookie")
            })?);
        let name_length = usize::try_from(u32::from_ne_bytes(header[12..16].try_into().map_err(
            |_| io::Error::new(io::ErrorKind::InvalidData, "invalid inotify name length"),
        )?))
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "inotify name length overflow"))?;
        offset = header_end;
        let name_end = offset
            .checked_add(name_length)
            .filter(|end| *end <= buffer.len())
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "truncated inotify event name")
            })?;
        let name = if name_length == 0 {
            None
        } else {
            let bytes = &buffer[offset..name_end];
            let name_length = bytes
                .iter()
                .position(|byte| *byte == 0)
                .unwrap_or(bytes.len());
            (name_length > 0).then(|| PathBuf::from(OsStr::from_bytes(&bytes[..name_length])))
        };
        offset = name_end;
        events.push(InotifyEvent {
            watch_descriptor,
            mask,
            name,
        });
    }
    Ok(events)
}

fn is_managed_plugin_directory(plugin_root: &Path, directory: &Path) -> bool {
    if directory.parent() != Some(plugin_root) {
        return false;
    }
    directory
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with(".decky-stage-") || name.starts_with(".decky-backup-"))
}

fn changed_plugin_directory(plugin_root: &Path, source: &Path) -> Option<String> {
    let mut components = source.strip_prefix(plugin_root).ok()?.components();
    let directory = match components.next()? {
        std::path::Component::Normal(directory) => directory.to_str()?,
        _ => return None,
    };
    if directory.starts_with('.') {
        return None;
    }
    let is_source = match (components.next(), components.next(), components.next()) {
        (Some(std::path::Component::Normal(file)), None, None) => file == "main.py",
        (
            Some(std::path::Component::Normal(dist)),
            Some(std::path::Component::Normal(file)),
            None,
        ) => dist == "dist" && file == "index.js",
        _ => false,
    };
    if is_source && plugin_root.join(directory).join("plugin.json").is_file() {
        Some(directory.to_owned())
    } else {
        None
    }
}

async fn live_reload_loop(
    plugin_root: PathBuf,
    registry: Weak<PluginRegistry>,
    mut shutdown: watch::Receiver<bool>,
    live_reload_notify: Arc<Notify>,
) {
    if shutdown_requested(&shutdown) {
        return;
    }
    tokio::select! {
        _ = sleep(Duration::from_secs(10)) => {}
        changed = shutdown.changed() => {
            if changed.is_err() || shutdown_requested(&shutdown) {
                return;
            }
        }
    }
    if shutdown_requested(&shutdown) {
        return;
    }
    let mut watcher = match InotifyWatcher::new(&plugin_root) {
        Ok(watcher) => watcher,
        Err(error) => {
            eprintln!("Could not start plugin live reload watcher: {error}");
            return;
        }
    };
    loop {
        let events = tokio::select! {
            biased;
            changed = shutdown.changed() => {
                if changed.is_err() || shutdown_requested(&shutdown) {
                    return;
                }
                continue;
            }
            _ = live_reload_notify.notified() => None,
            events = watcher.read_events() => match events {
                Ok(events) => Some(events),
                Err(error) => {
                    eprintln!("Plugin live reload watcher failed: {error}");
                    return;
                }
            },
        };
        let Some(owner) = registry.upgrade() else {
            return;
        };
        if owner.shutting_down.load(Ordering::Acquire) {
            return;
        }
        if owner.live_reload_paused.load(Ordering::Acquire) > 0 {
            continue;
        }
        if owner.live_reload_resync.swap(false, Ordering::AcqRel) {
            drop(owner);
            if let Err(error) = watcher.rebuild() {
                eprintln!("Could not resynchronize plugin live reload watcher: {error}");
                return;
            }
            continue;
        }
        drop(owner);
        let Some(events) = events else {
            continue;
        };

        let changed_plugins = match watcher.affected_plugins(events) {
            Ok(changed) => changed,
            Err(error) => {
                eprintln!("Could not update plugin live reload watches: {error}");
                return;
            }
        };
        for directory in changed_plugins {
            if !plugin_root.join(&directory).is_dir() {
                continue;
            }
            let Some(owner) = registry.upgrade() else {
                return;
            };
            if owner.shutting_down.load(Ordering::Acquire) {
                return;
            }
            if owner.live_reload_paused.load(Ordering::Acquire) > 0 {
                owner.live_reload_resync.store(true, Ordering::Release);
                break;
            }
            let _ = owner.import_directory(&directory, true, false).await;
        }
    }
}
fn process_exit_notification(pid: u32) -> io::Result<AsyncFd<OwnedFd>> {
    AsyncFd::new(platform::process_exit_fd(pid)?)
}

fn socket_filename() -> String {
    let identifier = Uuid::new_v4().as_u128() & ((1_u128 << 96) - 1);
    format!("{identifier:024x}.sock")
}

fn create_socket_directory(user_ids: (u32, u32)) -> Result<PathBuf> {
    let temporary_directory = std::env::var_os("TMPDIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/var/tmp/omp-1000"));
    if !temporary_directory.exists() {
        use std::os::unix::fs::DirBuilderExt;
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&temporary_directory)?;
    }
    let temporary_directory =
        fs::canonicalize(temporary_directory).context("resolving TMPDIR for worker IPC sockets")?;
    let directory = temporary_directory.join(format!("drp-{}", Uuid::new_v4().simple()));
    let example_socket = directory.join("user").join(socket_filename());
    if example_socket.as_os_str().as_bytes().len() >= 108 {
        anyhow::bail!(
            "resolved TMPDIR produces worker IPC paths at or above the Linux AF_UNIX limit: {}",
            example_socket.display()
        );
    }
    fs::create_dir(&directory).with_context(|| {
        format!(
            "Failed to create worker socket directory {}",
            directory.display()
        )
    })?;
    let prepare = || -> io::Result<()> {
        // Normal workers drop privileges before binding their sockets. They may
        // traverse this root-owned parent, but cannot list or alter its entries.
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o711))?;
        for name in ["root", "user"] {
            let child = directory.join(name);
            fs::create_dir(&child)?;
            fs::set_permissions(&child, fs::Permissions::from_mode(0o700))?;
        }
        if platform::effective_user_ids().0 == 0 {
            std::os::unix::fs::chown(directory.join("user"), Some(user_ids.0), Some(user_ids.1))?;
        }
        Ok(())
    };
    if let Err(error) = prepare() {
        let _ = fs::remove_dir_all(&directory);
        return Err(error).context("preparing private worker socket directories");
    }
    Ok(directory)
}

async fn launch_worker(
    config: &WorkerLaunchConfig<'_>,
    plugin: &PluginMetadata,
    events: broadcast::Sender<Value>,
    shutdown: &mut watch::Receiver<bool>,
) -> std::result::Result<WorkerProcess, WorkerLaunchError> {
    let WorkerLaunchConfig {
        worker_executable,
        worker_mode,
        worker_bootstrap,
        fork_launcher,
        plugin_root,
        homebrew,
        user,
        auth_token,
        server_host,
        server_port,
        chown_plugin_path,
        socket_directory,
    } = *config;
    if shutdown_requested(shutdown) {
        return Err(WorkerLaunchError::Interrupted);
    }
    if plugin.requires_root {
        let effective_uid = platform::effective_user_ids().0;
        if effective_uid != 0 {
            return Err(WorkerLaunchError::Failed(format!(
                "Plugin {} requires root privileges, but the worker launcher is running as UID {effective_uid}",
                display_name(plugin)
            )));
        }
    }

    let socket_path = socket_directory
        .join(if plugin.requires_root { "root" } else { "user" })
        .join(socket_filename());
    if socket_path.as_os_str().as_bytes().len() >= 108 {
        return Err(WorkerLaunchError::Failed(format!(
            "Worker IPC socket path exceeds the Linux AF_UNIX limit: {}",
            socket_path.display()
        )));
    }

    let mut supervisor = match worker_mode {
        WorkerMode::Exec => {
            let mut command = Command::new(worker_executable);
            command
                .arg("--plugin-root")
                .arg(plugin_root)
                .arg("--plugin-directory")
                .arg(&plugin.directory)
                .arg("--socket")
                .arg(&socket_path)
                .arg("--homebrew")
                .arg(homebrew)
                .arg("--user")
                .arg(user)
                .arg("--bootstrap")
                .arg(worker_bootstrap.as_str())
                .env("DECKY_AUTH_TOKEN", auth_token)
                .env("SERVER_HOST", server_host)
                .env("SERVER_PORT", server_port.to_string())
                .env(
                    "CHOWN_PLUGIN_PATH",
                    if chown_plugin_path { "1" } else { "0" },
                )
                .stdin(Stdio::null())
                .stdout(Stdio::inherit())
                .stderr(Stdio::inherit());
            command.as_std_mut().process_group(0);
            command.kill_on_drop(true);
            let child = command.spawn().map_err(|error| {
                WorkerLaunchError::Failed(format!(
                    "Failed to start worker for {} using {}: {error}",
                    display_name(plugin),
                    worker_executable.display()
                ))
            })?;
            let pid = child.id().expect("a spawned worker has a process ID");
            WorkerSupervisor::Direct {
                child,
                pid,
                group: ProcessGroupGuard { pid },
            }
        }
        WorkerMode::Fork => {
            let Some(launcher) = fork_launcher else {
                return Err(WorkerLaunchError::Failed(
                    "Fork worker mode has no shared launcher".to_owned(),
                ));
            };
            let id = Uuid::new_v4().to_string();
            match launcher
                .start_child(json!({
                    "op": "start",
                    "id": id,
                    "plugin_root": plugin_root,
                    "plugin_directory": plugin.directory,
                    "socket": socket_path,
                    "homebrew": homebrew,
                    "user": user,
                    "bootstrap": worker_bootstrap.as_str(),
                }))
                .await
            {
                Ok(pid) => {
                    let exit = match process_exit_notification(pid) {
                        Ok(exit) => exit,
                        Err(error) => {
                            terminate_fork_child(launcher, &id, pid).await;
                            return Err(WorkerLaunchError::Failed(format!(
                                "Could not monitor forked worker exit: {error}"
                            )));
                        }
                    };
                    WorkerSupervisor::Fork {
                        launcher: Arc::clone(launcher),
                        id,
                        pid,
                        exit,
                        group: ProcessGroupGuard { pid },
                    }
                }
                Err(error) => {
                    return Err(WorkerLaunchError::Failed(format!(
                        "Failed to fork worker for {}: {error:#}",
                        display_name(plugin)
                    )));
                }
            }
        }
    };
    if shutdown_requested(shutdown) {
        supervisor.abort_startup(&socket_path).await;
        return Err(WorkerLaunchError::Interrupted);
    }
    let release = match &supervisor {
        WorkerSupervisor::Direct { .. } => None,
        WorkerSupervisor::Fork { launcher, id, .. } => Some(launcher.release_child(id).await),
    };
    if let Some(Err(error)) = release {
        supervisor.abort_startup(&socket_path).await;
        return Err(WorkerLaunchError::Failed(format!(
            "Could not release forked worker for {}: {error:#}",
            display_name(plugin)
        )));
    }
    if shutdown_requested(shutdown) {
        supervisor.abort_startup(&socket_path).await;
        return Err(WorkerLaunchError::Interrupted);
    }

    let start = Instant::now();
    let stream = loop {
        if shutdown_requested(shutdown) {
            let mut supervisor = supervisor;
            supervisor.abort_startup(&socket_path).await;
            return Err(WorkerLaunchError::Interrupted);
        }

        let connection = tokio::select! {
            biased;
            changed = shutdown.changed() => {
                if changed.is_err() || shutdown_requested(shutdown) {
                    let mut supervisor = supervisor;
                    supervisor.abort_startup(&socket_path).await;
                    return Err(WorkerLaunchError::Interrupted);
                }
                continue;
            }
            result = UnixStream::connect(&socket_path) => result,
        };
        match connection {
            Ok(stream) => break stream,
            Err(last_socket_error) => {
                let child_status = supervisor.try_wait().await;
                match child_status {
                    Ok(Some(status)) => {
                        let _ = fs::remove_file(&socket_path);
                        return Err(WorkerLaunchError::Failed(format!(
                            "Worker for {} exited before IPC readiness: {status}",
                            display_name(plugin)
                        )));
                    }
                    Ok(None) => {}
                    Err(error) => {
                        let mut supervisor = supervisor;
                        supervisor.abort_startup(&socket_path).await;
                        return Err(WorkerLaunchError::Failed(format!(
                            "Could not check worker startup for {}: {error}",
                            display_name(plugin)
                        )));
                    }
                }

                if start.elapsed() >= WORKER_READY_TIMEOUT {
                    let mut supervisor = supervisor;
                    supervisor.abort_startup(&socket_path).await;
                    let detail = last_socket_error.to_string();
                    return Err(WorkerLaunchError::Failed(format!(
                        "Worker for {} did not become IPC-ready within {} seconds: {detail}",
                        display_name(plugin),
                        WORKER_READY_TIMEOUT.as_secs()
                    )));
                }

                tokio::select! {
                    biased;
                    changed = shutdown.changed() => {
                        if changed.is_err() || shutdown_requested(shutdown) {
                            let mut supervisor = supervisor;
                            supervisor.abort_startup(&socket_path).await;
                            return Err(WorkerLaunchError::Interrupted);
                        }
                    }
                    _ = sleep(SOCKET_RETRY_INTERVAL) => {}
                }
            }
        }
    };

    if shutdown_requested(shutdown) {
        drop(stream);
        let mut supervisor = supervisor;
        supervisor.abort_startup(&socket_path).await;
        return Err(WorkerLaunchError::Interrupted);
    }

    let (stop, stop_receiver) = mpsc::unbounded_channel();
    let reaper = supervisor.spawn_reaper(stop_receiver);
    let (reader, writer) = stream.into_split();
    let handle = WorkerHandle {
        writer: Arc::new(Mutex::new(writer)),
        state: Arc::new(Mutex::new(PendingState {
            alive: true,
            stopping: false,
            failure: None,
            requests: HashMap::new(),
        })),
    };
    let monitor = tokio::spawn(monitor_worker(
        reader,
        reaper,
        stop.clone(),
        handle.clone(),
        socket_path,
        plugin.name.clone(),
        events,
    ));

    Ok(WorkerProcess {
        handle,
        stop: Some(stop),
        monitor: Some(monitor),
    })
}
impl WorkerProcess {
    async fn stop(&mut self, uninstall: bool) {
        if uninstall {
            let notify_plugin = async {
                let mut writer = self.handle.writer.lock().await;
                writer.write_all(b"{\"uninstall\":true}\n").await?;
                writer.flush().await
            };
            if matches!(
                timeout(Duration::from_secs(1), notify_plugin).await,
                Ok(Ok(()))
            ) {
                sleep(Duration::from_millis(25)).await;
            }
        }
        {
            let mut state = self.handle.state.lock().await;
            state.stopping = true;
        }
        mark_worker_dead(&self.handle.state, None).await;
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(monitor) = self.monitor.take() {
            let _ = monitor.await;
        }
        let _ = self.handle.writer.lock().await.shutdown().await;
    }
}

async fn reap_child(
    mut child: Child,
    mut stop: mpsc::UnboundedReceiver<()>,
    pid: u32,
    _group: ProcessGroupGuard,
) -> io::Result<ExitStatus> {
    let status = tokio::select! {
        status = child.wait() => status,
        _ = stop.recv() => {
            send_signal(pid, libc::SIGTERM);
            match timeout(WORKER_GRACEFUL_TIMEOUT, child.wait()).await {
                Ok(status) => status,
                Err(_) => {
                    send_group_signal(pid, libc::SIGKILL);
                    child.wait().await
                }
            }
        }
    };
    // The bootloader may exit before a Python child or plugin descendant.
    send_group_signal(pid, libc::SIGKILL);
    status
}
async fn reap_fork_child(
    mut stop: mpsc::UnboundedReceiver<()>,
    launcher: Arc<ForkLauncher>,
    id: String,
    pid: u32,
    exit: AsyncFd<OwnedFd>,
    _group: ProcessGroupGuard,
) -> io::Result<ExitStatus> {
    let exited = tokio::select! {
        result = exit.readable() => result.map(drop),
        _ = stop.recv() => {
            send_signal(pid, libc::SIGTERM);
            match timeout(WORKER_GRACEFUL_TIMEOUT, exit.readable()).await {
                Ok(result) => result.map(drop),
                Err(_) => {
                    send_group_signal(pid, libc::SIGKILL);
                    match timeout(WORKER_GRACEFUL_TIMEOUT, exit.readable()).await {
                        Ok(result) => result.map(drop),
                        Err(_) => Err(io::Error::new(io::ErrorKind::TimedOut, "forked plugin did not exit after SIGKILL")),
                    }
                }
            }
        }
    };
    send_group_signal(pid, libc::SIGKILL);
    exited?;
    let status = launcher
        .child_status(&id)
        .await
        .map_err(|error| io::Error::other(error.to_string()))?
        .ok_or_else(|| io::Error::other("forked plugin exited without a launcher wait status"))?;
    launcher
        .forget_child(&id, pid)
        .await
        .map_err(|error| io::Error::other(error.to_string()))?;
    Ok(ExitStatus::from_raw(status))
}

async fn terminate_fork_child(launcher: &Arc<ForkLauncher>, id: &str, pid: u32) {
    send_signal(pid, libc::SIGTERM);
    let graceful_deadline = Instant::now() + WORKER_GRACEFUL_TIMEOUT;
    let mut kill_deadline = None;
    loop {
        match launcher.child_status(id).await {
            Ok(Some(_)) => {
                send_group_signal(pid, libc::SIGKILL);
                let _ = launcher.forget_child(id, pid).await;
                return;
            }
            Ok(None) => {}
            Err(_) => {
                send_group_signal(pid, libc::SIGKILL);
                return;
            }
        }
        let now = Instant::now();
        if kill_deadline.is_none() && now >= graceful_deadline {
            send_group_signal(pid, libc::SIGKILL);
            kill_deadline = Some(now + WORKER_GRACEFUL_TIMEOUT);
        }
        if kill_deadline.is_some_and(|deadline| now >= deadline) {
            return;
        }
        sleep(SOCKET_RETRY_INTERVAL).await;
    }
}

async fn terminate_and_reap(child: &mut Child, pid: u32) {
    send_signal(pid, libc::SIGTERM);
    match timeout(WORKER_GRACEFUL_TIMEOUT, child.wait()).await {
        Ok(Ok(_)) => send_group_signal(pid, libc::SIGKILL),
        Ok(Err(_)) | Err(_) => {
            send_group_signal(pid, libc::SIGKILL);
            let _ = child.wait().await;
        }
    }
}

fn send_signal(pid: u32, signal: i32) {
    let _ = platform::signal_process(pid, signal);
}

fn send_group_signal(pid: u32, signal: i32) {
    let _ = platform::signal_group(pid, signal);
}

async fn monitor_worker(
    reader: tokio::net::unix::OwnedReadHalf,
    mut reaper: JoinHandle<io::Result<ExitStatus>>,
    stop: mpsc::UnboundedSender<()>,
    handle: WorkerHandle,
    socket_path: PathBuf,
    plugin_name: String,
    events: broadcast::Sender<Value>,
) {
    let WorkerHandle { state, writer } = handle;
    let mut reader = BufReader::new(reader);
    let mut line = Vec::new();
    let failure: Option<String>;

    loop {
        tokio::select! {
            result = &mut reaper => {
                failure = match result {
                    Ok(Ok(status)) => {
                        let stopping = state.lock().await.stopping;
                        if stopping { None } else { Some(format!("Worker for {plugin_name} exited unexpectedly: {status}")) }
                    }
                    Ok(Err(error)) => Some(format!("Worker for {plugin_name} failed while being reaped: {error}")),
                    Err(error) => Some(format!("Worker monitor for {plugin_name} failed: {error}")),
                };
                let drain = async {
                    loop {
                        match reader.read_until(b'\n', &mut line).await {
                            Ok(0) | Err(_) => break,
                            Ok(_) => {
                                if process_worker_message(&line, &plugin_name, &state, &events)
                                    .await
                                    .is_err()
                                {
                                    break;
                                }
                                line.clear();
                            }
                        }
                    }
                };
                // A plugin-created descendant can retain the inherited socket.
                // Reaping the worker must still bound supervisor shutdown.
                let _ = timeout(Duration::from_millis(100), drain).await;
                break;
            }
            read_result = reader.read_until(b'\n', &mut line) => {
                match read_result {
                    Ok(0) => {
                        failure = unexpected_exit_failure(&state, &plugin_name).await;
                        mark_worker_dead(&state, failure.clone()).await;
                        let _ = stop.send(());
                        let _ = (&mut reaper).await;
                        break;
                    }
                    Ok(_) => {
                        if process_worker_message(&line, &plugin_name, &state, &events).await.is_err() {
                            failure = Some(format!("Worker for {plugin_name} sent malformed IPC data"));
                            mark_worker_dead(&state, failure.clone()).await;
                            let _ = stop.send(());
                            let _ = (&mut reaper).await;
                            break;
                        }
                        line.clear();
                    }
                    Err(error) => {
                        failure = Some(format!("Worker IPC read failed for {plugin_name}: {error}"));
                        mark_worker_dead(&state, failure.clone()).await;
                        let _ = stop.send(());
                        let _ = (&mut reaper).await;
                        break;
                    }
                }
            }
        }
    }

    mark_worker_dead(&state, failure).await;
    let _ = writer.lock().await.shutdown().await;
    let _ = fs::remove_file(socket_path);
}

async fn unexpected_exit_failure(
    state: &Arc<Mutex<PendingState>>,
    plugin_name: &str,
) -> Option<String> {
    let state = state.lock().await;
    if state.stopping {
        None
    } else {
        Some(format!(
            "Worker for {plugin_name} closed its IPC connection"
        ))
    }
}

async fn process_worker_message(
    line: &[u8],
    plugin_name: &str,
    state: &Arc<Mutex<PendingState>>,
    events: &broadcast::Sender<Value>,
) -> std::result::Result<(), ()> {
    let Value::Object(mut message) = serde_json::from_slice(line).map_err(|_| ())? else {
        return Err(());
    };
    match message.get("type").and_then(Value::as_i64) {
        Some(1) => {
            let Some(Value::String(id)) = message.remove("id") else {
                return Err(());
            };
            let success = message.get("success").and_then(Value::as_bool).ok_or(())?;
            let result = message.remove("res").ok_or(())?;
            let response = if success {
                Ok(result)
            } else {
                Err(WorkerError::Error(value_error_string(&result)))
            };
            let sender = state.lock().await.requests.remove(&id);
            if let Some(sender) = sender {
                let _ = sender.send(response);
            }
        }
        Some(2) => {
            if let (Some(Value::String(event)), Some(args)) =
                (message.remove("event"), message.remove("args"))
            {
                let mut payload = json!({"plugin": plugin_name});
                payload["event"] = Value::String(event);
                payload["args"] = args;
                let mut envelope = json!({"type": 5, "event": "loader/plugin_event"});
                envelope["args"] = Value::Array(vec![payload]);
                let _ = events.send(envelope);
            }
        }
        _ => {}
    }
    Ok(())
}

fn value_error_string(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
}

async fn mark_worker_dead(state: &Arc<Mutex<PendingState>>, failure: Option<String>) {
    let mut state = state.lock().await;
    state.alive = false;
    if !state.stopping
        && let Some(failure) = failure
    {
        state.failure = Some(failure);
    }
    for (_, request) in state.requests.drain() {
        let _ = request.send(Err(WorkerError::Stopped));
    }
}
#[cfg(test)]
mod inotify_parser_tests {
    use super::{INOTIFY_EVENT_HEADER_SIZE, parse_inotify_events};
    use std::path::Path;

    fn event_header(watch_descriptor: i32, mask: u32, name_length: u32) -> Vec<u8> {
        let mut header = Vec::with_capacity(INOTIFY_EVENT_HEADER_SIZE);
        header.extend_from_slice(&watch_descriptor.to_ne_bytes());
        header.extend_from_slice(&mask.to_ne_bytes());
        header.extend_from_slice(&0_u32.to_ne_bytes());
        header.extend_from_slice(&name_length.to_ne_bytes());
        header
    }

    #[test]
    fn rejects_truncated_header_and_name() {
        assert_eq!(
            parse_inotify_events(&[0; 15]).err().unwrap().kind(),
            std::io::ErrorKind::InvalidData
        );
        assert_eq!(
            parse_inotify_events(&event_header(1, 2, 4))
                .err()
                .unwrap()
                .kind(),
            std::io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn decodes_native_header_and_nul_terminated_name() {
        let mut bytes = event_header(7, 3, 4);
        bytes.extend_from_slice(b"abc\0");
        let events = parse_inotify_events(&bytes).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].watch_descriptor, 7);
        assert_eq!(events[0].mask, 3);
        assert_eq!(events[0].name.as_deref(), Some(Path::new("abc")));
    }
}
