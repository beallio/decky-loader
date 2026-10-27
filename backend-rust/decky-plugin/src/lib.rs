//! Write Decky Loader plugin backends in Rust.
//!
//! A native backend is an ELF executable named by `"backend"` in `plugin.json`.
//! Decky starts it in place of `main.py` and gives it what the Python `decky`
//! module gives: the same paths, a log file, frontend method calls and events.
//!
//! ```no_run
//! use decky_plugin::{Plugin, serde_json::Value};
//!
//! #[tokio::main]
//! async fn main() -> std::io::Result<()> {
//!     Plugin::new()
//!         .method("echo", |_context, args| async move { Ok(Value::Array(args)) })
//!         .on_main(|context| async move {
//!             log::info!("{} started", context.env().plugin_name);
//!             Ok(())
//!         })
//!         .run()
//!         .await
//! }
//! ```

use std::{
    collections::HashMap,
    env, fs,
    future::Future,
    io::{self, Write},
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    pin::Pin,
    sync::{
        Arc, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

pub use serde_json;
pub use serde_json::value::RawValue;
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{UnixListener, unix::OwnedWriteHalf},
    signal::unix::{SignalKind, signal},
    sync::{Mutex, watch},
};

/// What a method returns to the frontend: a JSON value, or an error message
/// that rejects the frontend call.
pub type MethodResult = Result<Value, String>;

/// Like [`MethodResult`], with the value already serialized. A large result can be
/// serialized straight from borrowed data instead of first being copied into a [`Value`].
pub type RawMethodResult = Result<Box<RawValue>, String>;

enum Reply {
    Value(Value),
    Raw(Box<RawValue>),
    Static(&'static str),
}

type BoxFuture<T> = Pin<Box<dyn Future<Output = T> + Send>>;
type Method = Box<dyn Fn(Context, Vec<Value>) -> BoxFuture<Result<Reply, String>> + Send + Sync>;
type Hook = Box<dyn FnOnce(Context) -> BoxFuture<Result<(), String>> + Send>;
type Writer = Arc<Mutex<OwnedWriteHalf>>;

/// The paths and identity Decky gives every plugin backend. These are the same
/// values as the constants of the Python `decky` module. A missing value is empty.
#[derive(Debug, Clone, Default)]
pub struct Env {
    /// `HOME`: home of the account the plugin runs as.
    pub home: PathBuf,
    /// `USER`: account the plugin runs as.
    pub user: String,
    /// `DECKY_VERSION`: Decky Loader version.
    pub decky_version: String,
    /// `DECKY_USER`: account Decky runs for.
    pub decky_user: String,
    /// `DECKY_USER_HOME`: home of `DECKY_USER`.
    pub decky_user_home: PathBuf,
    /// `DECKY_HOME`: Decky's own folder, usually `~/homebrew`.
    pub decky_home: PathBuf,
    /// `DECKY_PLUGIN_SETTINGS_DIR`: put settings here.
    pub plugin_settings_dir: PathBuf,
    /// `DECKY_PLUGIN_RUNTIME_DIR`: put other data here.
    pub plugin_runtime_dir: PathBuf,
    /// `DECKY_PLUGIN_LOG_DIR`: log files go here.
    pub plugin_log_dir: PathBuf,
    /// `DECKY_PLUGIN_DIR`: the installed plugin.
    pub plugin_dir: PathBuf,
    /// `DECKY_PLUGIN_NAME`: name from `plugin.json`.
    pub plugin_name: String,
    /// `DECKY_PLUGIN_VERSION`: version from `package.json`.
    pub plugin_version: String,
    /// `DECKY_PLUGIN_AUTHOR`: author from `plugin.json`.
    pub plugin_author: String,
}

impl Env {
    /// Reads the values from this process's environment.
    pub fn from_env() -> Self {
        let path = |name| PathBuf::from(env::var_os(name).unwrap_or_default());
        let text = |name| env::var(name).unwrap_or_default();
        Self {
            home: path("HOME"),
            user: text("USER"),
            decky_version: text("DECKY_VERSION"),
            decky_user: text("DECKY_USER"),
            decky_user_home: path("DECKY_USER_HOME"),
            decky_home: path("DECKY_HOME"),
            plugin_settings_dir: path("DECKY_PLUGIN_SETTINGS_DIR"),
            plugin_runtime_dir: path("DECKY_PLUGIN_RUNTIME_DIR"),
            plugin_log_dir: path("DECKY_PLUGIN_LOG_DIR"),
            plugin_dir: path("DECKY_PLUGIN_DIR"),
            plugin_name: text("DECKY_PLUGIN_NAME"),
            plugin_version: text("DECKY_PLUGIN_VERSION"),
            plugin_author: text("DECKY_PLUGIN_AUTHOR"),
        }
    }
}

/// Given to every method and hook.
#[derive(Clone)]
pub struct Context {
    env: Arc<Env>,
    link: watch::Receiver<Link>,
}

/// The plugin's connection to Decky, as `emit` sees it.
#[derive(Clone)]
enum Link {
    /// No connection can come: migration is running or the plugin is stopping.
    Unavailable,
    /// The plugin listens and Decky will connect soon.
    Waiting,
    Connected(Writer),
}

impl Context {
    pub fn env(&self) -> &Env {
        &self.env
    }

    /// Sends an event to the plugin's frontend, like `decky.emit`. Frontend code
    /// receives it with `addEventListener(event, listener)`. If Decky has not
    /// connected yet, this waits until it does. During migration, and after the
    /// plugin stops before Decky connected, it fails with
    /// [`io::ErrorKind::NotConnected`] instead of waiting.
    pub async fn emit(&self, event: &str, args: Vec<Value>) -> io::Result<()> {
        let mut link = self.link.clone();
        let link = link
            .wait_for(|link| !matches!(link, Link::Waiting))
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "the plugin has stopped"))?
            .clone();
        match link {
            Link::Connected(writer) => {
                write_line(&writer, &json!({"type": 2, "event": event, "args": args})).await
            }
            _ => Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "Decky is not connected: events cannot be sent during migration or after the plugin stops",
            )),
        }
    }
}

/// A plugin backend: its frontend methods and its lifecycle hooks.
#[derive(Default)]
pub struct Plugin {
    methods: HashMap<String, Method>,
    migration: Option<Hook>,
    main: Option<Hook>,
    unload: Option<Hook>,
    uninstall: Option<Hook>,
}

impl Plugin {
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds a method that the frontend calls with `callable(name)`. `args` are the
    /// call's arguments in order. Names that start with `_` cannot be called.
    pub fn method<F, Fut>(mut self, name: impl Into<String>, method: F) -> Self
    where
        F: Fn(Context, Vec<Value>) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = MethodResult> + Send + 'static,
    {
        self.methods.insert(
            name.into(),
            Box::new(move |context, args| {
                let call = method(context, args);
                Box::pin(async move { call.await.map(Reply::Value) })
            }),
        );
        self
    }

    /// Like [`Plugin::method`], for a method that returns already-serialized JSON.
    pub fn method_raw<F, Fut>(mut self, name: impl Into<String>, method: F) -> Self
    where
        F: Fn(Context, Vec<Value>) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = RawMethodResult> + Send + 'static,
    {
        self.methods.insert(
            name.into(),
            Box::new(move |context, args| {
                let call = method(context, args);
                Box::pin(async move { call.await.map(Reply::Raw) })
            }),
        );
        self
    }

    /// Runs before Decky can connect, like `_migration`. An error stops the plugin.
    pub fn on_migration<F, Fut>(mut self, hook: F) -> Self
    where
        F: FnOnce(Context) -> Fut + Send + 'static,
        Fut: Future<Output = Result<(), String>> + Send + 'static,
    {
        self.migration = Some(boxed_hook(hook));
        self
    }

    /// Runs as a task beside method calls, like `_main`. It is stopped after unload.
    pub fn on_main<F, Fut>(mut self, hook: F) -> Self
    where
        F: FnOnce(Context) -> Fut + Send + 'static,
        Fut: Future<Output = Result<(), String>> + Send + 'static,
    {
        self.main = Some(boxed_hook(hook));
        self
    }

    /// Runs when Decky stops the plugin, like `_unload`.
    pub fn on_unload<F, Fut>(mut self, hook: F) -> Self
    where
        F: FnOnce(Context) -> Fut + Send + 'static,
        Fut: Future<Output = Result<(), String>> + Send + 'static,
    {
        self.unload = Some(boxed_hook(hook));
        self
    }

    /// Runs after unload when the plugin is being uninstalled, like `_uninstall`.
    pub fn on_uninstall<F, Fut>(mut self, hook: F) -> Self
    where
        F: FnOnce(Context) -> Fut + Send + 'static,
        Fut: Future<Output = Result<(), String>> + Send + 'static,
    {
        self.uninstall = Some(boxed_hook(hook));
        self
    }

    /// Starts logging, serves Decky until it stops the plugin, then runs the
    /// unload and uninstall hooks. Decky allows five seconds for this before it
    /// kills the process. SIGTERM or SIGINT stops the plugin at any time,
    /// including during migration.
    ///
    /// With `DECKY_PLUGIN_HOOK=uninstall` it runs only the uninstall hook and
    /// returns. The upstream shim's relay mode uses this because the Python
    /// worker calls its uninstall hook after unloading the first backend.
    pub async fn run(self) -> io::Result<()> {
        set_process_name(&env::var("DECKY_PLUGIN_NAME").unwrap_or_default());
        if let Err(error) = init_logging() {
            eprintln!("Could not open the plugin log file: {error}");
        }
        if let Some(hook) = env::var_os("DECKY_PLUGIN_HOOK") {
            if hook != "uninstall" {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("unknown DECKY_PLUGIN_HOOK {hook:?}"),
                ));
            }
            return self.uninstall_only(Env::from_env()).await;
        }
        let socket = env::var_os("DECKY_PLUGIN_SOCKET").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "DECKY_PLUGIN_SOCKET is not set; Decky Loader must start this program",
            )
        })?;
        let mut terminate = signal(SignalKind::terminate())?;
        let mut interrupt = signal(SignalKind::interrupt())?;
        let stop = async move {
            tokio::select! {
                _ = terminate.recv() => {}
                _ = interrupt.recv() => {}
            }
        };
        self.serve(Env::from_env(), Path::new(&socket), stop).await
    }

    async fn uninstall_only(self, env: Env) -> io::Result<()> {
        let Some(uninstall) = self.uninstall else {
            return Ok(());
        };
        // Decky never connects in this mode, so emit fails at once.
        let (_, link) = watch::channel(Link::Unavailable);
        let context = Context {
            env: Arc::new(env),
            link,
        };
        uninstall(context)
            .await
            .map_err(|error| io::Error::other(format!("uninstall failed: {error}")))
    }

    async fn serve(
        self,
        env: Env,
        socket: &Path,
        stop: impl Future<Output = ()>,
    ) -> io::Result<()> {
        let Self {
            methods,
            migration,
            main,
            unload,
            uninstall,
        } = self;
        let mut stop = std::pin::pin!(stop);
        let (link, link_receiver) = watch::channel(Link::Unavailable);
        let context = Context {
            env: Arc::new(env),
            link: link_receiver,
        };

        let migrated = async {
            match migration {
                Some(migration) => migration(context.clone())
                    .await
                    .map_err(|error| io::Error::other(format!("migration failed: {error}"))),
                None => Ok(()),
            }
        };
        let stopped_early = tokio::select! {
            result = migrated => {
                result?;
                false
            }
            _ = &mut stop => true,
        };

        let uninstalling = AtomicBool::new(false);
        let mut main_task = None;
        let outcome = if stopped_early {
            Ok(())
        } else {
            link.send_replace(Link::Waiting);
            main_task = main.map(|main| {
                let context = context.clone();
                tokio::spawn(async move {
                    if let Err(error) = main(context).await {
                        log::error!("Main task failed: {error}");
                    }
                })
            });
            let methods = Arc::new(methods);
            let connection = async {
                let listener = UnixListener::bind(socket)?;
                let stopping = AtomicBool::new(false);
                let stream = tokio::select! {
                    biased;
                    accepted = listener.accept() => Some(accepted?.0),
                    _ = &mut stop => {
                        stopping.store(true, Ordering::Release);
                        match listener.into_std()?.accept() {
                            Ok((stream, _)) => {
                                stream.set_nonblocking(true)?;
                                Some(tokio::net::UnixStream::from_std(stream)?)
                            }
                            Err(error) if error.kind() == io::ErrorKind::WouldBlock => None,
                            Err(error) => return Err(error),
                        }
                    }
                };
                let Some(stream) = stream else {
                    return Ok(());
                };
                let (reader, writer) = stream.into_split();
                let fd = reader.as_ref().as_raw_fd();
                let writer = Arc::new(Mutex::new(writer));
                link.send_replace(Link::Connected(Arc::clone(&writer)));
                let incoming = async {
                    let mut lines = BufReader::new(reader).lines();
                    while let Some(line) = lines.next_line().await? {
                        let mut message: Value = match serde_json::from_str(&line) {
                            Ok(message) => message,
                            Err(error) => {
                                log::error!("Ignoring a message that is not JSON: {error}");
                                continue;
                            }
                        };
                        if let Some(flag) = message.get("uninstall") {
                            uninstalling.store(flag.as_bool() == Some(true), Ordering::Release);
                            continue;
                        }
                        if stopping.load(Ordering::Acquire) {
                            continue;
                        }
                        if message.get("type").and_then(Value::as_u64) != Some(0) {
                            continue;
                        }
                        let id = message.get("id").cloned().unwrap_or(Value::Null);
                        if message.get("legacy").and_then(Value::as_bool) == Some(true) {
                            let error =
                                Reply::Static("Legacy methods may not be used on api_version > 0");
                            if let Err(error) = write_reply(&writer, &id, false, &error).await {
                                log::error!("Could not reject legacy request: {error}");
                            }
                            continue;
                        }
                        let name = message
                            .get("method")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .to_owned();
                        let args = match message.get_mut("args").map(Value::take) {
                            Some(Value::Array(args)) => args,
                            _ => Vec::new(),
                        };
                        let (methods, context, writer) =
                            (Arc::clone(&methods), context.clone(), Arc::clone(&writer));
                        tokio::spawn(async move {
                            // The method is called and awaited in its own task, so a panic while it
                            // builds or runs its future becomes an error reply and the caller is answered.
                            let call = {
                                let name = name.clone();
                                tokio::spawn(async move {
                                    match methods.get(&name) {
                                        Some(method) => method(context, args).await,
                                        None => Err(format!("Plugin has no method {name}")),
                                    }
                                })
                            };
                            let result = call.await.unwrap_or_else(|error| {
                                Err(format!("Method {name} failed: {error}"))
                            });
                            let (success, res) = match result {
                                Ok(reply) => (true, reply),
                                Err(message) => (false, Reply::Value(Value::String(message))),
                            };
                            if let Err(error) = write_reply(&writer, &id, success, &res).await {
                                log::error!("Could not answer {name}: {error}");
                            }
                        });
                    }
                    Ok::<(), io::Error>(())
                };
                let mut incoming = std::pin::pin!(incoming);
                if stopping.load(Ordering::Acquire) {
                    shutdown_read(fd)?;
                    incoming.await
                } else {
                    tokio::select! {
                        biased;
                        _ = &mut stop => {
                            stopping.store(true, Ordering::Release);
                            shutdown_read(fd)?;
                            incoming.await
                        }
                        outcome = &mut incoming => outcome,
                    }
                }
            };
            connection.await
        };
        if let Err(error) = &outcome {
            log::error!("Decky connection failed: {error}");
        }
        // Decky can no longer connect; release any emit still waiting for it.
        link.send_if_modified(|link| {
            let waiting = matches!(link, Link::Waiting);
            if waiting {
                *link = Link::Unavailable;
            }
            waiting
        });

        log::info!("Unloading {}", context.env.plugin_name);
        if let Some(unload) = unload
            && let Err(error) = unload(context.clone()).await
        {
            log::error!("Unload failed: {error}");
        }
        if uninstalling.load(Ordering::Acquire)
            && let Some(uninstall) = uninstall
            && let Err(error) = uninstall(context.clone()).await
        {
            log::error!("Uninstall failed: {error}");
        }
        if let Some(main) = main_task {
            main.abort();
        }
        let _ = fs::remove_file(socket);
        outcome
    }
}

fn shutdown_read(fd: std::os::fd::RawFd) -> io::Result<()> {
    // Linux keeps already-queued bytes readable after SHUT_RD and then returns EOF.
    // SAFETY: the connection future keeps this socket open until the drain finishes.
    if unsafe { libc::shutdown(fd, libc::SHUT_RD) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// Names the process after the plugin, as the Python worker does, so that `ps`, `top`
/// and Decky's status show the plugin. Linux keeps the first 15 bytes.
fn set_process_name(name: &str) {
    let Ok(name) = std::ffi::CString::new(name) else {
        return;
    };
    if name.as_bytes().is_empty() {
        return;
    }
    // SAFETY: PR_SET_NAME reads a NUL-terminated string; the kernel copies at most 16 bytes.
    unsafe { libc::prctl(libc::PR_SET_NAME, name.as_ptr()) };
}

fn boxed_hook<F, Fut>(hook: F) -> Hook
where
    F: FnOnce(Context) -> Fut + Send + 'static,
    Fut: Future<Output = Result<(), String>> + Send + 'static,
{
    Box::new(move |context| Box::pin(hook(context)))
}

async fn write_line(writer: &Mutex<OwnedWriteHalf>, message: &Value) -> io::Result<()> {
    let mut line = serde_json::to_vec(message)?;
    line.push(b'\n');
    writer.lock().await.write_all(&line).await
}

/// Writes a reply line. A raw result goes to the socket as is, so a large reply is
/// never copied into a second buffer.
async fn write_reply(
    writer: &Mutex<OwnedWriteHalf>,
    id: &Value,
    success: bool,
    res: &Reply,
) -> io::Result<()> {
    let mut head = format!(
        r#"{{"type":1,"id":{},"success":{success},"res":"#,
        serde_json::to_string(id)?
    )
    .into_bytes();
    match res {
        Reply::Value(value) => serde_json::to_writer(&mut head, value)?,
        Reply::Static(value) => serde_json::to_writer(&mut head, value)?,
        Reply::Raw(raw) => {
            // Each message is one line. In valid JSON a newline can only be whitespace
            // (strings escape it), so dropping it keeps a pretty-printed value intact.
            let bytes = raw.get().as_bytes();
            let compact: Vec<u8>;
            let bytes = if bytes.contains(&b'\n') {
                compact = bytes
                    .iter()
                    .copied()
                    .filter(|byte| *byte != b'\n')
                    .collect();
                &compact
            } else {
                bytes
            };
            let mut writer = writer.lock().await;
            writer.write_all(&head).await?;
            writer.write_all(bytes).await?;
            return writer.write_all(b"}\n").await;
        }
    }
    head.extend_from_slice(b"}\n");
    writer.lock().await.write_all(&head).await
}

/// Sends [`log`] records to a new file in `DECKY_PLUGIN_LOG_DIR` and to standard
/// error, like the Python `decky.logger`, at level info. Older log files are
/// removed so that five remain. [`Plugin::run`] calls this; call it yourself to
/// log before `run`. Returns the log file path.
pub fn init_logging() -> io::Result<PathBuf> {
    static LOG_FILE: OnceLock<PathBuf> = OnceLock::new();
    if let Some(path) = LOG_FILE.get() {
        return Ok(path.clone());
    }
    let directory = PathBuf::from(env::var_os("DECKY_PLUGIN_LOG_DIR").unwrap_or_default());
    if directory.as_os_str().is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "DECKY_PLUGIN_LOG_DIR is not set",
        ));
    }
    if let Err(error) = remove_old_logs(&directory, 4) {
        eprintln!("Failed to delete old logs: {error}");
    }
    let (date, time, _) = local_time();
    let path = directory.join(format!("{date} {}.log", time.replace(':', ".")));
    let file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)?;
    log::set_boxed_logger(Box::new(FileLogger {
        file: std::sync::Mutex::new(file),
    }))
    .map_err(|error| io::Error::new(io::ErrorKind::AlreadyExists, error.to_string()))?;
    log::set_max_level(log::LevelFilter::Info);
    Ok(LOG_FILE.get_or_init(|| path).clone())
}

/// Keeps the `keep` newest `.log` files in `directory` and deletes the rest.
fn remove_old_logs(directory: &Path, keep: usize) -> io::Result<()> {
    let mut logs = Vec::new();
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        if entry.file_name().to_string_lossy().ends_with(".log") {
            logs.push((entry.metadata()?.modified()?, entry.path()));
        }
    }
    logs.sort_unstable_by_key(|(modified, _)| std::cmp::Reverse(*modified));
    for (_, path) in logs.into_iter().skip(keep) {
        fs::remove_file(path)?;
    }
    Ok(())
}

struct FileLogger {
    file: std::sync::Mutex<fs::File>,
}

impl log::Log for FileLogger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let level = match record.level() {
            log::Level::Error => "ERROR",
            log::Level::Warn => "WARNING",
            log::Level::Info => "INFO",
            log::Level::Debug => "DEBUG",
            log::Level::Trace => "TRACE",
        };
        let (date, time, millis) = local_time();
        let line = format!("[{date} {time},{millis:03}][{level}]: {}\n", record.args());
        if let Ok(mut file) = self.file.lock() {
            let _ = file.write_all(line.as_bytes());
        }
        let _ = io::stderr().write_all(line.as_bytes());
    }

    fn flush(&self) {}
}

/// Local date (`YYYY-MM-DD`), time (`HH:MM:SS`) and milliseconds.
fn local_time() -> (String, String, u32) {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    // The type comes from localtime_r; naming libc::time_t warns on musl targets.
    let seconds = now.as_secs() as _;
    // SAFETY: `tm` is plain data; all-zero bytes are a valid value.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: both pointers refer to live locals; localtime_r writes only `tm`.
    unsafe { libc::localtime_r(&seconds, &mut tm) };
    (
        format!(
            "{:04}-{:02}-{:02}",
            tm.tm_year + 1900,
            tm.tm_mon + 1,
            tm.tm_mday
        ),
        format!("{:02}:{:02}:{:02}", tm.tm_hour, tm.tm_min, tm.tm_sec),
        now.subsec_millis(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    use tokio::net::UnixStream;

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            let path = env::temp_dir().join(format!(
                "dp-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    async fn connect(socket: &Path) -> UnixStream {
        for _ in 0..500 {
            if let Ok(stream) = UnixStream::connect(socket).await {
                return stream;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("the plugin never listened on {}", socket.display());
    }

    async fn next(
        lines: &mut tokio::io::Lines<BufReader<tokio::net::unix::OwnedReadHalf>>,
    ) -> Value {
        let line = tokio::time::timeout(Duration::from_secs(5), lines.next_line())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        serde_json::from_str(&line).unwrap()
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn plugin_speaks_the_worker_protocol_and_runs_hooks_in_order() {
        for uninstall in [false, true] {
            let directory = TestDirectory::new();
            let socket = directory.0.join("p.sock");
            let hooks = Arc::new(std::sync::Mutex::new(Vec::new()));
            let legacy_called = Arc::new(AtomicBool::new(false));
            let record = |name: &'static str| {
                let hooks = Arc::clone(&hooks);
                move |_context: Context| async move {
                    hooks.lock().unwrap().push(name);
                    Ok(())
                }
            };
            let plugin = Plugin::new()
                .method("add", |_context, args| async move {
                    Ok(json!(args.iter().filter_map(Value::as_i64).sum::<i64>()))
                })
                .method("legacy_probe", {
                    let legacy_called = Arc::clone(&legacy_called);
                    move |_context, _args| {
                        let legacy_called = Arc::clone(&legacy_called);
                        async move {
                            legacy_called.store(true, Ordering::Release);
                            Ok(Value::Null)
                        }
                    }
                })
                .method("fail", |_context, _args| async { Err("nope".to_owned()) })
                .method_raw("raw", |_context, args| async move {
                    // Pretty-printed on purpose: newlines must not split the reply line.
                    let text = serde_json::to_string_pretty(&json!({"args": args, "text": "é\n"}))
                        .map_err(|error| error.to_string())?;
                    RawValue::from_string(text).map_err(|error| error.to_string())
                })
                .method("panic", |_context, _args| async { panic!("handler bug") })
                .method(
                    "panic_early",
                    |_context, _args| -> std::future::Ready<MethodResult> {
                        panic!("bad arguments")
                    },
                )
                .on_migration(record("migration"))
                .on_main(|context| async move {
                    context
                        .emit("started", vec![json!(1)])
                        .await
                        .map_err(|error| error.to_string())
                })
                .on_unload(record("unload"))
                .on_uninstall(record("uninstall"));
            let server = tokio::spawn({
                let socket = socket.clone();
                async move {
                    plugin
                        .serve(Env::default(), &socket, std::future::pending())
                        .await
                }
            });

            let (reader, mut writer) = connect(&socket).await.into_split();
            let mut lines = BufReader::new(reader).lines();
            assert_eq!(
                next(&mut lines).await,
                json!({"type": 2, "event": "started", "args": [1]})
            );
            for (id, method, args) in [
                ("a", "add", json!([2, 3])),
                ("b", "fail", json!([])),
                ("c", "gone", json!([])),
                ("d", "panic", json!([])),
                ("e", "panic_early", json!([])),
                ("f", "raw", json!([1, "x"])),
            ] {
                let call = json!({"type": 0, "method": method, "id": id, "args": args});
                writer
                    .write_all(format!("{call}\n").as_bytes())
                    .await
                    .unwrap();
                let reply = next(&mut lines).await;
                assert_eq!(reply["id"], id);
                match method {
                    "add" => assert_eq!(
                        (&reply["success"], &reply["res"]),
                        (&json!(true), &json!(5))
                    ),
                    "raw" => assert_eq!(
                        (&reply["success"], &reply["res"]),
                        (&json!(true), &json!({"args": [1, "x"], "text": "é\n"}))
                    ),
                    "fail" => assert_eq!(
                        (&reply["success"], &reply["res"]),
                        (&json!(false), &json!("nope"))
                    ),
                    "panic" | "panic_early" => assert_eq!(
                        (
                            &reply["success"],
                            reply["res"].as_str().unwrap().contains("panicked")
                        ),
                        (&json!(false), true)
                    ),
                    _ => assert_eq!(reply["success"], false),
                }
            }
            for (id, method) in [
                ("legacy-existing", "legacy_probe"),
                ("legacy-missing", "missing"),
            ] {
                let call = json!({"type": 0, "method": method, "id": id, "args": {"x": 1}, "legacy": true});
                writer
                    .write_all(format!("{call}\n").as_bytes())
                    .await
                    .unwrap();
                assert_eq!(
                    next(&mut lines).await,
                    json!({"type": 1, "id": id, "success": false, "res": "Legacy methods may not be used on api_version > 0"})
                );
            }
            assert!(!legacy_called.load(Ordering::Acquire));
            let call = json!({"type": 0, "method": "add", "id": "non-legacy", "args": [4, 5], "legacy": false});
            writer
                .write_all(format!("{call}\n").as_bytes())
                .await
                .unwrap();
            assert_eq!(
                next(&mut lines).await,
                json!({"type": 1, "id": "non-legacy", "success": true, "res": 9})
            );
            if uninstall {
                writer.write_all(b"{\"uninstall\":true}\n").await.unwrap();
            }
            drop(writer);
            tokio::time::timeout(Duration::from_secs(5), server)
                .await
                .unwrap()
                .unwrap()
                .unwrap();

            let expected: &[&str] = if uninstall {
                &["migration", "unload", "uninstall"]
            } else {
                &["migration", "unload"]
            };
            assert_eq!(*hooks.lock().unwrap(), expected);
            assert!(!socket.exists(), "the socket file is removed on exit");
        }
    }

    #[tokio::test]
    async fn uninstall_message_is_processed_when_stop_is_ready_at_the_same_time() {
        for _ in 0..30 {
            let directory = TestDirectory::new();
            let socket = directory.0.join("p.sock");
            let hooks = Arc::new(std::sync::Mutex::new(Vec::new()));
            let called = Arc::new(AtomicBool::new(false));
            let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
            let server = tokio::spawn({
                let (socket, hooks) = (socket.clone(), Arc::clone(&hooks));
                let called = Arc::clone(&called);
                async move {
                    Plugin::new()
                        .method("late", move |_context, _args| {
                            let called = Arc::clone(&called);
                            async move {
                                called.store(true, Ordering::Release);
                                Ok(Value::Null)
                            }
                        })
                        .on_unload({
                            let hooks = Arc::clone(&hooks);
                            move |_context| async move {
                                hooks.lock().unwrap().push("unload");
                                Ok(())
                            }
                        })
                        .on_uninstall(move |_context| async move {
                            hooks.lock().unwrap().push("uninstall");
                            Ok(())
                        })
                        .serve(Env::default(), &socket, async {
                            let _ = stopped.await;
                        })
                        .await
                }
            });
            let mut stream = connect(&socket).await;
            stream
                .write_all(b"{\"type\":0,\"method\":\"late\",\"id\":\"x\",\"args\":[]}\n{\"uninstall\":false}\n{\"uninstall\":true}\n")
                .await
                .unwrap();
            stop.send(()).unwrap();
            tokio::time::timeout(Duration::from_secs(5), server)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            tokio::time::sleep(Duration::from_millis(10)).await;
            assert!(!called.load(Ordering::Acquire));
            assert_eq!(*hooks.lock().unwrap(), ["unload", "uninstall"]);
        }
    }

    #[tokio::test]
    async fn uninstall_only_runs_just_the_uninstall_hook() {
        let hooks = Arc::new(std::sync::Mutex::new(Vec::new()));
        let record = |name: &'static str| {
            let hooks = Arc::clone(&hooks);
            move |context: Context| async move {
                hooks.lock().unwrap().push(name);
                // Nothing listens for events in this mode; emit must not wait.
                assert!(context.emit("gone", vec![]).await.is_err());
                Ok(())
            }
        };
        Plugin::new()
            .on_migration(record("migration"))
            .on_main(record("main"))
            .on_unload(record("unload"))
            .on_uninstall(record("uninstall"))
            .uninstall_only(Env::default())
            .await
            .unwrap();
        assert_eq!(*hooks.lock().unwrap(), ["uninstall"]);

        let error = Plugin::new()
            .on_uninstall(|_context| async { Err("disk full".to_owned()) })
            .uninstall_only(Env::default())
            .await
            .unwrap_err();
        assert!(error.to_string().contains("disk full"));
    }

    #[tokio::test]
    async fn failed_migration_stops_the_plugin_before_it_listens() {
        let directory = TestDirectory::new();
        let socket = directory.0.join("p.sock");
        let result = Plugin::new()
            .on_migration(|_context| async { Err("old data is broken".to_owned()) })
            .serve(Env::default(), &socket, std::future::pending())
            .await;
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("old data is broken")
        );
        assert!(!socket.exists());
    }

    #[tokio::test]
    async fn stop_during_migration_skips_listening_and_still_unloads() {
        let directory = TestDirectory::new();
        let socket = directory.0.join("p.sock");
        let unloaded = Arc::new(AtomicBool::new(false));
        let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
        let server = tokio::spawn({
            let (socket, unloaded) = (socket.clone(), Arc::clone(&unloaded));
            async move {
                Plugin::new()
                    .on_migration(|_context| std::future::pending())
                    .on_unload(move |_context| async move {
                        unloaded.store(true, Ordering::Release);
                        Ok(())
                    })
                    .serve(Env::default(), &socket, async {
                        let _ = stopped.await;
                    })
                    .await
            }
        });
        tokio::time::sleep(Duration::from_millis(50)).await;
        stop.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(5), server)
            .await
            .expect("a stop during migration must end the plugin")
            .unwrap()
            .unwrap();
        assert!(unloaded.load(Ordering::Acquire));
        assert!(!socket.exists());
    }

    #[tokio::test]
    async fn emit_fails_instead_of_waiting_when_decky_cannot_connect() {
        let directory = TestDirectory::new();
        let socket = directory.0.join("p.sock");
        let results = Arc::new(std::sync::Mutex::new(Vec::new()));
        let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
        let record = |results: &Arc<std::sync::Mutex<Vec<_>>>| {
            let results = Arc::clone(results);
            move |context: Context| async move {
                let kind = context.emit("early", vec![]).await.unwrap_err().kind();
                results.lock().unwrap().push(kind);
                Ok(())
            }
        };
        let server = tokio::spawn({
            let (socket, migration, main) = (socket.clone(), record(&results), record(&results));
            let finished = Arc::clone(&results);
            async move {
                Plugin::new()
                    .on_migration(migration)
                    // Decky never connects here, so main's emit waits until the stop.
                    .on_main(main)
                    // The main task is aborted after unload; let it record its result first.
                    .on_unload(move |_context| async move {
                        while finished.lock().unwrap().len() < 2 {
                            tokio::task::yield_now().await;
                        }
                        Ok(())
                    })
                    .serve(Env::default(), &socket, async {
                        let _ = stopped.await;
                    })
                    .await
            }
        });
        tokio::time::sleep(Duration::from_millis(50)).await;
        stop.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(5), server)
            .await
            .expect("emit during migration must not block the plugin")
            .unwrap()
            .unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(
            *results.lock().unwrap(),
            [io::ErrorKind::NotConnected, io::ErrorKind::NotConnected]
        );
    }

    #[test]
    fn process_takes_the_plugin_name() {
        // Runs on its own thread, so naming it does not affect other tests.
        let comm = || fs::read_to_string("/proc/thread-self/comm").unwrap();
        std::thread::spawn(move || {
            set_process_name("Native Example Plugin");
            assert_eq!(comm(), "Native Example \n");
            set_process_name("");
            assert_eq!(comm(), "Native Example \n");
        })
        .join()
        .unwrap();
    }

    #[test]
    fn only_the_newest_logs_are_kept() {
        let directory = TestDirectory::new();
        let base = SystemTime::now() - Duration::from_secs(3600);
        for index in 0..6 {
            let file = fs::File::create(directory.0.join(format!("{index}.log"))).unwrap();
            file.set_modified(base + Duration::from_secs(index))
                .unwrap();
        }
        fs::write(directory.0.join("notes.txt"), b"").unwrap();
        remove_old_logs(&directory.0, 4).unwrap();
        let mut names: Vec<_> = fs::read_dir(&directory.0)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        assert_eq!(names, ["2.log", "3.log", "4.log", "5.log", "notes.txt"]);
    }
}
