mod backend;
mod transport;
mod workers;

use std::{collections::HashMap, net::SocketAddr, path::PathBuf, sync::Arc};

use anyhow::{Context, bail};
use serde_json::Value;
use tokio::{
    process::Command,
    sync::{broadcast, oneshot, watch},
};

use backend::Backend;
use workers::PluginRegistry;

const HELP: &str = "Decky Rust runtime compatibility probe (not a production loader)\n\nRequired:\n  --worker PATH              Packaged DeckyPluginWorker executable\n  --plugin-root PATH         Isolated plugin directory\n  --homebrew PATH            Isolated settings/data/logs root\n  --user USER                Host user identity for normal plugins\n  --assets PATH              Existing Decky frontend bundles\n  --locales PATH             Existing Decky locale JSON directory\n\nOptional:\n  --listen ADDRESS           Loopback address (default 127.0.0.1:1339)\n  --auth-file PATH           Read token from a private file instead of generating one\n\nSupports plugin discovery/RPC/events/reload, loader settings, bootstrap information,\nstatic assets and the existing fetch proxy. Installation, OS administration and\nrelease-update routes are intentionally unavailable; they return explicit errors.\n";

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
            "--plugin-root",
            "--homebrew",
            "--user",
            "--assets",
            "--locales",
            "--listen",
            "--auth-file",
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
    let version = runtime["decky_version"]
        .as_str()
        .context("worker description is missing decky_version")?
        .to_owned();
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
    let (shutdown_watch_tx, mut shutdown_watch_rx) = watch::channel(false);
    let signal_shutdown_tx = shutdown_watch_tx.clone();
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
        let _ = shutdown_tx.send(());
    });
    let registry = match PluginRegistry::start(
        worker,
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
            return Err(error);
        }
    };
    let backend = tokio::select! {
        result = Backend::new(registry.clone(), homebrew, user, user_home, version) => {
            match result {
                Ok(backend) => Arc::new(backend),
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
            return Err(anyhow::anyhow!("Probe startup interrupted by shutdown"));
        }
    };
    eprintln!(
        "Decky Rust compatibility probe listening at {address}; CPython runtime: {}",
        runtime["python"]
    );
    let result =
        transport::serve(address, auth, assets, locales, backend, events, shutdown_rx).await;
    let _ = shutdown_watch_tx.send(true);
    registry.shutdown().await;
    signal_task.abort();
    result
}

fn required(options: &mut HashMap<String, String>, key: &str) -> anyhow::Result<String> {
    options
        .remove(key)
        .with_context(|| format!("{key} is required; use --help"))
}
