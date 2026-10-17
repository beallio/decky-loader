#![deny(clippy::undocumented_unsafe_blocks)]

mod backend;
mod cef;
mod launcher;
mod platform;
mod plugin_manager;
mod service_manager;
mod transport;
mod updater;
mod utilities;
mod workers;

use std::{collections::HashMap, net::SocketAddr, path::PathBuf, sync::Arc};

use anyhow::{Context, bail};
use serde_json::Value;
use tokio::{
    process::Command,
    sync::{Mutex, broadcast, oneshot, watch},
};

use backend::{Backend, RuntimeConfig};
use workers::{PluginRegistry, WorkerBootstrap, WorkerConfiguration, WorkerMode};

const HELP: &str = "Decky Rust backend (private SteamOS validation build)\n\nRequired:\n  --worker PATH              Packaged DeckyPluginWorker executable\n  --plugin-root PATH         Installed plugin directory\n  --homebrew PATH            Unprivileged settings/data/logs root\n  --user USER                Host user identity for normal plugins\n  --assets PATH              Existing Decky frontend bundles\n  --locales PATH             Existing Decky locale JSON directory\n\nOptional:\n  --worker-mode MODE         Worker supervision: fork or exec (default fork)\n  --worker-bootstrap MODE    Runtime preload: full or lean (default lean)\n  --release-version STRING  Installed Rust release identity (default dev)\n  --listen ADDRESS           Loopback address (default 127.0.0.1:1339)\n  --auth-file PATH           Read token from a private file\n  --service-name NAME        Loader user service (default plugin_loader)\n  --cef-url URL              Steam CEF debugger (default http://127.0.0.1:8080)\n  --inject-frontend BOOL     Run native frontend injector (default false)\n  --installer PATH           Transactional installer used for loader updates\n  --github-api URL           GitHub API origin\n  --release-repository NAME  Rust release repository (default beallio/decky-loader)\n  --testing-download-url URL Rust testing artifact service (default https://nightly.link)\n\nUses the unchanged v3.2.10 frontend and packaged Python SDK. Supports plugin\nmanagement, utilities, native CEF integration and transactional loader updates.\nPhysical Steam Deck validation and public distribution are not complete.\n";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let mut arguments = std::env::args().skip(1);
    let mut options = HashMap::new();
    while let Some(key) = arguments.next() {
        if key == "--help" || key == "-h" {
            print!("{HELP}");
            return Ok(());
        }
        if ![
            "--worker",
            "--worker-mode",
            "--worker-bootstrap",
            "--release-version",
            "--plugin-root",
            "--homebrew",
            "--user",
            "--assets",
            "--locales",
            "--listen",
            "--auth-file",
            "--service-name",
            "--cef-url",
            "--inject-frontend",
            "--installer",
            "--github-api",
            "--release-repository",
            "--testing-download-url",
        ]
        .contains(&key.as_str())
        {
            bail!("unknown argument {key}; use --help");
        }
        let value = arguments
            .next()
            .with_context(|| format!("missing value for {key}"))?;
        if options.contains_key(&key) {
            bail!("argument {key} was supplied more than once");
        }
        options.insert(key, value);
    }
    let worker_mode_name = options
        .remove("--worker-mode")
        .unwrap_or_else(|| "fork".to_owned());
    let worker_mode = match worker_mode_name.as_str() {
        "exec" => WorkerMode::Exec,
        "fork" => WorkerMode::Fork,
        value => bail!("invalid --worker-mode {value:?}; expected exec or fork"),
    };
    let worker_bootstrap = match options
        .remove("--worker-bootstrap")
        .as_deref()
        .unwrap_or("lean")
    {
        "full" => WorkerBootstrap::Full,
        "lean" => WorkerBootstrap::Lean,
        value => bail!("invalid --worker-bootstrap {value:?}; expected full or lean"),
    };
    let address: SocketAddr = options
        .remove("--listen")
        .unwrap_or_else(|| "127.0.0.1:1339".into())
        .parse()?;
    if !address.ip().is_loopback() {
        bail!("the compatibility probe must listen on loopback");
    }
    let worker = PathBuf::from(required(&mut options, "--worker")?);
    let plugin_root = PathBuf::from(required(&mut options, "--plugin-root")?);
    let homebrew = PathBuf::from(required(&mut options, "--homebrew")?);
    let user = required(&mut options, "--user")?;
    let assets = PathBuf::from(required(&mut options, "--assets")?);
    let locales = PathBuf::from(required(&mut options, "--locales")?);
    let defaults = RuntimeConfig::default();
    let config = RuntimeConfig {
        service_name: options
            .remove("--service-name")
            .unwrap_or(defaults.service_name),
        cef_url: options.remove("--cef-url").unwrap_or(defaults.cef_url),
        inject_frontend: match options
            .remove("--inject-frontend")
            .as_deref()
            .unwrap_or("false")
        {
            "true" | "1" => true,
            "false" | "0" => false,
            value => bail!("invalid --inject-frontend {value:?}; expected true or false"),
        },
        installer: options.remove("--installer").map(PathBuf::from),
        listen: address.to_string(),
        worker_mode: worker_mode_name,
        github_api: options
            .remove("--github-api")
            .unwrap_or(defaults.github_api),
        release_repository: options
            .remove("--release-repository")
            .unwrap_or(defaults.release_repository),
        testing_download_url: options
            .remove("--testing-download-url")
            .unwrap_or(defaults.testing_download_url),
        chown_plugin_path: std::env::var("CHOWN_PLUGIN_PATH").map_or(true, |value| value == "1"),
    };
    if config.inject_frontend && address.port() != 1337 {
        bail!("the unchanged Decky frontend requires port 1337 for native injection");
    }
    for (label, path) in [
        ("worker", &worker),
        ("plugins", &plugin_root),
        ("frontend assets", &assets),
        ("locales", &locales),
    ] {
        if !path.exists() {
            bail!("{label} path does not exist: {}", path.display());
        }
    }
    tokio::fs::create_dir_all(&homebrew).await?;
    let auth = if let Some(path) = options.remove("--auth-file") {
        let value = tokio::fs::read_to_string(path).await?;
        let value = value.trim();
        if value.is_empty() {
            bail!("authentication token file is empty");
        }
        value.to_owned()
    } else {
        uuid::Uuid::new_v4().to_string()
    };
    let description = Command::new(&worker)
        .arg("--describe")
        .output()
        .await
        .context("describing packaged Python worker")?;
    if !description.status.success() {
        bail!(
            "worker description failed: {}",
            String::from_utf8_lossy(&description.stderr)
        );
    }
    let runtime: Value = serde_json::from_slice(&description.stdout)
        .context("decoding worker runtime description")?;
    if runtime["python"]
        .as_str()
        .is_none_or(|version| !version.starts_with("3.11."))
    {
        bail!("probe requires the CPython 3.11 release-compatibility runtime, received {runtime}");
    }
    let sdk_version = runtime["decky_version"]
        .as_str()
        .context("worker description is missing decky_version")?
        .to_owned();
    let version = options
        .remove("--release-version")
        .unwrap_or_else(|| "dev".to_owned());
    if version.is_empty()
        || version
            .chars()
            .any(|character| character.is_ascii_control())
    {
        bail!("--release-version must be a non-empty printable release identifier");
    }
    let account = Command::new("getent")
        .args(["passwd", &user])
        .output()
        .await
        .context("resolving host user")?;
    if !account.status.success() {
        bail!("host user {user} could not be resolved");
    }
    let account = std::str::from_utf8(&account.stdout)?;
    let mut fields = account.split(':');
    let _ = fields.next();
    let _ = fields.next();
    let user_uid = fields.next().context("host account has no uid")?.parse()?;
    let user_gid = fields.next().context("host account has no gid")?.parse()?;
    let _ = fields.next();
    let user_home = fields
        .next()
        .context("host account has no home directory")?
        .to_owned();
    let (events, _) = broadcast::channel(256);
    let (shutdown_tx, shutdown_rx) = oneshot::channel();
    let shutdown_tx = Arc::new(Mutex::new(Some(shutdown_tx)));
    let (shutdown_watch_tx, mut shutdown_watch_rx) = watch::channel(false);
    let signal_shutdown_tx = shutdown_watch_tx.clone();
    let signal_server_shutdown = shutdown_tx.clone();
    #[cfg(unix)]
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .context("installing SIGTERM handler")?;
    let signal_task = tokio::spawn(async move {
        #[cfg(unix)]
        {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {},
                _ = terminate.recv() => {},
            }
        }
        #[cfg(not(unix))]
        let _ = tokio::signal::ctrl_c().await;
        let _ = signal_shutdown_tx.send(true);
        if let Some(shutdown) = signal_server_shutdown.lock().await.take() {
            let _ = shutdown.send(());
        }
    });
    let registry = match PluginRegistry::start(
        WorkerConfiguration {
            executable: worker,
            mode: worker_mode,
            bootstrap: worker_bootstrap,
            auth_token: auth.clone(),
            server_host: address.ip().to_string(),
            server_port: address.port(),
            chown_plugin_path: config.chown_plugin_path,
        },
        plugin_root,
        homebrew.clone(),
        user.clone(),
        (user_uid, user_gid),
        events.clone(),
        shutdown_watch_rx.clone(),
    )
    .await
    {
        Ok(registry) => Arc::new(registry),
        Err(error) => {
            signal_task.abort();
            if *shutdown_watch_rx.borrow() {
                return Ok(());
            }
            return Err(error);
        }
    };
    let backend = tokio::select! {
        result = Backend::new(registry.clone(), homebrew, user, user_home, version, sdk_version) => {
            match result {
                Ok(backend) => Arc::new(backend.with_config(config)),
                Err(error) => {
                    let _ = shutdown_watch_tx.send(true);
                    registry.shutdown().await;
                    signal_task.abort();
                    return Err(error);
                }
            }
        }
        _ = shutdown_watch_rx.changed() => {
            let _ = shutdown_watch_tx.send(true);
            registry.shutdown().await;
            signal_task.abort();
            return Ok(());
        }
    };
    let initialize = async {
        backend
            .initialize_plugin_order()
            .await
            .map_err(|error| anyhow::anyhow!(error.message().to_owned()))?;
        if backend.setting("branch", serde_json::json!(-1)).await == serde_json::json!(-1) {
            backend
                .set_setting(
                    "branch",
                    serde_json::json!(updater::default_branch(backend.loader_version())),
                )
                .await
                .map_err(|error| anyhow::anyhow!(error.message().to_owned()))?;
        }
        registry.start_live_reload().await?;
        Ok::<(), anyhow::Error>(())
    }
    .await;
    if let Err(error) = initialize {
        let _ = shutdown_watch_tx.send(true);
        registry.shutdown().await;
        signal_task.abort();
        return Err(error);
    }
    if backend.config.inject_frontend {
        let route = if backend.setting("cef_forward", Value::Bool(false)).await == Value::Bool(true)
        {
            "utilities/allow_remote_debugging"
        } else {
            "utilities/disallow_remote_debugging"
        };
        if let Err(error) = backend.dispatch(route, vec![]).await {
            eprintln!("CEF forwarding startup failed: {}", error.message());
        }
    }
    let updater_task = backend.updater.clone().start(backend.clone());
    let injector_task = backend.config.inject_frontend.then(|| {
        tokio::spawn(cef::Cef::run_injector(
            backend.clone(),
            shutdown_watch_rx.clone(),
        ))
    });
    eprintln!(
        "Decky Rust backend listening at {address}; CPython runtime: {}",
        runtime["python"]
    );
    let server = transport::serve(
        address,
        auth,
        assets,
        locales,
        backend.clone(),
        events,
        shutdown_rx,
    );
    tokio::pin!(server);
    let result = tokio::select! {
        result = &mut server => result,
        _ = shutdown_watch_rx.changed() => {
            backend.cancel_utilities_setup();
            server.await
        }
        _ = essential_launcher_failed(registry.essential_failure()) => {
            let requested = *shutdown_watch_rx.borrow();
            let _ = shutdown_watch_tx.send(true);
            if let Some(shutdown) = shutdown_tx.lock().await.take() {
                let _ = shutdown.send(());
            }
            backend.cancel_utilities_setup();
            let server_result = server.await;
            if requested {
                server_result
            } else {
                if let Err(error) = server_result {
                    eprintln!("Server shutdown after launcher loss failed: {error}");
                }
                Err(anyhow::anyhow!("shared Python launcher exited; stopping backend so its service can restart"))
            }
        }
    };
    let _ = shutdown_watch_tx.send(true);
    updater_task.shutdown().await;
    if let Some(task) = injector_task {
        let _ = task.await;
    }
    if let Err(error) = backend.shutdown_utilities().await {
        eprintln!("Utility shutdown failed: {}", error.message());
    }
    registry.shutdown().await;
    signal_task.abort();
    result
}

fn required(options: &mut HashMap<String, String>, key: &str) -> anyhow::Result<String> {
    options
        .remove(key)
        .with_context(|| format!("{key} is required; use --help"))
}

async fn essential_launcher_failed(receiver: Option<watch::Receiver<bool>>) {
    let Some(mut receiver) = receiver else {
        std::future::pending::<()>().await;
        return;
    };
    while !*receiver.borrow() {
        if receiver.changed().await.is_err() {
            return;
        }
    }
}
