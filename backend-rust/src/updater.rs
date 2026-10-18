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
use sha2::{Digest, Sha256};
use tokio::{
    io::AsyncWriteExt,
    process::Command,
    sync::{RwLock, oneshot},
    task::JoinHandle,
};
use uuid::Uuid;
use zip::ZipArchive;

use crate::backend::{Backend, RpcError, exact, string};
use crate::service_manager::systemctl_output;

const INITIAL_CHECK_DELAY: Duration = Duration::from_secs(30);
const UPDATE_CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);
const FALLBACK_DOWNLOAD_SIZE: u64 = 25 * 1024 * 1024;
const RUST_BUNDLE_ASSET: &str = "decky-loader-runtime-linux-x86_64.zip";
const RUST_CHECKSUM_ASSET: &str = "decky-loader-runtime-linux-x86_64.zip.sha256";
const MAX_CHECKSUM_BYTES: usize = 1024;
const RUST_TESTING_ARTIFACT: &str = "decky-loader-runtime-linux-x86_64";
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
        let release = backend.version.as_str() != "unknown" && backend.version.as_str() != "dev";
        json!({
            "current": backend.version.as_str(),
            "sdk_version": backend.sdk_version.as_str(),
            "remote": versions.remote,
            "all": versions.all,
            "updatable": release,
            // The frontend shows an "unsupported installation" warning unless this is true.
            "supported": true,
            "repo": backend.config.release_repository.trim_matches('/'),
            // Releases are published from main; development builds are marked like upstream's.
            "gitBranch": if release { "main" } else { "dirty" },
        })
    }

    async fn selected_branch(&self, backend: &Backend) -> Result<i64, RpcError> {
        let branch = backend.setting("branch", json!(-1)).await;
        let (selected, is_default) = resolve_branch(&branch, backend.version.as_str())?;
        if is_default {
            backend.set_setting("branch", json!(selected)).await?;
        }
        Ok(selected)
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
        let checksum_url = rust_checksum_asset(&remote)
            .and_then(|asset| asset.get("browser_download_url"))
            .and_then(Value::as_str)
            .ok_or_else(|| {
                RpcError::failure(format!(
                    "release has no {RUST_CHECKSUM_ASSET} checksum asset; refusing to install an unverified bundle"
                ))
            })?;
        let expected =
            parse_checksum_sidecar(&download_checksum_text(backend, checksum_url).await?)?;
        self.download_bundle_and_handoff(backend, download_url, version, size, Some(&expected))
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
            .find(|workflow| {
                workflow.get("name").and_then(Value::as_str) == Some("Runtime Builder")
            })
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
        // nightly.link artifacts have no checksum sidecar.
        self.download_bundle_and_handoff(
            backend,
            &archive_url,
            &format!("PR-{pr_id}"),
            artifact_size,
            None,
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
        expected_sha256: Option<&str>,
    ) -> Result<(), RpcError> {
        let mut temporary = UpdateFiles::new(backend).await?;
        let archive = temporary.reserve("zip").await?;
        download_to_file(backend, download_url, &archive, size).await?;
        if let Some(expected) = expected_sha256 {
            verify_bundle_digest(expected, &file_sha256(archive.clone()).await?)?;
        }
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
pub(crate) fn is_preview_version(version: &str) -> bool {
    version.starts_with('v') && (version.contains("-rc.") || version.contains("-pre"))
}

pub(crate) fn default_branch(version: &str) -> i64 {
    if is_preview_version(version) { 1 } else { 0 }
}

fn resolve_branch(setting: &Value, current_version: &str) -> Result<(i64, bool), RpcError> {
    if setting.as_i64() == Some(-1) {
        Ok((default_branch(current_version), true))
    } else {
        setting
            .as_i64()
            .map(|branch| (branch, false))
            .ok_or_else(|| RpcError::invalid("update branch setting must be an integer"))
    }
}

async fn wait_or_stop(duration: Duration, stopped: &mut oneshot::Receiver<()>) -> bool {
    tokio::select! {
        _ = tokio::time::sleep(duration) => true,
        _ = stopped => false,
    }
}

fn release_asset<'a>(release: &'a Value, name: &str) -> Option<&'a Value> {
    release.get("assets")?.as_array()?.iter().find(|asset| {
        asset.get("name").and_then(Value::as_str) == Some(name)
            && asset
                .get("browser_download_url")
                .and_then(Value::as_str)
                .is_some_and(|url| !url.is_empty())
    })
}

fn rust_bundle_asset(release: &Value) -> Option<&Value> {
    release_asset(release, RUST_BUNDLE_ASSET)
}

fn rust_checksum_asset(release: &Value) -> Option<&Value> {
    release_asset(release, RUST_CHECKSUM_ASSET)
}

/// Accepts only the `sha256sum` line for the Rust bundle: 64 lowercase hex digits, two spaces,
/// the exact asset name, and at most one final newline.
fn parse_checksum_sidecar(text: &str) -> Result<String, RpcError> {
    let line = text.strip_suffix('\n').unwrap_or(text);
    let malformed = || {
        RpcError::failure(format!(
            "{RUST_CHECKSUM_ASSET} is malformed; expected one line: <64 lowercase hex digits>  {RUST_BUNDLE_ASSET}"
        ))
    };
    let (digest, name) = line.split_once("  ").ok_or_else(malformed)?;
    let is_digest = digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    if !is_digest || name != RUST_BUNDLE_ASSET {
        return Err(malformed());
    }
    Ok(digest.to_owned())
}

fn verify_bundle_digest(expected: &str, actual: &str) -> Result<(), RpcError> {
    if expected == actual {
        Ok(())
    } else {
        Err(RpcError::failure(format!(
            "downloaded Rust bundle does not match {RUST_CHECKSUM_ASSET}: expected SHA-256 {expected}, received {actual}"
        )))
    }
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
                    && !tag.contains("-pre")
                    && !tag.contains("-rc"))
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

fn secure_staging_directory(directory: &Path, effective_uid: u32) -> Result<(), RpcError> {
    let metadata = fs::symlink_metadata(directory)
        .map_err(|error| io_failure("inspecting updater staging directory", error))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() || metadata.uid() != effective_uid {
        return Err(RpcError::failure(format!(
            "updater staging directory must be a real directory owned by effective UID {effective_uid}"
        )));
    }
    fs::set_permissions(directory, fs::Permissions::from_mode(0o700))
        .map_err(|error| io_failure("securing updater staging directory", error))
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
        secure_staging_directory(&directory, crate::platform::effective_user_ids().0)?;
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

async fn download_checksum_text(backend: &Backend, download_url: &str) -> Result<String, RpcError> {
    let url = Url::parse(download_url)
        .map_err(|error| RpcError::failure(format!("invalid checksum download URL: {error}")))?;
    let response =
        backend.client.get(url).send().await.map_err(|error| {
            RpcError::failure(format!("checksum download request failed: {error}"))
        })?;
    let response = checked_response(response, "checksum download").await?;
    let mut stream = response.bytes_stream();
    let mut body = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| {
            RpcError::failure(format!("reading checksum download failed: {error}"))
        })?;
        if body.len() + chunk.len() > MAX_CHECKSUM_BYTES {
            return Err(RpcError::failure(format!(
                "{RUST_CHECKSUM_ASSET} is larger than {MAX_CHECKSUM_BYTES} bytes"
            )));
        }
        body.extend_from_slice(&chunk);
    }
    String::from_utf8(body)
        .map_err(|_| RpcError::failure(format!("{RUST_CHECKSUM_ASSET} is not valid UTF-8")))
}

async fn file_sha256(path: PathBuf) -> Result<String, RpcError> {
    tokio::task::spawn_blocking(move || -> io::Result<String> {
        let mut file = File::open(path)?;
        let mut hasher = Sha256::new();
        io::copy(&mut file, &mut hasher)?;
        Ok(format!("{:x}", hasher.finalize()))
    })
    .await
    .map_err(|error| RpcError::failure(format!("hashing downloaded bundle failed: {error}")))?
    .map_err(|error| io_failure("hashing downloaded bundle", error))
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
    let reload = systemctl_output(&["daemon-reload"])
        .await
        .map_err(|error| {
            RpcError::failure(format!("starting systemctl daemon-reload failed: {error}"))
        })?;
    if !reload.status.success() {
        return Err(process_failure("systemctl daemon-reload", &reload));
    }
    let output = systemctl_output(&["restart", &backend.config.service_name, "--no-block"])
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
    let output = systemctl_output(&["--no-block", "stop", &backend.config.service_name])
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
        RUST_BUNDLE_ASSET, RUST_CHECKSUM_ASSET, RUST_TESTING_ARTIFACT, default_branch,
        filter_releases, parse_checksum_sidecar, resolve_branch, rust_bundle_asset,
        rust_checksum_asset, rust_testing_artifact, secure_staging_directory, verify_bundle_digest,
    };
    use crate::backend::RpcError;
    use serde_json::{Value, json};
    use std::{
        fs,
        os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    };

    fn rust_asset() -> Value {
        json!({
            "name": RUST_BUNDLE_ASSET,
            "size": 512,
            "browser_download_url": "https://github.com/beallio/decky-loader/releases/download/v1.0.0/decky-loader-runtime-linux-x86_64.zip"
        })
    }

    const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn checksum_asset() -> Value {
        json!({
            "name": RUST_CHECKSUM_ASSET,
            "browser_download_url": "https://github.com/beallio/decky-loader/releases/download/v1.0.0/decky-loader-runtime-linux-x86_64.zip.sha256"
        })
    }

    #[test]
    fn checksum_asset_is_required_and_needs_a_download_url() {
        let complete = json!({"assets":[rust_asset(), checksum_asset()]});
        assert!(rust_checksum_asset(&complete).is_some());

        let missing = json!({"assets":[rust_asset()]});
        assert!(rust_checksum_asset(&missing).is_none());

        let no_url = json!({"assets":[rust_asset(), {"name":RUST_CHECKSUM_ASSET, "browser_download_url":""}]});
        assert!(rust_checksum_asset(&no_url).is_none());
    }

    #[test]
    fn checksum_sidecar_accepts_only_the_exact_sha256sum_line() {
        let line = format!("{DIGEST}  {RUST_BUNDLE_ASSET}");
        assert_eq!(parse_checksum_sidecar(&line).unwrap(), DIGEST);
        assert_eq!(
            parse_checksum_sidecar(&format!("{line}\n")).unwrap(),
            DIGEST
        );

        let upper = format!("{}  {RUST_BUNDLE_ASSET}", DIGEST.to_uppercase());
        let short = format!("{}  {RUST_BUNDLE_ASSET}", &DIGEST[1..]);
        let long = format!("0{DIGEST}  {RUST_BUNDLE_ASSET}");
        let not_hex = format!("{}g  {RUST_BUNDLE_ASSET}", &DIGEST[1..]);
        for malformed in [
            String::new(),
            "\n".to_owned(),
            DIGEST.to_owned(),
            format!("{DIGEST} {RUST_BUNDLE_ASSET}"),
            format!("{DIGEST} *{RUST_BUNDLE_ASSET}"),
            format!("{DIGEST}  other.zip"),
            format!("{DIGEST}  {RUST_BUNDLE_ASSET}\n\n"),
            format!("{line}\n{line}\n"),
            format!(" {line}"),
            format!("{line} "),
            format!("{line}\r\n"),
            upper,
            short,
            long,
            not_hex,
        ] {
            let error = parse_checksum_sidecar(&malformed).unwrap_err();
            assert!(
                error.message().contains("malformed"),
                "accepted {malformed:?}"
            );
        }
    }

    #[test]
    fn bundle_digest_must_match_the_sidecar() {
        verify_bundle_digest(DIGEST, DIGEST).unwrap();
        let other = DIGEST.replace('0', "f");
        let error = verify_bundle_digest(DIGEST, &other).unwrap_err();
        assert!(error.message().contains("does not match"));
    }

    #[test]
    fn release_discovery_only_exposes_versions_with_the_rust_bundle() {
        let releases = vec![
            json!({"tag_name":"v3.2.10-rust.1", "prerelease":false, "assets":[rust_asset()]}),
            json!({"tag_name":"v3.2.11-pre1", "prerelease":true, "assets":[rust_asset()]}),
            json!({"tag_name":"v3.2.11-rust.2-rc.1", "prerelease":false, "assets":[rust_asset()]}),
            json!({"tag_name":"v3.2.9", "prerelease":true, "assets":[rust_asset()]}),
            json!({"tag_name":"v3.2.12", "prerelease":false, "assets":[{"name":"PluginLoader", "browser_download_url":"https://example.invalid/PluginLoader"}]}),
            json!({"tag_name":"v3.2.13", "prerelease":false, "assets":[{"name":RUST_BUNDLE_ASSET, "browser_download_url":""}]}),
        ];

        let stable = filter_releases(releases.clone(), 0).expect("stable branch is valid");
        assert_eq!(
            stable
                .iter()
                .map(|release| release["tag_name"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["v3.2.10-rust.1"]
        );
        assert!(rust_bundle_asset(&stable[0]).is_some());

        let prerelease = filter_releases(releases, 1).expect("prerelease branch is valid");
        assert_eq!(
            prerelease
                .iter()
                .map(|release| release["tag_name"].as_str().unwrap())
                .collect::<Vec<_>>(),
            [
                "v3.2.10-rust.1",
                "v3.2.11-pre1",
                "v3.2.11-rust.2-rc.1",
                "v3.2.9"
            ]
        );
    }

    #[test]
    fn default_branch_detects_rc_versions_without_overriding_saved_choices() {
        let preview = "v1.2.3-rust.4-rc.1";
        let stable = "v1.2.3-rust.4";
        assert_eq!(default_branch(preview), 1);
        assert_eq!(default_branch(stable), 0);
        assert_eq!(default_branch("v1.2.3-pre1"), 1);
        assert_eq!(resolve_branch(&json!(-1), preview).unwrap(), (1, true));
        assert_eq!(resolve_branch(&json!(-1), stable).unwrap(), (0, true));
        assert_eq!(resolve_branch(&json!(0), preview).unwrap(), (0, false));
        assert_eq!(resolve_branch(&json!(1), stable).unwrap(), (1, false));
    }

    #[test]
    fn updater_staging_requires_effective_ownership_and_removes_write_access() {
        let directory =
            std::env::temp_dir().join(format!("decky-updater-test-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o777)).unwrap();

        let uid = crate::platform::effective_user_ids().0;
        secure_staging_directory(&directory, uid).unwrap();
        let metadata = fs::metadata(&directory).unwrap();
        assert_eq!(metadata.uid(), uid);
        assert_eq!(metadata.mode() & 0o777, 0o700);
        assert!(secure_staging_directory(&directory, uid.wrapping_add(1)).is_err());

        let target = directory.join("target");
        let link = directory.join("link");
        fs::create_dir(&target).unwrap();
        symlink(&target, &link).unwrap();
        assert!(secure_staging_directory(&link, uid).is_err());
        fs::remove_dir_all(directory).unwrap();
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
