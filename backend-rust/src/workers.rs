use std::{
    collections::{HashMap, HashSet},
    ffi::OsStr,
    fs::{self, File},
    io,
    os::fd::{AsFd, AsRawFd, OwnedFd},
    os::unix::{
        ffi::OsStrExt,
        fs::PermissionsExt,
        process::{CommandExt, ExitStatusExt},
    },
    path::{Component, Path, PathBuf},
    process::{ExitStatus, Stdio},
    sync::{
        Arc, Weak,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use nix::{
    dir::Dir,
    errno::Errno,
    fcntl::{AtFlags, OFlag, open, openat},
    sys::stat::{Mode, SFlag, fchmod, fstat, fstatat},
    unistd::{self, Gid, Uid},
};

use anyhow::{Context, Result};
use serde_json::{Value, json, value::RawValue};
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
    root_host::{self, HostEnvironment, RootHost},
};

const WORKER_READY_TIMEOUT: Duration = Duration::from_secs(30);
const WORKER_GRACEFUL_TIMEOUT: Duration = Duration::from_secs(5);
const SOCKET_RETRY_INTERVAL: Duration = Duration::from_millis(50);
// A new worker is often ready within a few milliseconds. Check soon, then back off
// to SOCKET_RETRY_INTERVAL so a slow plugin does not cause many connection attempts.
const SOCKET_FIRST_RETRY: Duration = Duration::from_millis(2);
// Quiet time after the last directory attribute change before a refused plugin is retried.
const OWNERSHIP_RETRY_DEBOUNCE: Duration = Duration::from_secs(1);

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
    author: String,
    flags: Vec<String>,
    api_version: u64,
    version: Option<String>,
    load_type: u8,
    passive: bool,
    disabled: bool,
    requires_root: bool,
    debug: bool,
    approved_directory: Arc<OwnedFd>,
    directory_device: u64,
    directory_inode: u64,
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
    fork_launcher: Mutex<Option<Arc<ForkLauncher>>>,
    root_host: Mutex<Option<Arc<RootHost>>>,
    launcher_failure: Option<(watch::Sender<bool>, watch::Receiver<bool>)>,
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
    root_host: Option<&'a Arc<RootHost>>,
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
    /// A plugin marked `root`, run by the root plugin host. This process cannot
    /// signal it, so every stop goes through the host.
    Root {
        host: Arc<RootHost>,
        id: String,
        exit: AsyncFd<OwnedFd>,
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
            Self::Root { host, id, .. } => {
                let status = host
                    .child_status(id)
                    .await
                    .map_err(|error| io::Error::other(error.to_string()))?;
                let Some(status) = status else {
                    return Ok(None);
                };
                host.forget_child(id)
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
            Self::Root { host, id, .. } => {
                if host.stop_child(id).await.is_ok() {
                    let _ = host.forget_child(id).await;
                }
            }
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
            Self::Root { host, id, exit } => tokio::spawn(reap_root_child(stop, host, id, exit)),
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

/// A plugin call result as raw JSON text, or the reason the call failed.
type CallReply = std::result::Result<Box<RawValue>, WorkerError>;

struct PendingState {
    alive: bool,
    stopping: bool,
    failure: Option<String>,
    requests: HashMap<String, oneshot::Sender<CallReply>>,
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
        let launcher_failure = (worker_mode == WorkerMode::Fork).then(|| watch::channel(false));
        let mut registry = Self {
            plugins: tokio::sync::RwLock::new(HashMap::with_capacity(metadata.len())),
            mutations: Mutex::new(()),
            worker_executable,
            worker_mode,
            worker_bootstrap,
            fork_launcher: Mutex::new(None),
            root_host: Mutex::new(None),
            launcher_failure,
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
            shutdown: shutdown.clone(),
            shutting_down: AtomicBool::new(false),
            live_reload_task: Mutex::new(None),
            live_reload_paused: AtomicUsize::new(0),
            live_reload_resync: AtomicBool::new(false),
            live_reload_notify: Arc::new(Notify::new()),
            active_install_transactions: AtomicUsize::new(0),
            install_transactions_changed: Notify::new(),
        };

        let started = Instant::now();
        let mut running = 0_usize;
        for plugin in metadata.into_values() {
            if shutdown_requested(&shutdown) {
                registry.shutdown().await;
                return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
            }

            let root_error = root_plugin_refusal(
                &plugin,
                platform::effective_user_ids().0,
                registry.root_host_enabled(),
            );
            let entry = Arc::new(PluginEntry {
                disabled: AtomicBool::new(plugin.disabled),
                metadata: plugin,
                runtime: Mutex::new(PluginRuntime {
                    worker: None,
                    start_error: root_error.clone(),
                }),
            });

            if root_error.is_none()
                && !entry.metadata.passive
                && !entry.disabled.load(Ordering::Relaxed)
            {
                let mut runtime = entry.runtime.lock().await;
                match registry.set_approved_plugin_permissions(&entry.metadata) {
                    Err(error) => runtime.start_error = Some(error.to_string()),
                    Ok(()) if registry.uses_root_host(&entry.metadata) => {
                        match registry
                            .launch_root_worker(&entry.metadata, &mut shutdown)
                            .await
                        {
                            Ok(worker) => {
                                runtime.worker = Some(worker);
                                running += 1;
                            }
                            Err(WorkerError::Error(error)) => runtime.start_error = Some(error),
                            Err(WorkerError::Stopped) => {
                                drop(runtime);
                                registry.shutdown().await;
                                return Err(anyhow::anyhow!(
                                    "Plugin startup interrupted by shutdown"
                                ));
                            }
                        }
                    }
                    Ok(()) => {
                        let fork_launcher = if registry.worker_mode == WorkerMode::Fork {
                            match registry.ensure_fork_launcher(&mut shutdown).await {
                                Ok(launcher) => Some(launcher),
                                Err(WorkerError::Error(error)) => {
                                    drop(runtime);
                                    registry.shutdown().await;
                                    return Err(anyhow::anyhow!(error));
                                }
                                Err(WorkerError::Stopped) => {
                                    drop(runtime);
                                    registry.shutdown().await;
                                    return Err(anyhow::anyhow!(
                                        "Plugin startup interrupted by shutdown"
                                    ));
                                }
                            }
                        } else {
                            None
                        };
                        let launch_config = registry.launch_config(fork_launcher.as_ref(), None);
                        match launch_worker(
                            &launch_config,
                            &entry.metadata,
                            registry.events.clone(),
                            &mut shutdown,
                        )
                        .await
                        {
                            Ok(worker) => {
                                runtime.worker = Some(worker);
                                running += 1;
                            }
                            Err(WorkerLaunchError::Failed(error)) => {
                                runtime.start_error = Some(error)
                            }
                            Err(WorkerLaunchError::Interrupted) => {
                                drop(runtime);
                                registry.shutdown().await;
                                return Err(anyhow::anyhow!(
                                    "Plugin startup interrupted by shutdown"
                                ));
                            }
                        }
                    }
                }
            }

            registry
                .plugins
                .get_mut()
                .insert(entry.metadata.name.clone(), entry);
        }

        if shutdown_requested(&shutdown) {
            registry.shutdown().await;
            return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
        }

        eprintln!(
            "Started {running} plugin backends in {} ms",
            started.elapsed().as_millis()
        );
        Ok(registry)
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
        self.launcher_failure
            .as_ref()
            .map(|(_, receiver)| receiver.clone())
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

    /// Calls a plugin method. `args` must be valid JSON text (an array, or an object
    /// for legacy calls). The worker's result comes back unparsed, so large data is
    /// not decoded and encoded again on its way to the frontend.
    pub async fn call(
        &self,
        name: &str,
        method: &str,
        args: &str,
        legacy: bool,
    ) -> std::result::Result<Box<RawValue>, WorkerError> {
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
        let method = Value::String(method.to_owned()).to_string();
        let legacy = if legacy { r#","legacy":true"# } else { "" };
        let mut bytes =
            format!(r#"{{"type":0,"method":{method},"id":"{id}","args":{args}{legacy}}}"#)
                .into_bytes();
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
        let root_error = root_plugin_refusal(
            &metadata,
            platform::effective_user_ids().0,
            self.root_host_enabled(),
        );
        let previous = self.plugins.read().await.get(&metadata.name).cloned();
        if refresh && previous.is_some() && !metadata.debug {
            return Ok(());
        }
        metadata.disabled = disabled_plugins
            .iter()
            .any(|disabled| disabled == &metadata.name);
        if root_error.is_none() && !metadata.passive && !metadata.disabled {
            self.set_approved_plugin_permissions(&metadata)?;
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
        let worker = if root_error.is_some() {
            None
        } else {
            match self.launch_plugin_worker(&metadata, &mut shutdown).await {
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
            }
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
                start_error: root_error.clone(),
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
                    metadata
                        .version
                        .clone()
                        .map(Value::String)
                        .unwrap_or(Value::Null),
                    Value::from(metadata.load_type),
                    Value::Bool(true),
                    Value::from(15_000),
                    root_error.map(Value::String).unwrap_or(Value::Null),
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
        if self.uses_root_host(metadata) {
            return self.launch_root_worker(metadata, shutdown).await.map(Some);
        }
        let fork_launcher = if self.worker_mode == WorkerMode::Fork {
            Some(self.ensure_fork_launcher(shutdown).await?)
        } else {
            None
        };
        let config = self.launch_config(fork_launcher.as_ref(), None);
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
            if let Err(error) = self.set_approved_plugin_permissions(&entry.metadata) {
                let message = error.to_string();
                restart_error = Some(message.clone());
                start_error = Some(message);
            } else {
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
            self.set_approved_plugin_permissions(&metadata)?;
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
                metadata
                    .version
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
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
            self.set_approved_plugin_permissions(&metadata)?;
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
                metadata
                    .version
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
                Value::from(metadata.load_type),
                Value::Bool(false),
            ],
        );
        Ok(())
    }

    pub async fn start_live_reload(self: &Arc<Self>) -> std::result::Result<(), WorkerError> {
        // LIVE_RELOAD=0 turns off the whole watcher, including the retry of plugins
        // refused for their directory owner (benchmarks rely on a quiet backend).
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

    /// Loads again each plugin that was refused because of its directory owner,
    /// if `changes` names its directory. Other plugins are not touched.
    async fn retry_ownership_failed(&self, changes: &AttributeChanges) {
        let entries = self
            .plugins
            .read()
            .await
            .values()
            .cloned()
            .collect::<Vec<_>>();
        let mut candidates = Vec::with_capacity(entries.len());
        for entry in &entries {
            let error = entry.runtime.lock().await.start_error.clone();
            candidates.push((
                entry.metadata.name.clone(),
                entry.metadata.directory.clone(),
                error,
            ));
        }
        for name in ownership_retry_targets(&candidates, changes) {
            match self.reload(&name).await {
                Ok(()) | Err(WorkerError::Stopped) => {}
                Err(WorkerError::Error(message)) => {
                    // Keep the new reason. A plugin that now fails for another
                    // reason is not retried again, which stops a retry loop.
                    let entry = self.plugins.read().await.get(&name).cloned();
                    if let Some(entry) = entry {
                        entry.runtime.lock().await.start_error = Some(message);
                    }
                }
            }
        }
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
        let fork_launcher = self.fork_launcher.lock().await.clone();
        cleanup_plugins(&plugins, &self.socket_directory, fork_launcher.as_ref()).await;
        // Closing the connection makes the root host stop anything it still runs for us.
        self.root_host.lock().await.take();
    }

    fn set_approved_plugin_permissions(
        &self,
        metadata: &PluginMetadata,
    ) -> std::result::Result<(), WorkerError> {
        if self.uses_root_host(metadata) {
            // The root host checks ownership itself; this account cannot change root files.
            return Ok(());
        }
        let directory = self.plugin_root.join(&metadata.directory);
        set_plugin_permissions_on_approved(
            &metadata.approved_directory,
            &directory,
            metadata.requires_root,
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
        if let Some(error) = root_plugin_refusal(
            &metadata,
            platform::effective_user_ids().0,
            self.root_host_enabled(),
        ) {
            return Err(WorkerError::Error(error));
        }
        self.set_approved_plugin_permissions(&metadata)?;
        Ok(())
    }

    async fn ensure_fork_launcher(
        &self,
        shutdown: &mut watch::Receiver<bool>,
    ) -> std::result::Result<Arc<ForkLauncher>, WorkerError> {
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(shutdown) {
            return Err(WorkerError::Stopped);
        }
        let mut current = self.fork_launcher.lock().await;
        if let Some(launcher) = current.as_ref() {
            return Ok(Arc::clone(launcher));
        }
        let launcher = ForkLauncher::spawn(
            &self.worker_executable,
            self.worker_bootstrap.as_str(),
            &self.plugin_root,
            &self.homebrew,
            &self.user,
            shutdown,
            LauncherEnvironment {
                auth_token: &self.auth_token,
                server_host: &self.server_host,
                server_port: self.server_port,
                chown_plugin_path: self.chown_plugin_path,
            },
        )
        .await
        .map_err(|error| match error {
            LauncherStartError::Failed(error) => WorkerError::Error(error),
            LauncherStartError::Interrupted => WorkerError::Stopped,
        })?;
        let launcher = Arc::new(launcher);
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(shutdown) {
            launcher.shutdown().await;
            return Err(WorkerError::Stopped);
        }
        let failure = &self
            .launcher_failure
            .as_ref()
            .expect("fork failure watch")
            .0;
        monitor_launcher_failure(launcher.failure(), failure.clone());
        *current = Some(Arc::clone(&launcher));
        Ok(launcher)
    }

    /// The root plugin host is enabled when its socket unit has created the socket.
    fn root_host_enabled(&self) -> bool {
        root_host::socket_path(self.user_ids.0).exists()
    }

    fn uses_root_host(&self, metadata: &PluginMetadata) -> bool {
        metadata.requires_root && platform::effective_user_ids().0 != 0
    }

    async fn ensure_root_host(&self) -> std::result::Result<Arc<RootHost>, WorkerError> {
        let mut current = self.root_host.lock().await;
        if let Some(host) = current.as_ref()
            && !host.is_failed()
        {
            return Ok(Arc::clone(host));
        }
        if !self.root_host_enabled() {
            return Err(WorkerError::Error(root_host::NOT_ENABLED.to_owned()));
        }
        let host = RootHost::connect(
            &root_host::socket_path(self.user_ids.0),
            HostEnvironment {
                auth_token: &self.auth_token,
                server_host: &self.server_host,
                server_port: self.server_port,
            },
        )
        .await
        .map_err(|error| {
            WorkerError::Error(format!("Could not reach the root plugin host: {error:#}"))
        })?;
        let host = Arc::new(host);
        *current = Some(Arc::clone(&host));
        Ok(host)
    }

    async fn launch_root_worker(
        &self,
        metadata: &PluginMetadata,
        shutdown: &mut watch::Receiver<bool>,
    ) -> std::result::Result<WorkerProcess, WorkerError> {
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(shutdown) {
            return Err(WorkerError::Stopped);
        }
        let host = self.ensure_root_host().await?;
        let config = self.launch_config(None, Some(&host));
        match launch_worker(&config, metadata, self.events.clone(), shutdown).await {
            Ok(worker) => Ok(worker),
            Err(WorkerLaunchError::Failed(error)) => Err(WorkerError::Error(error)),
            Err(WorkerLaunchError::Interrupted) => Err(WorkerError::Stopped),
        }
    }

    fn launch_config<'a>(
        &'a self,
        fork_launcher: Option<&'a Arc<ForkLauncher>>,
        root_host: Option<&'a Arc<RootHost>>,
    ) -> WorkerLaunchConfig<'a> {
        WorkerLaunchConfig {
            worker_executable: &self.worker_executable,
            worker_mode: self.worker_mode,
            worker_bootstrap: self.worker_bootstrap,
            fork_launcher,
            root_host,
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

fn monitor_launcher_failure(
    mut launcher_failure: watch::Receiver<bool>,
    registry_failure: watch::Sender<bool>,
) {
    drop(tokio::spawn(async move {
        loop {
            if *launcher_failure.borrow() {
                registry_failure.send_replace(true);
                return;
            }
            if launcher_failure.changed().await.is_err() {
                registry_failure.send_replace(true);
                return;
            }
        }
    }));
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
        Some(version) if !version.is_empty() => format!("{} (v{version})", plugin.name),
        _ => plugin.name.clone(),
    }
}

fn root_plugin_refusal(
    plugin: &PluginMetadata,
    effective_uid: u32,
    root_host_enabled: bool,
) -> Option<String> {
    (plugin.requires_root && effective_uid != 0 && !root_host_enabled).then(|| {
        format!(
            "Plugin {}: {}",
            display_name(plugin),
            root_host::NOT_ENABLED
        )
    })
}
fn approved_launch_json(plugin: &PluginMetadata) -> Value {
    json!({
        "descriptor": {
            "pid": std::process::id(),
            "fd": plugin.approved_directory.as_raw_fd(),
            "device": plugin.directory_device,
            "inode": plugin.directory_inode,
        },
        "metadata": {
            "name": plugin.name.as_str(),
            "author": plugin.author.as_str(),
            "flags": &plugin.flags,
            "api_version": plugin.api_version,
            "version": plugin.version.as_deref(),
        },
    })
}

fn discover_plugins(
    plugin_root: &Path,
    disabled: &[String],
) -> Result<HashMap<String, PluginMetadata>> {
    let root_fd = open_plugin_directory_fd(plugin_root)
        .with_context(|| format!("Failed to open plugin root {}", plugin_root.display()))?;
    let mut root_stream = open_directory_stream(&root_fd)
        .with_context(|| format!("Failed to read plugin root {}", plugin_root.display()))?;
    let mut directories = root_stream
        .iter()
        .filter_map(std::result::Result::ok)
        .collect::<Vec<_>>();
    directories.sort_unstable_by(|left, right| {
        left.file_name()
            .to_bytes()
            .cmp(right.file_name().to_bytes())
    });

    let mut plugins = HashMap::with_capacity(directories.len());
    for entry in directories {
        let directory_name = OsStr::from_bytes(entry.file_name().to_bytes());
        let directory_name_bytes = directory_name.as_bytes();
        if directory_name_bytes == b"." || directory_name_bytes == b".." {
            continue;
        }
        let directory_path = plugin_root.join(directory_name);
        if is_managed_plugin_directory(plugin_root, &directory_path) {
            continue;
        }
        let directory_fd = match open_directory_at(&root_fd, directory_name) {
            Ok(fd) => fd,
            Err(_) => continue,
        };
        let display_directory = directory_name.to_string_lossy().into_owned();
        let metadata = match read_plugin_metadata_from_fd(
            directory_fd,
            display_directory,
            &directory_path,
            disabled,
        ) {
            Ok(metadata) => metadata,
            Err(_) => continue,
        };
        plugins.insert(metadata.name.clone(), metadata);
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
    let directory_name = directory
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| WorkerError::Error("Plugin directory name is not valid UTF-8".to_owned()))?
        .to_owned();
    let directory_fd = open_plugin_directory_fd(directory).map_err(|error| {
        WorkerError::Error(format!(
            "Could not securely open plugin directory {}: {error}",
            directory.display()
        ))
    })?;
    read_plugin_metadata_from_fd(directory_fd, directory_name, directory, disabled_plugins)
}

fn read_plugin_metadata_from_fd(
    directory_fd: OwnedFd,
    directory_name: String,
    directory_path: &Path,
    disabled_plugins: &[String],
) -> std::result::Result<PluginMetadata, WorkerError> {
    let approved_directory = Arc::new(directory_fd);
    let directory_stat = fstat(approved_directory.as_fd()).map_err(|error| {
        WorkerError::Error(format!(
            "Could not inspect approved plugin directory {}: {error}",
            directory_path.display()
        ))
    })?;
    if SFlag::from_bits_truncate(directory_stat.st_mode) != SFlag::S_IFDIR {
        return Err(WorkerError::Error(format!(
            "Plugin directory is not a directory: {}",
            directory_path.display()
        )));
    }

    let metadata_json = read_json_at(approved_directory.as_ref(), OsStr::new("plugin.json"))
        .map_err(|error| {
            WorkerError::Error(format!(
                "Could not read plugin metadata {}: {error}",
                directory_path.join("plugin.json").display()
            ))
        })?
        .ok_or_else(|| {
            WorkerError::Error(format!(
                "Could not read plugin metadata {}: file is missing",
                directory_path.join("plugin.json").display()
            ))
        })?;
    let name = metadata_json
        .get("name")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .ok_or_else(|| {
            WorkerError::Error(format!(
                "Plugin metadata has no valid name: {}",
                directory_path.join("plugin.json").display()
            ))
        })?;
    let author = metadata_json
        .get("author")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            WorkerError::Error(format!(
                "Plugin metadata has no valid author: {}",
                directory_path.join("plugin.json").display()
            ))
        })?;
    let raw_flags = metadata_json
        .get("flags")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            WorkerError::Error(format!(
                "Plugin metadata has no valid flags list: {}",
                directory_path.join("plugin.json").display()
            ))
        })?;
    let flags = raw_flags
        .iter()
        .map(|flag| {
            flag.as_str().map(str::to_owned).ok_or_else(|| {
                WorkerError::Error(format!(
                    "Plugin metadata flags must be strings: {}",
                    directory_path.join("plugin.json").display()
                ))
            })
        })
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let requires_root = flags.iter().any(|flag| flag == "root");
    let debug = flags.iter().any(|flag| flag == "debug");
    let api_version = match metadata_json.get("api_version") {
        Some(version) => version.as_u64().ok_or_else(|| {
            WorkerError::Error(format!(
                "Plugin metadata has no valid API version: {}",
                directory_path.join("plugin.json").display()
            ))
        })?,
        None => 0,
    };

    let package =
        read_json_at(approved_directory.as_ref(), OsStr::new("package.json")).map_err(|error| {
            WorkerError::Error(format!(
                "Could not read package metadata {}: {error}",
                directory_path.join("package.json").display()
            ))
        })?;
    let (version, load_type) = match package {
        Some(package) => {
            let version = match package.get("version") {
                Some(Value::String(version)) => Some(version.clone()),
                Some(Value::Null) => None,
                _ => {
                    return Err(WorkerError::Error(format!(
                        "Package metadata has no valid version: {}",
                        directory_path.join("package.json").display()
                    )));
                }
            };
            (
                version,
                u8::from(package.get("type").and_then(Value::as_str) == Some("module")),
            )
        }
        None => (None, 0),
    };
    let passive = !is_regular_file_at(approved_directory.as_ref(), OsStr::new("main.py")).map_err(
        |error| {
            WorkerError::Error(format!(
                "Could not inspect plugin entry point {}: {error}",
                directory_path.join("main.py").display()
            ))
        },
    )?;

    Ok(PluginMetadata {
        name: name.to_owned(),
        directory: directory_name,
        author: author.to_owned(),
        requires_root,
        debug,
        flags,
        api_version,
        version,
        load_type,
        passive,
        disabled: disabled_plugins.iter().any(|disabled| disabled == name),
        approved_directory,
        directory_device: directory_stat.st_dev as u64,
        directory_inode: directory_stat.st_ino as u64,
    })
}

fn plugin_ownership_error(directory_path: &Path, owner_uid: u32, service_uid: u32) -> String {
    format!(
        "Plugin directory {} is owned by uid {owner_uid}, not the service user (uid {service_uid}); change its owner to the service user",
        directory_path.display()
    )
}

fn is_plugin_ownership_error(message: &str) -> bool {
    message.starts_with("Plugin directory ") && message.contains(" is owned by uid ")
}

fn set_plugin_permissions_on_approved(
    directory_fd: &OwnedFd,
    directory_path: &Path,
    requires_root: bool,
    host_user_ids: (u32, u32),
    enabled: bool,
) -> std::result::Result<(), WorkerError> {
    if !enabled {
        return Ok(());
    }

    let mut root = open_directory_stream(directory_fd).map_err(|error| {
        WorkerError::Error(format!(
            "Could not securely open approved plugin directory {}: {error}",
            directory_path.display()
        ))
    })?;
    let effective_user_ids = platform::effective_user_ids();
    let tree_user_ids = if requires_root {
        effective_user_ids
    } else {
        host_user_ids
    };
    let locked_mode = if effective_user_ids.0 == 0 {
        Mode::empty()
    } else {
        Mode::from_bits_truncate(0o700)
    };
    if effective_user_ids.0 != 0 {
        let owner = fstat(&root).map_err(io::Error::from).map_err(|error| {
            WorkerError::Error(format!(
                "Could not inspect plugin directory {}: {error}",
                directory_path.display()
            ))
        })?;
        if owner.st_uid != effective_user_ids.0 {
            // A previous root loader leaves plugin directories owned by root.
            return Err(WorkerError::Error(plugin_ownership_error(
                directory_path,
                owner.st_uid,
                effective_user_ids.0,
            )));
        }
    }

    lock_directory(&root, effective_user_ids, locked_mode).map_err(|error| {
        WorkerError::Error(format!(
            "Could not secure plugin directory {}: {error}",
            directory_path.display()
        ))
    })?;
    update_directory_contents(
        &mut root,
        tree_user_ids,
        effective_user_ids,
        locked_mode,
        true,
    )
    .map_err(|error| {
        WorkerError::Error(format!(
            "Could not update plugin directory permissions {}: {error}",
            directory_path.display()
        ))
    })?;
    fchmod(&root, Mode::from_bits_truncate(0o755)).map_err(|error| {
        WorkerError::Error(format!(
            "Could not chmod plugin directory {}: {error}",
            directory_path.display()
        ))
    })?;
    Ok(())
}

fn open_plugin_directory_fd(path: &Path) -> io::Result<OwnedFd> {
    let flags = OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC;
    let mut current = if path.is_absolute() {
        open("/", flags, Mode::empty())
    } else {
        open(".", flags, Mode::empty())
    }
    .map_err(io::Error::from)?;

    for component in path.components() {
        let name = match component {
            Component::RootDir | Component::CurDir => continue,
            Component::ParentDir => OsStr::new(".."),
            Component::Normal(name) => name,
            Component::Prefix(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "plugin path has an unsupported prefix",
                ));
            }
        };
        current = openat(&current, name, flags, Mode::empty()).map_err(io::Error::from)?;
    }

    Ok(current)
}

fn open_directory_at(parent: &impl AsFd, name: &OsStr) -> io::Result<OwnedFd> {
    openat(
        parent,
        name,
        OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC,
        Mode::empty(),
    )
    .map_err(io::Error::from)
}

fn open_directory_stream(directory_fd: &impl AsFd) -> io::Result<Dir> {
    let fd = open_directory_at(directory_fd, OsStr::new("."))?;
    Dir::from_fd(fd).map_err(io::Error::from)
}

fn open_regular_file_at(parent: &impl AsFd, name: &OsStr) -> io::Result<Option<OwnedFd>> {
    let fd = match openat(
        parent,
        name,
        OFlag::O_RDONLY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC | OFlag::O_NONBLOCK,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::ENOENT) => return Ok(None),
        Err(error) => return Err(io::Error::from(error)),
    };
    let metadata = fstat(&fd).map_err(io::Error::from)?;
    if SFlag::from_bits_truncate(metadata.st_mode) != SFlag::S_IFREG {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "plugin metadata file is not a regular file",
        ));
    }
    Ok(Some(fd))
}

fn read_json_at(parent: &impl AsFd, name: &OsStr) -> io::Result<Option<Value>> {
    let Some(fd) = open_regular_file_at(parent, name)? else {
        return Ok(None);
    };
    serde_json::from_reader(File::from(fd))
        .map(Some)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn is_regular_file_at(parent: &impl AsFd, name: &OsStr) -> io::Result<bool> {
    let metadata = match fstatat(parent, name, AtFlags::empty()) {
        Ok(metadata) => metadata,
        Err(Errno::ENOENT) => return Ok(false),
        Err(error) => return Err(io::Error::from(error)),
    };
    Ok(SFlag::from_bits_truncate(metadata.st_mode) == SFlag::S_IFREG)
}

fn lock_directory(
    directory: &Dir,
    effective_user_ids: (u32, u32),
    locked_mode: Mode,
) -> io::Result<()> {
    chown_descriptor(directory, effective_user_ids)?;
    fchmod(directory, locked_mode).map_err(io::Error::from)
}

fn restore_directory_owner(directory: &Dir, owner: (u32, u32)) -> io::Result<()> {
    chown_descriptor(directory, owner)?;
    fchmod(directory, Mode::from_bits_truncate(0o755)).map_err(io::Error::from)
}

fn chown_descriptor(fd: &impl AsFd, owner: (u32, u32)) -> io::Result<()> {
    let metadata = fstat(fd).map_err(io::Error::from)?;
    if metadata.st_uid != owner.0 || metadata.st_gid != owner.1 {
        unistd::fchown(
            fd,
            Some(Uid::from_raw(owner.0)),
            Some(Gid::from_raw(owner.1)),
        )
        .map_err(io::Error::from)?;
    }
    Ok(())
}

fn set_file_permissions(fd: &OwnedFd, owner: (u32, u32)) -> io::Result<()> {
    let metadata = fstat(fd).map_err(io::Error::from)?;
    let owner_changes = metadata.st_uid != owner.0 || metadata.st_gid != owner.1;
    let mode_changes = metadata.st_mode & 0o7777 != 0o755;
    if metadata.st_nlink > 1 && (owner_changes || mode_changes) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "refusing to change a multiply-linked plugin file",
        ));
    }
    if owner_changes {
        unistd::fchown(
            fd,
            Some(Uid::from_raw(owner.0)),
            Some(Gid::from_raw(owner.1)),
        )
        .map_err(io::Error::from)?;
    }
    if mode_changes {
        fchmod(fd, Mode::from_bits_truncate(0o755)).map_err(io::Error::from)?;
    }
    Ok(())
}

fn set_protected_file_permissions(fd: &OwnedFd, owner: (u32, u32)) -> io::Result<()> {
    let metadata = fstat(fd).map_err(io::Error::from)?;
    let owner_changes = metadata.st_uid != owner.0 || metadata.st_gid != owner.1;
    let mode_changes = metadata.st_mode & 0o7777 != 0o755;
    if metadata.st_nlink > 1 && (owner_changes || mode_changes) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "refusing to change multiply-linked plugin metadata",
        ));
    }
    if !owner_changes && !mode_changes {
        return Ok(());
    }

    fchmod(fd, Mode::empty()).map_err(io::Error::from)?;
    if owner_changes {
        unistd::fchown(
            fd,
            Some(Uid::from_raw(owner.0)),
            Some(Gid::from_raw(owner.1)),
        )
        .map_err(io::Error::from)?;
    }
    fchmod(fd, Mode::from_bits_truncate(0o755)).map_err(io::Error::from)
}

fn update_directory_contents(
    directory: &mut Dir,
    tree_owner: (u32, u32),
    effective_user_ids: (u32, u32),
    locked_mode: Mode,
    protect_metadata: bool,
) -> io::Result<()> {
    let parent_fd = open_directory_at(&*directory, OsStr::new("."))?;
    for entry in directory.iter() {
        let entry = entry.map_err(io::Error::from)?;
        let name = entry.file_name();
        let name_bytes = name.to_bytes();
        if name_bytes == b"." || name_bytes == b".." {
            continue;
        }
        let is_plugin_metadata = protect_metadata && name_bytes == b"plugin.json";
        let metadata =
            fstatat(&parent_fd, name, AtFlags::AT_SYMLINK_NOFOLLOW).map_err(io::Error::from)?;
        let file_type = SFlag::from_bits_truncate(metadata.st_mode);

        if file_type == SFlag::S_IFLNK {
            if is_plugin_metadata {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "plugin metadata must not be a symbolic link",
                ));
            }
            continue;
        }

        if file_type == SFlag::S_IFDIR {
            let mut child = match Dir::openat(
                &parent_fd,
                name,
                OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC,
                Mode::empty(),
            ) {
                Ok(child) => child,
                Err(Errno::ELOOP | Errno::ENOENT) => continue,
                Err(error) => return Err(io::Error::from(error)),
            };
            lock_directory(&child, effective_user_ids, locked_mode)?;
            if is_plugin_metadata {
                fchmod(&child, Mode::from_bits_truncate(0o755)).map_err(io::Error::from)?;
            } else {
                update_directory_contents(
                    &mut child,
                    tree_owner,
                    effective_user_ids,
                    locked_mode,
                    false,
                )?;
                restore_directory_owner(&child, tree_owner)?;
            }
            continue;
        }

        if file_type != SFlag::S_IFREG && file_type != SFlag::S_IFIFO {
            continue;
        }
        let fd = match openat(
            &parent_fd,
            name,
            OFlag::O_RDONLY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC | OFlag::O_NONBLOCK,
            Mode::empty(),
        ) {
            Ok(fd) => fd,
            Err(Errno::ELOOP | Errno::ENOENT) => continue,
            Err(error) => return Err(io::Error::from(error)),
        };
        let opened_metadata = fstat(&fd).map_err(io::Error::from)?;
        let opened_type = SFlag::from_bits_truncate(opened_metadata.st_mode);
        if opened_type == SFlag::S_IFDIR {
            let mut child = Dir::from_fd(fd).map_err(io::Error::from)?;
            lock_directory(&child, effective_user_ids, locked_mode)?;
            if is_plugin_metadata {
                fchmod(&child, Mode::from_bits_truncate(0o755)).map_err(io::Error::from)?;
            } else {
                update_directory_contents(
                    &mut child,
                    tree_owner,
                    effective_user_ids,
                    locked_mode,
                    false,
                )?;
                restore_directory_owner(&child, tree_owner)?;
            }
        } else if opened_type == SFlag::S_IFREG || opened_type == SFlag::S_IFIFO {
            if is_plugin_metadata {
                set_protected_file_permissions(&fd, effective_user_ids)?;
            } else {
                set_file_permissions(&fd, tree_owner)?;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod plugin_permission_tests {
    use super::*;
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
    fn set_plugin_permissions_inner(
        directory: &Path,
        requires_root: bool,
        host_user_ids: (u32, u32),
        enabled: bool,
    ) -> std::result::Result<(), WorkerError> {
        if !enabled {
            return Ok(());
        }

        let directory_fd = open_plugin_directory_fd(directory).map_err(|error| {
            WorkerError::Error(format!(
                "Could not securely open plugin directory {}: {error}",
                directory.display()
            ))
        })?;
        set_plugin_permissions_on_approved(
            &directory_fd,
            directory,
            requires_root,
            host_user_ids,
            enabled,
        )
    }

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("decky-permissions-{}", Uuid::new_v4()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn set_mode(path: &Path, mode: u32) {
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    }

    fn host_user_ids(effective: (u32, u32)) -> (u32, u32) {
        if effective.0 == 0 {
            (65_534, 65_534)
        } else {
            effective
        }
    }

    fn write_approved_plugin(directory: &Path, flags: &[&str]) {
        fs::create_dir_all(directory).unwrap();
        let metadata = serde_json::json!({
            "name": "Example",
            "author": "Unit Test",
            "flags": flags,
            "api_version": 3,
        });
        fs::write(directory.join("plugin.json"), metadata.to_string()).unwrap();
        fs::write(
            directory.join("package.json"),
            br#"{"version":"1.2.3","type":"module"}"#,
        )
        .unwrap();
        fs::write(directory.join("main.py"), b"pass\n").unwrap();
        set_mode(directory, 0o700);
        set_mode(&directory.join("plugin.json"), 0o600);
        set_mode(&directory.join("package.json"), 0o600);
        set_mode(&directory.join("main.py"), 0o600);
    }

    #[test]
    fn root_plugin_policy_matches_process_credentials() {
        let temporary = TestDirectory::new();
        let plugin_root = temporary.0.join("plugins");
        let plugin = plugin_root.join("Example");
        write_approved_plugin(&plugin, &["root"]);
        let mut discovered = discover_plugins(&plugin_root, &[]).unwrap();
        let metadata = discovered.get_mut("Example").unwrap();
        for (requires_root, uid, root_host_enabled, denied) in [
            (true, 1000, false, true),
            (true, 1000, true, false),
            (true, 0, false, false),
            (false, 1000, false, false),
            (false, 0, false, false),
        ] {
            metadata.requires_root = requires_root;
            assert_eq!(
                root_plugin_refusal(metadata, uid, root_host_enabled).is_some(),
                denied
            );
        }
    }

    #[tokio::test]
    async fn disabled_root_plugin_can_be_registered_again_during_recovery() {
        let temporary = TestDirectory::new();
        let plugin_root = temporary.0.join("plugins");
        let plugin = plugin_root.join("Example");
        write_approved_plugin(&plugin, &["root"]);
        fs::create_dir(temporary.0.join("settings")).unwrap();
        fs::write(
            temporary.0.join("settings/loader.json"),
            br#"{"disabled_plugins":["Example"]}"#,
        )
        .unwrap();
        let (events, _events) = broadcast::channel(8);
        let (_shutdown, shutdown) = watch::channel(false);
        let ids = platform::effective_user_ids();
        let user = unistd::User::from_uid(Uid::current())
            .unwrap()
            .unwrap()
            .name;
        let registry = PluginRegistry::start(
            WorkerConfiguration {
                executable: std::env::current_exe().unwrap(),
                mode: WorkerMode::Exec,
                bootstrap: WorkerBootstrap::Lean,
                auth_token: "recovery-test".into(),
                server_host: "127.0.0.1".into(),
                server_port: 1339,
                chown_plugin_path: false,
            },
            plugin_root,
            temporary.0.clone(),
            user,
            ids,
            events,
            shutdown,
        )
        .await
        .unwrap();
        let removed = registry.remove("Example", false).await;
        let restored = registry.import_directory("Example", false, false).await;
        let directory = registry.directory("Example").await;
        let listed = registry.list().await;
        registry.shutdown().await;
        removed.unwrap();
        restored.unwrap();
        assert_eq!(directory, Some(plugin));
        let entry = listed
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "Example")
            .unwrap();
        assert_eq!(entry["disabled"], true);
    }

    #[test]
    fn linked_backend_entrypoints_remain_active() {
        let temporary = TestDirectory::new();
        let plugin = temporary.0.join("plugin");
        write_approved_plugin(&plugin, &[]);
        fs::rename(plugin.join("main.py"), plugin.join("backend.py")).unwrap();
        symlink("backend.py", plugin.join("main.py")).unwrap();
        let metadata = read_plugin_metadata(&plugin, &[]).unwrap();
        assert!(!metadata.passive);
    }

    #[test]
    fn approved_metadata_and_permissions_stay_bound_after_parent_replacement() {
        let temporary = TestDirectory::new();
        let plugin_root = temporary.0.join("plugins");
        let plugin_path = plugin_root.join("Example");
        write_approved_plugin(&plugin_path, &[]);

        let metadata = {
            let discovered = discover_plugins(&plugin_root, &[]).unwrap();
            discovered.get("Example").unwrap().clone()
        };
        assert!(!metadata.requires_root);

        let displaced_root = temporary.0.join("plugins-displaced");
        fs::rename(&plugin_root, &displaced_root).unwrap();
        let replacement_path = plugin_root.join("Example");
        write_approved_plugin(&replacement_path, &["root"]);
        let replacement = read_plugin_metadata(&replacement_path, &[]).unwrap();
        assert!(replacement.requires_root);
        assert_ne!(replacement.directory_inode, metadata.directory_inode);

        let effective = platform::effective_user_ids();
        let host = host_user_ids(effective);
        set_plugin_permissions_on_approved(
            &metadata.approved_directory,
            &replacement_path,
            metadata.requires_root,
            host,
            true,
        )
        .unwrap();

        let retained_plugin = displaced_root.join("Example");
        let standard_file = fs::metadata(retained_plugin.join("main.py")).unwrap();
        assert_eq!((standard_file.uid(), standard_file.gid()), host);
        assert_eq!(standard_file.mode() & 0o7777, 0o755);
        assert_eq!(
            fs::metadata(retained_plugin.join("plugin.json"))
                .unwrap()
                .mode()
                & 0o7777,
            0o755
        );

        assert_eq!(
            fs::metadata(&replacement_path).unwrap().mode() & 0o7777,
            0o700
        );
        assert_eq!(
            fs::metadata(replacement_path.join("main.py"))
                .unwrap()
                .mode()
                & 0o7777,
            0o600
        );
        assert_eq!(
            fs::metadata(replacement_path.join("plugin.json"))
                .unwrap()
                .mode()
                & 0o7777,
            0o600
        );
        set_plugin_permissions_on_approved(
            &replacement.approved_directory,
            &replacement_path,
            replacement.requires_root,
            host,
            true,
        )
        .unwrap();
        let root_file = fs::metadata(replacement_path.join("main.py")).unwrap();
        assert_eq!((root_file.uid(), root_file.gid()), effective);
        assert_eq!(root_file.mode() & 0o7777, 0o755);
    }

    #[test]
    fn approved_metadata_reads_require_regular_nofollow_json_files() {
        let temporary = TestDirectory::new();
        let plugin = temporary.0.join("plugin");
        write_approved_plugin(&plugin, &[]);
        let external_plugin_json = temporary.0.join("external-plugin.json");
        fs::write(
            &external_plugin_json,
            br#"{"name":"Replacement","author":"Outside","flags":["root"]}"#,
        )
        .unwrap();
        fs::remove_file(plugin.join("plugin.json")).unwrap();
        symlink(&external_plugin_json, plugin.join("plugin.json")).unwrap();
        assert!(read_plugin_metadata(&plugin, &[]).is_err());

        fs::remove_file(plugin.join("plugin.json")).unwrap();
        fs::write(
            plugin.join("plugin.json"),
            br#"{"name":"Example","author":"Unit Test","flags":[],"api_version":0}"#,
        )
        .unwrap();
        let external_package_json = temporary.0.join("external-package.json");
        fs::write(
            &external_package_json,
            br#"{"version":"9.9.9","type":"module"}"#,
        )
        .unwrap();
        fs::remove_file(plugin.join("package.json")).unwrap();
        symlink(&external_package_json, plugin.join("package.json")).unwrap();
        assert!(read_plugin_metadata(&plugin, &[]).is_err());
        fs::remove_file(plugin.join("plugin.json")).unwrap();
        unistd::mkfifo(&plugin.join("plugin.json"), Mode::from_bits_truncate(0o600)).unwrap();
        assert!(read_plugin_metadata(&plugin, &[]).is_err());
    }

    #[test]
    fn metadata_stays_protected_while_standard_and_root_plugin_permissions_are_preserved() {
        let temporary = TestDirectory::new();
        let root = fs::canonicalize(&temporary.0).unwrap();
        let plugin = root.join("plugin");
        let nested = plugin.join("nested");
        fs::create_dir_all(&nested).unwrap();
        fs::write(
            plugin.join("plugin.json"),
            br#"{"name":"Example","flags":[]}"#,
        )
        .unwrap();
        fs::write(plugin.join("main.py"), b"pass\n").unwrap();
        fs::write(nested.join("code.py"), b"pass\n").unwrap();
        set_mode(&plugin, 0o777);
        set_mode(&plugin.join("plugin.json"), 0o600);
        set_mode(&plugin.join("main.py"), 0o600);
        set_mode(&nested, 0o777);
        set_mode(&nested.join("code.py"), 0o600);

        let outside = root.join("outside.py");
        fs::write(&outside, b"outside\n").unwrap();
        set_mode(&outside, 0o640);
        symlink(&outside, nested.join("linked.py")).unwrap();

        let outside_directory = root.join("outside-directory");
        fs::create_dir(&outside_directory).unwrap();
        let outside_directory_file = outside_directory.join("victim.py");
        fs::write(&outside_directory_file, b"outside directory\n").unwrap();
        set_mode(&outside_directory_file, 0o640);
        symlink(&outside_directory, nested.join("linked-directory")).unwrap();
        let socket_path = nested.join("worker.sock");
        let _socket = std::os::unix::net::UnixListener::bind(&socket_path).unwrap();
        let effective = platform::effective_user_ids();
        let host = host_user_ids(effective);
        set_plugin_permissions_inner(&plugin, false, host, true).unwrap();
        std::os::unix::net::UnixStream::connect(&socket_path).unwrap();

        let plugin_metadata = fs::metadata(&plugin).unwrap();
        assert_eq!((plugin_metadata.uid(), plugin_metadata.gid()), effective);
        assert_eq!(plugin_metadata.mode() & 0o7777, 0o755);
        let metadata_file = fs::metadata(plugin.join("plugin.json")).unwrap();
        assert_eq!((metadata_file.uid(), metadata_file.gid()), effective);
        assert_eq!(metadata_file.mode() & 0o7777, 0o755);
        let standard_file = fs::metadata(plugin.join("main.py")).unwrap();
        assert_eq!((standard_file.uid(), standard_file.gid()), host);
        assert_eq!(standard_file.mode() & 0o7777, 0o755);
        let outside_metadata = fs::metadata(&outside).unwrap();
        assert_eq!((outside_metadata.uid(), outside_metadata.gid()), effective);
        assert_eq!(outside_metadata.mode() & 0o7777, 0o640);

        assert_eq!(
            fs::metadata(&outside_directory_file).unwrap().mode() & 0o7777,
            0o640
        );
        set_plugin_permissions_inner(&plugin, true, host, true).unwrap();
        let root_plugin_file = fs::metadata(plugin.join("main.py")).unwrap();
        assert_eq!((root_plugin_file.uid(), root_plugin_file.gid()), effective);
        assert_eq!(root_plugin_file.mode() & 0o7777, 0o755);

        set_plugin_permissions_inner(&root.join("missing"), false, host, false).unwrap();
    }

    #[test]
    fn ancestor_symlinks_and_external_hardlinks_are_not_changed() {
        let temporary = TestDirectory::new();
        let root = fs::canonicalize(&temporary.0).unwrap();
        let outside_tree = root.join("outside-tree");
        let outside_plugin = outside_tree.join("plugin");
        fs::create_dir_all(&outside_plugin).unwrap();
        fs::write(outside_plugin.join("plugin.json"), b"{}\n").unwrap();
        fs::write(outside_plugin.join("main.py"), b"pass\n").unwrap();
        set_mode(&outside_plugin, 0o700);
        set_mode(&outside_plugin.join("plugin.json"), 0o600);
        set_mode(&outside_plugin.join("main.py"), 0o600);

        let ancestor_link = root.join("plugin-root-link");
        symlink(&outside_tree, &ancestor_link).unwrap();
        let effective = platform::effective_user_ids();
        assert!(
            set_plugin_permissions_inner(&ancestor_link.join("plugin"), false, effective, true,)
                .is_err()
        );
        assert_eq!(
            fs::metadata(&outside_plugin).unwrap().mode() & 0o7777,
            0o700
        );
        assert_eq!(
            fs::metadata(outside_plugin.join("plugin.json"))
                .unwrap()
                .mode()
                & 0o7777,
            0o600
        );
        assert_eq!(
            fs::metadata(outside_plugin.join("main.py")).unwrap().mode() & 0o7777,
            0o600
        );

        let plugin = root.join("hardlink-plugin");
        fs::create_dir(&plugin).unwrap();
        fs::write(plugin.join("plugin.json"), b"{}\n").unwrap();
        let outside_file = root.join("outside-hardlink.py");
        fs::write(&outside_file, b"outside\n").unwrap();
        set_mode(&outside_file, 0o600);
        fs::hard_link(&outside_file, plugin.join("linked.py")).unwrap();
        assert!(set_plugin_permissions_inner(&plugin, false, effective, true).is_err());
        let outside_metadata = fs::metadata(&outside_file).unwrap();
        assert_eq!((outside_metadata.uid(), outside_metadata.gid()), effective);
        assert_eq!(outside_metadata.mode() & 0o7777, 0o600);
    }

    #[test]
    fn protected_metadata_cannot_reference_a_mutable_outside_file() {
        let temporary = TestDirectory::new();
        let root = fs::canonicalize(&temporary.0).unwrap();
        let plugin = root.join("plugin");
        fs::create_dir(&plugin).unwrap();
        let outside = root.join("outside.json");
        fs::write(&outside, br#"{"name":"Example","flags":[]}"#).unwrap();
        set_mode(&outside, 0o640);
        symlink(&outside, plugin.join("plugin.json")).unwrap();
        let before = fs::metadata(&outside).unwrap();
        assert!(
            set_plugin_permissions_inner(&plugin, false, platform::effective_user_ids(), true)
                .is_err()
        );
        let after = fs::metadata(&outside).unwrap();
        assert_eq!(
            (after.uid(), after.gid(), after.mode()),
            (before.uid(), before.gid(), before.mode())
        );
    }
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
        let mut mask = libc::IN_CREATE
            | libc::IN_CLOSE_WRITE
            | libc::IN_MOVED_TO
            | libc::IN_DELETE
            | libc::IN_MOVED_FROM
            | libc::IN_DELETE_SELF
            | libc::IN_MOVE_SELF
            | libc::IN_ONLYDIR
            | libc::IN_DONT_FOLLOW;
        // Owner changes matter only on the plugin root, the plugin directories and
        // their direct entries. Deeper directories do not need the extra events.
        if directory == self.plugin_root || directory.parent() == Some(self.plugin_root.as_path()) {
            mask |= libc::IN_ATTRIB;
        }
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

    fn affected_plugins(
        &mut self,
        events: Vec<InotifyEvent>,
    ) -> io::Result<(Vec<String>, AttributeChanges)> {
        let mut changed = HashSet::new();
        let mut attributes = AttributeChanges::default();
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
            attributes.record(&self.plugin_root, &path, event.mask);
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
        Ok((changed, attributes))
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

/// Directories whose attributes changed, as seen by the plugin root watcher.
#[derive(Default)]
struct AttributeChanges {
    plugin_root: bool,
    directories: HashSet<String>,
}

impl AttributeChanges {
    fn everything() -> Self {
        Self {
            plugin_root: true,
            directories: HashSet::new(),
        }
    }

    fn is_empty(&self) -> bool {
        !self.plugin_root && self.directories.is_empty()
    }

    fn merge(&mut self, other: Self) {
        self.plugin_root |= other.plugin_root;
        self.directories.extend(other.directories);
    }

    /// Records an event on `path`: the plugin root itself, a plugin directory, or
    /// an entry inside a plugin directory.
    fn record(&mut self, plugin_root: &Path, path: &Path, mask: u32) {
        if mask & libc::IN_ATTRIB == 0 {
            return;
        }
        if path == plugin_root {
            self.plugin_root = true;
            return;
        }
        let Ok(relative) = path.strip_prefix(plugin_root) else {
            return;
        };
        if let Some(Component::Normal(directory)) = relative.components().next()
            && let Some(directory) = directory.to_str()
            && !directory.starts_with('.')
        {
            self.directories.insert(directory.to_owned());
        }
    }

    fn covers(&self, directory: &str) -> bool {
        self.plugin_root || self.directories.contains(directory)
    }
}

/// Names of the plugins to load again: refused for their owner, and named by
/// `changes`. Each candidate is (name, directory, start error).
fn ownership_retry_targets(
    candidates: &[(String, String, Option<String>)],
    changes: &AttributeChanges,
) -> Vec<String> {
    let mut targets = candidates
        .iter()
        .filter(|(_, directory, error)| {
            changes.covers(directory) && error.as_deref().is_some_and(is_plugin_ownership_error)
        })
        .map(|(name, _, _)| name.clone())
        .collect::<Vec<_>>();
    targets.sort_unstable();
    targets
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
    // An owner change made before the watcher existed leaves no event to wait for.
    match registry.upgrade() {
        Some(owner) => {
            owner
                .retry_ownership_failed(&AttributeChanges::everything())
                .await
        }
        None => return,
    }
    let mut pending_attributes = AttributeChanges::default();
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
            _ = sleep(OWNERSHIP_RETRY_DEBOUNCE), if !pending_attributes.is_empty() => {
                let Some(owner) = registry.upgrade() else {
                    return;
                };
                if owner.shutting_down.load(Ordering::Acquire) {
                    return;
                }
                if owner.live_reload_paused.load(Ordering::Acquire) == 0 {
                    owner
                        .retry_ownership_failed(&std::mem::take(&mut pending_attributes))
                        .await;
                }
                continue;
            }
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

        let (changed_plugins, attributes) = match watcher.affected_plugins(events) {
            Ok(changed) => changed,
            Err(error) => {
                eprintln!("Could not update plugin live reload watches: {error}");
                return;
            }
        };
        pending_attributes.merge(attributes);
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
    // Prefer the private per-user runtime directory that systemd --user provides.
    let temporary_directory = ["TMPDIR", "XDG_RUNTIME_DIR"]
        .into_iter()
        .find_map(|name| std::env::var_os(name).filter(|value| !value.is_empty()))
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
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

async fn start_root_worker(
    host: &Arc<RootHost>,
    plugin: &PluginMetadata,
) -> std::result::Result<(WorkerSupervisor, PathBuf), WorkerLaunchError> {
    let id = Uuid::new_v4().to_string();
    let (pid, socket_path) = host
        .start_child(&id, &plugin.directory)
        .await
        .map_err(|error| {
            WorkerLaunchError::Failed(format!(
                "Could not start {} in the root plugin host: {error:#}",
                display_name(plugin)
            ))
        })?;
    let exit = match process_exit_notification(pid) {
        Ok(exit) => exit,
        Err(error) => {
            if host.stop_child(&id).await.is_ok() {
                let _ = host.forget_child(&id).await;
            }
            return Err(WorkerLaunchError::Failed(format!(
                "Could not monitor root plugin exit: {error}"
            )));
        }
    };
    let supervisor = WorkerSupervisor::Root {
        host: Arc::clone(host),
        id,
        exit,
    };
    Ok((supervisor, socket_path))
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
        root_host,
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
    let requires_root = plugin.requires_root;
    let uses_root_host = requires_root && platform::effective_user_ids().0 != 0;
    let (mut supervisor, socket_path) = if uses_root_host {
        let Some(host) = root_host else {
            return Err(WorkerLaunchError::Failed(root_host::NOT_ENABLED.to_owned()));
        };
        start_root_worker(host, plugin).await?
    } else {
        let socket_path = socket_directory
            .join(if requires_root { "root" } else { "user" })
            .join(socket_filename());
        if socket_path.as_os_str().as_bytes().len() >= 108 {
            return Err(WorkerLaunchError::Failed(format!(
                "Worker IPC socket path exceeds the Linux AF_UNIX limit: {}",
                socket_path.display()
            )));
        }
        let approved_launch = approved_launch_json(plugin);

        let supervisor = match worker_mode {
            WorkerMode::Exec => {
                let mut command = Command::new(worker_executable);
                let approved_launch_argument =
                    serde_json::to_string(&approved_launch).map_err(|error| {
                        WorkerLaunchError::Failed(format!(
                            "Failed to serialize approved plugin launch metadata: {error}"
                        ))
                    })?;
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
                    .arg("--approved-launch")
                    .arg(&approved_launch_argument)
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
                        "approved_launch": &approved_launch,
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
        (supervisor, socket_path)
    };
    if shutdown_requested(shutdown) {
        supervisor.abort_startup(&socket_path).await;
        return Err(WorkerLaunchError::Interrupted);
    }
    let release = match &supervisor {
        WorkerSupervisor::Direct { .. } => None,
        WorkerSupervisor::Fork { launcher, id, .. } => Some(launcher.release_child(id).await),
        WorkerSupervisor::Root { host, id, .. } => Some(host.release_child(id).await),
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
    let mut retry = SOCKET_FIRST_RETRY;
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
                    _ = sleep(retry) => retry = (retry * 2).min(SOCKET_RETRY_INTERVAL),
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

async fn reap_root_child(
    mut stop: mpsc::UnboundedReceiver<()>,
    host: Arc<RootHost>,
    id: String,
    exit: AsyncFd<OwnedFd>,
) -> io::Result<ExitStatus> {
    let to_io = |error: anyhow::Error| io::Error::other(format!("{error:#}"));
    let status = tokio::select! {
        result = exit.readable() => {
            result.map(drop)?;
            // The host reaps the exited child when asked for its status.
            host.child_status(&id)
                .await
                .map_err(to_io)?
                .ok_or_else(|| io::Error::other("root plugin exited without a host wait status"))?
        }
        _ = stop.recv() => host.stop_child(&id).await.map_err(to_io)?,
    };
    host.forget_child(&id).await.map_err(to_io)?;
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
    // Only the small envelope fields are decoded. A call result stays raw JSON text.
    let message: HashMap<String, &RawValue> = serde_json::from_slice(line).map_err(|_| ())?;
    let field = |key: &str| -> Option<Value> {
        message
            .get(key)
            .and_then(|raw| serde_json::from_str(raw.get()).ok())
    };
    match field("type").as_ref().and_then(Value::as_i64) {
        Some(1) => {
            let Some(Value::String(id)) = field("id") else {
                return Err(());
            };
            let success = field("success")
                .as_ref()
                .and_then(Value::as_bool)
                .ok_or(())?;
            let result = *message.get("res").ok_or(())?;
            let response = if success {
                Ok(result.to_owned())
            } else {
                let result = serde_json::from_str(result.get()).map_err(|_| ())?;
                Err(WorkerError::Error(value_error_string(&result)))
            };
            let sender = state.lock().await.requests.remove(&id);
            if let Some(sender) = sender {
                let _ = sender.send(response);
            }
        }
        Some(2) => {
            if let (Some(Value::String(event)), Some(args)) = (field("event"), field("args")) {
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
mod worker_message_tests {
    use super::*;

    fn pending(id: &str) -> (Arc<Mutex<PendingState>>, oneshot::Receiver<CallReply>) {
        let (sender, receiver) = oneshot::channel();
        let mut requests = HashMap::new();
        requests.insert(id.to_owned(), sender);
        let state = PendingState {
            alive: true,
            stopping: false,
            failure: None,
            requests,
        };
        (Arc::new(Mutex::new(state)), receiver)
    }

    #[tokio::test]
    async fn plugin_results_keep_their_exact_json() {
        let (state, receiver) = pending("a");
        let (events, _) = broadcast::channel(1);
        let result = r#"{"big":123456789012345678901234567890,"small":1e-7,"text":"caf\u00e9 ☕","list":[1.50,null]}"#;
        let line = format!(r#"{{"type":1,"id":"a","success":true,"res":{result}}}"#);
        process_worker_message(line.as_bytes(), "Example", &state, &events)
            .await
            .unwrap();
        assert_eq!(receiver.await.unwrap().unwrap().get(), result);
    }

    #[tokio::test]
    async fn plugin_errors_become_messages_and_bad_lines_are_rejected() {
        let (state, receiver) = pending("b");
        let (events, _) = broadcast::channel(1);
        let line = br#"{"type":1,"id":"b","success":false,"res":"it broke"}"#;
        process_worker_message(line, "Example", &state, &events)
            .await
            .unwrap();
        match receiver.await.unwrap() {
            Err(WorkerError::Error(message)) => assert_eq!(message, "it broke"),
            other => panic!("unexpected reply {other:?}"),
        }
        for bad in [
            &b"[1]"[..],
            b"{\"type\":1,\"id\":\"b\"",
            br#"{"type":1,"id":"b","success":true}"#,
        ] {
            assert!(
                process_worker_message(bad, "Example", &state, &events)
                    .await
                    .is_err()
            );
        }
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

#[cfg(test)]
mod launcher_failure_tests {
    use super::*;

    struct TestDirectory(PathBuf);

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn lost_launcher_monitor_enters_failure_shutdown_and_removes_runtime_sockets() {
        let homebrew =
            TestDirectory(std::env::temp_dir().join(format!("decky-monitor-{}", Uuid::new_v4())));
        let plugin_root = homebrew.0.join("plugins");
        fs::create_dir_all(&plugin_root).unwrap();
        let (events, _) = broadcast::channel(8);
        let (_shutdown_tx, shutdown_rx) = watch::channel(false);
        let registry = Arc::new(
            PluginRegistry::start(
                WorkerConfiguration {
                    executable: std::env::current_exe().unwrap(),
                    mode: WorkerMode::Fork,
                    bootstrap: WorkerBootstrap::Lean,
                    auth_token: "monitor-regression".into(),
                    server_host: "127.0.0.1".into(),
                    server_port: 1339,
                    chown_plugin_path: false,
                },
                plugin_root,
                homebrew.0.clone(),
                std::env::var("USER").expect("test account USER"),
                platform::effective_user_ids(),
                events,
                shutdown_rx,
            )
            .await
            .unwrap(),
        );
        let socket_directory = registry.socket_directory.clone();
        assert!(socket_directory.is_dir());
        let (launcher_tx, launcher_rx) = watch::channel(false);
        monitor_launcher_failure(
            launcher_rx,
            registry.launcher_failure.as_ref().unwrap().0.clone(),
        );
        let shutdown_registry = Arc::clone(&registry);
        let (started_tx, started_rx) = oneshot::channel();
        let mut shutdown = tokio::spawn(async move {
            let _ = started_tx.send(());
            crate::essential_launcher_failed(shutdown_registry.essential_failure()).await;
            shutdown_registry.shutdown().await;
        });
        started_rx.await.unwrap();
        assert!(!shutdown.is_finished());
        drop(launcher_tx);
        let completed = timeout(Duration::from_secs(1), &mut shutdown).await;
        if completed.is_err() {
            shutdown.abort();
            registry.shutdown().await;
        }
        assert!(
            completed.is_ok(),
            "lost launcher supervision did not enter the backend failure shutdown"
        );
        completed.unwrap().unwrap();
        assert!(registry.shutting_down.load(Ordering::Acquire));
        assert!(!socket_directory.exists());
    }
}

#[cfg(test)]
mod ownership_retry_tests {
    use super::*;

    fn candidate(
        name: &str,
        directory: &str,
        error: Option<String>,
    ) -> (String, String, Option<String>) {
        (name.to_owned(), directory.to_owned(), error)
    }

    fn ownership(directory: &str) -> Option<String> {
        Some(plugin_ownership_error(Path::new(directory), 0, 1000))
    }

    fn changes(events: &[(&str, u32)]) -> AttributeChanges {
        let root = Path::new("/plugins");
        let mut changes = AttributeChanges::default();
        for (path, mask) in events {
            changes.record(root, Path::new(path), *mask);
        }
        changes
    }

    #[test]
    fn attribute_events_name_the_plugin_directory() {
        let attrib = libc::IN_ATTRIB;
        let changed = changes(&[
            ("/plugins/alpha", attrib | libc::IN_ISDIR),
            ("/plugins/beta/main.py", attrib),
            ("/plugins/.hidden", attrib),
            ("/elsewhere/gamma", attrib),
            ("/plugins/delta", libc::IN_CLOSE_WRITE),
        ]);
        assert!(!changed.plugin_root);
        assert!(changed.covers("alpha"));
        assert!(changed.covers("beta"));
        assert!(!changed.covers(".hidden"));
        assert!(!changed.covers("gamma"));
        assert!(!changed.covers("delta"));
        assert!(changes(&[("/plugins", attrib)]).plugin_root);
        assert!(changes(&[("/plugins/alpha", libc::IN_CREATE)]).is_empty());
    }

    #[test]
    fn only_plugins_refused_for_their_owner_are_retried() {
        let candidates = [
            candidate("Refused", "refused", ownership("/plugins/refused")),
            candidate(
                "Also Refused",
                "also-refused",
                ownership("/plugins/also-refused"),
            ),
            candidate("Healthy", "healthy", None),
            candidate(
                "Other Failure",
                "other",
                Some("Could not update plugin directory permissions".to_owned()),
            ),
        ];
        let attrib = libc::IN_ATTRIB;

        assert_eq!(
            ownership_retry_targets(&candidates, &changes(&[("/plugins/refused", attrib)])),
            ["Refused"]
        );
        assert_eq!(
            ownership_retry_targets(&candidates, &changes(&[("/plugins/healthy", attrib)])),
            Vec::<String>::new()
        );
        assert_eq!(
            ownership_retry_targets(&candidates, &changes(&[("/plugins/other", attrib)])),
            Vec::<String>::new()
        );
        assert_eq!(
            ownership_retry_targets(&candidates, &changes(&[("/plugins", attrib)])),
            ["Also Refused", "Refused"]
        );
        assert_eq!(
            ownership_retry_targets(&candidates, &AttributeChanges::default()),
            Vec::<String>::new()
        );
    }
}
