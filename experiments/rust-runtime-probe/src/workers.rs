use std::{
    collections::HashMap,
    fs, io,
    os::unix::{fs::PermissionsExt, process::CommandExt},
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
    process::{Child, Command},
    sync::{Mutex, broadcast, mpsc, oneshot, watch},
    task::JoinHandle,
    time::{sleep, timeout},
};
use uuid::Uuid;

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
    plugins: HashMap<String, Arc<PluginEntry>>,
    worker_executable: PathBuf,
    plugin_root: PathBuf,
    homebrew: PathBuf,
    user: String,
    socket_directory: PathBuf,
    events: broadcast::Sender<Value>,
    shutdown: watch::Receiver<bool>,
    shutting_down: AtomicBool,
}

struct WorkerLaunchConfig<'a> {
    worker_executable: &'a Path,
    plugin_root: &'a Path,
    homebrew: &'a Path,
    user: &'a str,
    socket_directory: &'a Path,
}

struct WorkerProcess {
    handle: WorkerHandle,
    stop: Option<mpsc::UnboundedSender<()>>,
    monitor: Option<JoinHandle<()>>,
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
        worker_executable: PathBuf,
        plugin_root: PathBuf,
        homebrew: PathBuf,
        user: String,
        user_ids: (u32, u32),
        events: broadcast::Sender<Value>,
        mut shutdown: watch::Receiver<bool>,
    ) -> Result<Self> {
        let disabled = read_disabled_plugins(&homebrew);
        let metadata = discover_plugins(&plugin_root, &disabled)?;
        let socket_directory = create_socket_directory(user_ids)?;
        let launch_config = WorkerLaunchConfig {
            worker_executable: &worker_executable,
            plugin_root: &plugin_root,
            homebrew: &homebrew,
            user: &user,
            socket_directory: &socket_directory,
        };
        let mut plugins = HashMap::with_capacity(metadata.len());

        for plugin in metadata.into_values() {
            if shutdown_requested(&shutdown) {
                cleanup_plugins(&plugins, &socket_directory).await;
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
                match launch_worker(
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
                        cleanup_plugins(&plugins, &socket_directory).await;
                        return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
                    }
                }
            }

            plugins.insert(entry.metadata.name.clone(), entry);
        }

        if shutdown_requested(&shutdown) {
            cleanup_plugins(&plugins, &socket_directory).await;
            return Err(anyhow::anyhow!("Plugin startup interrupted by shutdown"));
        }

        Ok(Self {
            plugins,
            worker_executable,
            plugin_root,
            homebrew,
            user,
            socket_directory,
            events,
            shutdown,
            shutting_down: AtomicBool::new(false),
        })
    }

    pub async fn list(&self) -> Value {
        let mut entries: Vec<_> = self.plugins.values().collect();
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
            .get(name)
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

    pub async fn reload(&self, name: &str) -> std::result::Result<(), WorkerError> {
        let mut shutdown = self.shutdown.clone();
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            return Err(WorkerError::Stopped);
        }

        let entry = self
            .plugins
            .get(name)
            .ok_or_else(|| WorkerError::Error(format!("Plugin {name} not found")))?;
        let disabled = read_disabled_plugins(&self.homebrew)
            .iter()
            .any(|entry| entry == name);
        entry.disabled.store(disabled, Ordering::Release);
        let mut metadata = entry.metadata.clone();
        metadata.disabled = disabled;
        let mut runtime = entry.runtime.lock().await;
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            return Err(WorkerError::Stopped);
        }

        if let Some(mut worker) = runtime.worker.take() {
            worker.stop().await;
        }
        runtime.start_error = None;
        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            return Err(WorkerError::Stopped);
        }
        if metadata.disabled {
            return Ok(());
        }

        let worker = if metadata.passive {
            None
        } else {
            let launch_config = WorkerLaunchConfig {
                worker_executable: &self.worker_executable,
                plugin_root: &self.plugin_root,
                homebrew: &self.homebrew,
                user: &self.user,
                socket_directory: &self.socket_directory,
            };
            match launch_worker(
                &launch_config,
                &metadata,
                self.events.clone(),
                &mut shutdown,
            )
            .await
            {
                Ok(worker) => Some(worker),
                Err(WorkerLaunchError::Failed(error)) => {
                    runtime.start_error = Some(error.clone());
                    return Err(WorkerError::Error(error));
                }
                Err(WorkerLaunchError::Interrupted) => return Err(WorkerError::Stopped),
            }
        };

        if self.shutting_down.load(Ordering::Acquire) || shutdown_requested(&shutdown) {
            if let Some(mut worker) = worker {
                worker.stop().await;
            }
            return Err(WorkerError::Stopped);
        }
        runtime.worker = worker;
        let mut event = json!({"type": 5, "event": "loader/import_plugin"});
        event["args"] = Value::Array(vec![
            Value::String(metadata.name),
            metadata.version.unwrap_or(Value::Null),
            Value::from(metadata.load_type),
            Value::Bool(false),
        ]);
        let _ = self.events.send(event);
        Ok(())
    }

    pub async fn shutdown(&self) {
        if self.shutting_down.swap(true, Ordering::AcqRel) {
            return;
        }
        cleanup_plugins(&self.plugins, &self.socket_directory).await;
    }
}

fn shutdown_requested(shutdown: &watch::Receiver<bool>) -> bool {
    *shutdown.borrow()
}

async fn cleanup_plugins(plugins: &HashMap<String, Arc<PluginEntry>>, socket_directory: &Path) {
    let entries: Vec<_> = plugins.values().cloned().collect();
    let stops = entries.into_iter().map(|entry| async move {
        let mut runtime = entry.runtime.lock().await;
        if let Some(mut worker) = runtime.worker.take() {
            worker.stop().await;
        }
    });
    futures_util::future::join_all(stops).await;
    let _ = fs::remove_dir_all(socket_directory);
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
        let requires_root = match metadata_json.get("flags") {
            Some(Value::Array(flags)) => flags.iter().any(|flag| flag.as_str() == Some("root")),
            Some(Value::String(flags)) => flags.contains("root"),
            Some(Value::Object(flags)) => flags.contains_key("root"),
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
        };
        plugins.insert(plugin.name.clone(), plugin);
    }

    Ok(plugins)
}

fn read_json(path: &Path) -> io::Result<Value> {
    let bytes = fs::read(path)?;
    serde_json::from_slice(&bytes)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn read_disabled_plugins(homebrew: &Path) -> Vec<String> {
    let path = homebrew.join("settings").join("loader.json");
    let Ok(settings) = read_json(&path) else {
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

fn create_socket_directory(user_ids: (u32, u32)) -> Result<PathBuf> {
    // AF_UNIX path lengths are limited; keep addresses independent of a long
    // frozen-runtime TMPDIR while retaining a private owned namespace.
    let directory = Path::new("/tmp").join(format!("drp-{}", Uuid::new_v4().simple()));
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
        if unsafe { libc::geteuid() } == 0 {
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
        plugin_root,
        homebrew,
        user,
        socket_directory,
    } = *config;
    if shutdown_requested(shutdown) {
        return Err(WorkerLaunchError::Interrupted);
    }
    if plugin.requires_root {
        let effective_uid = unsafe { libc::geteuid() };
        if effective_uid != 0 {
            return Err(WorkerLaunchError::Failed(format!(
                "Plugin {} requires root privileges, but the worker launcher is running as UID {effective_uid}",
                display_name(plugin)
            )));
        }
    }

    let socket_path = socket_directory
        .join(if plugin.requires_root { "root" } else { "user" })
        .join(format!("{}.sock", Uuid::new_v4().simple()));
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
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    // PyInstaller forks its Python runtime; isolate both in an owned process group.
    command.as_std_mut().process_group(0);
    let mut child = command.spawn().map_err(|error| {
        WorkerLaunchError::Failed(format!(
            "Failed to start worker for {} using {}: {error}",
            display_name(plugin),
            worker_executable.display()
        ))
    })?;
    let pid = child.id().expect("a spawned worker has a process ID");

    let start = Instant::now();
    let stream = loop {
        if shutdown_requested(shutdown) {
            abort_worker_startup(&mut child, pid, &socket_path).await;
            return Err(WorkerLaunchError::Interrupted);
        }

        let connection = tokio::select! {
            biased;
            changed = shutdown.changed() => {
                if changed.is_err() || shutdown_requested(shutdown) {
                    abort_worker_startup(&mut child, pid, &socket_path).await;
                    return Err(WorkerLaunchError::Interrupted);
                }
                continue;
            }
            result = UnixStream::connect(&socket_path) => result,
        };
        match connection {
            Ok(stream) => break stream,
            Err(last_socket_error) => {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        send_group_signal(pid, libc::SIGKILL);
                        let _ = fs::remove_file(&socket_path);
                        return Err(WorkerLaunchError::Failed(format!(
                            "Worker for {} exited before IPC readiness: {status}",
                            display_name(plugin)
                        )));
                    }
                    Ok(None) => {}
                    Err(error) => {
                        abort_worker_startup(&mut child, pid, &socket_path).await;
                        return Err(WorkerLaunchError::Failed(format!(
                            "Could not check worker startup for {}: {error}",
                            display_name(plugin)
                        )));
                    }
                }

                if start.elapsed() >= WORKER_READY_TIMEOUT {
                    abort_worker_startup(&mut child, pid, &socket_path).await;
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
                            abort_worker_startup(&mut child, pid, &socket_path).await;
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
        abort_worker_startup(&mut child, pid, &socket_path).await;
        return Err(WorkerLaunchError::Interrupted);
    }

    let (stop, stop_receiver) = mpsc::unbounded_channel();
    let reaper = tokio::spawn(reap_child(child, stop_receiver, pid));
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

async fn abort_worker_startup(child: &mut Child, pid: u32, socket_path: &Path) {
    terminate_and_reap(child, pid).await;
    let _ = fs::remove_file(socket_path);
}

impl WorkerProcess {
    async fn stop(&mut self) {
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
    unsafe {
        libc::kill(pid as libc::pid_t, signal);
    }
}

fn send_group_signal(pid: u32, signal: i32) {
    unsafe {
        libc::kill(-(pid as libc::pid_t), signal);
    }
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
