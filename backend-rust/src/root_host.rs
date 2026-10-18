//! Client for the optional root plugin host (see docs/root-plugin-host.md).
//!
//! The host is a socket-activated system service that runs only plugins marked
//! `root`. It checks each plugin folder itself and accepts only names from this
//! client, never paths or descriptors. This side cannot signal root processes,
//! so it stops root plugins through the host.

use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{
        UnixStream,
        unix::{OwnedReadHalf, OwnedWriteHalf},
    },
    sync::Mutex,
    time::timeout,
};

const PROTOCOL: i64 = 1;
const CONTROL_TIMEOUT: Duration = Duration::from_secs(5);
// The host waits up to 5 s for SIGTERM, then sends SIGKILL and reaps the group.
const STOP_TIMEOUT: Duration = Duration::from_secs(15);
// The host downloads and checks the plugin itself.
const INSTALL_TIMEOUT: Duration = Duration::from_secs(15 * 60);

pub(crate) const NOT_ENABLED: &str = "This plugin needs root. Run `bash install-to-deck.sh --enable-root-plugins` to allow plugins that need root.";

pub(crate) fn socket_path(uid: u32) -> PathBuf {
    PathBuf::from(format!("/run/decky-loader-runtime/{uid}/root-host.sock"))
}

pub(crate) struct HostEnvironment<'a> {
    pub auth_token: &'a str,
    pub server_host: &'a str,
    pub server_port: u16,
    pub loader_version: &'a str,
}

struct Control {
    input: OwnedWriteHalf,
    output: BufReader<OwnedReadHalf>,
}

pub(crate) struct RootHost {
    control: Mutex<Control>,
    failed: AtomicBool,
}

/// The host answered `ok: false`. This concerns one request only (for example one
/// plugin's folder check); the connection stays usable.
#[derive(Debug)]
struct Refused(String);

impl std::fmt::Display for Refused {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "the root plugin host refused the request: {}",
            self.0
        )
    }
}

impl std::error::Error for Refused {}

impl RootHost {
    pub(crate) async fn connect(path: &Path, environment: HostEnvironment<'_>) -> Result<Self> {
        let stream = timeout(CONTROL_TIMEOUT, UnixStream::connect(path))
            .await
            .context("timed out connecting to the root plugin host")?
            .with_context(|| format!("connecting to the root plugin host at {}", path.display()))?;
        let (output, input) = stream.into_split();
        let host = Self {
            control: Mutex::new(Control {
                input,
                output: BufReader::new(output),
            }),
            failed: AtomicBool::new(false),
        };
        let hello = host
            .request(
                json!({
                    "op": "hello",
                    "protocol": PROTOCOL,
                    "auth_token": environment.auth_token,
                    "server_host": environment.server_host,
                    "server_port": environment.server_port,
                    "loader_version": environment.loader_version,
                }),
                "hello",
                CONTROL_TIMEOUT,
            )
            .await?;
        if hello["protocol"].as_i64() != Some(PROTOCOL) {
            host.failed.store(true, Ordering::Release);
            bail!(
                "the root plugin host speaks protocol {}, but this loader needs {PROTOCOL}; it updates itself, so restart Decky in a few minutes",
                hello["protocol"]
            );
        }
        Ok(host)
    }

    pub(crate) fn is_failed(&self) -> bool {
        self.failed.load(Ordering::Acquire)
    }

    /// Starts the plugin in `directory` and returns its PID and IPC socket path.
    pub(crate) async fn start_child(&self, id: &str, directory: &str) -> Result<(u32, PathBuf)> {
        let forked = self
            .request(
                json!({"op": "start", "id": id, "directory": directory}),
                "forked",
                CONTROL_TIMEOUT,
            )
            .await?;
        let pid = forked["pid"]
            .as_u64()
            .and_then(|pid| u32::try_from(pid).ok())
            .filter(|pid| *pid > 0);
        let socket = forked["socket"].as_str().map(PathBuf::from);
        match (pid, socket) {
            (Some(pid), Some(socket)) if socket.is_absolute() => Ok((pid, socket)),
            _ => {
                self.failed.store(true, Ordering::Release);
                bail!("the root plugin host returned an invalid start response: {forked}")
            }
        }
    }

    pub(crate) async fn release_child(&self, id: &str) -> Result<()> {
        self.request(
            json!({"op": "release", "id": id}),
            "released",
            CONTROL_TIMEOUT,
        )
        .await
        .map(drop)
    }

    /// `None` while the plugin runs; its wait status after it exits.
    pub(crate) async fn child_status(&self, id: &str) -> Result<Option<i32>> {
        let response = self
            .request(json!({"op": "status", "id": id}), "status", CONTROL_TIMEOUT)
            .await?;
        if response["running"].as_bool() == Some(true) {
            return Ok(None);
        }
        let status = wait_status(&response);
        if status.is_err() {
            self.failed.store(true, Ordering::Release);
        }
        status.map(Some)
    }

    /// Stops the plugin's process group (SIGTERM, then SIGKILL) and returns its wait status.
    pub(crate) async fn stop_child(&self, id: &str) -> Result<i32> {
        let response = self
            .request(json!({"op": "stop", "id": id}), "stopped", STOP_TIMEOUT)
            .await?;
        wait_status(&response)
    }

    /// Has the host download a store plugin that needs root, check it against its
    /// store SHA-256, and put it in `directory`. The host accepts only store URLs.
    pub(crate) async fn install(
        &self,
        directory: &str,
        name: &str,
        sha256: &str,
        artifact: &str,
    ) -> Result<()> {
        self.request(
            json!({
                "op": "install", "directory": directory, "name": name,
                "sha256": sha256, "artifact": artifact,
            }),
            "installed",
            INSTALL_TIMEOUT,
        )
        .await
        .map(drop)
    }

    pub(crate) async fn uninstall(&self, directory: &str) -> Result<()> {
        self.request(
            json!({"op": "uninstall", "directory": directory}),
            "uninstalled",
            CONTROL_TIMEOUT,
        )
        .await
        .map(drop)
    }

    pub(crate) async fn forget_child(&self, id: &str) -> Result<()> {
        self.request(
            json!({"op": "forget", "id": id}),
            "forgotten",
            CONTROL_TIMEOUT,
        )
        .await
        .map(drop)
    }

    async fn request(&self, request: Value, expected: &str, limit: Duration) -> Result<Value> {
        let result = self.request_inner(request, expected, limit).await;
        if let Err(error) = &result
            && error.downcast_ref::<Refused>().is_none()
        {
            self.failed.store(true, Ordering::Release);
        }
        result
    }

    async fn request_inner(
        &self,
        request: Value,
        expected: &str,
        limit: Duration,
    ) -> Result<Value> {
        if self.is_failed() {
            bail!("the root plugin host connection has failed");
        }
        let mut control = self.control.lock().await;
        let mut bytes = serde_json::to_vec(&request).context("encoding root host request")?;
        bytes.push(b'\n');
        control
            .input
            .write_all(&bytes)
            .await
            .context("writing root host request")?;
        let mut line = String::new();
        let count = timeout(limit, control.output.read_line(&mut line))
            .await
            .context("timed out waiting for the root plugin host")?
            .context("reading root host response")?;
        if count == 0 {
            bail!("the root plugin host closed its control connection");
        }
        let response: Value = serde_json::from_str(&line).context("decoding root host response")?;
        if response["ok"].as_bool() != Some(true) {
            let error = response["error"].as_str().unwrap_or("unspecified error");
            return Err(Refused(error.to_owned()).into());
        }
        if response["op"].as_str() != Some(expected) {
            bail!("the root plugin host returned an unexpected response: {response}");
        }
        Ok(response)
    }
}

fn wait_status(response: &Value) -> Result<i32> {
    response["wait_status"]
        .as_i64()
        .and_then(|status| i32::try_from(status).ok())
        .with_context(|| {
            format!("the root plugin host returned an invalid wait status: {response}")
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::UnixListener;

    const ENVIRONMENT: HostEnvironment<'static> = HostEnvironment {
        auth_token: "token",
        server_host: "127.0.0.1",
        server_port: 1337,
        loader_version: "v3.2.10-rust.8",
    };

    /// A fake host that answers each request with the next scripted reply and
    /// returns the requests it saw.
    async fn serve(replies: Vec<Value>) -> (PathBuf, tokio::task::JoinHandle<Vec<Value>>) {
        let directory =
            std::env::temp_dir().join(format!("decky-root-host-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&directory).unwrap();
        let path = directory.join("host.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let task = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let (read, mut write) = stream.into_split();
            let mut lines = BufReader::new(read).lines();
            let mut seen = Vec::new();
            for reply in replies {
                let Some(line) = lines.next_line().await.unwrap() else {
                    break;
                };
                seen.push(serde_json::from_str(&line).unwrap());
                write
                    .write_all(format!("{reply}\n").as_bytes())
                    .await
                    .unwrap();
            }
            let _ = std::fs::remove_dir_all(directory);
            seen
        });
        (path, task)
    }

    #[tokio::test]
    async fn starts_a_plugin_after_hello_and_stops_it() {
        let (path, server) = serve(vec![
            json!({"ok": true, "op": "hello", "protocol": 1, "pid": 9}),
            json!({"ok": true, "op": "forked", "id": "a", "pid": 42, "socket": "/run/x/a.sock"}),
            json!({"ok": true, "op": "status", "id": "a", "running": true}),
            json!({"ok": true, "op": "stopped", "id": "a", "wait_status": 15}),
        ])
        .await;
        let host = RootHost::connect(&path, ENVIRONMENT).await.unwrap();
        assert_eq!(
            host.start_child("a", "PowerTools").await.unwrap(),
            (42, PathBuf::from("/run/x/a.sock"))
        );
        assert_eq!(host.child_status("a").await.unwrap(), None);
        assert_eq!(host.stop_child("a").await.unwrap(), 15);
        let seen = server.await.unwrap();
        assert_eq!(seen[0]["auth_token"], "token");
        assert_eq!(
            seen[1],
            json!({"op": "start", "id": "a", "directory": "PowerTools"})
        );
        assert!(!host.is_failed());
    }

    #[tokio::test]
    async fn protocol_mismatch_fails_but_a_refusal_keeps_the_connection() {
        let (path, _server) = serve(vec![json!({"ok": true, "op": "hello", "protocol": 2})]).await;
        let error = RootHost::connect(&path, ENVIRONMENT).await.err().unwrap();
        assert!(error.to_string().contains("protocol"), "{error}");

        let (path, _server) = serve(vec![
            json!({"ok": true, "op": "hello", "protocol": 1}),
            json!({"ok": false, "error": "plugin tree hash does not match"}),
            json!({"ok": true, "op": "status", "id": "b", "running": true}),
            json!({"ok": true, "op": "wrong"}),
        ])
        .await;
        let host = RootHost::connect(&path, ENVIRONMENT).await.unwrap();
        let error = host.start_child("a", "Swapped").await.unwrap_err();
        assert!(error.to_string().contains("tree hash"), "{error}");
        assert!(!host.is_failed());
        assert_eq!(host.child_status("b").await.unwrap(), None);
        assert!(host.child_status("b").await.is_err());
        assert!(host.is_failed());
    }
}
