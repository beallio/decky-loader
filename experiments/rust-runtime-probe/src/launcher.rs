use std::{
    collections::HashSet,
    io,
    os::unix::process::CommandExt,
    path::Path,
    process::{ExitStatus, Stdio},
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::{Mutex, mpsc, watch},
    task::JoinHandle,
    time::timeout,
};

const LAUNCHER_READY_TIMEOUT: Duration = Duration::from_secs(30);
const CONTROL_TIMEOUT: Duration = Duration::from_secs(5);
const LAUNCHER_GRACEFUL_TIMEOUT: Duration = Duration::from_secs(5);

struct Control {
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}

pub(crate) struct ForkLauncher {
    control: Mutex<Control>,
    groups: Arc<Mutex<HashSet<u32>>>,
    stop: mpsc::UnboundedSender<()>,
    reaper: Mutex<Option<JoinHandle<io::Result<ExitStatus>>>>,
    failure: watch::Receiver<bool>,
}

#[derive(Debug)]
pub(crate) enum LauncherStartError {
    Failed(String),
    Interrupted,
}

pub(crate) struct LauncherEnvironment<'a> {
    pub auth_token: &'a str,
    pub server_host: &'a str,
    pub server_port: u16,
    pub chown_plugin_path: bool,
}

impl ForkLauncher {
    pub(crate) async fn spawn(
        worker_executable: &Path,
        bootstrap: &str,
        plugin_root: &Path,
        homebrew: &Path,
        user: &str,
        shutdown: &mut watch::Receiver<bool>,
        environment: LauncherEnvironment<'_>,
    ) -> std::result::Result<Self, LauncherStartError> {
        if *shutdown.borrow() {
            return Err(LauncherStartError::Interrupted);
        }

        let mut command = Command::new(worker_executable);
        command
            .arg("--launcher")
            .arg("--bootstrap")
            .arg(bootstrap)
            .env("PLUGIN_PATH", plugin_root)
            .env("UNPRIVILEGED_PATH", homebrew)
            .env("PRIVILEGED_PATH", plugin_root.parent().unwrap_or(homebrew))
            .env("UNPRIVILEGED_USER", user)
            .env("DECKY_AUTH_TOKEN", environment.auth_token)
            .env("SERVER_HOST", environment.server_host)
            .env("SERVER_PORT", environment.server_port.to_string())
            .env(
                "CHOWN_PLUGIN_PATH",
                if environment.chown_plugin_path {
                    "1"
                } else {
                    "0"
                },
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        command.as_std_mut().process_group(0);
        let parent_pid = nix::unistd::Pid::this();
        // SAFETY: the child-side callback only uses nix wrappers around the
        // Linux prctl/getppid syscalls and constructs an error if the syscall
        // fails. Those paths take no locks or access mutable shared state; the
        // successful startup path does not allocate.
        unsafe {
            command.as_std_mut().pre_exec(move || {
                nix::sys::prctl::set_pdeathsig(nix::sys::signal::Signal::SIGTERM)
                    .map_err(io::Error::from)?;
                if nix::unistd::Pid::parent() != parent_pid {
                    return Err(io::Error::from(nix::errno::Errno::ECHILD));
                }
                Ok(())
            });
        }
        let mut child = command.spawn().map_err(|error| {
            LauncherStartError::Failed(format!(
                "Failed to start shared worker launcher using {}: {error}",
                worker_executable.display()
            ))
        })?;
        let pid = child.id().expect("a spawned launcher has a process ID");
        let Some(input) = child.stdin.take() else {
            terminate_launcher_startup(&mut child, pid).await;
            return Err(LauncherStartError::Failed(
                "Shared worker launcher stdin was not available".to_owned(),
            ));
        };
        let Some(output) = child.stdout.take() else {
            terminate_launcher_startup(&mut child, pid).await;
            return Err(LauncherStartError::Failed(
                "Shared worker launcher stdout was not available".to_owned(),
            ));
        };
        let mut output = BufReader::new(output);
        let mut line = String::new();
        let ready = tokio::select! {
            biased;
            changed = shutdown.changed() => {
                if changed.is_err() || *shutdown.borrow() {
                    terminate_launcher_startup(&mut child, pid).await;
                    return Err(LauncherStartError::Interrupted);
                }
                continue_ready_read(&mut output, &mut line).await
            }
            result = timeout(LAUNCHER_READY_TIMEOUT, output.read_line(&mut line)) => {
                match result {
                    Ok(Ok(0)) => Err("Shared worker launcher exited before becoming ready".to_owned()),
                    Ok(Ok(_)) => parse_ready(&line),
                    Ok(Err(error)) => Err(format!("Could not read shared worker launcher readiness: {error}")),
                    Err(_) => Err(format!(
                        "Shared worker launcher did not become ready within {} seconds",
                        LAUNCHER_READY_TIMEOUT.as_secs()
                    )),
                }
            }
        };
        if let Err(error) = ready {
            terminate_launcher_startup(&mut child, pid).await;
            return Err(LauncherStartError::Failed(error));
        }
        if *shutdown.borrow() {
            terminate_launcher_startup(&mut child, pid).await;
            return Err(LauncherStartError::Interrupted);
        }

        let groups = Arc::new(Mutex::new(HashSet::new()));
        let (stop, stop_receiver) = mpsc::unbounded_channel();
        let (failed, failure) = watch::channel(false);
        let reaper_groups = groups.clone();
        let reaper = tokio::spawn(async move {
            let result = reap_launcher(child, pid, reaper_groups, stop_receiver).await;
            let _ = failed.send(true);
            result
        });
        Ok(Self {
            control: Mutex::new(Control { input, output }),
            groups,
            stop,
            reaper: Mutex::new(Some(reaper)),
            failure,
        })
    }

    pub(crate) fn failure(&self) -> watch::Receiver<bool> {
        self.failure.clone()
    }

    pub(crate) async fn start_child(&self, request: Value) -> Result<u32> {
        let forked = self.request(request).await?;
        if let Err(error) = expect_operation(&forked, "forked") {
            self.fail();
            return Err(error);
        }
        let pid = match forked["pid"]
            .as_u64()
            .and_then(|pid| u32::try_from(pid).ok())
        {
            Some(pid) => pid,
            None => {
                self.fail();
                bail!("shared worker launcher returned an invalid child PID")
            }
        };
        self.groups.lock().await.insert(pid);

        Ok(pid)
    }

    pub(crate) async fn release_child(&self, id: &str) -> Result<()> {
        let released = self.request(json!({"op": "release", "id": id})).await?;
        if let Err(error) = expect_operation(&released, "released") {
            self.fail();
            return Err(error);
        }
        Ok(())
    }

    pub(crate) async fn child_status(&self, id: &str) -> Result<Option<i32>> {
        let response = self.request(json!({"op": "status", "id": id})).await?;
        if let Err(error) = expect_operation(&response, "status") {
            self.fail();
            return Err(error);
        }
        if response["running"].as_bool() == Some(true) {
            return Ok(None);
        }
        match response["wait_status"]
            .as_i64()
            .and_then(|status| i32::try_from(status).ok())
        {
            Some(status) => Ok(Some(status)),
            None => {
                self.fail();
                bail!("shared worker launcher returned an invalid wait status")
            }
        }
    }

    pub(crate) async fn forget_child(&self, id: &str, pid: u32) -> Result<()> {
        let response = self.request(json!({"op": "forget", "id": id})).await?;
        if let Err(error) = expect_operation(&response, "forgotten") {
            self.fail();
            return Err(error);
        }
        self.groups.lock().await.remove(&pid);
        Ok(())
    }

    pub(crate) async fn shutdown(&self) {
        let _ = self
            .request(json!({"op": "shutdown", "id": "launcher"}))
            .await;
        if let Some(mut reaper) = self.reaper.lock().await.take() {
            match timeout(
                LAUNCHER_GRACEFUL_TIMEOUT + Duration::from_secs(1),
                &mut reaper,
            )
            .await
            {
                Ok(Ok(Ok(_))) => {}
                Ok(Ok(Err(error))) => eprintln!("Shared worker launcher reap failed: {error}"),
                Ok(Err(error)) => eprintln!("Shared worker launcher monitor failed: {error}"),
                Err(_) => {
                    self.fail();
                    if timeout(
                        LAUNCHER_GRACEFUL_TIMEOUT + Duration::from_secs(1),
                        &mut reaper,
                    )
                    .await
                    .is_err()
                    {
                        eprintln!("Shared worker launcher did not finish its owned-group cleanup");
                    }
                }
            }
        }
    }

    fn fail(&self) {
        let _ = self.stop.send(());
    }

    async fn request(&self, request: Value) -> Result<Value> {
        let result = self.request_inner(request).await;
        if result.is_err() {
            self.fail();
        }
        result
    }

    async fn request_inner(&self, request: Value) -> Result<Value> {
        let mut control = self.control.lock().await;
        let mut bytes =
            serde_json::to_vec(&request).context("encoding launcher control request")?;
        bytes.push(b'\n');
        control
            .input
            .write_all(&bytes)
            .await
            .context("writing shared worker launcher control request")?;
        control
            .input
            .flush()
            .await
            .context("flushing shared worker launcher control request")?;
        let mut line = String::new();
        let count = timeout(CONTROL_TIMEOUT, control.output.read_line(&mut line))
            .await
            .context("timed out waiting for shared worker launcher response")?
            .context("reading shared worker launcher response")?;
        if count == 0 {
            bail!("shared worker launcher closed its control channel")
        }
        let response: Value = serde_json::from_str(&line)
            .context("decoding shared worker launcher control response")?;
        if response["ok"].as_bool() != Some(true) {
            bail!(
                "shared worker launcher rejected control request: {}",
                response["error"].as_str().unwrap_or("unspecified error")
            )
        }
        Ok(response)
    }
}

impl Drop for ForkLauncher {
    fn drop(&mut self) {
        let _ = self.stop.send(());
    }
}

fn parse_ready(line: &str) -> std::result::Result<(), String> {
    let response: Value = serde_json::from_str(line)
        .map_err(|error| format!("Invalid shared worker launcher readiness response: {error}"))?;
    if response["ok"].as_bool() == Some(true) && response["op"].as_str() == Some("ready") {
        Ok(())
    } else {
        Err(format!(
            "Shared worker launcher failed during bootstrap: {}",
            response["error"]
                .as_str()
                .unwrap_or("invalid readiness response")
        ))
    }
}

async fn continue_ready_read(
    output: &mut BufReader<ChildStdout>,
    line: &mut String,
) -> std::result::Result<(), String> {
    match timeout(LAUNCHER_READY_TIMEOUT, output.read_line(line)).await {
        Ok(Ok(0)) => Err("Shared worker launcher exited before becoming ready".to_owned()),
        Ok(Ok(_)) => parse_ready(line),
        Ok(Err(error)) => Err(format!(
            "Could not read shared worker launcher readiness: {error}"
        )),
        Err(_) => Err(format!(
            "Shared worker launcher did not become ready within {} seconds",
            LAUNCHER_READY_TIMEOUT.as_secs()
        )),
    }
}

fn expect_operation(response: &Value, expected: &str) -> Result<()> {
    if response["op"].as_str() == Some(expected) {
        Ok(())
    } else {
        bail!("shared worker launcher returned an unexpected response: {response}")
    }
}

async fn terminate_launcher_startup(child: &mut Child, pid: u32) {
    send_signal(pid, libc::SIGTERM);
    match timeout(LAUNCHER_GRACEFUL_TIMEOUT, child.wait()).await {
        Ok(Ok(_)) => send_group_signal(pid, libc::SIGKILL),
        Ok(Err(_)) | Err(_) => {
            send_group_signal(pid, libc::SIGKILL);
            let _ = child.wait().await;
        }
    }
}

async fn reap_launcher(
    mut child: Child,
    pid: u32,
    groups: Arc<Mutex<HashSet<u32>>>,
    mut stop: mpsc::UnboundedReceiver<()>,
) -> io::Result<ExitStatus> {
    let status = tokio::select! {
        status = child.wait() => status,
        _ = stop.recv() => {
            send_signal(pid, libc::SIGTERM);
            match timeout(LAUNCHER_GRACEFUL_TIMEOUT, child.wait()).await {
                Ok(status) => status,
                Err(_) => {
                    send_group_signal(pid, libc::SIGKILL);
                    child.wait().await
                }
            }
        }
    };
    for group in groups.lock().await.drain() {
        send_group_signal(group, libc::SIGKILL);
    }
    send_group_signal(pid, libc::SIGKILL);
    status
}

fn send_signal(pid: u32, signal: i32) {
    let _ = crate::platform::signal_process(pid, signal);
}

fn send_group_signal(pid: u32, signal: i32) {
    let _ = crate::platform::signal_group(pid, signal);
}
