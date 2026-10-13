use std::{
    collections::{HashMap, HashSet},
    path::{Component, Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{
        DefaultBodyLimit, FromRequest, Path as AxumPath, Query, State,
        ws::{Message, WebSocket, WebSocketUpgrade},
    },
    http::{
        HeaderMap, HeaderName, HeaderValue, Method, Request, StatusCode,
        header::{CACHE_CONTROL, CONTENT_LENGTH, CONTENT_TYPE, USER_AGENT},
    },
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::get,
};
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::{
    io::AsyncReadExt,
    net::TcpListener,
    sync::{Mutex, broadcast, mpsc, oneshot},
};

use crate::backend::{Backend, RpcError};

type PendingCalls = HashMap<i64, PendingCall>;

struct PendingCall {
    identity: Arc<()>,
    reply: Option<Arc<Value>>,
}

enum CallRegistration {
    New(Arc<()>),
    Existing(Option<Arc<Value>>),
}

fn register_call(pending: &mut PendingCalls, call_id: i64) -> CallRegistration {
    if let Some(existing) = pending.get(&call_id) {
        return CallRegistration::Existing(existing.reply.clone());
    }

    let identity = Arc::new(());
    pending.insert(
        call_id,
        PendingCall {
            identity: identity.clone(),
            reply: None,
        },
    );
    CallRegistration::New(identity)
}

fn prune_pending_calls(pending: &mut PendingCalls, live_ids: &HashSet<i64>) {
    pending.retain(|call_id, _| live_ids.contains(call_id));
}

#[derive(Clone)]
struct AppState {
    token: Arc<str>,
    assets: Arc<PathBuf>,
    locales: Arc<PathBuf>,
    backend: Arc<Backend>,
    user_agent: HeaderValue,
    pending: Arc<Mutex<PendingCalls>>,
    sockets: Arc<Mutex<SocketRegistry>>,
    socket_lifecycle: Arc<Mutex<()>>,
    next_connection_id: Arc<AtomicU64>,
    http_client: Client,
}

#[derive(Default)]
struct SocketRegistry {
    active: Option<ActiveSocket>,
}

struct ActiveSocket {
    id: u64,
    sender: mpsc::UnboundedSender<Message>,
}

#[derive(Deserialize)]
struct WsQuery {
    auth: Option<String>,
}

#[derive(Deserialize)]
struct FetchQuery {
    auth: Option<String>,
    fetch_url: Option<String>,
}

pub async fn serve(
    address: std::net::SocketAddr,
    auth: String,
    assets: PathBuf,
    locales: PathBuf,
    backend: Arc<Backend>,
    events: broadcast::Sender<Value>,
    shutdown: oneshot::Receiver<()>,
) -> anyhow::Result<()> {
    let listener = TcpListener::bind(address).await?;
    let http_client = Client::builder().build()?;
    let token: Arc<str> = Arc::from(auth);
    let user_agent = HeaderValue::from_str(&format!(
        "Decky/{} (https://decky.xyz)",
        backend.loader_version()
    ))?;
    let state = AppState {
        token: token.clone(),
        assets: Arc::new(assets),
        locales: Arc::new(locales),
        backend,
        user_agent,
        pending: Arc::new(Mutex::new(HashMap::new())),
        sockets: Arc::new(Mutex::new(SocketRegistry::default())),
        socket_lifecycle: Arc::new(Mutex::new(())),
        next_connection_id: Arc::new(AtomicU64::new(1)),
        http_client,
    };

    let mut event_receiver = events.subscribe();
    let (event_stop_sender, mut event_stop_receiver) = oneshot::channel::<()>();
    let event_sockets = state.sockets.clone();
    let event_task = tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut event_stop_receiver => break,
                event = event_receiver.recv() => match event {
                    Ok(event) => send_to_active_socket(&event_sockets, &event).await,
                    Err(broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(broadcast::error::RecvError::Closed) => break,
                }
            }
        }
    });

    let cors = tower_http::cors::CorsLayer::new()
        .allow_origin(HeaderValue::from_static("https://steamloopback.host"))
        .allow_credentials(true)
        .allow_headers(tower_http::cors::AllowHeaders::mirror_request())
        .allow_methods([
            Method::GET,
            Method::POST,
            Method::PUT,
            Method::DELETE,
            Method::PATCH,
            Method::HEAD,
            Method::OPTIONS,
        ]);

    let app = Router::new()
        .route("/auth/token", get(auth_token))
        .route("/ws", get(websocket))
        .route("/frontend/{*path}", get(frontend_asset))
        .route("/static/{*path}", get(static_asset))
        .route("/locales/{*path}", get(locale_asset))
        .route(
            "/plugins/{plugin_name}/frontend_bundle",
            get(frontend_bundle),
        )
        .route(
            "/plugins/{plugin_name}/dist/{*path}",
            get(plugin_dist_asset),
        )
        .route("/plugins/{plugin_name}/assets/{*path}", get(plugin_asset))
        .route(
            "/plugins/{plugin_name}/data/{*path}",
            get(plugin_data_asset),
        )
        .route(
            "/fetch",
            get(proxy_fetch)
                .post(proxy_fetch)
                .put(proxy_fetch)
                .delete(proxy_fetch)
                .patch(proxy_fetch)
                .head(proxy_fetch)
                // aiohttp's existing 1 MiB upload limit rejects the boundary itself.
                .layer(DefaultBodyLimit::max(1024 * 1024 - 1)),
        )
        .layer(cors)
        .layer(middleware::from_fn(expose_response_headers))
        .layer(middleware::from_fn_with_state(token, csrf_middleware))
        .with_state(state.clone());

    let shutdown_sockets = state.sockets.clone();
    let server = axum::serve(listener, app).with_graceful_shutdown(async move {
        let _ = shutdown.await;
        close_active_socket(&shutdown_sockets).await;
    });
    let result = server.await;
    drop(events);
    let _ = event_stop_sender.send(());
    let _ = event_task.await;
    result?;
    Ok(())
}

async fn expose_response_headers(request: Request<Body>, next: Next) -> Response {
    let expose = request
        .headers()
        .get("Origin")
        .is_some_and(|value| value.as_bytes() == b"https://steamloopback.host");
    let mut response = next.run(request).await;
    if expose {
        let mut names = String::with_capacity(response.headers().len() * 32);
        for name in response.headers().keys() {
            if !names.is_empty() {
                names.push_str(", ");
            }
            names.push_str(name.as_str());
        }
        if let Ok(names) = HeaderValue::try_from(names) {
            response
                .headers_mut()
                .insert(axum::http::header::ACCESS_CONTROL_EXPOSE_HEADERS, names);
        }
    }
    response
}

async fn auth_token(State(state): State<AppState>) -> Response {
    let mut response = Response::new(Body::from(state.token.to_string()));
    response.headers_mut().insert(
        CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    response
}

async fn csrf_middleware(
    State(token): State<Arc<str>>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let path = request.uri().path();
    let authorized = request
        .headers()
        .get("X-Decky-Auth")
        .and_then(|value| value.to_str().ok())
        == Some(token.as_ref());
    let exempt = (path == "/auth/token" && request.uri().query().is_none())
        || path.starts_with("/plugins/load_main/")
        || path.starts_with("/static/")
        || path.starts_with("/steam_resource/")
        || path.starts_with("/frontend/")
        || path == "/fetch"
        || path == "/ws"
        || is_plugin_public_path(path);

    if request.method() == Method::OPTIONS || authorized || exempt {
        next.run(request).await
    } else {
        (StatusCode::FORBIDDEN, "Forbidden").into_response()
    }
}

fn is_plugin_public_path(path: &str) -> bool {
    let Some(rest) = path.strip_prefix("/plugins/") else {
        return false;
    };
    ["/assets/", "/data/", "/dist/"]
        .iter()
        .any(|segment| rest.contains(segment))
}

async fn websocket(
    State(state): State<AppState>,
    Query(query): Query<WsQuery>,
    upgrade: WebSocketUpgrade,
) -> Response {
    if query.auth.as_deref() != Some(state.token.as_ref()) {
        return (StatusCode::FORBIDDEN, "Forbidden").into_response();
    }

    upgrade
        .on_upgrade(move |socket| run_websocket(socket, state))
        .into_response()
}

async fn run_websocket(socket: WebSocket, state: AppState) {
    let (sink, mut stream) = socket.split();
    let (sender, mut receiver) = mpsc::unbounded_channel::<Message>();
    let connection_id = state.next_connection_id.fetch_add(1, Ordering::Relaxed);

    let writer = tokio::spawn(async move {
        let mut sink = sink;
        while let Some(message) = receiver.recv().await {
            let closing = matches!(message, Message::Close(_));
            if sink.send(message).await.is_err() || closing {
                break;
            }
        }
        let _ = sink.close().await;
    });

    {
        let _lifecycle = state.socket_lifecycle.lock().await;
        let mut sockets = state.sockets.lock().await;
        if let Some(older) = sockets.active.take() {
            let _ = older.sender.send(Message::Close(None));
        }
        sockets.active = Some(ActiveSocket {
            id: connection_id,
            sender: sender.clone(),
        });
    }

    while let Some(message) = stream.next().await {
        let Ok(message) = message else {
            break;
        };
        let _lifecycle = state.socket_lifecycle.lock().await;
        if !is_active_socket(&state.sockets, connection_id).await {
            break;
        }
        match message {
            Message::Text(text) => {
                if let Ok(data) = serde_json::from_str::<Value>(text.as_str()) {
                    handle_client_message(&state, data).await;
                }
            }
            Message::Ping(payload) => {
                let _ = sender.send(Message::Pong(payload));
            }
            Message::Close(_) => break,
            Message::Binary(_) | Message::Pong(_) => {}
        }
    }

    {
        let _lifecycle = state.socket_lifecycle.lock().await;
        let mut sockets = state.sockets.lock().await;
        if sockets
            .active
            .as_ref()
            .is_some_and(|active| active.id == connection_id)
        {
            sockets.active = None;
        }
    }
    let _ = sender.send(Message::Close(None));
    drop(sender);
    let _ = writer.await;
}

async fn is_active_socket(sockets: &Arc<Mutex<SocketRegistry>>, connection_id: u64) -> bool {
    sockets
        .lock()
        .await
        .active
        .as_ref()
        .is_some_and(|active| active.id == connection_id)
}

async fn send_to_active_socket(sockets: &Arc<Mutex<SocketRegistry>>, data: &Value) {
    let Ok(text) = serde_json::to_string(data) else {
        return;
    };
    let mut registry = sockets.lock().await;
    let send_failed = registry
        .active
        .as_ref()
        .map(|active| active.sender.send(Message::Text(text.into())).is_err())
        .unwrap_or(false);
    if send_failed {
        registry.active = None;
    }
}

async fn close_active_socket(sockets: &Arc<Mutex<SocketRegistry>>) {
    let mut registry = sockets.lock().await;
    if let Some(active) = registry.active.take() {
        let _ = active.sender.send(Message::Close(None));
    }
}

async fn handle_client_message(state: &AppState, data: Value) {
    match data.get("type").and_then(Value::as_i64) {
        Some(0) => handle_call(state, data).await,
        Some(3) => {
            if let Some(call_id) = data.get("id").and_then(Value::as_i64) {
                state.pending.lock().await.remove(&call_id);
            }
        }
        Some(4) => handle_full_sync(state, data).await,
        _ => {}
    }
}

async fn handle_call(state: &AppState, data: Value) {
    let Value::Object(mut fields) = data else {
        return;
    };
    let Some(call_id) = fields.get("id").and_then(Value::as_i64) else {
        return;
    };
    let Some(Value::String(route)) = fields.remove("route") else {
        return;
    };
    let Some(Value::Array(args)) = fields.remove("args") else {
        return;
    };

    let identity = {
        let mut pending = state.pending.lock().await;
        match register_call(&mut pending, call_id) {
            CallRegistration::Existing(reply) => {
                drop(pending);
                if let Some(reply) = reply {
                    send_to_active_socket(&state.sockets, &reply).await;
                }
                return;
            }
            CallRegistration::New(identity) => identity,
        }
    };

    let backend = state.backend.clone();
    let pending = state.pending.clone();
    let sockets = state.sockets.clone();
    tokio::spawn(async move {
        let message = Arc::new(match backend.dispatch(&route, args).await {
            Ok(result) => {
                let mut reply = json!({ "type": 1, "id": call_id });
                reply["result"] = result;
                reply
            }
            Err(RpcError::Stopped) => json!({ "type": 2, "id": call_id }),
            Err(RpcError::Error {
                name,
                message,
                traceback,
            }) => {
                let message_key = if name == "RouteNotFoundError" {
                    "error"
                } else {
                    "message"
                };
                let mut error = json!({});
                error["name"] = Value::String(name);
                error[message_key] = Value::String(message);
                error["traceback"] = traceback.map(Value::String).unwrap_or(Value::Null);
                let mut reply = json!({ "type": -1, "id": call_id });
                reply["error"] = error;
                reply
            }
        });

        let should_send = {
            let mut pending = pending.lock().await;
            match pending.get_mut(&call_id) {
                Some(current) if Arc::ptr_eq(&current.identity, &identity) => {
                    current.reply = Some(message.clone());
                    true
                }
                _ => false,
            }
        };
        if should_send {
            send_to_active_socket(&sockets, &message).await;
        }
    });
}

async fn handle_full_sync(state: &AppState, data: Value) {
    let Value::Object(mut fields) = data else {
        return;
    };
    let Some(Value::Array(messages)) = fields.remove("messages") else {
        return;
    };

    let mut live_ids = HashSet::with_capacity(messages.len());
    for message in messages {
        if let Some(call_id) = message.get("id").and_then(Value::as_i64) {
            live_ids.insert(call_id);
        }
        handle_call(state, message).await;
    }

    prune_pending_calls(&mut *state.pending.lock().await, &live_ids);
}

async fn frontend_asset(
    State(state): State<AppState>,
    AxumPath(path): AxumPath<String>,
) -> Response {
    serve_file(&state.assets, &path, Some("no-cache"), None).await
}

async fn static_asset(State(state): State<AppState>, AxumPath(path): AxumPath<String>) -> Response {
    serve_file(&state.assets, &path, Some("no-cache"), None).await
}

async fn locale_asset(State(state): State<AppState>, AxumPath(path): AxumPath<String>) -> Response {
    match resolve_file(&state.locales, &path).await {
        Some(file) => read_file_response(&file, Some("no-cache"), Some("application/json")).await,
        None => {
            let mut response = Json(json!({})).into_response();
            response
                .headers_mut()
                .insert(CACHE_CONTROL, HeaderValue::from_static("no-cache"));
            response
        }
    }
}

async fn frontend_bundle(
    State(state): State<AppState>,
    AxumPath(plugin_name): AxumPath<String>,
) -> Response {
    let Some(plugin_root) = state.backend.plugin_directory(&plugin_name).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    match resolve_file(&plugin_root, "dist/index.js").await {
        Some(file) => read_file_response(&file, None, Some("application/javascript")).await,
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn plugin_dist_asset(
    State(state): State<AppState>,
    AxumPath((plugin_name, path)): AxumPath<(String, String)>,
) -> Response {
    let Some(plugin_root) = state.backend.plugin_directory(&plugin_name).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let root = plugin_root.join("dist");
    serve_file(&root, &path, Some("no-cache"), None).await
}

async fn plugin_asset(
    State(state): State<AppState>,
    AxumPath((plugin_name, path)): AxumPath<(String, String)>,
) -> Response {
    let Some(plugin_root) = state.backend.plugin_directory(&plugin_name).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let root = plugin_root.join("dist").join("assets");
    serve_file(&root, &path, Some("no-cache"), None).await
}

async fn plugin_data_asset(
    State(state): State<AppState>,
    AxumPath((plugin_name, path)): AxumPath<(String, String)>,
) -> Response {
    let Some(data_root) = state.backend.plugin_data_directory(&plugin_name).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    serve_file(&data_root, &path, Some("no-cache"), None).await
}

async fn serve_file(
    root: &Path,
    relative: &str,
    cache_control: Option<&'static str>,
    content_type: Option<&'static str>,
) -> Response {
    let Some(file) = resolve_file(root, relative).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    read_file_response(&file, cache_control, content_type).await
}

async fn resolve_file(root: &Path, relative: &str) -> Option<PathBuf> {
    if !is_safe_relative_path(relative) {
        return None;
    }
    let canonical_root = tokio::fs::canonicalize(root).await.ok()?;
    let canonical_file = tokio::fs::canonicalize(canonical_root.join(relative))
        .await
        .ok()?;
    if !canonical_file.starts_with(&canonical_root)
        || !tokio::fs::metadata(&canonical_file).await.ok()?.is_file()
    {
        return None;
    }
    Some(canonical_file)
}

fn is_safe_relative_path(relative: &str) -> bool {
    if relative.is_empty() || relative.contains('\\') {
        return false;
    }
    let path = Path::new(relative);
    !path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

async fn read_file_response(
    file: &Path,
    cache_control: Option<&'static str>,
    content_type: Option<&'static str>,
) -> Response {
    let Ok(file_handle) = tokio::fs::File::open(file).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(metadata) = file_handle.metadata().await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let content_length = metadata.len();
    let file_stream = futures_util::stream::try_unfold(file_handle, |mut file| async move {
        let mut chunk = vec![0; 16 * 1024];
        let bytes_read = file.read(&mut chunk).await?;
        if bytes_read == 0 {
            Ok::<_, std::io::Error>(None)
        } else {
            chunk.truncate(bytes_read);
            Ok(Some((Bytes::from(chunk), file)))
        }
    });
    let mut response = Response::new(Body::from_stream(file_stream));
    if let Ok(content_length) = HeaderValue::from_str(&content_length.to_string()) {
        response
            .headers_mut()
            .insert(CONTENT_LENGTH, content_length);
    }
    if let Some(content_type) = content_type {
        response
            .headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static(content_type));
    } else if let Some(content_type) = mime_guess::from_path(file).first_raw()
        && let Ok(content_type) = HeaderValue::from_str(content_type)
    {
        response.headers_mut().insert(CONTENT_TYPE, content_type);
    }
    if let Some(cache_control) = cache_control {
        response
            .headers_mut()
            .insert(CACHE_CONTROL, HeaderValue::from_static(cache_control));
    }
    response
}

async fn proxy_fetch(
    State(state): State<AppState>,
    Query(query): Query<FetchQuery>,
    request: Request<Body>,
) -> Response {
    if query.auth.as_deref() != Some(state.token.as_ref()) {
        return (StatusCode::FORBIDDEN, "Forbidden").into_response();
    }
    let Some(url) = query.fetch_url.as_deref().map(decode_percent_once) else {
        return (StatusCode::BAD_REQUEST, "Missing fetch_url").into_response();
    };

    let (mut headers, extra_headers_to_add) =
        transform_proxy_headers(request.headers(), &state.user_agent);
    for (name, value) in extra_headers_to_add {
        if let (Ok(name), Ok(value)) = (
            HeaderName::from_bytes(name.as_bytes()),
            HeaderValue::from_bytes(value.as_bytes()),
        ) {
            headers.insert(name, value);
        }
    }

    let method = match reqwest::Method::from_bytes(request.method().as_str().as_bytes()) {
        Ok(method) => method,
        Err(_) => return StatusCode::METHOD_NOT_ALLOWED.into_response(),
    };
    // Reusable bytes preserve aiohttp's server-side redirect behavior, including 307 uploads.
    let body = match Bytes::from_request(request, &state).await {
        Ok(body) => body,
        Err(rejection) => return rejection.into_response(),
    };
    let upstream = match state
        .http_client
        .request(method, url)
        .headers(headers)
        .body(body)
        .send()
        .await
    {
        Ok(response) => response,
        Err(_) => return StatusCode::BAD_GATEWAY.into_response(),
    };

    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut headers = upstream.headers().clone();
    for name in [
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-expose-headers",
    ] {
        headers.remove(name);
    }
    // Transfer-Encoding is hop-by-hop. Hyper applies the correct framing to the streamed body.
    headers.remove("transfer-encoding");
    let body = Body::from_stream(upstream.bytes_stream());
    let mut response = Response::new(body);
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    response
}

fn transform_proxy_headers(
    request_headers: &HeaderMap,
    user_agent: &HeaderValue,
) -> (HeaderMap, Vec<(String, String)>) {
    let mut headers = request_headers.clone();
    headers.insert(USER_AGENT, user_agent.clone());

    for name in [
        "host",
        "origin",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
    ] {
        headers.remove(name);
    }
    if let Some(excluded) = request_headers
        .get("X-Decky-Fetch-Excluded-Headers")
        .and_then(|value| value.to_str().ok())
    {
        for name in excluded.split(", ") {
            headers.remove(name);
        }
    }

    let extra_headers = request_headers
        .iter()
        .filter_map(|(name, value)| {
            let name = name.as_str();
            let suffix = name.strip_prefix("x-decky-header-")?;
            let value = value.to_str().ok()?;
            Some((suffix.to_owned(), value.to_owned()))
        })
        .collect();

    let wrapped_names: Vec<HeaderName> = headers
        .keys()
        .filter(|name| name.as_str().starts_with("x-decky-"))
        .cloned()
        .collect();
    for name in wrapped_names {
        headers.remove(name);
    }

    (headers, extra_headers)
}

fn decode_percent_once(input: &str) -> String {
    fn hex(byte: u8) -> Option<u8> {
        match byte {
            b'0'..=b'9' => Some(byte - b'0'),
            b'a'..=b'f' => Some(byte - b'a' + 10),
            b'A'..=b'F' => Some(byte - b'A' + 10),
            _ => None,
        }
    }

    let bytes = input.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%'
            && index + 2 < bytes.len()
            && let (Some(high), Some(low)) = (hex(bytes[index + 1]), hex(bytes[index + 2]))
        {
            decoded.push((high << 4) | low);
            index += 3;
            continue;
        }
        decoded.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDirectory(PathBuf);

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn replayed_calls_do_not_repeat_persistent_settings_changes() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("decky-rpc-test-{}", uuid::Uuid::new_v4())),
        );
        let plugin_root = directory.0.join("plugins");
        tokio::fs::create_dir_all(&plugin_root).await.unwrap();
        let (events, _) = tokio::sync::broadcast::channel(8);
        let (_shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);
        let user = std::env::var("USER").expect("test account USER");
        let registry = Arc::new(
            crate::workers::PluginRegistry::start(
                std::env::current_exe().unwrap(),
                plugin_root,
                directory.0.clone(),
                user.clone(),
                (unsafe { libc::geteuid() }, unsafe { libc::getegid() }),
                events,
                shutdown_rx,
            )
            .await
            .unwrap(),
        );
        let backend = Arc::new(
            Backend::new(
                registry.clone(),
                directory.0.clone(),
                user,
                directory.0.to_string_lossy().into_owned(),
                "dev".into(),
            )
            .await
            .unwrap(),
        );
        let (sender, mut replies) = mpsc::unbounded_channel();
        let state = AppState {
            token: Arc::from("test-token"),
            assets: Arc::new(directory.0.clone()),
            locales: Arc::new(directory.0.clone()),
            backend,
            user_agent: HeaderValue::from_static("Decky/dev"),
            pending: Arc::new(Mutex::new(PendingCalls::new())),
            sockets: Arc::new(Mutex::new(SocketRegistry {
                active: Some(ActiveSocket { id: 1, sender }),
            })),
            socket_lifecycle: Arc::new(Mutex::new(())),
            next_connection_id: Arc::new(AtomicU64::new(2)),
            http_client: Client::new(),
        };

        handle_client_message(
            &state,
            json!({
                "type": 0, "id": 17, "route": "utilities/settings/set",
                "args": ["replay-proof", 1]
            }),
        )
        .await;
        let first = tokio::time::timeout(std::time::Duration::from_secs(10), replies.recv())
            .await
            .unwrap()
            .unwrap();
        let Message::Text(first) = first else {
            panic!("RPC reply must be text")
        };
        assert_eq!(serde_json::from_str::<Value>(&first).unwrap()["type"], 1);

        handle_client_message(
            &state,
            json!({
                "type": 4, "messages": [{
                    "type": 0, "id": 17, "route": "utilities/settings/set",
                    "args": ["replay-proof", 2]
                }]
            }),
        )
        .await;
        let _ = tokio::time::timeout(std::time::Duration::from_secs(10), replies.recv())
            .await
            .unwrap()
            .unwrap();
        let persisted: Value = serde_json::from_slice(
            &tokio::fs::read(directory.0.join("settings/loader.json"))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(persisted["replay-proof"], 1);

        // A full sync with no outstanding calls removes the old request ID.
        // Reusing that ID must apply a genuinely new settings change.
        handle_client_message(&state, json!({"type": 4, "messages": []})).await;
        handle_client_message(
            &state,
            json!({
                "type": 0, "id": 17, "route": "utilities/settings/set",
                "args": ["replay-proof", 3]
            }),
        )
        .await;
        let _ = tokio::time::timeout(std::time::Duration::from_secs(10), replies.recv())
            .await
            .unwrap()
            .unwrap();
        let persisted: Value = serde_json::from_slice(
            &tokio::fs::read(directory.0.join("settings/loader.json"))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(persisted["replay-proof"], 3);
        registry.shutdown().await;
    }

    #[test]
    fn file_paths_cannot_escape_their_configured_root() {
        assert!(is_safe_relative_path("assets/index.js"));
        assert!(!is_safe_relative_path("../outside"));
        assert!(!is_safe_relative_path("assets/../../outside"));
        assert!(!is_safe_relative_path("/etc/passwd"));
        assert!(!is_safe_relative_path("assets\\..\\outside"));
    }
}
