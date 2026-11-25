use std::{
    collections::{HashMap, HashSet},
    path::{Component, Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{
        DefaultBodyLimit, FromRequest, Path as AxumPath, Query, State,
        ws::{Message, Utf8Bytes, WebSocket, WebSocketUpgrade},
    },
    http::{
        HeaderMap, HeaderName, HeaderValue, Method, Request, StatusCode,
        header::{CACHE_CONTROL, CONTENT_LENGTH, CONTENT_TYPE, HOST, ORIGIN, USER_AGENT},
    },
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{Value, json, value::RawValue};
use tokio::{
    io::AsyncReadExt,
    net::TcpListener,
    sync::{Mutex, broadcast, mpsc, oneshot},
};

use crate::backend::{Backend, RpcError};

const HTTP_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const CATALOG_TIMEOUT: Duration = Duration::from_secs(20);
const CATALOG_MAX_BYTES: usize = 5_000_000;
const INLINE_FILE_LIMIT: u64 = 2 * 1024 * 1024;
const STREAM_CHUNK_SIZE: usize = 64 * 1024;
const DISCONNECTED_SESSION_TTL: Duration = Duration::from_secs(60);
const MAX_DISCONNECTED_SESSIONS: usize = 16;
const MAX_CLIENT_ID_BYTES: usize = 128;

enum AssetRoot {
    Canonical(PathBuf),
    Raw(PathBuf),
}

impl AssetRoot {
    async fn new(path: PathBuf) -> Self {
        match tokio::fs::canonicalize(&path).await {
            Ok(canonical) => Self::Canonical(canonical),
            Err(_) => Self::Raw(path),
        }
    }

    async fn resolve(&self, relative: &str) -> Option<PathBuf> {
        match self {
            Self::Canonical(root) => resolve_file_in_canonical_root(root, relative).await,
            Self::Raw(root) => resolve_file(root, relative).await,
        }
    }
}

type PendingCalls = HashMap<i64, PendingCall>;

struct PendingCall {
    identity: Arc<()>,
    reply: Option<Arc<str>>,
}

enum CallRegistration {
    New(Arc<()>),
    Existing(Option<Arc<str>>),
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
    assets: Arc<AssetRoot>,
    locales: Arc<AssetRoot>,
    backend: Arc<Backend>,
    user_agent: HeaderValue,
    sessions: Arc<Mutex<SessionRegistry>>,
    next_connection_id: Arc<AtomicU64>,
    http_client: Client,
    catalog_client: Client,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
enum SessionKey {
    Client(String),
    AnonymousDefault,
    Anonymous(u64),
}

struct ClientSession {
    socket: Option<ActiveSocket>,
    pending: PendingCalls,
    disconnected_at: Option<Instant>,
}

impl ClientSession {
    fn new(socket: ActiveSocket) -> Self {
        Self {
            socket: Some(socket),
            pending: PendingCalls::new(),
            disconnected_at: None,
        }
    }
}

#[derive(Default)]
struct SessionRegistry {
    sessions: HashMap<SessionKey, ClientSession>,
}

impl SessionRegistry {
    fn attach(
        &mut self,
        client_id: Option<String>,
        connection_id: u64,
        sender: mpsc::UnboundedSender<Message>,
    ) -> SessionKey {
        self.prune_disconnected(Instant::now());
        let key = match client_id {
            Some(id) => SessionKey::Client(id),
            None => {
                let default_live = self
                    .sessions
                    .get(&SessionKey::AnonymousDefault)
                    .is_some_and(|session| session.socket.is_some());
                if default_live {
                    SessionKey::Anonymous(connection_id)
                } else {
                    SessionKey::AnonymousDefault
                }
            }
        };
        let socket = ActiveSocket {
            id: connection_id,
            sender,
        };
        match self.sessions.get_mut(&key) {
            Some(session) => {
                if let Some(older) = session.socket.replace(socket) {
                    let _ = older.sender.send(Message::Close(None));
                }
                session.disconnected_at = None;
            }
            None => {
                self.sessions
                    .insert(key.clone(), ClientSession::new(socket));
            }
        }
        key
    }

    fn detach(&mut self, key: &SessionKey, connection_id: u64) {
        let now = Instant::now();
        let remove = if let Some(session) = self.sessions.get_mut(key) {
            if session
                .socket
                .as_ref()
                .is_some_and(|active| active.id == connection_id)
            {
                session.socket = None;
                if session.pending.is_empty() {
                    true
                } else {
                    session.disconnected_at = Some(now);
                    false
                }
            } else {
                false
            }
        } else {
            false
        };
        if remove {
            self.sessions.remove(key);
        }
        self.prune_disconnected(now);
    }

    fn is_current_socket(&self, key: &SessionKey, connection_id: u64) -> bool {
        self.sessions
            .get(key)
            .and_then(|session| session.socket.as_ref())
            .is_some_and(|active| active.id == connection_id)
    }

    fn send_to_session(&mut self, key: &SessionKey, text: &str) {
        let now = Instant::now();
        let remove = if let Some(session) = self.sessions.get_mut(key) {
            let failed = session
                .socket
                .as_ref()
                .is_some_and(|active| active.sender.send(Message::Text(text.into())).is_err());
            if failed {
                session.socket = None;
                if session.pending.is_empty() {
                    true
                } else {
                    session.disconnected_at = Some(now);
                    false
                }
            } else {
                false
            }
        } else {
            false
        };
        if remove {
            self.sessions.remove(key);
        }
    }

    fn broadcast_event(&mut self, text: &str) {
        let now = Instant::now();
        let payload = Utf8Bytes::from(text);
        let mut dead = Vec::new();
        for (key, session) in &mut self.sessions {
            let failed = session
                .socket
                .as_ref()
                .is_some_and(|active| active.sender.send(Message::Text(payload.clone())).is_err());
            if failed {
                session.socket = None;
                if session.pending.is_empty() {
                    dead.push(key.clone());
                } else {
                    session.disconnected_at = Some(now);
                }
            }
        }
        for key in dead {
            self.sessions.remove(&key);
        }
    }

    fn close_all(&mut self) {
        for session in self.sessions.values_mut() {
            if let Some(active) = session.socket.take() {
                let _ = active.sender.send(Message::Close(None));
            }
        }
        self.sessions.clear();
    }

    fn prune_disconnected(&mut self, now: Instant) {
        self.sessions.retain(|_, session| {
            if session.socket.is_some() {
                return true;
            }
            if session.pending.is_empty() {
                return false;
            }
            session
                .disconnected_at
                .is_none_or(|at| now.saturating_duration_since(at) < DISCONNECTED_SESSION_TTL)
        });

        let mut disconnected: Vec<(SessionKey, Instant)> = self
            .sessions
            .iter()
            .filter(|(_, session)| session.socket.is_none())
            .map(|(key, session)| (key.clone(), session.disconnected_at.unwrap_or(now)))
            .collect();
        if disconnected.len() > MAX_DISCONNECTED_SESSIONS {
            disconnected.sort_by_key(|(_, at)| *at);
            let excess = disconnected.len() - MAX_DISCONNECTED_SESSIONS;
            for (key, _) in disconnected.into_iter().take(excess) {
                self.sessions.remove(&key);
            }
        }
    }
}

struct ActiveSocket {
    id: u64,
    sender: mpsc::UnboundedSender<Message>,
}

#[derive(Deserialize)]
struct WsQuery {
    auth: Option<String>,
    client_id: Option<String>,
}

fn normalize_client_id(client_id: Option<String>) -> Option<String> {
    client_id.filter(|id| !id.is_empty() && id.len() <= MAX_CLIENT_ID_BYTES)
}

#[derive(Deserialize)]
struct FetchQuery {
    auth: Option<String>,
    fetch_url: Option<String>,
    #[serde(default)]
    catalog: bool,
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
    let http_client = backend.client.clone();
    let token: Arc<str> = Arc::from(auth);
    let user_agent = HeaderValue::from_str(&format!(
        "Decky/{} (https://decky.xyz)",
        backend.loader_version()
    ))?;
    let assets = AssetRoot::new(assets).await;
    let locales = AssetRoot::new(locales).await;
    let state = AppState {
        token: token.clone(),
        assets: Arc::new(assets),
        locales: Arc::new(locales),
        backend,
        user_agent,
        sessions: Arc::new(Mutex::new(SessionRegistry::default())),
        next_connection_id: Arc::new(AtomicU64::new(1)),
        http_client,
        catalog_client: catalog_client(CATALOG_TIMEOUT)?,
    };

    let mut event_receiver = events.subscribe();
    let (event_stop_sender, mut event_stop_receiver) = oneshot::channel::<()>();
    let event_sessions = state.sessions.clone();
    let event_task = tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut event_stop_receiver => break,
                event = event_receiver.recv() => match event {
                    Ok(event) => {
                        if let Ok(text) = serde_json::to_string(&event) {
                            event_sessions.lock().await.broadcast_event(&text);
                        }
                    }
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
        .route(
            "/methods/{method_name}",
            post(legacy_utility).layer(DefaultBodyLimit::max(1024 * 1024 - 1)),
        )
        .layer(cors)
        .layer(middleware::from_fn(expose_response_headers))
        .layer(middleware::from_fn_with_state(token, csrf_middleware))
        .layer(middleware::from_fn_with_state(
            LocalClients::new(address),
            local_client_guard,
        ))
        .with_state(state.clone());

    let result = serve_http(listener, app, state.sessions.clone(), shutdown).await;
    drop(events);
    let _ = event_stop_sender.send(());
    let _ = event_task.await;
    result?;
    Ok(())
}

async fn serve_http(
    listener: TcpListener,
    app: Router,
    sessions: Arc<Mutex<SessionRegistry>>,
    shutdown: oneshot::Receiver<()>,
) -> std::io::Result<()> {
    let (drain_started_sender, drain_started_receiver) = oneshot::channel();
    let server = axum::serve(listener, app).with_graceful_shutdown(async move {
        let _ = shutdown.await;
        // Include connected-socket closure in the bounded shutdown interval.
        let _ = drain_started_sender.send(());
        sessions.lock().await.close_all();
    });
    let server = std::future::IntoFuture::into_future(server);
    tokio::pin!(server);

    tokio::select! {
        result = &mut server => result,
        started = drain_started_receiver => {
            if started.is_ok() {
                match tokio::time::timeout(HTTP_DRAIN_TIMEOUT, &mut server).await {
                    Ok(result) => result,
                    Err(_) => {
                        eprintln!("HTTP server did not drain within {HTTP_DRAIN_TIMEOUT:?}; ending server task");
                        Ok(())
                    }
                }
            } else {
                server.await
            }
        }
    }
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

async fn legacy_utility(
    State(state): State<AppState>,
    AxumPath(method): AxumPath<String>,
    request: Request<Body>,
) -> Response {
    let body = match Bytes::from_request(request, &state).await {
        Ok(body) => body,
        Err(rejection) => return rejection.into_response(),
    };
    let kwargs = serde_json::from_slice(&body).unwrap_or_else(|_| json!({}));
    Json(state.backend.legacy_utility(&method, kwargs).await).into_response()
}

/// The Host and Origin values that local clients of this listener send.
#[derive(Clone)]
struct LocalClients {
    hosts: Arc<[String]>,
    origins: Arc<[String]>,
}

impl LocalClients {
    fn new(address: std::net::SocketAddr) -> Self {
        let port = address.port();
        let mut hosts = vec![
            format!("127.0.0.1:{port}"),
            format!("localhost:{port}"),
            format!("[::1]:{port}"),
        ];
        if !hosts.contains(&address.to_string()) {
            hosts.push(address.to_string());
        }
        let mut origins: Vec<String> = hosts.iter().map(|host| format!("http://{host}")).collect();
        origins.push("https://steamloopback.host".to_owned());
        Self {
            hosts: hosts.into(),
            origins: origins.into(),
        }
    }
}

/// Refuses requests addressed to another host name (a web page whose name was
/// pointed at loopback) or sent by a web page other than Steam's and Decky's own.
/// Requests without Origin, from plugins, tools and Decky itself, only need the Host.
async fn local_client_guard(
    State(clients): State<LocalClients>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let host = request
        .headers()
        .get(HOST)
        .and_then(|value| value.to_str().ok());
    let origin = request.headers().get(ORIGIN);
    let host_allowed = host.is_some_and(|host| {
        clients
            .hosts
            .iter()
            .any(|allowed| allowed.eq_ignore_ascii_case(host))
    });
    let origin_allowed = origin.is_none_or(|origin| {
        clients
            .origins
            .iter()
            .any(|allowed| allowed.as_bytes() == origin.as_bytes())
    });
    if host_allowed && origin_allowed {
        return next.run(request).await;
    }
    eprintln!(
        "Refused {} {} for Host {:?} and Origin {:?}: not a local Decky client",
        request.method(),
        request.uri().path(),
        host,
        origin
    );
    (StatusCode::FORBIDDEN, "Forbidden").into_response()
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

    let client_id = normalize_client_id(query.client_id);
    upgrade
        .on_upgrade(move |socket| run_websocket(socket, state, client_id))
        .into_response()
}

async fn run_websocket(socket: WebSocket, state: AppState, client_id: Option<String>) {
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

    let session_key = state
        .sessions
        .lock()
        .await
        .attach(client_id, connection_id, sender.clone());

    while let Some(message) = stream.next().await {
        let Ok(message) = message else {
            break;
        };
        match message {
            Message::Text(text) => {
                let calls_to_spawn = {
                    let mut sessions = state.sessions.lock().await;
                    if !sessions.is_current_socket(&session_key, connection_id) {
                        break;
                    }
                    prepare_text_frame(&mut sessions, &session_key, text.as_str())
                };
                for (call_id, identity, call) in calls_to_spawn {
                    spawn_call(&state, session_key.clone(), call_id, identity, call);
                }
            }
            Message::Ping(payload) => {
                if !state
                    .sessions
                    .lock()
                    .await
                    .is_current_socket(&session_key, connection_id)
                {
                    break;
                }
                let _ = sender.send(Message::Pong(payload));
            }
            Message::Close(_) => break,
            Message::Binary(_) | Message::Pong(_) => {
                if !state
                    .sessions
                    .lock()
                    .await
                    .is_current_socket(&session_key, connection_id)
                {
                    break;
                }
            }
        }
    }

    state
        .sessions
        .lock()
        .await
        .detach(&session_key, connection_id);
    let _ = sender.send(Message::Close(None));
    drop(sender);
    let _ = writer.await;
}

type PreparedCall = (i64, Arc<()>, Call);

fn prepare_text_frame(
    sessions: &mut SessionRegistry,
    session_key: &SessionKey,
    text: &str,
) -> Vec<PreparedCall> {
    if let Some((call_id, legacy, args)) = plugin_call_frame(text) {
        return register_session_call(
            sessions,
            session_key,
            call_id,
            Call::Plugin { legacy, args },
        )
        .into_iter()
        .collect();
    }
    if let Ok(data) = serde_json::from_str::<Value>(text) {
        return prepare_client_message(sessions, session_key, data);
    }
    Vec::new()
}

fn prepare_client_message(
    sessions: &mut SessionRegistry,
    session_key: &SessionKey,
    data: Value,
) -> Vec<PreparedCall> {
    match data.get("type").and_then(Value::as_i64) {
        Some(0) => prepare_call(sessions, session_key, data)
            .into_iter()
            .collect(),
        Some(3) => {
            if let Some(call_id) = data.get("id").and_then(Value::as_i64)
                && let Some(session) = sessions.sessions.get_mut(session_key)
            {
                session.pending.remove(&call_id);
            }
            Vec::new()
        }
        Some(4) => prepare_full_sync(sessions, session_key, data),
        _ => Vec::new(),
    }
}

/// A call frame for a plugin method, with the plugin arguments left as JSON text.
/// Returns `None` for any other frame; those take the general path.
fn plugin_call_frame(text: &str) -> Option<(i64, bool, Vec<Box<RawValue>>)> {
    let fields: HashMap<String, &RawValue> = serde_json::from_str(text).ok()?;
    let field = |key: &str| -> Option<Value> {
        fields
            .get(key)
            .and_then(|raw| serde_json::from_str(raw.get()).ok())
    };
    if field("type")?.as_i64()? != 0 {
        return None;
    }
    let legacy = match field("route")?.as_str()? {
        "loader/call_plugin_method" => false,
        "loader/call_legacy_plugin_method" => true,
        _ => return None,
    };
    let call_id = field("id")?.as_i64()?;
    let args: Vec<&RawValue> = serde_json::from_str(fields.get("args")?.get()).ok()?;
    Some((
        call_id,
        legacy,
        args.into_iter().map(ToOwned::to_owned).collect(),
    ))
}

enum Call {
    Dispatch {
        route: String,
        args: Vec<Value>,
    },
    Plugin {
        legacy: bool,
        args: Vec<Box<RawValue>>,
    },
}

fn prepare_call(
    sessions: &mut SessionRegistry,
    session_key: &SessionKey,
    data: Value,
) -> Option<PreparedCall> {
    let Value::Object(mut fields) = data else {
        return None;
    };
    let call_id = fields.get("id").and_then(Value::as_i64)?;
    let Value::String(route) = fields.remove("route")? else {
        return None;
    };
    let Value::Array(args) = fields.remove("args")? else {
        return None;
    };
    register_session_call(
        sessions,
        session_key,
        call_id,
        Call::Dispatch { route, args },
    )
}

fn register_session_call(
    sessions: &mut SessionRegistry,
    session_key: &SessionKey,
    call_id: i64,
    call: Call,
) -> Option<PreparedCall> {
    let registration = {
        let session = sessions.sessions.get_mut(session_key)?;
        register_call(&mut session.pending, call_id)
    };
    match registration {
        CallRegistration::Existing(reply) => {
            if let Some(reply) = reply {
                sessions.send_to_session(session_key, &reply);
            }
            None
        }
        CallRegistration::New(identity) => Some((call_id, identity, call)),
    }
}

fn spawn_call(
    state: &AppState,
    session_key: SessionKey,
    call_id: i64,
    identity: Arc<()>,
    call: Call,
) {
    let backend = state.backend.clone();
    let sessions = state.sessions.clone();
    tokio::spawn(async move {
        let result = match call {
            Call::Dispatch { route, args } => backend
                .dispatch(&route, args)
                .await
                .map(|result| result.to_string()),
            Call::Plugin { legacy, args } => {
                let args: Vec<&RawValue> = args.iter().map(AsRef::as_ref).collect();
                backend.call_plugin(legacy, &args).await
            }
        };
        let message: Arc<str> = match result {
            Ok(result) => format!(r#"{{"type":1,"id":{call_id},"result":{result}}}"#).into(),
            Err(RpcError::Stopped) => json!({ "type": 2, "id": call_id }).to_string().into(),
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
                reply.to_string().into()
            }
        };

        let mut sessions = sessions.lock().await;
        let should_send = sessions
            .sessions
            .get_mut(&session_key)
            .and_then(|session| session.pending.get_mut(&call_id))
            .is_some_and(|current| {
                if Arc::ptr_eq(&current.identity, &identity) {
                    current.reply = Some(message.clone());
                    true
                } else {
                    false
                }
            });
        if should_send {
            sessions.send_to_session(&session_key, &message);
        }
    });
}

fn prepare_full_sync(
    sessions: &mut SessionRegistry,
    session_key: &SessionKey,
    data: Value,
) -> Vec<PreparedCall> {
    let Value::Object(mut fields) = data else {
        return Vec::new();
    };
    let Some(Value::Array(messages)) = fields.remove("messages") else {
        return Vec::new();
    };

    let mut live_ids = HashSet::with_capacity(messages.len());
    let mut prepared = Vec::new();
    for message in messages {
        if let Some(call_id) = message.get("id").and_then(Value::as_i64) {
            live_ids.insert(call_id);
        }
        if let Some(call) = prepare_call(sessions, session_key, message) {
            prepared.push(call);
        }
    }

    if let Some(session) = sessions.sessions.get_mut(session_key) {
        prune_pending_calls(&mut session.pending, &live_ids);
    }
    prepared
}

#[cfg(test)]
async fn handle_client_message(state: &AppState, session_key: &SessionKey, data: Value) {
    let calls_to_spawn = {
        let mut sessions = state.sessions.lock().await;
        prepare_client_message(&mut sessions, session_key, data)
    };
    for (call_id, identity, call) in calls_to_spawn {
        spawn_call(state, session_key.clone(), call_id, identity, call);
    }
}

fn frontend_cache_control(path: &str) -> &'static str {
    if !path.contains('/')
        && path.starts_with("chunk-")
        && (path.ends_with(".js") || path.ends_with(".js.map"))
    {
        "public, max-age=31536000, immutable"
    } else {
        "no-cache"
    }
}

async fn frontend_asset(
    State(state): State<AppState>,
    AxumPath(path): AxumPath<String>,
) -> Response {
    serve_asset(
        &state.assets,
        &path,
        Some(frontend_cache_control(&path)),
        None,
    )
    .await
}

async fn static_asset(State(state): State<AppState>, AxumPath(path): AxumPath<String>) -> Response {
    serve_asset(&state.assets, &path, Some("no-cache"), None).await
}

async fn locale_asset(State(state): State<AppState>, AxumPath(path): AxumPath<String>) -> Response {
    match state.locales.resolve(&path).await {
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

async fn serve_asset(
    root: &AssetRoot,
    relative: &str,
    cache_control: Option<&'static str>,
    content_type: Option<&'static str>,
) -> Response {
    let Some(file) = root.resolve(relative).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    read_file_response(&file, cache_control, content_type).await
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
    resolve_file_in_canonical_root(&canonical_root, relative).await
}

async fn resolve_file_in_canonical_root(canonical_root: &Path, relative: &str) -> Option<PathBuf> {
    if !is_safe_relative_path(relative) {
        return None;
    }
    let canonical_file = tokio::fs::canonicalize(canonical_root.join(relative))
        .await
        .ok()?;
    if !canonical_file.starts_with(canonical_root)
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
    let Ok(mut file_handle) = tokio::fs::File::open(file).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(metadata) = file_handle.metadata().await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let content_length = metadata.len();
    let (body, content_length) = if content_length <= INLINE_FILE_LIMIT {
        let mut buffer = Vec::with_capacity(content_length as usize);
        if file_handle.read_to_end(&mut buffer).await.is_err() {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
        let actual_length = buffer.len() as u64;
        (Body::from(buffer), actual_length)
    } else {
        let file_stream = futures_util::stream::try_unfold(file_handle, |mut file| async move {
            let mut chunk = vec![0; STREAM_CHUNK_SIZE];
            let bytes_read = file.read(&mut chunk).await?;
            if bytes_read == 0 {
                Ok::<_, std::io::Error>(None)
            } else {
                chunk.truncate(bytes_read);
                Ok(Some((Bytes::from(chunk), file)))
            }
        });
        (Body::from_stream(file_stream), content_length)
    };
    let mut response = Response::new(body);
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

    if query.catalog {
        if request.method() != Method::GET {
            return StatusCode::METHOD_NOT_ALLOWED.into_response();
        }
        return fetch_catalog(&state.catalog_client, url, headers)
            .await
            .unwrap_or_else(IntoResponse::into_response);
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
        Err(error) if error.is_timeout() => return StatusCode::GATEWAY_TIMEOUT.into_response(),
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

fn catalog_client(timeout: Duration) -> Result<Client, reqwest::Error> {
    Client::builder()
        .no_proxy()
        .gzip(true)
        .no_brotli()
        .no_deflate()
        .timeout(timeout)
        .build()
}

async fn fetch_catalog(
    client: &Client,
    url: String,
    mut headers: HeaderMap,
) -> Result<Response, (StatusCode, &'static str)> {
    headers.insert("accept-encoding", HeaderValue::from_static("gzip"));
    let mut upstream = client
        .get(url)
        .headers(headers)
        .send()
        .await
        .map_err(catalog_upstream_error)?;
    // Reject undecoded encodings so the browser cannot expand a body past the limit.
    if upstream
        .headers()
        .get("content-encoding")
        .is_some_and(|value| value != "identity")
    {
        return Err((StatusCode::BAD_GATEWAY, "Unsupported catalog encoding"));
    }
    let too_large = (StatusCode::BAD_GATEWAY, "Catalog exceeds 5 MB");
    if upstream
        .content_length()
        .is_some_and(|size| size > CATALOG_MAX_BYTES as u64)
    {
        return Err(too_large);
    }
    let status = upstream.status();
    let mut headers = upstream.headers().clone();
    for name in [
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-expose-headers",
        "transfer-encoding",
        "content-length",
    ] {
        headers.remove(name);
    }
    let mut body = Vec::with_capacity(upstream.content_length().unwrap_or(0) as usize);
    while let Some(chunk) = upstream.chunk().await.map_err(catalog_upstream_error)? {
        if chunk.len() > CATALOG_MAX_BYTES - body.len() {
            return Err(too_large);
        }
        body.extend_from_slice(&chunk);
    }
    let mut response = (status, body).into_response();
    *response.headers_mut() = headers;
    Ok(response)
}

fn catalog_upstream_error(error: reqwest::Error) -> (StatusCode, &'static str) {
    if error.is_timeout() {
        (StatusCode::GATEWAY_TIMEOUT, "Catalog request timed out")
    } else {
        (StatusCode::BAD_GATEWAY, "Catalog request failed")
    }
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
    use crate::test_support::TestDirectory;

    #[tokio::test]
    async fn catalog_limit_counts_decoded_bytes_with_or_without_content_length() {
        use std::io::Write;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = Router::new().route(
            "/{encoding}/{size}",
            get(
                |AxumPath((encoding, size)): AxumPath<(String, usize)>, headers: HeaderMap| async move {
                    let mut body = vec![b'a'; size];
                    body[0] = b'"';
                    body[size - 1] = b'"';
                    let mut response = if encoding == "chunked" {
                        Response::new(Body::from_stream(futures_util::stream::iter([Ok::<
                            _,
                            std::convert::Infallible,
                        >(
                            Bytes::from(body),
                        )])))
                    } else if encoding == "gzip" {
                        if headers.get("accept-encoding") != Some(&HeaderValue::from_static("gzip")) {
                            return StatusCode::NOT_ACCEPTABLE.into_response();
                        }
                        let mut gzip =
                            flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
                        gzip.write_all(&body).unwrap();
                        let mut response = gzip.finish().unwrap().into_response();
                        response
                            .headers_mut()
                            .insert("content-encoding", HeaderValue::from_static("gzip"));
                        response
                    } else {
                        body.into_response()
                    };
                    if encoding == "unsupported" {
                        response
                            .headers_mut()
                            .insert("content-encoding", HeaderValue::from_static("zstd"));
                    }
                    response
                        .headers_mut()
                        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                    response
                        .headers_mut()
                        .insert(CACHE_CONTROL, HeaderValue::from_static("max-age=600"));
                    response
                },
            ),
        );
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = catalog_client(Duration::from_secs(5)).unwrap();
        for encoding in ["plain", "gzip", "chunked"] {
            for size in [CATALOG_MAX_BYTES, CATALOG_MAX_BYTES + 1] {
                let response = fetch_catalog(
                    &client,
                    format!("http://{address}/{encoding}/{size}"),
                    HeaderMap::from_iter([(
                        HeaderName::from_static("accept-encoding"),
                        HeaderValue::from_static("identity"),
                    )]),
                )
                .await;
                if size > CATALOG_MAX_BYTES {
                    assert_eq!(
                        response.unwrap_err().0,
                        StatusCode::BAD_GATEWAY,
                        "{encoding}"
                    );
                } else {
                    let response = response.unwrap();
                    assert_eq!(response.status(), StatusCode::OK, "{encoding}");
                    assert_eq!(response.headers()[CACHE_CONTROL], "max-age=600");
                    assert!(!response.headers().contains_key("content-encoding"));
                    let body = axum::body::to_bytes(response.into_body(), CATALOG_MAX_BYTES)
                        .await
                        .unwrap();
                    let decoded: String = serde_json::from_slice(&body).unwrap();
                    assert_eq!(decoded, "a".repeat(size - 2), "{encoding}");
                }
            }
        }
        let unsupported = fetch_catalog(
            &client,
            format!("http://{address}/unsupported/2"),
            HeaderMap::new(),
        )
        .await;
        assert_eq!(unsupported.unwrap_err().0, StatusCode::BAD_GATEWAY);
        server.abort();
    }

    #[tokio::test]
    async fn catalog_deadline_covers_headers_and_the_entire_body() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = Router::new()
            .route(
                "/headers",
                get(|| async { std::future::pending::<Response>().await }),
            )
            .route(
                "/body",
                get(|| async {
                    Response::new(Body::from_stream(futures_util::stream::unfold(
                        (),
                        |()| async {
                            tokio::time::sleep(Duration::from_millis(10)).await;
                            Some((
                                Ok::<_, std::convert::Infallible>(Bytes::from_static(b" ")),
                                (),
                            ))
                        },
                    )))
                }),
            );
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = catalog_client(Duration::from_millis(100)).unwrap();
        for path in ["headers", "body"] {
            let result = tokio::time::timeout(
                Duration::from_secs(2),
                fetch_catalog(
                    &client,
                    format!("http://{address}/{path}"),
                    HeaderMap::new(),
                ),
            )
            .await
            .expect("a store must not keep the request alive");
            assert_eq!(result.unwrap_err().0, StatusCode::GATEWAY_TIMEOUT, "{path}");
        }
        server.abort();
    }

    #[test]
    fn only_plugin_call_frames_take_the_raw_path() {
        let (id, legacy, args) = plugin_call_frame(
            r#"{"type":0,"id":5,"route":"loader/call_plugin_method","args":["P","m",{"n":10000000000000000000001}, "é"]}"#,
        )
        .unwrap();
        assert_eq!((id, legacy), (5, false));
        let args: Vec<&str> = args.iter().map(|arg| arg.get()).collect();
        assert_eq!(
            args,
            [
                r#""P""#,
                r#""m""#,
                r#"{"n":10000000000000000000001}"#,
                r#""é""#
            ]
        );

        let (_, legacy, _) = plugin_call_frame(
            r#"{"type":0,"id":6,"route":"loader/call_legacy_plugin_method","args":["P","m",{}]}"#,
        )
        .unwrap();
        assert!(legacy);

        for other in [
            r#"{"type":0,"id":7,"route":"utilities/ping","args":[]}"#,
            r#"{"type":3,"id":7,"route":"loader/call_plugin_method","args":[]}"#,
            r#"{"type":0,"id":"7","route":"loader/call_plugin_method","args":[]}"#,
            r#"{"type":0,"id":7,"route":"loader/call_plugin_method","args":{}}"#,
            r#"{"type":4,"messages":[]}"#,
        ] {
            assert!(plugin_call_frame(other).is_none(), "{other}");
        }
    }

    #[tokio::test]
    async fn shutdown_bounds_a_pending_http_stream_without_affecting_regular_requests() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = Router::new()
            .route(
                "/fetch",
                get(|| async {
                    let body = futures_util::stream::once(async {
                        Ok::<_, std::convert::Infallible>(Bytes::from_static(b"partial"))
                    })
                    .chain(futures_util::stream::pending::<
                        Result<Bytes, std::convert::Infallible>,
                    >());
                    Response::builder()
                        .header(CONTENT_LENGTH, "100")
                        .body(Body::from_stream(body))
                        .unwrap()
                }),
            )
            .route("/ordinary", get(|| async { "ordinary response" }));
        let sessions = Arc::new(Mutex::new(SessionRegistry::default()));
        let (shutdown_sender, shutdown_receiver) = oneshot::channel();
        let server = tokio::spawn(serve_http(listener, app, sessions, shutdown_receiver));
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let base_url = format!("http://{address}");

        let ordinary = client
            .get(format!("{base_url}/ordinary"))
            .send()
            .await
            .unwrap();
        assert_eq!(ordinary.status(), StatusCode::OK);
        assert_eq!(ordinary.text().await.unwrap(), "ordinary response");

        let mut pending_response = client
            .get(format!("{base_url}/fetch"))
            .send()
            .await
            .unwrap();
        assert_eq!(pending_response.status(), StatusCode::OK);
        let first_chunk = tokio::time::timeout(Duration::from_secs(5), pending_response.chunk())
            .await
            .expect("the streaming response should produce its first chunk")
            .unwrap()
            .expect("the streaming response should remain readable");
        assert_eq!(first_chunk, Bytes::from_static(b"partial"));

        let ordinary = client
            .get(format!("{base_url}/ordinary"))
            .send()
            .await
            .unwrap();
        assert_eq!(ordinary.status(), StatusCode::OK);
        assert_eq!(ordinary.text().await.unwrap(), "ordinary response");

        shutdown_sender.send(()).unwrap();
        let server_result =
            tokio::time::timeout(HTTP_DRAIN_TIMEOUT + Duration::from_secs(5), server)
                .await
                .expect("a pending stream must not keep the server alive after shutdown")
                .expect("server task must not panic");
        assert!(
            server_result.is_ok(),
            "bounded shutdown should complete cleanly"
        );
        drop(pending_response);
    }

    #[tokio::test]
    async fn only_local_hosts_and_known_pages_reach_the_api() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = Router::new()
            .route("/auth/token", get(|| async { "token" }))
            .layer(middleware::from_fn_with_state(
                LocalClients::new(address),
                local_client_guard,
            ));
        let sessions = Arc::new(Mutex::new(SessionRegistry::default()));
        let (shutdown_sender, shutdown_receiver) = oneshot::channel();
        let server = tokio::spawn(serve_http(listener, app, sessions, shutdown_receiver));
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let port = address.port();
        let cases: [(&str, Option<&str>, StatusCode); 9] = [
            // Plugins, tools and Decky's own pages.
            (&format!("127.0.0.1:{port}"), None, StatusCode::OK),
            (&format!("LocalHost:{port}"), None, StatusCode::OK),
            (
                &format!("127.0.0.1:{port}"),
                Some("https://steamloopback.host"),
                StatusCode::OK,
            ),
            (
                &format!("localhost:{port}"),
                Some(&format!("http://localhost:{port}")),
                StatusCode::OK,
            ),
            // A site under another name that resolves to loopback.
            ("other-site.example", None, StatusCode::FORBIDDEN),
            (
                &format!("other-site.example:{port}"),
                Some(&format!("http://other-site.example:{port}")),
                StatusCode::FORBIDDEN,
            ),
            // Another port on this machine is another site.
            ("127.0.0.1:1", None, StatusCode::FORBIDDEN),
            // A page from another site sending to the real address.
            (
                &format!("127.0.0.1:{port}"),
                Some("https://other-site.example"),
                StatusCode::FORBIDDEN,
            ),
            (
                &format!("127.0.0.1:{port}"),
                Some("null"),
                StatusCode::FORBIDDEN,
            ),
        ];
        for (host, origin, expected) in cases {
            let mut request = client
                .get(format!("http://{address}/auth/token"))
                .header(HOST, host);
            if let Some(origin) = origin {
                request = request.header(ORIGIN, origin);
            }
            let status = request.send().await.unwrap().status();
            assert_eq!(status, expected, "Host {host}, Origin {origin:?}");
        }
        shutdown_sender.send(()).unwrap();
        server.await.unwrap().unwrap();
    }

    async fn test_backend(
        directory: &TestDirectory,
    ) -> (Arc<crate::workers::PluginRegistry>, Arc<Backend>) {
        let plugin_root = directory.0.join("plugins");
        tokio::fs::create_dir_all(&plugin_root).await.unwrap();
        let (events, _) = tokio::sync::broadcast::channel(8);
        let (_shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);
        let user = std::env::var("USER").expect("test account USER");
        let registry = Arc::new(
            crate::workers::PluginRegistry::start(
                crate::workers::WorkerConfiguration {
                    executable: std::env::current_exe().unwrap(),
                    mode: crate::workers::WorkerMode::Exec,
                    auth_token: "test-token".into(),
                    server_host: "127.0.0.1".into(),
                    server_port: 1339,
                    chown_plugin_path: false,
                    loader_version: "dev".into(),
                },
                plugin_root,
                directory.0.clone(),
                user.clone(),
                crate::platform::effective_user_ids(),
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
                "v3.2.10".into(),
            )
            .await
            .unwrap(),
        );
        (registry, backend)
    }

    #[tokio::test]
    async fn root_install_answers_reach_the_waiting_install() {
        let directory = TestDirectory::new("decky-rpc-test");
        let (registry, backend) = test_backend(&directory).await;
        // Without a waiting install the plugin manager must answer, not "route not found".
        for (route, args) in [
            (
                "utilities/confirm_root_plugin_install",
                vec![json!("no-such-prompt"), json!("password")],
            ),
            (
                "utilities/cancel_root_plugin_install",
                vec![json!("no-such-prompt")],
            ),
        ] {
            let error = backend.dispatch(route, args).await.unwrap_err();
            let error = format!("{error:?}");
            assert!(
                error.contains("Unknown root plugin prompt"),
                "{route}: {error}"
            );
        }
        registry.shutdown().await;
    }

    #[tokio::test]
    async fn replayed_calls_do_not_repeat_persistent_settings_changes() {
        let directory = TestDirectory::new("decky-rpc-test");
        let (registry, backend) = test_backend(&directory).await;
        let (sender, mut replies) = mpsc::unbounded_channel();
        let mut sessions = SessionRegistry::default();
        let session_key = sessions.attach(Some("replay-test".into()), 1, sender);
        let state = AppState {
            token: Arc::from("test-token"),
            assets: Arc::new(AssetRoot::Raw(directory.0.clone())),
            locales: Arc::new(AssetRoot::Raw(directory.0.clone())),
            backend,
            user_agent: HeaderValue::from_static("Decky/dev"),
            sessions: Arc::new(Mutex::new(sessions)),
            next_connection_id: Arc::new(AtomicU64::new(2)),
            http_client: Client::new(),
            catalog_client: catalog_client(CATALOG_TIMEOUT).unwrap(),
        };

        handle_client_message(
            &state,
            &session_key,
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
            &session_key,
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
        handle_client_message(&state, &session_key, json!({"type": 4, "messages": []})).await;
        handle_client_message(
            &state,
            &session_key,
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

    #[test]
    fn frontend_cache_control_marks_hashed_chunks_immutable_and_entrypoints_no_cache() {
        assert_eq!(
            frontend_cache_control("chunk-CqJUDUa7.js"),
            "public, max-age=31536000, immutable"
        );
        assert_eq!(
            frontend_cache_control("chunk-CqJUDUa7.js.map"),
            "public, max-age=31536000, immutable"
        );
        assert_eq!(frontend_cache_control("index.js"), "no-cache");
        assert_eq!(frontend_cache_control("fallback.js"), "no-cache");
        assert_eq!(frontend_cache_control("nested/chunk-abc.js"), "no-cache");
    }

    #[tokio::test]
    async fn read_file_response_serves_inline_and_streamed_files_with_exact_content() {
        let directory = TestDirectory::new("decky-static-file-test");
        let small_path = directory.0.join("chunk-test.js");
        let small_bytes = b"export const ok = true;\n".to_vec();
        tokio::fs::write(&small_path, &small_bytes).await.unwrap();

        let large_path = directory.0.join("large.bin");
        let large_bytes = vec![0x5Au8; INLINE_FILE_LIMIT as usize + 4096];
        tokio::fs::write(&large_path, &large_bytes).await.unwrap();

        for (path, expected, cache_control) in [
            (
                &small_path,
                &small_bytes,
                "public, max-age=31536000, immutable",
            ),
            (&large_path, &large_bytes, "no-cache"),
        ] {
            let response = read_file_response(path, Some(cache_control), None).await;
            assert_eq!(response.status(), StatusCode::OK);
            assert_eq!(
                response.headers()[CONTENT_LENGTH],
                expected.len().to_string()
            );
            assert_eq!(response.headers()[CACHE_CONTROL], cache_control);
            let body = axum::body::to_bytes(response.into_body(), expected.len() + 1)
                .await
                .unwrap();
            assert_eq!(body.as_ref(), expected.as_slice());
        }
    }

    async fn start_ws_test_server(
        directory: &TestDirectory,
    ) -> (
        std::net::SocketAddr,
        Arc<crate::workers::PluginRegistry>,
        broadcast::Sender<Value>,
        oneshot::Sender<()>,
        tokio::task::JoinHandle<anyhow::Result<()>>,
    ) {
        let (registry, backend) = test_backend(directory).await;
        let (events, _) = broadcast::channel(32);
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let probe = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = probe.local_addr().unwrap();
        drop(probe);
        let server = tokio::spawn(serve(
            address,
            "test-token".into(),
            directory.0.clone(),
            directory.0.clone(),
            backend,
            events.clone(),
            shutdown_rx,
        ));
        for _ in 0..50 {
            if tokio::net::TcpStream::connect(address).await.is_ok() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        (address, registry, events, shutdown_tx, server)
    }

    async fn recv_ws_json(
        socket: &mut tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
    ) -> Value {
        let frame = tokio::time::timeout(Duration::from_secs(5), socket.next())
            .await
            .expect("timed out waiting for WebSocket message")
            .expect("WebSocket stream ended unexpectedly")
            .expect("WebSocket read error");
        let tokio_tungstenite::tungstenite::Message::Text(text) = frame else {
            panic!("expected text WebSocket frame, got {frame:?}");
        };
        serde_json::from_str(&text).unwrap()
    }

    #[tokio::test]
    async fn concurrent_clients_do_not_evict_each_other_and_route_replies_independently() {
        use tokio_tungstenite::tungstenite::Message as ClientMessage;

        let directory = TestDirectory::new("decky-ws-multi-client");
        let (address, registry, events, shutdown_tx, server) =
            start_ws_test_server(&directory).await;

        let (mut client_a, _) = tokio_tungstenite::connect_async(format!(
            "ws://{address}/ws?auth=test-token&client_id=router-a"
        ))
        .await
        .unwrap();
        let (mut client_b, _) = tokio_tungstenite::connect_async(format!(
            "ws://{address}/ws?auth=test-token&client_id=router-b"
        ))
        .await
        .unwrap();

        // Both clients use the same call_id = 1 concurrently and must each receive their own reply.
        client_a
            .send(ClientMessage::Text(
                json!({"type":0,"id":1,"route":"utilities/settings/get","args":["k-a","val-a"]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        client_b
            .send(ClientMessage::Text(
                json!({"type":0,"id":1,"route":"utilities/settings/get","args":["k-b","val-b"]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();

        let reply_a = recv_ws_json(&mut client_a).await;
        let reply_b = recv_ws_json(&mut client_b).await;
        assert_eq!(reply_a, json!({"type":1,"id":1,"result":"val-a"}));
        assert_eq!(reply_b, json!({"type":1,"id":1,"result":"val-b"}));

        // Client A leaves call_id = 2 unacknowledged; Client B's empty FULL_SYNC must not prune it.
        client_a
            .send(ClientMessage::Text(
                json!({"type":0,"id":2,"route":"utilities/settings/set","args":["isolated-key",10]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        let set_reply = recv_ws_json(&mut client_a).await;
        assert_eq!(set_reply["type"], 1);
        assert_eq!(set_reply["id"], 2);

        client_b
            .send(ClientMessage::Text(
                json!({"type":4,"messages":[]}).to_string().into(),
            ))
            .await
            .unwrap();
        // Round-trip on Client B so its FULL_SYNC is guaranteed to have been processed.
        client_b
            .send(ClientMessage::Text(
                json!({"type":0,"id":2,"route":"utilities/settings/get","args":["k-b","after-sync"]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        assert_eq!(
            recv_ws_json(&mut client_b).await,
            json!({"type":1,"id":2,"result":"after-sync"})
        );

        client_a
            .send(ClientMessage::Text(
                json!({
                    "type": 4,
                    "messages": [{
                        "type": 0,
                        "id": 2,
                        "route": "utilities/settings/set",
                        "args": ["isolated-key", 99]
                    }]
                })
                .to_string()
                .into(),
            ))
            .await
            .unwrap();
        let replay = recv_ws_json(&mut client_a).await;
        assert_eq!(replay["type"], 1);
        assert_eq!(replay["id"], 2);
        let persisted: Value = serde_json::from_slice(
            &tokio::fs::read(directory.0.join("settings/loader.json"))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(persisted["isolated-key"], 10);

        // Backend pub/sub events reach every connected client.
        let event = json!({
            "type": 5,
            "event": "loader/add_plugin_install_prompt",
            "args": [{"request_id": "req-1"}]
        });
        events.send(event.clone()).unwrap();
        assert_eq!(recv_ws_json(&mut client_a).await, event);
        assert_eq!(recv_ws_json(&mut client_b).await, event);

        // Closing Client B must leave Client A connected and responsive.
        let _ = client_b.close(None).await;
        client_a
            .send(ClientMessage::Text(
                json!({"type":0,"id":3,"route":"utilities/settings/get","args":["k-a","still-alive"]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        assert_eq!(
            recv_ws_json(&mut client_a).await,
            json!({"type":1,"id":3,"result":"still-alive"})
        );

        shutdown_tx.send(()).unwrap();
        server.await.unwrap().unwrap();
        registry.shutdown().await;
    }

    #[tokio::test]
    async fn concurrent_anonymous_clients_do_not_evict_each_other() {
        use tokio_tungstenite::tungstenite::Message as ClientMessage;

        let directory = TestDirectory::new("decky-ws-multi-anon");
        let (address, registry, events, shutdown_tx, server) =
            start_ws_test_server(&directory).await;

        let (mut first, _) =
            tokio_tungstenite::connect_async(format!("ws://{address}/ws?auth=test-token"))
                .await
                .unwrap();
        let (mut second, _) =
            tokio_tungstenite::connect_async(format!("ws://{address}/ws?auth=test-token"))
                .await
                .unwrap();

        first
            .send(ClientMessage::Text(
                json!({"type":0,"id":1,"route":"utilities/settings/get","args":["a","one"]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        second
            .send(ClientMessage::Text(
                json!({"type":0,"id":1,"route":"utilities/settings/get","args":["b","two"]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();

        assert_eq!(
            recv_ws_json(&mut first).await,
            json!({"type":1,"id":1,"result":"one"})
        );
        assert_eq!(
            recv_ws_json(&mut second).await,
            json!({"type":1,"id":1,"result":"two"})
        );

        let event = json!({"type": 5, "event": "loader/notify_updates", "args": []});
        events.send(event.clone()).unwrap();
        assert_eq!(recv_ws_json(&mut first).await, event);
        assert_eq!(recv_ws_json(&mut second).await, event);

        shutdown_tx.send(()).unwrap();
        server.await.unwrap().unwrap();
        registry.shutdown().await;
    }

    #[tokio::test]
    async fn same_client_id_reconnect_replaces_own_socket_and_preserves_full_sync() {
        use tokio_tungstenite::tungstenite::Message as ClientMessage;

        let directory = TestDirectory::new("decky-ws-same-client-reconnect");
        let (address, registry, _events, shutdown_tx, server) =
            start_ws_test_server(&directory).await;

        let (mut router_1_old, _) = tokio_tungstenite::connect_async(format!(
            "ws://{address}/ws?auth=test-token&client_id=router-1"
        ))
        .await
        .unwrap();
        let (mut router_2, _) = tokio_tungstenite::connect_async(format!(
            "ws://{address}/ws?auth=test-token&client_id=router-2"
        ))
        .await
        .unwrap();

        router_1_old
            .send(ClientMessage::Text(
                json!({"type":0,"id":17,"route":"utilities/settings/set","args":["same-client",1]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        assert_eq!(recv_ws_json(&mut router_1_old).await["type"], 1);

        // Reconnect with the same client_id: replaces router_1_old, keeps router_2 untouched.
        let (mut router_1_new, _) = tokio_tungstenite::connect_async(format!(
            "ws://{address}/ws?auth=test-token&client_id=router-1"
        ))
        .await
        .unwrap();

        let evicted = tokio::time::timeout(Duration::from_secs(5), router_1_old.next())
            .await
            .expect("replaced socket should close promptly");
        assert!(matches!(evicted, None | Some(Ok(ClientMessage::Close(_)))));

        // router_2 is still live.
        router_2
            .send(ClientMessage::Text(
                json!({"type":0,"id":1,"route":"utilities/settings/get","args":["r2","ok"]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        assert_eq!(
            recv_ws_json(&mut router_2).await,
            json!({"type":1,"id":1,"result":"ok"})
        );

        // Replaying id=17 on router_1_new deduplicates against the cached reply.
        router_1_new
            .send(ClientMessage::Text(
                json!({
                    "type": 4,
                    "messages": [{
                        "type": 0,
                        "id": 17,
                        "route": "utilities/settings/set",
                        "args": ["same-client", 2]
                    }]
                })
                .to_string()
                .into(),
            ))
            .await
            .unwrap();
        assert_eq!(recv_ws_json(&mut router_1_new).await["type"], 1);
        let persisted: Value = serde_json::from_slice(
            &tokio::fs::read(directory.0.join("settings/loader.json"))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(persisted["same-client"], 1);

        shutdown_tx.send(()).unwrap();
        server.await.unwrap().unwrap();
        registry.shutdown().await;
    }

    #[tokio::test]
    async fn anonymous_client_reconnect_after_disconnect_preserves_full_sync() {
        use tokio_tungstenite::tungstenite::Message as ClientMessage;

        let directory = TestDirectory::new("decky-ws-anon-reconnect");
        let (address, registry, _events, shutdown_tx, server) =
            start_ws_test_server(&directory).await;

        let (mut first, _) =
            tokio_tungstenite::connect_async(format!("ws://{address}/ws?auth=test-token"))
                .await
                .unwrap();
        first
            .send(ClientMessage::Text(
                json!({"type":0,"id":17,"route":"utilities/settings/set","args":["anon-key",1]})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        assert_eq!(recv_ws_json(&mut first).await["type"], 1);
        let _ = first.close(None).await;
        drop(first);
        tokio::time::sleep(Duration::from_millis(50)).await;

        let (mut reconnected, _) =
            tokio_tungstenite::connect_async(format!("ws://{address}/ws?auth=test-token"))
                .await
                .unwrap();
        reconnected
            .send(ClientMessage::Text(
                json!({
                    "type": 4,
                    "messages": [{
                        "type": 0,
                        "id": 17,
                        "route": "utilities/settings/set",
                        "args": ["anon-key", 2]
                    }]
                })
                .to_string()
                .into(),
            ))
            .await
            .unwrap();
        assert_eq!(recv_ws_json(&mut reconnected).await["type"], 1);
        let persisted: Value = serde_json::from_slice(
            &tokio::fs::read(directory.0.join("settings/loader.json"))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(persisted["anon-key"], 1);

        shutdown_tx.send(()).unwrap();
        server.await.unwrap().unwrap();
        registry.shutdown().await;
    }

    #[test]
    fn disconnected_session_cleanup_bounds_ttl_and_count() {
        let mut registry = SessionRegistry::default();
        let base = Instant::now();

        // A disconnected session with no pending calls is removed immediately on detach.
        let (tx, _rx) = mpsc::unbounded_channel();
        let empty_key = registry.attach(Some("empty".into()), 1, tx);
        registry.detach(&empty_key, 1);
        assert!(!registry.sessions.contains_key(&empty_key));

        // Disconnected sessions with pending calls stay until TTL or MAX_DISCONNECTED_SESSIONS.
        for index in 0..(MAX_DISCONNECTED_SESSIONS + 3) {
            let key = SessionKey::Client(format!("stale-{index}"));
            let mut pending = PendingCalls::new();
            register_call(&mut pending, 1);
            registry.sessions.insert(
                key,
                ClientSession {
                    socket: None,
                    pending,
                    disconnected_at: Some(base + Duration::from_secs(index as u64)),
                },
            );
        }
        // Keep one live session that must never be evicted.
        let (live_tx, _live_rx) = mpsc::unbounded_channel();
        let live_key = SessionKey::Client("live".into());
        registry.sessions.insert(
            live_key.clone(),
            ClientSession::new(ActiveSocket {
                id: 999,
                sender: live_tx,
            }),
        );

        let after_attach = base + Duration::from_secs((MAX_DISCONNECTED_SESSIONS + 4) as u64);
        registry.prune_disconnected(after_attach);
        assert!(registry.sessions.contains_key(&live_key));
        assert_eq!(registry.sessions.len(), MAX_DISCONNECTED_SESSIONS + 1);
        assert!(
            !registry
                .sessions
                .contains_key(&SessionKey::Client("stale-0".into()))
        );
        assert!(registry.sessions.contains_key(&SessionKey::Client(format!(
            "stale-{}",
            MAX_DISCONNECTED_SESSIONS + 2
        ))));

        // After TTL passes, all disconnected sessions expire while the live one remains.
        registry.prune_disconnected(after_attach + DISCONNECTED_SESSION_TTL);
        assert_eq!(registry.sessions.len(), 1);
        assert!(registry.sessions.contains_key(&live_key));
    }
}
