use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Component, Path, PathBuf},
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
const RUST_BUNDLE_ASSET: &str = "decky-rust-runtime-linux-x86_64.zip";
const RUST_TESTING_ARTIFACT: &str = "decky-rust-runtime-linux-x86_64";
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
            "sdk_version": backend.sdk_version.as_str(),
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
        let asset = rust_bundle_asset(&remote).ok_or_else(|| {
            RpcError::failure(format!(
                "release has no {RUST_BUNDLE_ASSET} Rust bundle asset"
            ))
        })?;
        let download_url = asset
            .get("browser_download_url")
            .and_then(Value::as_str)
            .filter(|url| !url.is_empty())
            .ok_or_else(|| RpcError::failure("Rust bundle asset has no download URL"))?;
        let size = asset.get("size").and_then(Value::as_u64);
        self.download_bundle_and_handoff(backend, download_url, version, size)
            .await?;
        Ok(Value::Null)
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
        let artifact = artifacts
            .get("artifacts")
            .and_then(Value::as_array)
            .and_then(|items| rust_testing_artifact(items))
            .ok_or_else(|| {
                RpcError::failure(format!(
                    "Workflow run has no {RUST_TESTING_ARTIFACT} Rust bundle artifact."
                ))
            })?;
        let artifact_id = artifact
            .get("id")
            .and_then(Value::as_u64)
            .ok_or_else(|| RpcError::failure("Rust testing artifact has no valid id"))?;
        let artifact_size = artifact.get("size_in_bytes").and_then(Value::as_u64);

        // Keep the original anonymous artifact download route rather than GitHub's auth-only ZIP API.
        let repository = backend.config.release_repository.trim_matches('/');
        let archive_url = format!(
            "{}/{repository}/actions/artifacts/{artifact_id}.zip",
            backend.config.testing_download_url.trim_end_matches('/')
        );
        self.download_bundle_and_handoff(
            backend,
            &archive_url,
            &format!("PR-{pr_id}"),
            artifact_size,
        )
        .await?;
        Ok(Value::Null)
    }

    async fn download_bundle_and_handoff(
        &self,
        backend: &Backend,
        download_url: &str,
        version: &str,
        size: Option<u64>,
    ) -> Result<(), RpcError> {
        let mut temporary = UpdateFiles::new(backend).await?;
        let archive = temporary.reserve("zip").await?;
        download_to_file(backend, download_url, &archive, size).await?;
        let bundle = temporary.reserve_directory("bundle").await?;
        extract_bundle_archive(archive.clone(), bundle.clone()).await?;
        tokio::fs::remove_file(&archive)
            .await
            .map_err(|error| io_failure("removing downloaded bundle archive", error))?;
        queue_install_bundle(backend, &bundle, version).await
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

fn rust_bundle_asset(release: &Value) -> Option<&Value> {
    release.get("assets")?.as_array()?.iter().find(|asset| {
        asset.get("name").and_then(Value::as_str) == Some(RUST_BUNDLE_ASSET)
            && asset
                .get("browser_download_url")
                .and_then(Value::as_str)
                .is_some_and(|url| !url.is_empty())
    })
}

fn rust_testing_artifact(artifacts: &[Value]) -> Option<&Value> {
    artifacts.iter().find(|artifact| {
        artifact.get("name").and_then(Value::as_str) == Some(RUST_TESTING_ARTIFACT)
    })
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
            if !tag.starts_with('v') || rust_bundle_asset(release).is_none() {
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
        let metadata = tokio::fs::symlink_metadata(&directory)
            .await
            .map_err(|error| io_failure("inspecting updater staging directory", error))?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() || metadata.uid() != 0 {
            return Err(RpcError::failure(
                "updater staging directory must be a root-owned real directory",
            ));
        }
        tokio::fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
            .await
            .map_err(|error| io_failure("securing updater staging directory", error))?;
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
    async fn reserve_directory(&mut self, suffix: &str) -> Result<PathBuf, RpcError> {
        for _ in 0..4 {
            let path = self
                .directory
                .join(format!("decky-update-{}.{}", Uuid::new_v4(), suffix));
            match tokio::fs::create_dir(&path).await {
                Ok(()) => {
                    if let Err(error) =
                        tokio::fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).await
                    {
                        let _ = tokio::fs::remove_dir(&path).await;
                        return Err(io_failure("securing staged bundle directory", error));
                    }
                    self.paths.push(path.clone());
                    return Ok(path);
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(io_failure("creating updater staging directory", error)),
            }
        }
        Err(RpcError::failure(
            "could not reserve a unique updater staging directory",
        ))
    }
}

impl Drop for UpdateFiles {
    fn drop(&mut self) {
        for path in &self.paths {
            match fs::symlink_metadata(path) {
                Ok(metadata) if metadata.is_dir() => {
                    let _ = fs::remove_dir_all(path);
                }
                Ok(_) => {
                    let _ = fs::remove_file(path);
                }
                Err(_) => {}
            }
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

async fn extract_bundle_archive(archive: PathBuf, bundle: PathBuf) -> Result<(), RpcError> {
    tokio::task::spawn_blocking(move || -> Result<(), String> {
        if !fs::metadata(&bundle).is_ok_and(|metadata| metadata.is_dir()) {
            return Err("bundle staging path is not a directory".into());
        }
        let file = File::open(&archive).map_err(|error| error.to_string())?;
        let mut zip = ZipArchive::new(file).map_err(|error| error.to_string())?;
        let mut members = HashSet::new();
        for index in 0..zip.len() {
            let mut member = zip.by_index(index).map_err(|error| error.to_string())?;
            let name = member.name().map_err(|error| error.to_string())?;
            let relative = member
                .enclosed_name()
                .ok_or_else(|| format!("bundle archive contains an unsafe path: {name}"))?;
            if relative.as_os_str().is_empty()
                || relative
                    .components()
                    .any(|component| !matches!(component, Component::Normal(_)))
            {
                return Err(format!("bundle archive contains an unsafe path: {}", name));
            }
            if !members.insert(relative.clone()) {
                return Err(format!(
                    "bundle archive contains a duplicate member: {}",
                    name
                ));
            }
            let mode = member
                .unix_mode()
                .unwrap_or(if member.is_dir() { 0o755 } else { 0o644 });
            let file_type = mode & 0o170000;
            if mode & 0o7022 != 0 {
                return Err(format!(
                    "bundle archive member has unsafe permissions: {}",
                    name
                ));
            }
            let target = bundle.join(&relative);
            if member.is_dir() {
                if file_type != 0 && file_type != 0o040000 {
                    return Err(format!(
                        "bundle archive directory has an invalid type: {}",
                        name
                    ));
                }
                fs::create_dir_all(&target).map_err(|error| error.to_string())?;
                fs::set_permissions(&target, fs::Permissions::from_mode(0o755))
                    .map_err(|error| error.to_string())?;
            } else {
                if file_type != 0 && file_type != 0o100000 {
                    return Err(format!(
                        "bundle archive member is not a regular file: {}",
                        name
                    ));
                }
                let parent = target
                    .parent()
                    .ok_or_else(|| format!("bundle archive member has no parent: {name}"))?;
                fs::create_dir_all(parent).map_err(|error| error.to_string())?;
                let mut output = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&target)
                    .map_err(|error| error.to_string())?;
                io::copy(&mut member, &mut output).map_err(|error| error.to_string())?;
                output.flush().map_err(|error| error.to_string())?;
                output.sync_all().map_err(|error| error.to_string())?;
                fs::set_permissions(&target, fs::Permissions::from_mode(mode & 0o777))
                    .map_err(|error| error.to_string())?;
            }
        }
        Ok(())
    })
    .await
    .map_err(|error| RpcError::failure(format!("extracting Rust bundle archive failed: {error}")))?
    .map_err(|error| RpcError::failure(format!("extracting Rust bundle archive failed: {error}")))
}

async fn queue_install_bundle(
    backend: &Backend,
    bundle: &Path,
    version: &str,
) -> Result<(), RpcError> {
    let installer = backend.config.installer.as_deref().ok_or_else(|| {
        RpcError::failure("installer is not configured; refusing an unsafe Rust bundle update")
    })?;
    let mut command = Command::new("python3");
    command
        .arg(installer)
        .arg("queue-install-bundle")
        .arg("--bundle")
        .arg(bundle)
        .arg("--version")
        .arg(version)
        .arg("--homebrew")
        .arg(&backend.homebrew)
        .arg("--service-name")
        .arg(&backend.config.service_name)
        .arg("--user")
        .arg(&backend.user)
        .arg("--listen")
        .arg(&backend.config.listen)
        .arg("--cef-url")
        .arg(&backend.config.cef_url)
        .arg("--inject-frontend")
        .arg(if backend.config.inject_frontend {
            "true"
        } else {
            "false"
        })
        .arg("--worker-mode")
        .arg(&backend.config.worker_mode)
        .arg("--github-api")
        .arg(&backend.config.github_api)
        .arg("--release-repository")
        .arg(&backend.config.release_repository)
        .arg("--testing-download-url")
        .arg(&backend.config.testing_download_url)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = command.output().await.map_err(|error| {
        RpcError::failure(format!(
            "starting installer queue-install-bundle failed: {error}"
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
            "installer queue-install-bundle failed ({}): {}",
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
            "installer returned invalid Rust bundle handoff acknowledgement: {error}"
        ))
    })?;
    if accepted.get("accepted").and_then(Value::as_bool) != Some(true) {
        return Err(RpcError::failure(
            "installer did not confirm durable Rust bundle handoff",
        ));
    }
    let request_id = accepted
        .get("request_id")
        .and_then(Value::as_str)
        .filter(|id| id.len() == 32 && id.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .ok_or_else(|| {
            RpcError::failure("installer returned an invalid Rust bundle handoff identifier")
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
    use super::{
        RUST_BUNDLE_ASSET, RUST_TESTING_ARTIFACT, filter_releases, rust_bundle_asset,
        rust_testing_artifact,
    };
    use crate::backend::RpcError;
    use serde_json::{Value, json};

    fn rust_asset() -> Value {
        json!({
            "name": RUST_BUNDLE_ASSET,
            "size": 512,
            "browser_download_url": "https://github.com/beallio/decky-loader/releases/download/v1.0.0/decky-rust-runtime-linux-x86_64.zip"
        })
    }

    #[test]
    fn release_discovery_only_exposes_versions_with_the_rust_bundle() {
        let releases = vec![
            json!({"tag_name":"v3.2.10", "prerelease":false, "assets":[rust_asset()]}),
            json!({"tag_name":"v3.2.11-pre1", "prerelease":true, "assets":[rust_asset()]}),
            json!({"tag_name":"v3.2.9", "prerelease":true, "assets":[rust_asset()]}),
            json!({"tag_name":"v3.2.12", "prerelease":false, "assets":[{"name":"PluginLoader", "browser_download_url":"https://example.invalid/PluginLoader"}]}),
            json!({"tag_name":"v3.2.13", "prerelease":false, "assets":[{"name":RUST_BUNDLE_ASSET, "browser_download_url":""}]}),
            json!({"tag_name":"3.2.14", "prerelease":false, "assets":[rust_asset()]}),
        ];

        let stable = filter_releases(releases.clone(), 0).expect("stable branch is valid");
        assert_eq!(
            stable
                .iter()
                .map(|release| release["tag_name"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["v3.2.10"]
        );
        assert!(rust_bundle_asset(&stable[0]).is_some());

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
    fn testing_download_selects_only_the_rust_bundle_artifact() {
        let artifacts = vec![
            json!({"id":1, "name":"PluginLoader"}),
            json!({"id":2, "name":"decky-loader-linux"}),
            json!({"id":3, "name":RUST_TESTING_ARTIFACT}),
        ];
        assert_eq!(
            rust_testing_artifact(&artifacts).and_then(|artifact| artifact["id"].as_u64()),
            Some(3)
        );
        assert!(rust_testing_artifact(&artifacts[..2]).is_none());
    }

    #[test]
    fn release_discovery_rejects_unsupported_branches() {
        assert!(matches!(
            filter_releases(vec![], 2),
            Err(RpcError::Error { name, .. }) if name == "ValueError"
        ));
    }
}
