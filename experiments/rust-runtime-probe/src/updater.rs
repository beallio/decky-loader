use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};

use futures_util::StreamExt;
use reqwest::{Response, Url};
use serde_json::{Value, json};
use tokio::{
    io::AsyncWriteExt,
    process::Command,
    sync::{RwLock, oneshot},
    task::JoinHandle,
};
use uuid::Uuid;
use zip::ZipArchive;

use crate::backend::{Backend, RpcError, exact, string};

const INITIAL_CHECK_DELAY: Duration = Duration::from_secs(30);
const UPDATE_CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);
const FALLBACK_DOWNLOAD_SIZE: u64 = 25 * 1024 * 1024;
const LINUX_EXECUTABLE: &str = "PluginLoader";
const GITHUB_API_VERSION: &str = "2022-11-28";

#[derive(Default)]
struct VersionState {
    remote: Option<Value>,
    all: Vec<Value>,
}

/// Implements the Python updater's WebSocket routes for Linux and SteamOS.
pub struct Updater {
    versions: RwLock<VersionState>,
}

/// Handle returned by [`Updater::start`]. Shutdown cancels the periodic task and waits for it.
pub struct UpdaterTask {
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<()>>,
}

impl UpdaterTask {
    pub async fn shutdown(mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}

impl Drop for UpdaterTask {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
    }
}

impl Updater {
    pub fn new() -> Self {
        Self {
            versions: RwLock::new(VersionState::default()),
        }
    }

    /// Start the delayed six-hour update notification loop. Retain the returned task and call
    /// `shutdown().await` during backend shutdown.
    pub fn start(self: Arc<Self>, backend: Arc<Backend>) -> UpdaterTask {
        let (stop, mut stopped) = oneshot::channel();
        let task = tokio::spawn(async move {
            if !wait_or_stop(INITIAL_CHECK_DELAY, &mut stopped).await {
                return;
            }
            loop {
                // The original updater retries on its next periodic check after discovery errors.
                tokio::select! {
                    _ = &mut stopped => return,
                    _ = self.check_for_updates(&backend) => {},
                }
                if !wait_or_stop(UPDATE_CHECK_INTERVAL, &mut stopped).await {
                    return;
                }
            }
        });
        UpdaterTask {
            stop: Some(stop),
            task: Some(task),
        }
    }

    pub async fn dispatch(
        &self,
        backend: &Backend,
        route: &str,
        args: Vec<Value>,
    ) -> Option<Result<Value, RpcError>> {
        if !matches!(
            route,
            "updater/get_version_info"
                | "updater/check_for_updates"
                | "updater/do_update"
                | "updater/do_restart"
                | "updater/do_shutdown"
                | "updater/get_testing_versions"
                | "updater/download_testing_version"
        ) {
            return None;
        }
        let result = async {
            match route {
                "updater/get_version_info" => {
                    exact(&args, 0)?;
                    Ok(self.version_info(backend).await)
                }
                "updater/check_for_updates" => {
                    exact(&args, 0)?;
                    self.check_for_updates(backend).await
                }
                "updater/do_update" => {
                    exact(&args, 0)?;
                    self.do_update(backend).await
                }
                "updater/do_restart" => {
                    exact(&args, 0)?;
                    restart_service(backend).await.map(|()| Value::Null)
                }
                "updater/do_shutdown" => {
                    exact(&args, 0)?;
                    stop_service(backend).await.map(|()| Value::Null)
                }
                "updater/get_testing_versions" => {
                    exact(&args, 0)?;
                    self.get_testing_versions(backend).await
                }
                "updater/download_testing_version" => {
                    exact(&args, 2)?;
                    let pr_id = args[0].as_u64().filter(|id| *id > 0).ok_or_else(|| {
                        RpcError::invalid("argument 0 must be a positive integer")
                    })?;
                    let sha = string(&args, 1)?;
                    if sha.is_empty() {
                        return Err(RpcError::invalid("argument 1 must not be empty"));
                    }
                    self.download_testing_version(backend, pr_id, sha).await
                }
                _ => unreachable!("updater route was checked above"),
            }
        }
        .await;
        Some(result)
    }

    async fn version_info(&self, backend: &Backend) -> Value {
        let versions = self.versions.read().await;
        json!({
            "current": backend.version.as_str(),
            "remote": versions.remote,
            "all": versions.all,
            "updatable": backend.version.as_str() != "unknown" && backend.version.as_str() != "dev",
        })
    }

    async fn selected_branch(&self, backend: &Backend) -> Result<i64, RpcError> {
        let branch = backend.setting("branch", json!(-1)).await;
        if branch.as_i64() == Some(-1) {
            let current = backend.version.as_str();
            let detected = if current.starts_with('v') && current.contains("-pre") {
                1
            } else {
                0
            };
            backend.set_setting("branch", json!(detected)).await?;
            Ok(detected)
        } else {
            branch
                .as_i64()
                .ok_or_else(|| RpcError::invalid("update branch setting must be an integer"))
        }
    }

    async fn check_for_updates(&self, backend: &Backend) -> Result<Value, RpcError> {
        let branch = self.selected_branch(backend).await?;
        let url = github_url(backend, "releases")?;
        let response = github_request(backend.client.get(url)).await?;
        let mut releases = response.json::<Value>().await.map_err(|error| {
            RpcError::failure(format!("invalid GitHub releases response: {error}"))
        })?;
        let releases = releases
            .as_array_mut()
            .map(std::mem::take)
            .ok_or_else(|| RpcError::failure("GitHub releases response must be an array"))?;
        let filtered = filter_releases(releases, branch)?;
        let remote = filtered.first().cloned();
        {
            let mut versions = self.versions.write().await;
            versions.all = filtered;
            versions.remote = remote;
        }
        backend.emit("loader/notify_updates", Vec::new());
        Ok(self.version_info(backend).await)
    }

    async fn do_update(&self, backend: &Backend) -> Result<Value, RpcError> {
        let remote = self.versions.read().await.remote.clone().ok_or_else(|| {
            RpcError::failure("no remote release is available; check for updates first")
        })?;
        let version = remote
            .get("tag_name")
            .and_then(Value::as_str)
            .filter(|tag| !tag.is_empty())
            .ok_or_else(|| RpcError::failure("selected release has no tag_name"))?;
        let asset = remote
            .get("assets")
            .and_then(Value::as_array)
            .and_then(|assets| {
                assets.iter().find(|asset| {
                    asset.get("name").and_then(Value::as_str) == Some(LINUX_EXECUTABLE)
                })
            })
            .ok_or_else(|| RpcError::failure(format!("release has no {LINUX_EXECUTABLE} asset")))?;
        let download_url = asset
            .get("browser_download_url")
            .and_then(Value::as_str)
            .filter(|url| !url.is_empty())
            .ok_or_else(|| RpcError::failure("release executable asset has no download URL"))?;
        let size = asset.get("size").and_then(Value::as_u64);
        let branch = self.selected_branch(backend).await?;
        let service_template = if backend.config.keep_systemd_service {
            None
        } else {
            Some(self.fetch_service_template(backend, branch).await?)
        };
        self.download_and_handoff_release(backend, download_url, version, size, service_template)
            .await?;
        Ok(Value::Null)
    }

    async fn fetch_service_template(
        &self,
        backend: &Backend,
        branch: i64,
    ) -> Result<String, RpcError> {
        let template_url = if branch == 0 {
            &backend.config.service_release_url
        } else {
            &backend.config.service_prerelease_url
        };
        let url = Url::parse(template_url)
            .map_err(|error| RpcError::failure(format!("invalid service template URL: {error}")))?;
        let response = backend.client.get(url).send().await.map_err(|error| {
            RpcError::failure(format!("service template request failed: {error}"))
        })?;
        let response = checked_response(response, "service template request").await?;
        let template = response.text().await.map_err(|error| {
            RpcError::failure(format!("reading service template failed: {error}"))
        })?;
        Ok(template.replace("${HOMEBREW_FOLDER}", &backend.homebrew.to_string_lossy()))
    }

    async fn get_testing_versions(&self, backend: &Backend) -> Result<Value, RpcError> {
        let url = github_url(backend, "pulls")?;
        let response = github_request(backend.client.get(url).query(&[("state", "open")])).await?;
        let pulls = response.json::<Value>().await.map_err(|error| {
            RpcError::failure(format!("invalid GitHub pull requests response: {error}"))
        })?;
        let pulls = pulls
            .as_array()
            .ok_or_else(|| RpcError::failure("GitHub pull requests response must be an array"))?;
        pulls
            .iter()
            .map(|pull| {
                let id = pull
                    .get("number")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| RpcError::failure("GitHub pull request has no valid number"))?;
                let name = pull
                    .get("title")
                    .and_then(Value::as_str)
                    .ok_or_else(|| RpcError::failure("GitHub pull request has no title"))?;
                let link = pull
                    .get("html_url")
                    .and_then(Value::as_str)
                    .ok_or_else(|| RpcError::failure("GitHub pull request has no html_url"))?;
                let head_sha = pull
                    .get("head")
                    .and_then(|head| head.get("sha"))
                    .and_then(Value::as_str)
                    .ok_or_else(|| RpcError::failure("GitHub pull request has no head.sha"))?;
                Ok(json!({
                    "id": id,
                    "name": name,
                    "link": link,
                    "head_sha": head_sha,
                }))
            })
            .collect::<Result<Vec<_>, RpcError>>()
            .map(Value::Array)
    }

    async fn download_testing_version(
        &self,
        backend: &Backend,
        pr_id: u64,
        sha: &str,
    ) -> Result<Value, RpcError> {
        let url = github_url(backend, "actions/runs")?;
        let response = github_request(backend.client.get(url).query(&[("head_sha", sha)])).await?;
        let runs = response.json::<Value>().await.map_err(|error| {
            RpcError::failure(format!("invalid GitHub workflow runs response: {error}"))
        })?;
        let workflows = runs
            .get("workflow_runs")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                RpcError::failure("GitHub workflow runs response has no workflow_runs array")
            })?;
        let run_id = workflows
            .iter()
            .find(|workflow| workflow.get("name").and_then(Value::as_str) == Some("Builder"))
            .and_then(|workflow| workflow.get("id"))
            .and_then(Value::as_u64)
            .ok_or_else(|| RpcError::failure("Workflow run not found."))?;

        let path = format!("actions/runs/{run_id}/artifacts");
        let url = github_url(backend, &path)?;
        let response = github_request(backend.client.get(url)).await?;
        let artifacts = response.json::<Value>().await.map_err(|error| {
            RpcError::failure(format!(
                "invalid GitHub workflow artifacts response: {error}"
            ))
        })?;
        let total_count = artifacts
            .get("total_count")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                RpcError::failure("GitHub workflow artifacts response has no total_count")
            })?;
        if total_count == 0 {
            return Err(RpcError::failure(
                "Workflow run has no downloadable artifacts.",
            ));
        }
        let artifact_id = artifacts
            .get("artifacts")
            .and_then(Value::as_array)
            .and_then(|items| items.first())
            .and_then(|artifact| artifact.get("id"))
            .and_then(Value::as_u64)
            .ok_or_else(|| RpcError::failure("GitHub workflow artifact has no valid id"))?;
        let artifact_size = artifacts
            .get("artifacts")
            .and_then(Value::as_array)
            .and_then(|items| items.first())
            .and_then(|artifact| artifact.get("size_in_bytes"))
            .and_then(Value::as_u64);

        // Keep the original anonymous artifact download route rather than GitHub's auth-only ZIP API.
        let repository = backend.config.release_repository.trim_matches('/');
        let archive_url = format!(
            "{}/{repository}/actions/artifacts/{artifact_id}.zip",
            backend.config.testing_download_url.trim_end_matches('/')
        );
        self.download_zip_and_handoff(backend, &archive_url, &format!("PR-{pr_id}"), artifact_size)
            .await?;
        Ok(Value::Null)
    }

    async fn download_and_handoff_release(
        &self,
        backend: &Backend,
        download_url: &str,
        version: &str,
        size: Option<u64>,
        service_template: Option<String>,
    ) -> Result<(), RpcError> {
        let mut temporary = UpdateFiles::new(backend).await?;
        let binary = temporary.reserve("binary").await?;
        download_to_file(backend, download_url, &binary, size).await?;
        set_executable(&binary).await?;
        self.handoff_activation(backend, &mut temporary, &binary, version, service_template)
            .await
    }

    async fn download_zip_and_handoff(
        &self,
        backend: &Backend,
        download_url: &str,
        version: &str,
        size: Option<u64>,
    ) -> Result<(), RpcError> {
        let mut temporary = UpdateFiles::new(backend).await?;
        let archive = temporary.reserve("zip").await?;
        download_to_file(backend, download_url, &archive, size).await?;
        let binary = temporary.reserve("binary").await?;
        extract_linux_executable(archive.clone(), binary.clone()).await?;
        tokio::fs::remove_file(&archive)
            .await
            .map_err(|error| io_failure("removing extracted testing artifact", error))?;
        self.handoff_activation(backend, &mut temporary, &binary, version, None)
            .await
    }

    async fn handoff_activation(
        &self,
        backend: &Backend,
        temporary: &mut UpdateFiles,
        binary: &Path,
        version: &str,
        service_template: Option<String>,
    ) -> Result<(), RpcError> {
        let service_file = if let Some(template) = service_template {
            let path = temporary.reserve("service").await?;
            tokio::fs::write(&path, template)
                .await
                .map_err(|error| io_failure("writing staged service template", error))?;
            Some(path)
        } else {
            None
        };
        queue_activation(backend, binary, version, service_file.as_deref()).await
    }
}

impl Default for Updater {
    fn default() -> Self {
        Self::new()
    }
}

async fn wait_or_stop(duration: Duration, stopped: &mut oneshot::Receiver<()>) -> bool {
    tokio::select! {
        _ = tokio::time::sleep(duration) => true,
        _ = stopped => false,
    }
}

fn filter_releases(releases: Vec<Value>, branch: i64) -> Result<Vec<Value>, RpcError> {
    if branch != 0 && branch != 1 {
        return Err(RpcError::invalid("no valid release branch found"));
    }
    Ok(releases
        .into_iter()
        .filter(|release| {
            let Some(tag) = release.get("tag_name").and_then(Value::as_str) else {
                return false;
            };
            if !tag.starts_with('v') {
                return false;
            }
            branch == 1
                || (!release
                    .get("prerelease")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                    && !tag.contains("-pre"))
        })
        .collect())
}

fn github_url(backend: &Backend, endpoint: &str) -> Result<Url, RpcError> {
    let repository = backend.config.release_repository.trim_matches('/');
    let mut components = repository.split('/');
    let owner = components.next().unwrap_or_default();
    let repository_name = components.next().unwrap_or_default();
    if owner.is_empty() || repository_name.is_empty() || components.next().is_some() {
        return Err(RpcError::failure(
            "release_repository must be OWNER/REPOSITORY",
        ));
    }
    let base = backend.config.github_api.trim_end_matches('/');
    Url::parse(&format!(
        "{base}/repos/{owner}/{repository_name}/{}",
        endpoint.trim_start_matches('/')
    ))
    .map_err(|error| RpcError::failure(format!("invalid GitHub API URL: {error}")))
}

async fn github_request(request: reqwest::RequestBuilder) -> Result<Response, RpcError> {
    let response = request
        .header("X-GitHub-Api-Version", GITHUB_API_VERSION)
        .send()
        .await
        .map_err(|error| RpcError::failure(format!("GitHub API request failed: {error}")))?;
    checked_response(response, "GitHub API request").await
}

async fn checked_response(response: Response, context: &str) -> Result<Response, RpcError> {
    if response.status().is_success() {
        Ok(response)
    } else {
        Err(RpcError::failure(format!(
            "{context} returned HTTP {}",
            response.status()
        )))
    }
}

struct UpdateFiles {
    directory: PathBuf,
    paths: Vec<PathBuf>,
}

impl UpdateFiles {
    async fn new(backend: &Backend) -> Result<Self, RpcError> {
        let directory = backend.homebrew.join(".updates");
        tokio::fs::create_dir_all(&directory)
            .await
            .map_err(|error| io_failure("creating updater staging directory", error))?;
        Ok(Self {
            directory,
            paths: Vec::new(),
        })
    }

    async fn reserve(&mut self, suffix: &str) -> Result<PathBuf, RpcError> {
        for _ in 0..4 {
            let path = self
                .directory
                .join(format!("decky-update-{}.{}", Uuid::new_v4(), suffix));
            match tokio::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)
                .await
            {
                Ok(file) => {
                    drop(file);
                    self.paths.push(path.clone());
                    return Ok(path);
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(io_failure("creating updater staging file", error)),
            }
        }
        Err(RpcError::failure(
            "could not reserve a unique updater staging file",
        ))
    }
}

impl Drop for UpdateFiles {
    fn drop(&mut self) {
        for path in &self.paths {
            let _ = fs::remove_file(path);
        }
    }
}

async fn download_to_file(
    backend: &Backend,
    download_url: &str,
    path: &Path,
    expected_size: Option<u64>,
) -> Result<(), RpcError> {
    let url = Url::parse(download_url)
        .map_err(|error| RpcError::failure(format!("invalid update download URL: {error}")))?;
    let response =
        backend.client.get(url).send().await.map_err(|error| {
            RpcError::failure(format!("update download request failed: {error}"))
        })?;
    let response = checked_response(response, "update download").await?;
    let total = response
        .content_length()
        .or(expected_size)
        .filter(|total| *total > 0)
        .unwrap_or(FALLBACK_DOWNLOAD_SIZE);
    let mut output = tokio::fs::OpenOptions::new()
        .write(true)
        .truncate(true)
        .open(path)
        .await
        .map_err(|error| io_failure("opening updater staging file", error))?;
    let mut stream = response.bytes_stream();
    let mut received = 0u64;
    let mut last_progress = -1i64;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| {
            RpcError::failure(format!("reading update download failed: {error}"))
        })?;
        output
            .write_all(&chunk)
            .await
            .map_err(|error| io_failure("writing updater staging file", error))?;
        received = received.saturating_add(chunk.len() as u64);
        let percentage = (((received as f64 / total as f64) * 100.0).round() as i64).clamp(0, 99);
        if percentage != last_progress {
            backend.emit(
                "updater/update_download_percentage",
                vec![json!(percentage)],
            );
            last_progress = percentage;
        }
    }
    output
        .flush()
        .await
        .map_err(|error| io_failure("flushing updater staging file", error))?;
    output
        .sync_all()
        .await
        .map_err(|error| io_failure("syncing updater staging file", error))?;
    if received == 0 {
        return Err(RpcError::failure("update download was empty"));
    }
    backend.emit("updater/update_download_percentage", vec![json!(100)]);
    Ok(())
}

async fn set_executable(path: &Path) -> Result<(), RpcError> {
    tokio::fs::set_permissions(path, fs::Permissions::from_mode(0o755))
        .await
        .map_err(|error| io_failure("setting downloaded executable permissions", error))
}

async fn extract_linux_executable(archive: PathBuf, binary: PathBuf) -> Result<(), RpcError> {
    tokio::task::spawn_blocking(move || -> Result<(), String> {
        let file = File::open(&archive).map_err(|error| error.to_string())?;
        let mut zip = ZipArchive::new(file).map_err(|error| error.to_string())?;
        let mut executable = zip
            .by_name(LINUX_EXECUTABLE)
            .map_err(|error| format!("testing artifact has no {LINUX_EXECUTABLE}: {error}"))?;
        if !executable.is_file() {
            return Err(format!(
                "testing artifact member {LINUX_EXECUTABLE} is not a file"
            ));
        }
        let mut output = OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(&binary)
            .map_err(|error| error.to_string())?;
        io::copy(&mut executable, &mut output).map_err(|error| error.to_string())?;
        output.flush().map_err(|error| error.to_string())?;
        output.sync_all().map_err(|error| error.to_string())?;
        if fs::metadata(&binary)
            .map_err(|error| error.to_string())?
            .len()
            == 0
        {
            return Err(format!(
                "testing artifact member {LINUX_EXECUTABLE} is empty"
            ));
        }
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o755))
            .map_err(|error| error.to_string())?;
        Ok(())
    })
    .await
    .map_err(|error| RpcError::failure(format!("extracting testing artifact failed: {error}")))?
    .map_err(|error| RpcError::failure(format!("extracting testing artifact failed: {error}")))
}

async fn queue_activation(
    backend: &Backend,
    binary: &Path,
    version: &str,
    service_file: Option<&Path>,
) -> Result<(), RpcError> {
    let installer = backend.config.installer.as_deref().ok_or_else(|| {
        RpcError::failure("installer is not configured; refusing unsafe binary replacement")
    })?;
    let mut command = Command::new("python3");
    command
        .arg(installer)
        .arg("queue-activate-binary")
        .arg("--binary")
        .arg(binary)
        .arg("--version")
        .arg(version)
        .arg("--homebrew")
        .arg(&backend.homebrew)
        .arg("--service-name")
        .arg(&backend.config.service_name)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(service_file) = service_file {
        command.arg("--service-file").arg(service_file);
    }
    let output = command.output().await.map_err(|error| {
        RpcError::failure(format!(
            "starting installer queue-activate-binary failed: {error}"
        ))
    })?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let details = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(RpcError::failure(format!(
            "installer queue-activate-binary failed ({}): {}",
            output.status,
            if details.is_empty() {
                "no diagnostic output"
            } else {
                details
            }
        )));
    }
    let accepted: Value = serde_json::from_slice(&output.stdout).map_err(|error| {
        RpcError::failure(format!(
            "installer returned invalid handoff acknowledgement: {error}"
        ))
    })?;
    if accepted.get("accepted").and_then(Value::as_bool) != Some(true) {
        return Err(RpcError::failure(
            "installer did not confirm durable update handoff",
        ));
    }
    let request_id = accepted
        .get("request_id")
        .and_then(Value::as_str)
        .filter(|id| id.len() == 32 && id.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .ok_or_else(|| {
            RpcError::failure("installer returned an invalid update handoff identifier")
        })?;

    backend.emit("updater/finish_download", Vec::new());
    let released = Command::new("python3")
        .arg(installer)
        .arg("release-queued")
        .arg("--homebrew")
        .arg(&backend.homebrew)
        .arg("--service-name")
        .arg(&backend.config.service_name)
        .arg("--request-id")
        .arg(request_id)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|error| {
            RpcError::failure(format!("starting installer release-queued failed: {error}"))
        })?;
    if released.status.success() {
        Ok(())
    } else {
        Err(process_failure("installer release-queued", &released))
    }
}

async fn restart_service(backend: &Backend) -> Result<(), RpcError> {
    let reload = Command::new("systemctl")
        .arg("daemon-reload")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|error| {
            RpcError::failure(format!("starting systemctl daemon-reload failed: {error}"))
        })?;
    if !reload.status.success() {
        return Err(process_failure("systemctl daemon-reload", &reload));
    }
    let output = Command::new("systemctl")
        .arg("restart")
        .arg(&backend.config.service_name)
        .arg("--no-block")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|error| {
            RpcError::failure(format!("starting systemctl restart failed: {error}"))
        })?;
    if output.status.success() {
        Ok(())
    } else {
        Err(process_failure("systemctl restart", &output))
    }
}

async fn stop_service(backend: &Backend) -> Result<(), RpcError> {
    let output = Command::new("systemctl")
        .arg("--no-block")
        .arg("stop")
        .arg(&backend.config.service_name)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|error| {
            RpcError::failure(format!(
                "starting nonblocking systemctl stop failed: {error}"
            ))
        })?;
    if output.status.success() {
        Ok(())
    } else {
        Err(process_failure("systemctl stop --no-block", &output))
    }
}

fn process_failure(operation: &str, output: &std::process::Output) -> RpcError {
    let stderr = String::from_utf8_lossy(&output.stderr);
    RpcError::failure(format!(
        "{operation} failed ({}): {}",
        output.status,
        if stderr.trim().is_empty() {
            "no diagnostic output"
        } else {
            stderr.trim()
        }
    ))
}

fn io_failure(operation: &str, error: io::Error) -> RpcError {
    RpcError::failure(format!("{operation} failed: {error}"))
}

#[cfg(test)]
mod tests {
    use super::filter_releases;
    use crate::backend::RpcError;
    use serde_json::json;

    #[test]
    fn release_channels_keep_the_original_tag_and_prerelease_filters() {
        let releases = vec![
            json!({"tag_name":"v3.2.10", "prerelease":false}),
            json!({"tag_name":"v3.2.11-pre1", "prerelease":true}),
            json!({"tag_name":"v3.2.9", "prerelease":true}),
            json!({"tag_name":"3.2.12", "prerelease":false}),
            json!({"tag_name":"dev", "prerelease":false}),
        ];

        let stable = filter_releases(releases.clone(), 0).expect("stable branch is valid");
        assert_eq!(stable.len(), 1);
        assert_eq!(stable[0]["tag_name"], "v3.2.10");

        let prerelease = filter_releases(releases, 1).expect("prerelease branch is valid");
        assert_eq!(
            prerelease
                .iter()
                .map(|release| release["tag_name"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["v3.2.10", "v3.2.11-pre1", "v3.2.9"]
        );
    }

    #[test]
    fn testing_branch_is_not_a_release_discovery_branch() {
        let error = filter_releases(vec![], 2).expect_err("testing is not a release branch");
        match error {
            RpcError::Error { message, .. } => {
                assert_eq!(message, "no valid release branch found");
            }
            RpcError::Stopped => panic!("invalid branch must not stop the RPC"),
        }
    }
}
