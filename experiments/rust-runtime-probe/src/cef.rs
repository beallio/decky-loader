use std::{collections::VecDeque, sync::Arc, time::Duration};

use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::{
    net::TcpStream,
    process::Command,
    sync::watch,
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
}

impl Cef {
    pub(crate) fn new(client: Client, address: String) -> Self {
        Self { client, address }
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
            }
            _ => {}
        }
    }
}

async fn has_loader(session: &mut Session) -> Result<bool, RpcError> {
    let result=session.request("Runtime.evaluate",json!({"expression":"window['deckyHasLoaded'] !== null && window['deckyHasLoaded'] !== undefined","userGesture":true,"awaitPromise":false})).await?;
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
