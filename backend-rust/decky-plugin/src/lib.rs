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
    path::{Path, PathBuf},
    pin::Pin,
    sync::{
        Arc, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

pub use serde_json;
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

type BoxFuture<T> = Pin<Box<dyn Future<Output = T> + Send>>;
type Method = Box<dyn Fn(Context, Vec<Value>) -> BoxFuture<MethodResult> + Send + Sync>;
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
    writer: watch::Receiver<Option<Writer>>,
}

impl Context {
    pub fn env(&self) -> &Env {
        &self.env
    }

    /// Sends an event to the plugin's frontend, like `decky.emit`. Frontend code
    /// receives it with `addEventListener(event, listener)`. If Decky has not
    /// connected yet, this waits until it does.
    pub async fn emit(&self, event: &str, args: Vec<Value>) -> io::Result<()> {
        let mut writer = self.writer.clone();
        let writer = writer
            .wait_for(Option::is_some)
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "the plugin has stopped"))?
            .clone()
            .expect("wait_for returned a connection");
        write_line(&writer, &json!({"type": 2, "event": event, "args": args})).await
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
            Box::new(move |context, args| Box::pin(method(context, args))),
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
    /// kills the process.
    pub async fn run(self) -> io::Result<()> {
        if let Err(error) = init_logging() {
            eprintln!("Could not open the plugin log file: {error}");
        }
        let socket = env::var_os("DECKY_PLUGIN_SOCKET").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "DECKY_PLUGIN_SOCKET is not set; Decky Loader must start this program",
            )
        })?;
        self.serve(Env::from_env(), Path::new(&socket)).await
    }

    async fn serve(self, env: Env, socket: &Path) -> io::Result<()> {
        let Self {
            methods,
            migration,
            main,
            unload,
            uninstall,
        } = self;
        let (connected, writer) = watch::channel(None);
        let context = Context {
            env: Arc::new(env),
            writer,
        };
        // Install the handlers first so that an early stop still runs the hooks.
        let mut terminate = signal(SignalKind::terminate())?;
        let mut interrupt = signal(SignalKind::interrupt())?;

        if let Some(migration) = migration {
            migration(context.clone())
                .await
                .map_err(|error| io::Error::other(format!("migration failed: {error}")))?;
        }
        let main = main.map(|main| {
            let context = context.clone();
            tokio::spawn(async move {
                if let Err(error) = main(context).await {
                    log::error!("Main task failed: {error}");
                }
            })
        });

        let methods = Arc::new(methods);
        let uninstalling = AtomicBool::new(false);
        let connection = async {
            let listener = UnixListener::bind(socket)?;
            let (stream, _) = listener.accept().await?;
            let (reader, writer) = stream.into_split();
            let writer = Arc::new(Mutex::new(writer));
            connected.send_replace(Some(Arc::clone(&writer)));
            let mut lines = BufReader::new(reader).lines();
            while let Some(line) = lines.next_line().await? {
                let message: Value = match serde_json::from_str(&line) {
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
                if message.get("type").and_then(Value::as_u64) != Some(0) {
                    continue;
                }
                let id = message.get("id").cloned().unwrap_or(Value::Null);
                let name = message
                    .get("method")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned();
                let args = match message.get("args") {
                    Some(Value::Array(args)) => args.clone(),
                    _ => Vec::new(),
                };
                let (methods, context, writer) =
                    (Arc::clone(&methods), context.clone(), Arc::clone(&writer));
                tokio::spawn(async move {
                    let result = match methods.get(&name) {
                        // A separate task turns a panic into an error reply, so the caller is answered.
                        Some(method) => match tokio::spawn(method(context, args)).await {
                            Ok(result) => result,
                            Err(error) => Err(format!("Method {name} failed: {error}")),
                        },
                        None => Err(format!("Plugin has no method {name}")),
                    };
                    let (success, res) = match result {
                        Ok(value) => (true, value),
                        Err(message) => (false, Value::String(message)),
                    };
                    let reply = json!({"type": 1, "id": id, "success": success, "res": res});
                    if let Err(error) = write_line(&writer, &reply).await {
                        log::error!("Could not answer {name}: {error}");
                    }
                });
            }
            Ok::<(), io::Error>(())
        };
        let outcome = tokio::select! {
            outcome = connection => outcome,
            _ = terminate.recv() => Ok(()),
            _ = interrupt.recv() => Ok(()),
        };
        if let Err(error) = &outcome {
            log::error!("Decky connection failed: {error}");
        }

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
        if let Some(main) = main {
            main.abort();
        }
        let _ = fs::remove_file(socket);
        outcome
    }
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
    let seconds = now.as_secs() as libc::time_t;
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
                .method("fail", |_context, _args| async { Err("nope".to_owned()) })
                .method("panic", |_context, _args| async { panic!("handler bug") })
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
                async move { plugin.serve(Env::default(), &socket).await }
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
                    "fail" => assert_eq!(
                        (&reply["success"], &reply["res"]),
                        (&json!(false), &json!("nope"))
                    ),
                    "panic" => assert_eq!(
                        (
                            &reply["success"],
                            reply["res"].as_str().unwrap().contains("panicked")
                        ),
                        (&json!(false), true)
                    ),
                    _ => assert_eq!(reply["success"], false),
                }
            }
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
    async fn failed_migration_stops_the_plugin_before_it_listens() {
        let directory = TestDirectory::new();
        let socket = directory.0.join("p.sock");
        let result = Plugin::new()
            .on_migration(|_context| async { Err("old data is broken".to_owned()) })
            .serve(Env::default(), &socket)
            .await;
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("old data is broken")
        );
        assert!(!socket.exists());
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
