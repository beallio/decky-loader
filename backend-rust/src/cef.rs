use std::{
    collections::{HashMap, VecDeque},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::{
    net::TcpStream,
    process::Command,
    sync::{Mutex, mpsc, oneshot, watch},
    time::{sleep, timeout},
};
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async, tungstenite::Message};

use crate::backend::{Backend, RpcError};

const SHARED_CONTEXT_NAMES: &[&str] = &[
    "SharedJSContext",
    "Steam Shared Context presented by Valve™",
    "Steam",
    "SP",
];
const COMMAND_TIMEOUT: Duration = Duration::from_secs(300);

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Deserialize)]
struct Target {
    id: String,
    title: String,
    url: String,
    #[serde(rename = "webSocketDebuggerUrl")]
    websocket_url: Option<String>,
}

impl Target {
    fn is_gamepadui(&self) -> bool {
        SHARED_CONTEXT_NAMES.contains(&self.title.as_str())
            && (self.url.contains("https://steamloopback.host/routes/")
                || self.url.contains("https://steamloopback.host/index.html"))
    }
}

struct Session {
    socket: Socket,
    next_id: u64,
    events: VecDeque<Value>,
}

impl Session {
    async fn connect(target: &Target) -> Result<Self, RpcError> {
        let url = target
            .websocket_url
            .as_deref()
            .ok_or_else(|| RpcError::failure("CEF target has no debugger socket"))?;
        let (socket, _) = connect_async(url)
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        Ok(Self {
            socket,
            next_id: 0,
            events: VecDeque::new(),
        })
    }

    async fn read(&mut self) -> Result<Value, RpcError> {
        loop {
            match self.socket.next().await {
                Some(Ok(Message::Text(text))) => {
                    return serde_json::from_str(&text)
                        .map_err(|error| RpcError::failure(error.to_string()));
                }
                Some(Ok(Message::Binary(bytes))) => {
                    return serde_json::from_slice(&bytes)
                        .map_err(|error| RpcError::failure(error.to_string()));
                }
                Some(Ok(Message::Ping(bytes))) => self
                    .socket
                    .send(Message::Pong(bytes))
                    .await
                    .map_err(|error| RpcError::failure(error.to_string()))?,
                Some(Ok(Message::Pong(_))) => {}
                Some(Ok(Message::Close(_))) | None => {
                    return Err(RpcError::failure("CEF debugger disconnected"));
                }
                Some(Ok(_)) => {}
                Some(Err(error)) => return Err(RpcError::failure(error.to_string())),
            }
        }
    }

    async fn send(&mut self, method: &str, params: Value) -> Result<u64, RpcError> {
        self.next_id += 1;
        let id = self.next_id;
        self.socket
            .send(Message::Text(
                json!({"id":id,"method":method,"params":params})
                    .to_string()
                    .into(),
            ))
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        Ok(id)
    }

    async fn request(&mut self, method: &str, params: Value) -> Result<Value, RpcError> {
        let id = self.send(method, params).await?;
        timeout(COMMAND_TIMEOUT, async {
            loop {
                let mut message = self.read().await?;
                if message.get("id").and_then(Value::as_u64) == Some(id) {
                    if let Some(error) = message.get("error") {
                        return Err(RpcError::failure(error.to_string()));
                    }
                    return message
                        .get_mut("result")
                        .map(Value::take)
                        .ok_or_else(|| RpcError::failure("CEF command response has no result"));
                }
                if message.get("method").is_some() {
                    self.events.push_back(message);
                }
            }
        })
        .await
        .map_err(|_| RpcError::failure("CEF command timed out"))?
    }

    async fn event(&mut self) -> Result<Value, RpcError> {
        if let Some(event) = self.events.pop_front() {
            return Ok(event);
        }
        loop {
            let message = self.read().await?;
            if message.get("method").is_some() {
                return Ok(message);
            }
        }
    }

    async fn close(&mut self) {
        let _ = self.socket.close(None).await;
    }
}

pub(crate) struct Cef {
    client: Client,
    address: String,
    /// Shared-context CDP session for scope resolves; see `resolver_connection`.
    resolver: Mutex<Option<Arc<ScopeConnection>>>,
}

impl Cef {
    pub(crate) fn new(client: Client, address: String) -> Self {
        Self {
            client,
            address,
            resolver: Mutex::new(None),
        }
    }

    async fn targets(&self) -> Result<Vec<Target>, RpcError> {
        let response = self
            .client
            .get(format!("{}/json", self.address.trim_end_matches('/')))
            .timeout(Duration::from_secs(3))
            .send()
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        let response = response
            .error_for_status()
            .map_err(|error| RpcError::failure(error.to_string()))?;
        let bytes = response
            .bytes()
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        serde_json::from_slice(&bytes).map_err(|error| RpcError::failure(error.to_string()))
    }

    async fn target(&self, selector: &str) -> Result<Target, RpcError> {
        self.targets()
            .await?
            .into_iter()
            .find(|target| target.title == selector || target.id == selector)
            .ok_or_else(|| RpcError::invalid(format!("Tab {selector} not found")))
    }

    async fn gamepadui(&self) -> Result<Target, RpcError> {
        self.targets()
            .await?
            .into_iter()
            .find(Target::is_gamepadui)
            .ok_or_else(|| RpcError::invalid("GamepadUI Tab not found"))
    }

    pub(crate) async fn get_gamepadui_tab(&self) -> Result<String, RpcError> {
        Ok(self.gamepadui().await?.id)
    }

    pub(crate) async fn get_tab_id(&self, selector: &str) -> Result<Value, RpcError> {
        Ok(Value::String(self.target(selector).await?.id))
    }

    pub(crate) async fn command(
        &self,
        selector: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, RpcError> {
        let target = self.target(selector).await?;
        let mut session = Session::connect(&target).await?;
        let result = if method == "Page.close" {
            session.send(method, params).await.map(|_| Value::Null)
        } else {
            session.request(method, params).await
        };
        session.close().await;
        result
    }

    pub(crate) async fn evaluate_raw(
        &self,
        selector: &str,
        code: &str,
        await_promise: bool,
    ) -> Result<Value, RpcError> {
        self.command(
            selector,
            "Runtime.evaluate",
            json!({"expression":code,"userGesture":true,"awaitPromise":await_promise}),
        )
        .await
    }

    pub(crate) async fn execute(
        &self,
        selector: &str,
        run_async: bool,
        code: &str,
    ) -> Result<Value, RpcError> {
        match self.evaluate_raw(selector, code, run_async).await {
            Ok(result) if result.get("exceptionDetails").is_some() => {
                Ok(json!({"success":false,"result":result}))
            }
            Ok(mut result) => Ok(
                json!({"success":true,"result":result.get_mut("result").and_then(|object|object.get_mut("value")).map(Value::take).unwrap_or(Value::Null)}),
            ),
            Err(error) => Ok(json!({"success":false,"result":error.message()})),
        }
    }

    pub(crate) async fn inject_css(&self, selector: &str, style: &str) -> Result<Value, RpcError> {
        let id = uuid::Uuid::new_v4().to_string();
        let code = format!(
            "(() => {{const style=document.createElement('style');style.id={};style.textContent={};document.head.append(style);}})()",
            json!(id),
            json!(style)
        );
        let result = self.evaluate_raw(selector, &code, false).await?;
        if let Some(error) = result.get("exceptionDetails") {
            return Err(RpcError::failure(error.to_string()));
        }
        Ok(Value::String(id))
    }

    pub(crate) async fn remove_css(&self, selector: &str, id: &str) -> Result<Value, RpcError> {
        let code = format!(
            "(() => {{const style=document.getElementById({});if(style.nodeName.toLowerCase()==='style')style.remove();}})()",
            json!(id)
        );
        let result = self.evaluate_raw(selector, &code, false).await?;
        if let Some(error) = result.get("exceptionDetails") {
            return Err(RpcError::failure(error.to_string()));
        }
        Ok(Value::Null)
    }

    pub(crate) async fn reload_and_evaluate(
        &self,
        selector: &str,
        source: &str,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<(), RpcError> {
        let target = self.target(selector).await?;
        let mut session = Session::connect(&target).await?;
        session.request("Page.enable", json!({})).await?;
        if *cancelled.borrow() {
            session.close().await;
            return Err(RpcError::failure("CEF page reload cancelled"));
        }
        // Complete the registration handshake even if shutdown starts. Its identifier
        // is required to remove the script before this session is closed.
        let registration = session
            .request(
                "Page.addScriptToEvaluateOnNewDocument",
                json!({"source":source}),
            )
            .await?;
        let id = registration["identifier"]
            .as_str()
            .ok_or_else(|| RpcError::failure("CEF did not return a script identifier"))?;
        let result = if *cancelled.borrow() {
            Err(RpcError::failure("CEF page reload cancelled"))
        } else {
            tokio::select! {
                _ = cancelled.changed() => Err(RpcError::failure("CEF page reload cancelled")),
                result = async {
                    session.request("Page.reload", json!({})).await?;
                    timeout(COMMAND_TIMEOUT, async {
                        loop {
                            let event = session.event().await?;
                            if event["method"] == "Page.domContentEventFired" {
                                return Ok::<(),RpcError>(());
                            }
                        }
                    }).await.map_err(|_| RpcError::failure("CEF page reload timed out"))?
                } => result,
            }
        };
        let removed = session
            .request(
                "Page.removeScriptToEvaluateOnNewDocument",
                json!({"identifier":id}),
            )
            .await;
        session.close().await;
        removed?;
        result
    }

    /// Stores the page-side module scope of `object_key` so the frontend resolver can read it.
    pub(crate) async fn resolve_scope(&self, object_key: u64) -> Result<(), RpcError> {
        let connection = self.resolver_connection().await?;
        let object_group = format!(
            "decky-scope-{}",
            NEXT_OBJECT_GROUP.fetch_add(1, Ordering::Relaxed)
        );
        resolve_scope_in(&connection, object_key, &object_group).await
    }

    /// Lazily connects once and reuses the session; a closed socket reconnects on the
    /// next call, once the dead session has failed its pending requests.
    async fn resolver_connection(&self) -> Result<Arc<ScopeConnection>, RpcError> {
        let mut state = self.resolver.lock().await;
        if let Some(connection) = state.as_ref().filter(|connection| connection.is_alive()) {
            return Ok(connection.clone());
        }
        let connection = Arc::new(ScopeConnection::connect(&self.gamepadui().await?).await?);
        *state = Some(connection.clone());
        Ok(connection)
    }

    pub(crate) async fn close_old_tabs(&self) -> Result<(), RpcError> {
        for target in self.targets().await? {
            let shared = SHARED_CONTEXT_NAMES.contains(&target.title.as_str());
            let safe_url = target.url.contains("about:blank")
                || target
                    .url
                    .contains("data:text/html,%3Cbody%3E%3C%2Fbody%3E");
            let gamepad = target.url.contains("Valve Steam Gamepad/default")
                || target.url.contains("Valve%20Steam%20Gamepad");
            if target.title.is_empty() || (!shared && safe_url && !gamepad) {
                self.command(&target.id, "Page.close", json!({})).await?;
                sleep(Duration::from_millis(500)).await;
            }
        }
        Ok(())
    }

    pub(crate) async fn run_injector(backend: Arc<Backend>, mut shutdown: watch::Receiver<bool>) {
        let mut crash_count = 0;
        let mut last_exit = None;
        while !*shutdown.borrow() {
            let target = tokio::select! {
                _ = shutdown.changed() => break,
                result = backend.cef.gamepadui() => match result {
                    Ok(target) => target,
                    Err(error) => {
                        eprintln!("CEF unavailable: {}",error.message());
                        if cancelled_sleep(&mut shutdown, Duration::from_secs(5)).await {break;}
                        continue;
                    }
                }
            };
            let mut session = match tokio::select! {
                _ = shutdown.changed() => break,
                result = Session::connect(&target) => result,
            } {
                Ok(session) => session,
                Err(error) => {
                    eprintln!("CEF connection failed: {}", error.message());
                    if cancelled_sleep(&mut shutdown, Duration::from_secs(5)).await {
                        break;
                    }
                    continue;
                }
            };
            let mut listener_shutdown = shutdown.clone();
            let outcome = tokio::select! {
                _ = shutdown.changed() => {session.close().await; break;},
                result = inject_and_listen(&mut session,&backend,&mut listener_shutdown,crash_count) => result,
            };
            session.close().await;
            if *shutdown.borrow() {
                break;
            }
            match outcome {
                Ok(InjectionExit::Restarted) => {
                    if cancelled_sleep(&mut shutdown, Duration::from_secs(1)).await {
                        break;
                    }
                    continue;
                }
                Ok(InjectionExit::Fallback) => {
                    if cancelled_sleep(&mut shutdown, Duration::from_secs(1)).await {
                        break;
                    }
                    if let Err(error) = backend.dispatch("updater/do_shutdown", vec![]).await {
                        eprintln!("CEF fallback shutdown failed: {}", error.message());
                    }
                    break;
                }
                Ok(InjectionExit::Shutdown) => break,
                Err(error) => eprintln!("CEF disconnected: {}", error.message()),
            }
            let now = std::time::Instant::now();
            crash_count = if last_exit
                .is_some_and(|previous| now.duration_since(previous) < Duration::from_secs(60))
            {
                crash_count + 1
            } else {
                0
            };
            last_exit = Some(now);
            if crash_count > 4 {
                if let Err(error) = backend.dispatch("updater/do_shutdown", vec![]).await {
                    eprintln!("CEF repeated-crash shutdown failed: {}", error.message());
                }
                break;
            }
        }
    }
}

type ScopeResponse = oneshot::Sender<Result<Value, RpcError>>;

/// CDP object ids only exist in the session that created them, so every resolve reuses
/// this one connection to the shared context. A single task owns the socket so
/// concurrent resolves can share it: requests carry unique ids, responses are routed
/// by id, and events are dropped. When the socket dies every waiter fails and the next
/// request reconnects.
struct ScopeConnection {
    requests: mpsc::UnboundedSender<String>,
    pending: Arc<Mutex<HashMap<u64, ScopeResponse>>>,
    next_id: AtomicU64,
}

impl ScopeConnection {
    async fn connect(target: &Target) -> Result<Self, RpcError> {
        let url = target
            .websocket_url
            .as_deref()
            .ok_or_else(|| RpcError::failure("CEF target has no debugger socket"))?;
        let (socket, _) = connect_async(url)
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        let (mut sink, mut stream) = socket.split();
        let (requests, mut outbox) = mpsc::unbounded_channel::<String>();
        let pending = Arc::new(Mutex::new(HashMap::<u64, ScopeResponse>::new()));
        let router_pending = pending.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    request = outbox.recv() => match request {
                        Some(text) => {
                            if sink.send(Message::Text(text.into())).await.is_err() {
                                break;
                            }
                        }
                        None => break,
                    },
                    message = stream.next() => match message {
                        Some(Ok(Message::Text(text))) => {
                            route_response(&router_pending, &text).await;
                        }
                        Some(Ok(Message::Binary(bytes))) => {
                            if let Ok(text) = std::str::from_utf8(&bytes) {
                                route_response(&router_pending, text).await;
                            }
                        }
                        Some(Ok(Message::Ping(bytes))) => {
                            if sink.send(Message::Pong(bytes)).await.is_err() {
                                break;
                            }
                        }
                        Some(Ok(Message::Close(_))) | None => break,
                        Some(Ok(_)) => {}
                        Some(Err(_)) => break,
                    },
                }
            }
            // Object ids die with the socket, so fail every waiter instead of hanging.
            for (_, response) in router_pending.lock().await.drain() {
                let _ = response.send(Err(RpcError::failure("CEF resolver disconnected")));
            }
            let _ = sink.close().await;
        });
        Ok(Self {
            requests,
            pending,
            next_id: AtomicU64::new(0),
        })
    }

    fn is_alive(&self) -> bool {
        !self.requests.is_closed()
    }

    async fn request(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed) + 1;
        let (response, receiver) = oneshot::channel();
        self.pending.lock().await.insert(id, response);
        let message = json!({"id":id,"method":method,"params":params}).to_string();
        if self.requests.send(message).is_err() {
            self.pending.lock().await.remove(&id);
            return Err(RpcError::failure("CEF resolver disconnected"));
        }
        match timeout(COMMAND_TIMEOUT, receiver).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(RpcError::failure("CEF resolver disconnected")),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(RpcError::failure("CEF command timed out"))
            }
        }
    }

    fn notify(&self, method: &str, params: Value) {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed) + 1;
        let message = json!({"id":id,"method":method,"params":params}).to_string();
        let _ = self.requests.send(message);
    }
}

/// Routes one CDP message: command responses go to their waiter, events are dropped.
async fn route_response(pending: &Mutex<HashMap<u64, ScopeResponse>>, text: &str) {
    let Ok(message) = serde_json::from_str::<Value>(text) else {
        return;
    };
    let Some(id) = message.get("id").and_then(Value::as_u64) else {
        return;
    };
    let Some(response) = pending.lock().await.remove(&id) else {
        return;
    };
    let result = match message.get("error") {
        Some(error) => Err(RpcError::failure(error.to_string())),
        None => message
            .get("result")
            .cloned()
            .ok_or_else(|| RpcError::failure("CEF command response has no result")),
    };
    let _ = response.send(result);
}

static NEXT_OBJECT_GROUP: AtomicU64 = AtomicU64::new(0);

async fn resolve_scope_in(
    connection: &ScopeConnection,
    object_key: u64,
    object_group: &str,
) -> Result<(), RpcError> {
    let evaluated = connection
        .request(
            "Runtime.evaluate",
            json!({
                "expression": format!("window._deckyModuleResolverInternals.scopedObjects.get({object_key})"),
                "userGesture": true,
                "returnByValue": false,
                "awaitPromise": false,
                "objectGroup": object_group
            }),
        )
        .await;
    let result = match evaluated {
        Ok(evaluated) => resolve_scope_objects(connection, object_key, &evaluated).await,
        Err(error) => Err(error),
    };
    // The group holds every object id this resolve created; drop them even after a failure
    // without blocking the caller on an extra CDP round trip.
    connection.notify(
        "Runtime.releaseObjectGroup",
        json!({"objectGroup": object_group}),
    );
    result
}

async fn resolve_scope_objects(
    connection: &ScopeConnection,
    object_key: u64,
    evaluated: &Value,
) -> Result<(), RpcError> {
    let function = evaluated_object_id(evaluated)?;
    let properties = connection
        .request(
            "Runtime.getProperties",
            json!({"objectId": function, "ownProperties": true}),
        )
        .await?;
    let scopes = scopes_object_id(&properties)?;
    let scope_list = connection
        .request("Runtime.getProperties", json!({"objectId": scopes}))
        .await?;
    let scope = module_scope_id(&scope_list)?;
    let stored = connection
        .request(
            "Runtime.callFunctionOn",
            json!({
                "functionDeclaration": "function(k, s) { window._deckyModuleResolverInternals.scopes.set(k, s); }",
                "objectId": function,
                "arguments": [{"value": object_key}, {"objectId": scope}]
            }),
        )
        .await?;
    if stored.get("exceptionDetails").is_some() {
        return Err(RpcError::failure("CEF could not store the module scope"));
    }
    Ok(())
}

fn evaluated_object_id(evaluated: &Value) -> Result<&str, RpcError> {
    if evaluated.get("exceptionDetails").is_some() {
        return Err(RpcError::failure("CEF scope object evaluation failed"));
    }
    evaluated
        .get("result")
        .and_then(|result| result.get("objectId"))
        .and_then(Value::as_str)
        .ok_or_else(|| RpcError::failure("CEF scope object has no id"))
}

/// Picks `[[Scopes]]` out of a `Runtime.getProperties` response.
fn scopes_object_id(properties: &Value) -> Result<&str, RpcError> {
    properties
        .get("internalProperties")
        .and_then(Value::as_array)
        .and_then(|properties| {
            properties
                .iter()
                .find(|property| property["name"] == "[[Scopes]]")
        })
        .and_then(|property| property.get("value"))
        .and_then(|value| value.get("objectId"))
        .and_then(Value::as_str)
        .ok_or_else(|| RpcError::failure("CEF function has no [[Scopes]]"))
}

/// Picks the `Module` scope out of the `[[Scopes]]` property list.
fn module_scope_id(scopes: &Value) -> Result<&str, RpcError> {
    scopes
        .get("result")
        .and_then(Value::as_array)
        .and_then(|scopes| {
            scopes
                .iter()
                .find(|scope| scope["value"]["description"] == "Module")
        })
        .and_then(|scope| scope.get("value"))
        .and_then(|value| value.get("objectId"))
        .and_then(Value::as_str)
        .ok_or_else(|| RpcError::failure("CEF module scope not found"))
}

enum InjectionExit {
    Restarted,
    Fallback,
    Shutdown,
}

async fn inject_and_listen(
    session: &mut Session,
    backend: &Backend,
    shutdown: &mut watch::Receiver<bool>,
    crash_count: u32,
) -> Result<InjectionExit, RpcError> {
    session.request("Page.enable", json!({})).await?;
    if has_loader(session).await? {
        let status = Command::new("killall")
            .args(["-s", "SIGTERM", "steamwebhelper"])
            .status()
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        if !status.success() {
            return Err(RpcError::failure(
                "Could not restart steamwebhelper for loader injection",
            ));
        }
        return Ok(InjectionExit::Restarted);
    }
    let script = if crash_count < 3 {
        "index.js"
    } else {
        "fallback.js"
    };
    let code = injection_script(script, backend.loader_version());
    let result = session
        .request(
            "Runtime.evaluate",
            json!({"expression":code,"userGesture":true,"awaitPromise":false}),
        )
        .await?;
    if let Some(error) = result.get("exceptionDetails") {
        return Err(RpcError::failure(error.to_string()));
    }
    if crash_count > 2 {
        return Ok(InjectionExit::Fallback);
    }
    loop {
        let event = tokio::select! {
            _ = shutdown.changed() => return Ok(InjectionExit::Shutdown),
            result = session.event() => result?,
        };
        match event["method"].as_str() {
            Some("Inspector.detached") => {
                return Err(RpcError::failure("CEF requested debugger detachment"));
            }
            Some("Page.domContentEventFired") if !has_loader(session).await? => {
                let result = session
                    .request(
                        "Runtime.evaluate",
                        json!({"expression":code,"userGesture":true,"awaitPromise":false}),
                    )
                    .await?;
                if let Some(error) = result.get("exceptionDetails") {
                    return Err(RpcError::failure(error.to_string()));
                }
                if crash_count > 2 {
                    return Ok(InjectionExit::Fallback);
                }
            }
            _ => {}
        }
    }
}

async fn has_loader(session: &mut Session) -> Result<bool, RpcError> {
    let result = session
        .request(
            "Runtime.evaluate",
            json!({"expression":"window['deckyHasLoaded'] !== null && window['deckyHasLoaded'] !== undefined","userGesture":true,"awaitPromise":false}),
        )
        .await?;
    Ok(result["result"]["value"].as_bool().unwrap_or(false))
}

fn injection_script(script: &str, version: &str) -> String {
    let url = json!(format!(
        "http://localhost:1337/frontend/{script}?v={version}"
    ));
    format!(
        "try{{if(window.deckyHasLoaded){{setTimeout(()=>SteamClient.Browser.RestartJSContext(),100)}}else{{window.deckyHasLoaded=true;(async()=>{{try{{await import({url})}}catch(e){{console.error(e)}}}})();}}}}catch(e){{console.error(e)}}"
    )
}

async fn cancelled_sleep(shutdown: &mut watch::Receiver<bool>, duration: Duration) -> bool {
    tokio::select! {
        _ = shutdown.changed() => true,
        _ = sleep(duration) => *shutdown.borrow(),
    }
}

#[cfg(test)]
mod tests {
    use futures_util::{SinkExt, StreamExt};
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_async;

    use super::*;

    fn internal_property(name: &str, object_id: &str) -> Value {
        json!({"name":name,"value":{"type":"object","objectId":object_id}})
    }

    fn scope_entry(description: &str, object_id: &str) -> Value {
        json!({"name":"0","value":{"description":description,"objectId":object_id}})
    }

    #[test]
    fn finds_the_scopes_object() {
        let response = json!({"internalProperties":[
            internal_property("[[Prototype]]", "prototype"),
            internal_property("[[Scopes]]", "scopes")
        ]});
        assert_eq!(scopes_object_id(&response).unwrap(), "scopes");
    }

    #[test]
    fn missing_scopes_is_an_error() {
        let response =
            json!({"internalProperties":[internal_property("[[Prototype]]", "prototype")]});
        assert!(scopes_object_id(&response).is_err());
        assert!(scopes_object_id(&json!({"result":[]})).is_err());
    }

    #[test]
    fn finds_the_module_scope() {
        let response = json!({"result":[
            scope_entry("Closure", "closure"),
            scope_entry("Module", "module")
        ]});
        assert_eq!(module_scope_id(&response).unwrap(), "module");
    }

    #[test]
    fn missing_module_scope_is_an_error() {
        let response = json!({"result":[
            scope_entry("Closure", "closure"),
            scope_entry("Script", "script")
        ]});
        assert!(module_scope_id(&response).is_err());
    }

    /// The scope list only has a `Closure` entry, so step c fails; the object group
    /// must still be released over the same session.
    #[tokio::test]
    async fn releases_the_object_group_when_the_module_scope_is_missing() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = accept_async(stream).await.unwrap();
            let mut seen = Vec::new();
            while let Some(Ok(Message::Text(request))) = socket.next().await {
                let request: Value = serde_json::from_str(&request).unwrap();
                let id = request["id"].clone();
                let method = request["method"].as_str().unwrap().to_owned();
                seen.push((
                    method.clone(),
                    request["params"]["objectGroup"].as_str().map(str::to_owned),
                ));
                let result = if method == "Runtime.evaluate" {
                    json!({"result":{"objectId":"fn"}})
                } else if method == "Runtime.getProperties" && request["params"]["objectId"] == "fn"
                {
                    json!({"internalProperties":[internal_property("[[Scopes]]","scopes")]})
                } else if method == "Runtime.getProperties" {
                    json!({"result":[scope_entry("Closure","closure")]})
                } else {
                    json!({})
                };
                socket
                    .send(Message::Text(
                        json!({"id":id,"result":result}).to_string().into(),
                    ))
                    .await
                    .unwrap();
            }
            seen
        });
        let target = Target {
            id: "test".into(),
            title: "SharedJSContext".into(),
            url: "https://steamloopback.host/routes/library".into(),
            websocket_url: Some(format!("ws://{address}")),
        };
        let connection = ScopeConnection::connect(&target).await.unwrap();
        let error = resolve_scope_in(&connection, 7, "decky-scope-test")
            .await
            .unwrap_err();
        assert!(
            error.message().contains("module scope"),
            "{}",
            error.message()
        );
        drop(connection);
        let seen = server.await.unwrap();
        let methods: Vec<&str> = seen.iter().map(|(method, _)| method.as_str()).collect();
        assert_eq!(
            methods,
            [
                "Runtime.evaluate",
                "Runtime.getProperties",
                "Runtime.getProperties",
                "Runtime.releaseObjectGroup"
            ]
        );
        assert_eq!(seen[3].1.as_deref(), Some("decky-scope-test"));
    }

    /// Step 2 requests only own properties (plus `[[Scopes]]` internal properties), and
    /// releasing the object group does not block the caller waiting for a CDP reply.
    #[tokio::test]
    async fn scope_resolve_uses_own_properties_and_does_not_wait_for_release_reply() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = accept_async(stream).await.unwrap();
            let mut fn_own_properties = None;
            let mut released_group = None;
            while let Some(Ok(Message::Text(request))) = socket.next().await {
                let request: Value = serde_json::from_str(&request).unwrap();
                let id = request["id"].clone();
                let method = request["method"].as_str().unwrap();
                if method == "Runtime.releaseObjectGroup" {
                    released_group = request["params"]["objectGroup"].as_str().map(str::to_owned);
                    // Deliberately do not reply; caller must already have returned.
                    continue;
                }
                let result = if method == "Runtime.evaluate" {
                    json!({"result":{"objectId":"fn"}})
                } else if method == "Runtime.getProperties" && request["params"]["objectId"] == "fn"
                {
                    fn_own_properties = request["params"]["ownProperties"].as_bool();
                    json!({"internalProperties":[internal_property("[[Scopes]]","scopes")]})
                } else if method == "Runtime.getProperties" {
                    json!({"result":[scope_entry("Module","module")]})
                } else {
                    json!({"result":{}})
                };
                socket
                    .send(Message::Text(
                        json!({"id":id,"result":result}).to_string().into(),
                    ))
                    .await
                    .unwrap();
            }
            (fn_own_properties, released_group)
        });
        let target = Target {
            id: "test".into(),
            title: "SharedJSContext".into(),
            url: "https://steamloopback.host/routes/library".into(),
            websocket_url: Some(format!("ws://{address}")),
        };
        let connection = ScopeConnection::connect(&target).await.unwrap();
        tokio::time::timeout(
            Duration::from_secs(2),
            resolve_scope_in(&connection, 42, "decky-scope-fast"),
        )
        .await
        .expect("resolve_scope_in should not wait for Runtime.releaseObjectGroup reply")
        .unwrap();
        drop(connection);
        let (fn_own_properties, released_group) = server.await.unwrap();
        assert_eq!(fn_own_properties, Some(true));
        assert_eq!(released_group.as_deref(), Some("decky-scope-fast"));
    }
}
