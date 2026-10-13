use std::{
    collections::{BTreeMap, HashSet},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::Output,
    sync::Arc,
    time::Duration,
};

use encoding_rs::Encoding;
use fancy_regex::Regex;
use flate2::{
    Compression,
    write::{GzEncoder, ZlibEncoder},
};
use reqwest::{
    Client, Method,
    header::{
        ACCEPT, ACCEPT_ENCODING, CONTENT_ENCODING, CONTENT_LENGTH, CONTENT_TYPE, EXPECT, HeaderMap,
        HeaderName, HeaderValue, TRANSFER_ENCODING,
    },
};
use serde_json::{Map, Value, json};
use tokio::{
    io::copy_bidirectional,
    net::{TcpListener, TcpStream},
    process::Command,
    sync::{Mutex, watch},
    task::{JoinHandle, JoinSet},
    time::timeout,
};
use unicode_casefold::UnicodeCaseFold;
use uuid::Uuid;

use crate::backend::{Backend, RpcError, exact, string};

const REMOTE_DEBUGGER_UNIT: &str = "steam-web-debug-portforward.service";
const RDT_PORT: u16 = 8097;
const RDT_BIND: &str = "127.0.0.1";

const RDT_CLEANUP_TIMEOUT: Duration = Duration::from_secs(5);
const RDT_DOCUMENT_MARKER: &str = "__DECKY_RDT_DOCUMENT_OWNER__";
static CEF_SOCKET_CLOSE_LOCK: Mutex<()> = Mutex::const_new(());

/// Linux implementation of the non-plugin utility routes in the Python SDK.
///
/// `shutdown` must be awaited before the CEF client is torn down. It cancels
/// RDT setup, joins all listener/connection tasks, and clears only its marked
/// page state.
pub struct Utilities {
    legacy_client: Client,
    rdt_proxy: Mutex<Option<RdtProxy>>,
    rdt_document: Mutex<Option<RdtDocument>>,
    rdt_lifecycle: Mutex<()>,
    shutdown_tx: watch::Sender<bool>,
}

struct RdtProxy {
    stop: watch::Sender<bool>,
    task: JoinHandle<Result<(), String>>,
}

#[derive(Clone)]
struct RdtDocument {
    target: String,
    marker: String,
}

struct FileEntry {
    name: String,
    sort_name: String,
    path: PathBuf,
    is_dir: bool,
    size: u64,
    modified: f64,
    created: f64,
}

struct FilePickerOptions {
    path: Option<String>,
    include_files: bool,
    include_folders: bool,
    include_ext: Option<Vec<String>>,
    include_hidden: bool,
    order_by: String,
    filter_for: Option<String>,
    page: i128,
    max: i128,
}

impl Utilities {
    pub fn new() -> Self {
        let (shutdown_tx, _) = watch::channel(false);
        Self {
            legacy_client: Client::builder()
                .no_proxy()
                .build()
                .expect("default HTTP client configuration is valid"),
            rdt_proxy: Mutex::new(None),
            rdt_document: Mutex::new(None),
            rdt_lifecycle: Mutex::new(()),
            shutdown_tx,
        }
    }

    pub(crate) fn cancel_setup(&self) {
        self.shutdown_tx.send_replace(true);
    }

    /// Stop every owned RDT listener, stream and setup operation before CEF teardown.
    pub async fn shutdown(&self, backend: &Backend) -> Result<(), RpcError> {
        self.cancel_setup();
        let _lifecycle = self.rdt_lifecycle.lock().await;
        let proxy_result = self.stop_rdt_proxy().await;
        let document = self.rdt_document.lock().await.clone();
        let page_result = match document.as_ref() {
            Some(document) => clear_rdt_document(backend, document).await.map(|_| ()),
            None => Ok(()),
        };
        if page_result.is_ok() && document.is_some() {
            *self.rdt_document.lock().await = None;
        }
        match (proxy_result, page_result) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
            (Err(proxy_error), Err(page_error)) => Err(RpcError::failure(format!(
                "{}; additionally failed to clear RDT page state: {}",
                rpc_error_message(&proxy_error),
                rpc_error_message(&page_error)
            ))),
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
            "utilities/ping"
                | "utilities/settings/get"
                | "utilities/settings/set"
                | "utilities/get_user_info"
                | "utilities/filepicker_ls"
                | "utilities/http_request"
                | "utilities/execute_in_tab"
                | "utilities/inject_css_into_tab"
                | "utilities/remove_css_from_tab"
                | "utilities/get_tab_id"
                | "utilities/allow_remote_debugging"
                | "utilities/disallow_remote_debugging"
                | "utilities/start_ssh"
                | "utilities/stop_ssh"
                | "utilities/restart_webhelper"
                | "utilities/close_cef_socket"
                | "utilities/enable_rdt"
                | "utilities/disable_rdt"
                | "utilities/_call_legacy_utility"
        ) {
            return None;
        }
        Some(self.dispatch_owned(backend, route, args).await)
    }

    async fn dispatch_owned(
        &self,
        backend: &Backend,
        route: &str,
        args: Vec<Value>,
    ) -> Result<Value, RpcError> {
        match route {
            "utilities/ping" => {
                exact(&args, 0)?;
                Ok(json!("pong"))
            }
            "utilities/settings/get" => self.call_legacy_method(backend, "get_setting", args).await,
            "utilities/settings/set" => self.call_legacy_method(backend, "set_setting", args).await,
            "utilities/get_user_info" => {
                self.call_legacy_method(backend, "get_user_info", args)
                    .await
            }
            "utilities/filepicker_ls" => {
                self.call_legacy_method(backend, "filepicker_ls", args)
                    .await
            }
            "utilities/http_request" => {
                self.call_legacy_method(backend, "http_request", args).await
            }
            "utilities/execute_in_tab" => {
                self.call_legacy_method(backend, "execute_in_tab", args)
                    .await
            }
            "utilities/inject_css_into_tab" => {
                self.call_legacy_method(backend, "inject_css_into_tab", args)
                    .await
            }
            "utilities/remove_css_from_tab" => {
                self.call_legacy_method(backend, "remove_css_from_tab", args)
                    .await
            }
            "utilities/get_tab_id" => self.call_legacy_method(backend, "get_tab_id", args).await,
            "utilities/allow_remote_debugging" | "utilities/start_ssh" | "utilities/stop_ssh" => {
                exact(&args, 0)?;
                service_start(REMOTE_DEBUGGER_UNIT).await.map(Value::Bool)
            }
            "utilities/disallow_remote_debugging" => {
                exact(&args, 0)?;
                service_stop(REMOTE_DEBUGGER_UNIT).await.map(Value::Bool)
            }
            "utilities/restart_webhelper" => {
                exact(&args, 0)?;
                restart_webhelper().await.map(Value::Bool)
            }
            "utilities/close_cef_socket" => {
                exact(&args, 0)?;
                close_cef_socket().await.map(|()| Value::Null)
            }
            "utilities/enable_rdt" => {
                exact(&args, 0)?;
                self.enable_rdt(backend).await
            }
            "utilities/disable_rdt" => {
                exact(&args, 0)?;
                self.disable_rdt(backend).await
            }
            "utilities/_call_legacy_utility" => {
                exact(&args, 2)?;
                let method = string(&args, 0)?;
                Ok(self.legacy_call(backend, method, args[1].clone()).await)
            }
            _ => unreachable!("route ownership is checked before dispatch"),
        }
    }

    /// Execute a legacy `/methods/{method_name}` call or the WebSocket
    /// `_call_legacy_utility` route, preserving its `{success,result}` envelope.
    pub async fn legacy_call(&self, backend: &Backend, method: &str, kwargs: Value) -> Value {
        let args = match legacy_args(method, &kwargs) {
            Ok(args) => args,
            Err(message) => return json!({"success": false, "result": message}),
        };
        match self.call_legacy_method(backend, method, args).await {
            Ok(result) => json!({"success": true, "result": result}),
            Err(error) => json!({
                "success": false,
                "result": rpc_error_message(&error),
            }),
        }
    }

    async fn call_legacy_method(
        &self,
        backend: &Backend,
        method: &str,
        args: Vec<Value>,
    ) -> Result<Value, RpcError> {
        match method {
            "ping" => {
                exact(&args, 0)?;
                Ok(json!("pong"))
            }
            "get_setting" => {
                exact(&args, 2)?;
                Ok(backend.setting(string(&args, 0)?, args[1].clone()).await)
            }
            "set_setting" => {
                exact(&args, 2)?;
                backend
                    .set_setting(string(&args, 0)?, args[1].clone())
                    .await?;
                Ok(Value::Null)
            }
            "get_user_info" => {
                exact(&args, 0)?;
                Ok(json!({"username": backend.user, "path": backend.user_home}))
            }
            "get_tab_id" => {
                exact(&args, 1)?;
                backend.cef.get_tab_id(string(&args, 0)?).await
            }
            "execute_in_tab" => {
                exact(&args, 3)?;
                let tab = string(&args, 0)?;
                let run_async = args[1]
                    .as_bool()
                    .ok_or_else(|| RpcError::invalid("argument 1 must be a boolean"))?;
                let code = string(&args, 2)?;
                match backend.cef.execute(tab, run_async, code).await {
                    Ok(value) => Ok(value),
                    Err(error) => Ok(json!({
                        "success": false,
                        "result": rpc_error_message(&error),
                    })),
                }
            }
            "inject_css_into_tab" => {
                exact(&args, 2)?;
                backend
                    .cef
                    .inject_css(string(&args, 0)?, string(&args, 1)?)
                    .await
            }
            "remove_css_from_tab" => {
                exact(&args, 2)?;
                backend
                    .cef
                    .remove_css(string(&args, 0)?, string(&args, 1)?)
                    .await
            }
            "filepicker_ls" => {
                let options = parse_filepicker_options(&args)?;
                filepicker_listing(backend.user_home.as_str(), options)
            }
            "http_request" => self.http_request(&args).await,
            _ => Err(RpcError::invalid(format!(
                "unknown legacy utility '{method}'"
            ))),
        }
    }

    async fn http_request(&self, args: &[Value]) -> Result<Value, RpcError> {
        if !(2..=4).contains(&args.len()) {
            return Err(RpcError::invalid(format!(
                "http_request expects 2 to 4 arguments, received {}",
                args.len()
            )));
        }
        let method = string(args, 0)?;
        let url = string(args, 1)?;
        let empty_options = Map::new();
        let extra_opts = match args.get(2) {
            None => &empty_options,
            Some(Value::Object(options)) => options,
            Some(_) => {
                return Err(RpcError::invalid(
                    "http_request extra_opts must be an object",
                ));
            }
        };
        let timeout = args.get(3);
        self.send_legacy_http(method, url, extra_opts, timeout)
            .await
    }

    async fn send_legacy_http(
        &self,
        method: &str,
        url: &str,
        options: &Map<String, Value>,
        timeout: Option<&Value>,
    ) -> Result<Value, RpcError> {
        const ALLOWED: &[&str] = &[
            "allow_redirects",
            "auth",
            "auto_decompress",
            "chunked",
            "compress",
            "cookies",
            "data",
            "expect100",
            "headers",
            "json",
            "max_redirects",
            "params",
            "proxy",
            "proxy_auth",
            "proxy_headers",
            "raise_for_status",
            "skip_auto_headers",
            "verify_ssl",
        ];
        if let Some(option) = options
            .keys()
            .find(|option| !ALLOWED.contains(&option.as_str()))
        {
            return Err(RpcError::invalid(format!(
                "http_request got an unexpected option '{option}'"
            )));
        }

        if options.get("auth").is_some_and(|value| !value.is_null()) {
            return Err(RpcError::invalid(
                "http_request auth must be null; aiohttp BasicAuth is not JSON-serializable",
            ));
        }

        let data = options.get("data").filter(|value| !value.is_null());
        let json_body = options.get("json").filter(|value| !value.is_null());
        if data.is_some() && json_body.is_some() {
            return Err(RpcError::invalid(
                "http_request cannot use both data and json options",
            ));
        }

        let allow_redirects = option_bool(options, "allow_redirects", true)?;
        let max_redirects = max_redirects(options.get("max_redirects"))?;
        let auto_decompress = option_optional_bool(options, "auto_decompress", true)?;
        let raise_for_status = option_optional_bool(options, "raise_for_status", false)?;
        let verify_ssl = option_optional_bool(options, "verify_ssl", true)?;
        let mut chunked = option_optional_bool(options, "chunked", false)?;
        let expect_100 = option_optional_bool(options, "expect100", false)?;
        let skip_auto_headers = parse_skip_auto_headers(options.get("skip_auto_headers"))?;
        let mut headers = options
            .get("headers")
            .map(parse_headers)
            .transpose()?
            .unwrap_or_default();
        let has_content_type = headers.contains_key(CONTENT_TYPE);
        let has_content_encoding = headers.contains_key(CONTENT_ENCODING);
        let has_explicit_accept_encoding = headers.contains_key(ACCEPT_ENCODING);
        let has_content_length = headers.contains_key(CONTENT_LENGTH);
        let transfer_encoding_chunked = headers
            .get(TRANSFER_ENCODING)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value.to_ascii_lowercase().contains("chunked"));
        let skip_content_type =
            skip_auto_headers.contains(&HeaderName::from_static("content-type"));
        let skip_accept_encoding = skip_auto_headers
            .contains(&HeaderName::from_static("accept-encoding"))
            && !has_explicit_accept_encoding;
        if !skip_auto_headers.contains(&HeaderName::from_static("accept"))
            && !headers.contains_key(ACCEPT)
        {
            headers.insert(ACCEPT, HeaderValue::from_static("*/*"));
        }
        if !skip_accept_encoding && !has_explicit_accept_encoding {
            headers.insert(
                ACCEPT_ENCODING,
                HeaderValue::from_static("gzip, deflate, br"),
            );
        }

        let proxy = options.get("proxy").and_then(Value::as_str);
        if options
            .get("proxy")
            .is_some_and(|value| !value.is_null() && proxy.is_none())
        {
            return Err(RpcError::invalid("http_request proxy must be a string"));
        }
        if options
            .get("proxy_auth")
            .is_some_and(|value| !value.is_null())
        {
            return Err(RpcError::invalid(
                "http_request proxy_auth must be null; aiohttp BasicAuth is not JSON-serializable",
            ));
        }
        let proxy_headers = options
            .get("proxy_headers")
            .map(parse_headers)
            .transpose()?
            .unwrap_or_default();

        let mut request_url =
            reqwest::Url::parse(url).map_err(|error| RpcError::invalid(error.to_string()))?;
        match options.get("params") {
            Some(Value::String(query)) if !query.is_empty() => {
                extend_raw_query(&mut request_url, query);
            }
            Some(Value::String(_)) | None => {}
            Some(params) => {
                let pairs = query_pairs(params)?;
                if !pairs.is_empty() {
                    extend_query_pairs(&mut request_url, pairs);
                }
            }
        }

        let cookie_jar = Arc::new(reqwest::cookie::Jar::default());
        if let Some(cookies) = options.get("cookies").filter(|value| !value.is_null()) {
            let cookies = cookie_pairs(cookies)?;
            for (name, value) in cookies {
                cookie_jar.add_cookie_str(&format!("{name}={value}"), &request_url);
            }
        }

        let policy = if allow_redirects {
            reqwest::redirect::Policy::limited(max_redirects)
        } else {
            reqwest::redirect::Policy::none()
        };
        let manual_decompress = auto_decompress && skip_accept_encoding;
        let mut builder = Client::builder()
            .no_proxy()
            .cookie_provider(cookie_jar)
            .redirect(policy);
        if !verify_ssl {
            builder = builder.danger_accept_invalid_certs(true);
        }
        if !auto_decompress || manual_decompress {
            builder = builder.no_gzip().no_brotli().no_deflate();
        }
        if let Some(proxy) = proxy {
            let mut proxy =
                reqwest::Proxy::all(proxy).map_err(|error| RpcError::invalid(error.to_string()))?;
            if !proxy_headers.is_empty() {
                proxy = proxy.headers(proxy_headers);
            }
            builder = builder.proxy(proxy);
        }
        let client = builder
            .build()
            .map_err(|error| RpcError::failure(error.to_string()))?;
        let method = Method::from_bytes(method.to_ascii_uppercase().as_bytes())
            .map_err(|error| RpcError::invalid(error.to_string()))?;
        let mut request = client.request(method, request_url);
        if let Some(timeout) = timeout_duration(timeout)? {
            request = request.timeout(timeout);
        }
        request = request.headers(headers);
        if expect_100 {
            request = request.header(EXPECT, "100-continue");
        }

        let mut body = if let Some(json_body) = json_body {
            let body = serde_json::to_vec(json_body)
                .map_err(|error| RpcError::failure(error.to_string()))?;
            if !has_content_type && !skip_content_type {
                request = request.header(CONTENT_TYPE, "application/json");
            }
            Some(body)
        } else if let Some(data) = data {
            match data {
                Value::String(body) => Some(body.as_bytes().to_vec()),
                Value::Object(_) | Value::Array(_) => {
                    let pairs = form_pairs(data)?;
                    if !has_content_type && !skip_content_type {
                        request = request.header(CONTENT_TYPE, "application/x-www-form-urlencoded");
                    }
                    Some(encode_form_pairs(&pairs).into_bytes())
                }
                _ => {
                    return Err(RpcError::invalid(
                        "http_request data must be a string, object, or pair array",
                    ));
                }
            }
        } else {
            None
        };
        if let Some(encoding) = request_compression(options.get("compress"))?
            && body.as_ref().is_some_and(|body| !body.is_empty())
        {
            if has_content_encoding {
                return Err(RpcError::invalid(
                    "http_request compress cannot be combined with Content-Encoding",
                ));
            }
            if has_content_length {
                return Err(RpcError::invalid(
                    "http_request chunked cannot be set if Content-Length is set",
                ));
            }
            let content_encoding = match encoding {
                RequestCompression::Gzip => "gzip",
                RequestCompression::Deflate => "deflate",
            };
            body = Some(compress_request_body(body.as_deref().unwrap(), encoding)?);
            request = request.header(CONTENT_ENCODING, content_encoding);
            chunked = true;
        }
        if chunked {
            if has_content_length {
                return Err(RpcError::invalid(
                    "http_request chunked cannot be set if Content-Length is set",
                ));
            }
            if transfer_encoding_chunked {
                return Err(RpcError::invalid(
                    "http_request chunked cannot be set if Transfer-Encoding: chunked is set",
                ));
            }
            let body = body.unwrap_or_default();
            let body =
                reqwest::Body::wrap_stream(futures_util::stream::iter([Ok::<_, io::Error>(body)]));
            request = request.body(body);
        } else if let Some(body) = body {
            request = request.body(body);
        }

        let response = request
            .send()
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        let status = response.status();
        if raise_for_status && status.is_server_error()
            || raise_for_status && status.is_client_error()
        {
            return Err(RpcError::failure(format!(
                "{status}, message='{}', url='{}'",
                status.canonical_reason().unwrap_or(""),
                response.url()
            )));
        }
        let status_code = status.as_u16();
        let content_type = response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let content_encoding = response
            .headers()
            .get(CONTENT_ENCODING)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let mut response_headers = BTreeMap::new();
        for (name, value) in response.headers() {
            response_headers.insert(
                name.as_str().to_owned(),
                String::from_utf8_lossy(value.as_bytes()).into_owned(),
            );
        }
        let bytes = response
            .bytes()
            .await
            .map_err(|error| RpcError::failure(error.to_string()))?;
        let decompressed;
        let body_bytes = if manual_decompress {
            match content_encoding.as_deref() {
                Some(encoding) => {
                    decompressed = decompress_response_body(&bytes, encoding)?;
                    decompressed.as_slice()
                }
                None => bytes.as_ref(),
            }
        } else {
            bytes.as_ref()
        };
        let body = decode_response_body(body_bytes, content_type.as_deref())?;
        Ok(json!({"status": status_code, "headers": response_headers, "body": body}))
    }

    async fn enable_rdt(&self, backend: &Backend) -> Result<Value, RpcError> {
        let _lifecycle = self.rdt_lifecycle.lock().await;
        let mut cancelled = self.shutdown_tx.subscribe();
        if *cancelled.borrow() {
            return Err(RpcError::failure("utilities are shutting down"));
        }
        let proxy_result = self.stop_rdt_proxy().await;
        let page_result = self.clear_active_rdt_document(backend).await;
        if let Err(error) = proxy_result {
            return match page_result {
                Ok(()) => Err(error),
                Err(page_error) => Err(RpcError::failure(format!(
                    "{}; additionally failed to clear previous RDT document: {}",
                    rpc_error_message(&error),
                    rpc_error_message(&page_error)
                ))),
            };
        }
        page_result?;

        let ip = tokio::select! {
            changed = cancelled.changed() => {
                let _ = changed;
                return Err(RpcError::failure("RDT setup cancelled during shutdown"));
            }
            ip = backend.setting("developer.rdt.ip", Value::Null) => ip,
        };
        let Some(ip) = ip.as_str() else {
            if ip.is_null() {
                return Ok(Value::Null);
            }
            return Err(RpcError::invalid("developer.rdt.ip must be a string"));
        };

        let response = tokio::select! {
            changed = cancelled.changed() => {
                let _ = changed;
                return Err(RpcError::failure("RDT setup cancelled during shutdown"));
            }
            response = self.legacy_client
                .get(format!("http://{ip}:{RDT_PORT}"))
                .timeout(Duration::from_secs(300))
                .send() => {
                    response.map_err(|error| RpcError::failure(format!("failed to connect to React DevTools at {ip}: {error}")))?
                }
        };
        if response.status() != reqwest::StatusCode::OK {
            return Err(RpcError::failure(format!(
                "React DevTools at {ip} returned HTTP {}",
                response.status()
            )));
        }
        let source = tokio::select! {
            changed = cancelled.changed() => {
                let _ = changed;
                return Err(RpcError::failure("RDT setup cancelled during shutdown"));
            }
            source = async {
                let content_type = response
                    .headers()
                    .get(CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok())
                    .map(str::to_owned);
                let bytes = response
                    .bytes()
                    .await
                    .map_err(|error| RpcError::failure(error.to_string()))?;
                decode_response_body(&bytes, content_type.as_deref())
            } => source?,
        };
        let marker = Uuid::new_v4().to_string();
        let marker_assignment = format!(
            "window[{}] = {};",
            json!(RDT_DOCUMENT_MARKER),
            json!(marker),
        );
        let script = format!(
            "{marker_assignment}\n\
             if (!window.deckyHasConnectedRDT) {{\n\
                 window.deckyHasConnectedRDT = true;\n\
                 Object.defineProperty(window, '__REACT_DEVTOOLS_TARGET_WINDOW__', {{\n\
                     enumerable: true,\n\
                     configurable: true,\n\
                     get: function() {{\n\
                         return (GamepadNavTree?.m_context?.m_controller || FocusNavController)?.m_ActiveContext?.ActiveWindow || window;\n\
                     }}\n\
                 }});\n\
                 {source}\n\
             }}"
        );
        self.start_rdt_proxy(ip.to_owned(), &mut cancelled).await?;

        let setup = async {
            let tab = tokio::select! {
                changed = cancelled.changed() => {
                    let _ = changed;
                    return Err(RpcError::failure("RDT setup cancelled during shutdown"));
                }
                tab = backend.cef.get_gamepadui_tab() => tab?,
            };
            tokio::select! {
                changed = cancelled.changed() => {
                    let _ = changed;
                    return Err(RpcError::failure("RDT setup cancelled during shutdown"));
                }
                result = backend.cef.close_old_tabs() => result?,
            }
            let document = RdtDocument {
                target: tab.clone(),
                marker,
            };
            *self.rdt_document.lock().await = Some(document.clone());
            backend
                .cef
                .reload_and_evaluate(&tab, &script, &mut cancelled)
                .await?;
            Ok(Value::Null)
        }
        .await;
        if let Err(error) = &setup {
            let proxy_result = self.stop_rdt_proxy().await;
            let page_result = self.clear_active_rdt_document(backend).await;
            if let Err(cleanup_error) = proxy_result {
                return Err(RpcError::failure(format!(
                    "{}; additionally failed to stop RDT proxy: {}",
                    rpc_error_message(error),
                    rpc_error_message(&cleanup_error)
                )));
            }
            if let Err(cleanup_error) = page_result {
                return Err(RpcError::failure(format!(
                    "{}; additionally failed to clear RDT document: {}",
                    rpc_error_message(error),
                    rpc_error_message(&cleanup_error)
                )));
            }
        }
        setup
    }

    async fn disable_rdt(&self, backend: &Backend) -> Result<Value, RpcError> {
        let _lifecycle = self.rdt_lifecycle.lock().await;
        let cancelled = self.shutdown_tx.subscribe();
        if *cancelled.borrow() {
            return Err(RpcError::failure("utilities are shutting down"));
        }
        let proxy_result = self.stop_rdt_proxy().await;
        let page_result = self.clear_active_rdt_document(backend).await;
        match (proxy_result, page_result) {
            (Ok(()), Ok(())) => Ok(Value::Null),
            (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
            (Err(proxy_error), Err(page_error)) => Err(RpcError::failure(format!(
                "{}; additionally failed to clear RDT document: {}",
                rpc_error_message(&proxy_error),
                rpc_error_message(&page_error)
            ))),
        }
    }

    async fn clear_active_rdt_document(&self, backend: &Backend) -> Result<(), RpcError> {
        let document = self.rdt_document.lock().await.clone();
        let Some(document) = document else {
            return Ok(());
        };
        clear_rdt_document(backend, &document).await?;
        let mut active = self.rdt_document.lock().await;
        if active
            .as_ref()
            .is_some_and(|active| active.marker == document.marker)
        {
            *active = None;
        }
        Ok(())
    }

    async fn start_rdt_proxy(
        &self,
        host: String,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<(), RpcError> {
        let listener = tokio::select! {
            changed = cancelled.changed() => {
                let _ = changed;
                return Err(RpcError::failure("RDT setup cancelled during shutdown"));
            }
            listener = TcpListener::bind((RDT_BIND, RDT_PORT)) => {
                listener.map_err(|error| RpcError::failure(format!("failed to bind RDT proxy: {error}")))?
            }
        };
        if *cancelled.borrow() {
            return Err(RpcError::failure("RDT setup cancelled during shutdown"));
        }
        let (stop, stopped) = watch::channel(false);
        let task = tokio::spawn(run_rdt_proxy(listener, host, stopped));
        *self.rdt_proxy.lock().await = Some(RdtProxy { stop, task });
        Ok(())
    }

    async fn stop_rdt_proxy(&self) -> Result<(), RpcError> {
        let proxy = self.rdt_proxy.lock().await.take();
        let Some(proxy) = proxy else {
            return Ok(());
        };
        let _ = proxy.stop.send(true);
        match proxy.task.await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(error)) => Err(RpcError::failure(format!(
                "RDT proxy stopped with an error: {error}"
            ))),
            Err(error) => Err(RpcError::failure(format!("RDT proxy task failed: {error}"))),
        }
    }
}

impl Default for Utilities {
    fn default() -> Self {
        Self::new()
    }
}

fn legacy_args(method: &str, kwargs: &Value) -> Result<Vec<Value>, String> {
    let values = kwargs
        .as_object()
        .ok_or_else(|| "legacy utility arguments must be an object".to_owned())?;
    if method == "ping" {
        return Ok(Vec::new());
    }

    let (required, optional): (&[&str], &[(&str, Value)]) = match method {
        "http_request" => (
            &["method", "url"],
            &[("extra_opts", json!({})), ("timeout", Value::Null)],
        ),
        "execute_in_tab" => (&["tab", "run_async", "code"], &[]),
        "inject_css_into_tab" => (&["tab", "style"], &[]),
        "remove_css_from_tab" => (&["tab", "css_id"], &[]),
        "set_setting" => (&["key", "value"], &[]),
        "get_setting" => (&["key", "default"], &[]),
        "filepicker_ls" => (
            &[],
            &[
                ("path", Value::Null),
                ("include_files", json!(true)),
                ("include_folders", json!(true)),
                ("include_ext", Value::Null),
                ("include_hidden", json!(false)),
                ("order_by", json!("name_desc")),
                ("filter_for", Value::Null),
                ("page", json!(1)),
                ("max", json!(1000)),
            ],
        ),
        "get_tab_id" => (&["name"], &[]),
        "get_user_info" => (&[], &[]),
        _ => return Err(format!("'{method}'")),
    };
    let allowed = required
        .iter()
        .copied()
        .chain(optional.iter().map(|(name, _)| *name))
        .collect::<Vec<_>>();
    if let Some(extra) = values.keys().find(|key| !allowed.contains(&key.as_str())) {
        return Err(format!("unexpected keyword argument '{extra}'"));
    }
    let mut args = Vec::with_capacity(required.len() + optional.len());
    for name in required {
        args.push(
            values
                .get(*name)
                .cloned()
                .ok_or_else(|| format!("{method}() missing required argument '{name}'"))?,
        );
    }
    for (name, default) in optional {
        args.push(
            values
                .get(*name)
                .cloned()
                .unwrap_or_else(|| default.clone()),
        );
    }
    Ok(args)
}

fn parse_filepicker_options(args: &[Value]) -> Result<FilePickerOptions, RpcError> {
    if args.len() > 9 {
        return Err(RpcError::invalid(format!(
            "filepicker_ls expects at most 9 arguments, received {}",
            args.len()
        )));
    }
    let path = match args.first() {
        None | Some(Value::Null) => None,
        Some(Value::String(path)) => Some(path.clone()),
        Some(_) => return Err(RpcError::invalid("argument 0 must be a string or null")),
    };
    let include_files = args.get(1).is_none_or(python_truthy);
    let include_folders = args.get(2).is_none_or(python_truthy);
    let include_ext = match args.get(3) {
        None | Some(Value::Null) => None,
        Some(Value::Array(values)) => Some(
            values
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| RpcError::invalid("include_ext values must be strings"))
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        Some(_) => return Err(RpcError::invalid("include_ext must be an array or null")),
    };
    let include_hidden = args.get(4).is_some_and(python_truthy);
    let order_by = match args.get(5) {
        None => "name_desc".to_owned(),
        Some(Value::String(value)) => value.clone(),
        Some(_) => return Err(RpcError::invalid("order_by must be a string")),
    };
    let filter_for = match args.get(6) {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) => Some(value.clone()),
        Some(Value::Object(value)) => value
            .get("source")
            .or_else(|| value.get("pattern"))
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| RpcError::invalid("filter_for must be a string pattern"))
            .map(Some)?,
        Some(_) => return Err(RpcError::invalid("filter_for must be a string pattern")),
    };
    let page = args.get(7).map_or(Ok(1), parse_integer)?;
    let max = args.get(8).map_or(Ok(1000), parse_integer)?;
    Ok(FilePickerOptions {
        path,
        include_files,
        include_folders,
        include_ext,
        include_hidden,
        order_by,
        filter_for,
        page,
        max,
    })
}

fn filepicker_listing(home: &str, options: FilePickerOptions) -> Result<Value, RpcError> {
    let requested_path = options.path.as_deref().unwrap_or(home);
    let scan_path = if requested_path.is_empty() {
        Path::new(".")
    } else {
        Path::new(requested_path)
    };
    let resolved = scan_path
        .canonicalize()
        .map_err(|error| filepicker_io_error(error, requested_path))?;
    let entries =
        std::fs::read_dir(&resolved).map_err(|error| filepicker_io_error(error, requested_path))?;
    let filter = options.filter_for.as_deref().map(compile_filter);
    let order = parse_order(&options.order_by)?;
    let mut folders = Vec::new();
    let mut files = Vec::new();

    for entry in entries {
        let entry = entry.map_err(|error| filepicker_io_error(error, requested_path))?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let metadata = match path.metadata() {
            Ok(metadata) => metadata,
            Err(_) => continue,
        };
        let is_hidden = name.starts_with('.');
        let is_dir = metadata.is_dir();
        let size = metadata.len();
        let (modified, created) = metadata_times(&metadata);
        let record = FileEntry {
            sort_name: name.as_str().case_fold().collect(),
            name,
            path,
            is_dir,
            size,
            modified,
            created,
        };
        if is_hidden && !options.include_hidden {
            continue;
        }
        if options.include_folders && is_dir {
            folders.push(record);
        } else if options.include_files
            && extension_matches(&record.name, options.include_ext.as_deref())
        {
            if let Some(filter) = &filter
                && !filter_matches(filter, &record.name)?
            {
                continue;
            }
            files.push(FileEntry {
                is_dir: false,
                ..record
            });
        }
    }

    sort_entries(&mut files, order.files);
    sort_entries(&mut folders, order.folders);
    let total = folders.len() + files.len();
    let bounds = python_slice_bounds(total, options.page, options.max);
    let page = folders
        .into_iter()
        .chain(files)
        .skip(bounds.start)
        .take(bounds.end - bounds.start)
        .map(|entry| {
            json!({
                "isdir": entry.is_dir,
                "name": entry.name,
                "realpath": entry.path.to_string_lossy(),
                "size": entry.size,
                "modified": entry.modified,
                "created": entry.created,
            })
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "realpath": requested_path,
        "files": page,
        "total": total,
    }))
}

#[derive(Clone, Copy)]
struct FileOrder {
    by: FileOrderBy,
    descending: bool,
}

#[derive(Clone, Copy)]
enum FileOrderBy {
    Name,
    Modified,
    Created,
    Size,
}

struct FileOrderPair {
    files: FileOrder,
    folders: FileOrder,
}

fn parse_order(order_by: &str) -> Result<FileOrderPair, RpcError> {
    let mut pieces = order_by.split('_');
    let field = pieces.next().unwrap_or_default();
    let direction = pieces
        .next()
        .ok_or_else(|| RpcError::failure("order_by must include a direction"))?;
    let reversed = direction == "asc";
    let (by, descending) = match field {
        "modified" => (FileOrderBy::Modified, !reversed),
        "created" => (FileOrderBy::Created, !reversed),
        "size" => (FileOrderBy::Size, !reversed),
        "name" => (FileOrderBy::Name, reversed),
        _ => (FileOrderBy::Name, reversed),
    };
    Ok(FileOrderPair {
        files: FileOrder { by, descending },
        folders: if matches!(by, FileOrderBy::Size) {
            FileOrder {
                by: FileOrderBy::Name,
                descending: false,
            }
        } else {
            FileOrder { by, descending }
        },
    })
}

fn sort_entries(entries: &mut [FileEntry], order: FileOrder) {
    entries.sort_by(|left, right| {
        let compared = match order.by {
            FileOrderBy::Name => left.sort_name.cmp(&right.sort_name),
            FileOrderBy::Modified => left.modified.total_cmp(&right.modified),
            FileOrderBy::Created => left.created.total_cmp(&right.created),
            FileOrderBy::Size => left.size.cmp(&right.size),
        };
        if order.descending {
            compared.reverse()
        } else {
            compared
        }
    });
}

fn extension_matches(name: &str, include_ext: Option<&[String]>) -> bool {
    let Some(extensions) = include_ext.filter(|extensions| !extensions.is_empty()) else {
        return true;
    };
    if extensions.iter().any(|extension| extension == "all_files") {
        return true;
    }
    let actual = Path::new(name)
        .extension()
        .unwrap_or_default()
        .to_string_lossy()
        .to_uppercase();
    extensions
        .iter()
        .any(|candidate| candidate.to_uppercase() == actual)
}

enum FilenameFilter {
    Regex(Regex, String),
    Literal(String),
}

fn compile_filter(pattern: &str) -> FilenameFilter {
    match Regex::new(pattern) {
        Ok(regex) => FilenameFilter::Regex(regex, pattern.to_owned()),
        Err(_) => FilenameFilter::Literal(pattern.to_owned()),
    }
}

fn filter_matches(filter: &FilenameFilter, name: &str) -> Result<bool, RpcError> {
    match filter {
        FilenameFilter::Regex(regex, pattern) => Ok(regex
            .is_match(name)
            .unwrap_or_else(|_| name.contains(pattern))),
        FilenameFilter::Literal(pattern) => Ok(name.contains(pattern)),
    }
}

fn metadata_times(metadata: &std::fs::Metadata) -> (f64, f64) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        (
            metadata.mtime() as f64 + metadata.mtime_nsec() as f64 / 1_000_000_000.0,
            metadata.ctime() as f64 + metadata.ctime_nsec() as f64 / 1_000_000_000.0,
        )
    }
    #[cfg(not(unix))]
    {
        let modified = metadata
            .modified()
            .ok()
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0.0, |duration| duration.as_secs_f64());
        let created = metadata
            .created()
            .ok()
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0.0, |duration| duration.as_secs_f64());
        (modified, created)
    }
}

fn filepicker_io_error(error: io::Error, path: &str) -> RpcError {
    match error.kind() {
        io::ErrorKind::NotFound => {
            RpcError::failure(format!("[Errno 2] No such file or directory: '{path}'"))
        }
        io::ErrorKind::PermissionDenied => {
            RpcError::failure(format!("[Errno 13] Permission denied: '{path}'"))
        }
        _ if error.raw_os_error() == Some(20) => {
            RpcError::failure(format!("[Errno 20] Not a directory: '{path}'"))
        }
        _ => RpcError::failure(error.to_string()),
    }
}

fn python_slice_bounds(length: usize, page: i128, max: i128) -> std::ops::Range<usize> {
    let length = length as i128;
    let start = page.saturating_sub(1).saturating_mul(max);
    let end = page.saturating_mul(max);
    let normalize = |index: i128| {
        if index < 0 {
            length.saturating_add(index).max(0)
        } else {
            index.min(length)
        }
    };
    let start = normalize(start);
    let end = normalize(end).max(start);
    start as usize..end as usize
}

fn parse_integer(value: &Value) -> Result<i128, RpcError> {
    if let Some(value) = value.as_i64() {
        Ok(value as i128)
    } else if let Some(value) = value.as_u64() {
        Ok(value as i128)
    } else if let Some(value) = value.as_bool() {
        Ok(i128::from(value))
    } else {
        Err(RpcError::invalid("page and max must be integers"))
    }
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(_) | Value::Object(_) => true,
    }
}

fn option_bool(options: &Map<String, Value>, key: &str, default: bool) -> Result<bool, RpcError> {
    match options.get(key) {
        None => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        Some(Value::Null) => Ok(false),
        Some(_) => Err(RpcError::invalid(format!(
            "http_request {key} must be a boolean"
        ))),
    }
}

fn option_optional_bool(
    options: &Map<String, Value>,
    key: &str,
    default: bool,
) -> Result<bool, RpcError> {
    match options.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        Some(_) => Err(RpcError::invalid(format!(
            "http_request {key} must be a boolean"
        ))),
    }
}

fn max_redirects(value: Option<&Value>) -> Result<usize, RpcError> {
    let value = match value {
        None => return Ok(10),
        Some(Value::Bool(value)) => i128::from(*value),
        Some(Value::Number(number)) => {
            if let Some(value) = number.as_i64() {
                i128::from(value)
            } else if let Some(value) = number.as_u64() {
                i128::from(value)
            } else {
                return Err(RpcError::invalid(
                    "http_request max_redirects must be an integer",
                ));
            }
        }
        Some(_) => {
            return Err(RpcError::invalid(
                "http_request max_redirects must be an integer",
            ));
        }
    };
    usize::try_from(value)
        .map_err(|_| RpcError::invalid("http_request max_redirects must be non-negative"))
}

fn parse_skip_auto_headers(value: Option<&Value>) -> Result<HashSet<HeaderName>, RpcError> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(HashSet::new());
    };
    let names: Vec<&str> = match value {
        Value::Array(values) => values
            .iter()
            .map(|value| {
                value.as_str().ok_or_else(|| {
                    RpcError::invalid("http_request skip_auto_headers values must be strings")
                })
            })
            .collect::<Result<_, _>>()?,
        Value::Object(values) => values.keys().map(String::as_str).collect(),
        _ => {
            return Err(RpcError::invalid(
                "http_request skip_auto_headers must be an array or object",
            ));
        }
    };
    names
        .into_iter()
        .map(|name| {
            HeaderName::from_bytes(name.as_bytes())
                .map_err(|error| RpcError::invalid(error.to_string()))
        })
        .collect()
}

fn timeout_duration(value: Option<&Value>) -> Result<Option<Duration>, RpcError> {
    match value {
        None | Some(Value::Null) => Ok(Some(Duration::from_secs(300))),
        Some(Value::Number(number)) => {
            let seconds = number.as_f64().ok_or_else(|| {
                RpcError::invalid("http_request timeout must be a number or null")
            })?;
            if !seconds.is_finite() {
                return Err(RpcError::invalid("http_request timeout must be finite"));
            }
            if seconds <= 0.0 {
                return Ok(None);
            }
            Duration::try_from_secs_f64(seconds)
                .map(Some)
                .map_err(|error| RpcError::invalid(error.to_string()))
        }
        Some(_) => Err(RpcError::invalid(
            "http_request timeout must be a number or null",
        )),
    }
}

fn parse_headers(value: &Value) -> Result<HeaderMap, RpcError> {
    let mut parsed = HeaderMap::new();
    match value {
        Value::Null => {}
        Value::Object(headers) => {
            for (name, value) in headers {
                append_header(&mut parsed, name, value)?;
            }
        }
        Value::Array(headers) => {
            for pair in headers {
                let pair = pair
                    .as_array()
                    .filter(|pair| pair.len() == 2)
                    .ok_or_else(|| {
                        RpcError::invalid(
                            "http_request headers arrays must contain name/value pairs",
                        )
                    })?;
                let name = pair[0].as_str().ok_or_else(|| {
                    RpcError::invalid("http_request header names must be strings")
                })?;
                append_header(&mut parsed, name, &pair[1])?;
            }
        }
        _ => {
            return Err(RpcError::invalid(
                "http_request headers must be an object or array",
            ));
        }
    }
    Ok(parsed)
}

fn append_header(headers: &mut HeaderMap, name: &str, value: &Value) -> Result<(), RpcError> {
    let name = HeaderName::from_bytes(name.as_bytes())
        .map_err(|error| RpcError::invalid(error.to_string()))?;
    let value = value
        .as_str()
        .ok_or_else(|| RpcError::invalid("http_request header values must be strings"))?;
    let value =
        HeaderValue::from_str(value).map_err(|error| RpcError::invalid(error.to_string()))?;
    headers.append(name, value);
    Ok(())
}

fn query_pairs(value: &Value) -> Result<Vec<(String, String)>, RpcError> {
    match value {
        Value::Null => Ok(Vec::new()),
        Value::Object(values) => {
            let mut pairs = Vec::new();
            for (key, value) in values {
                match value {
                    Value::Array(values) => {
                        for value in values {
                            pairs.push((key.clone(), query_value_string(value)?));
                        }
                    }
                    value => pairs.push((key.clone(), query_value_string(value)?)),
                }
            }
            Ok(pairs)
        }
        Value::Array(values) => values
            .iter()
            .map(|value| {
                let pair = value
                    .as_array()
                    .filter(|pair| pair.len() == 2)
                    .ok_or_else(|| {
                        RpcError::invalid("http_request params arrays must contain key/value pairs")
                    })?;
                let key = pair[0]
                    .as_str()
                    .ok_or_else(|| RpcError::invalid("http_request params keys must be strings"))?;
                Ok((key.to_owned(), query_value_string(&pair[1])?))
            })
            .collect(),
        _ => Err(RpcError::invalid(
            "http_request params must be an object, array, or null",
        )),
    }
}

fn query_value_string(value: &Value) -> Result<String, RpcError> {
    match value {
        Value::String(value) => Ok(value.clone()),
        Value::Number(value) => Ok(value.to_string()),
        _ => Err(RpcError::invalid(
            "http_request query values must be strings or numbers",
        )),
    }
}

fn extend_raw_query(url: &mut reqwest::Url, query: &str) {
    let existing = url.query().unwrap_or_default();
    let mut combined = String::with_capacity(existing.len() + query.len() + 1);
    combined.push_str(existing);
    if !existing.is_empty() && !existing.ends_with('&') {
        combined.push('&');
    }
    combined.push_str(query);
    url.set_query(Some(&combined));
}

fn extend_query_pairs(url: &mut reqwest::Url, pairs: Vec<(String, String)>) {
    let existing = url.query().unwrap_or_default();
    let mut combined = String::from(existing);
    for (key, value) in pairs {
        if !combined.is_empty() && !combined.ends_with('&') {
            combined.push('&');
        }
        push_query_component(&mut combined, &key);
        combined.push('=');
        push_query_component(&mut combined, &value);
    }
    url.set_query(Some(&combined));
}

fn push_query_component(query: &mut String, value: &str) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric()
            || matches!(byte, b'-' | b'.' | b'_' | b'~' | b'?' | b'/' | b':' | b'@')
        {
            query.push(char::from(byte));
        } else {
            query.push('%');
            query.push(char::from(HEX[(byte >> 4) as usize]));
            query.push(char::from(HEX[(byte & 0x0f) as usize]));
        }
    }
}

fn form_pairs(value: &Value) -> Result<Vec<(String, String)>, RpcError> {
    match value {
        Value::Object(values) => Ok(values
            .iter()
            .flat_map(|(key, value)| value_pairs(key, value))
            .collect()),
        Value::Array(values) => values
            .iter()
            .map(|value| {
                let pair = value
                    .as_array()
                    .filter(|pair| pair.len() == 2)
                    .ok_or_else(|| {
                        RpcError::invalid("http_request data arrays must contain key/value pairs")
                    })?;
                Ok((python_value_string(&pair[0]), python_value_string(&pair[1])))
            })
            .collect(),
        _ => Err(RpcError::invalid(
            "http_request form data must be an object or pair array",
        )),
    }
}

fn value_pairs(key: &str, value: &Value) -> Vec<(String, String)> {
    match value {
        Value::Array(values) => values
            .iter()
            .map(|value| (key.to_owned(), python_value_string(value)))
            .collect(),
        value => vec![(key.to_owned(), python_value_string(value))],
    }
}

fn encode_form_pairs(pairs: &[(String, String)]) -> String {
    let mut url =
        reqwest::Url::parse("http://localhost").expect("static form encoding base URL is valid");
    url.query_pairs_mut()
        .extend_pairs(pairs.iter().map(|(key, value)| (key, value)));
    url.query().unwrap_or_default().to_owned()
}

fn cookie_pairs(value: &Value) -> Result<Vec<(String, String)>, RpcError> {
    match value {
        Value::Object(cookies) => Ok(cookies
            .iter()
            .map(|(key, value)| (key.clone(), python_value_string(value)))
            .collect()),
        Value::Array(cookies) => cookies
            .iter()
            .map(|value| {
                let pair = value
                    .as_array()
                    .filter(|pair| pair.len() == 2)
                    .ok_or_else(|| {
                        RpcError::invalid(
                            "http_request cookies arrays must contain name/value pairs",
                        )
                    })?;
                Ok((python_value_string(&pair[0]), python_value_string(&pair[1])))
            })
            .collect(),
        _ => Err(RpcError::invalid(
            "http_request cookies must be an object, array, or null",
        )),
    }
}

#[derive(Clone, Copy)]
enum RequestCompression {
    Gzip,
    Deflate,
}

fn request_compression(value: Option<&Value>) -> Result<Option<RequestCompression>, RpcError> {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Ok(None),
        Some(Value::Bool(true)) => Ok(Some(RequestCompression::Deflate)),
        Some(Value::String(encoding)) if encoding == "gzip" => Ok(Some(RequestCompression::Gzip)),
        Some(Value::String(encoding)) if encoding == "deflate" => {
            Ok(Some(RequestCompression::Deflate))
        }
        Some(_) => Err(RpcError::invalid(
            "http_request compress must be false, true, 'gzip', 'deflate', or null",
        )),
    }
}

fn compress_request_body(body: &[u8], encoding: RequestCompression) -> Result<Vec<u8>, RpcError> {
    match encoding {
        RequestCompression::Gzip => {
            let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
            encoder
                .write_all(body)
                .map_err(|error| RpcError::failure(error.to_string()))?;
            encoder
                .finish()
                .map_err(|error| RpcError::failure(error.to_string()))
        }
        RequestCompression::Deflate => {
            let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
            encoder
                .write_all(body)
                .map_err(|error| RpcError::failure(error.to_string()))?;
            encoder
                .finish()
                .map_err(|error| RpcError::failure(error.to_string()))
        }
    }
}

fn decompress_response_body(body: &[u8], content_encoding: &str) -> Result<Vec<u8>, RpcError> {
    let encodings = content_encoding
        .split(',')
        .map(str::trim)
        .filter(|encoding| !encoding.is_empty())
        .collect::<Vec<_>>();
    let mut decoded = None;
    for encoding in encodings.into_iter().rev() {
        let input = decoded.as_deref().unwrap_or(body);
        let mut output = Vec::new();
        match encoding.to_ascii_lowercase().as_str() {
            "identity" => output.extend_from_slice(input),
            "gzip" | "x-gzip" => {
                flate2::read::GzDecoder::new(input)
                    .read_to_end(&mut output)
                    .map_err(|error| RpcError::failure(error.to_string()))?;
            }
            "deflate" => {
                if flate2::read::ZlibDecoder::new(input)
                    .read_to_end(&mut output)
                    .is_err()
                {
                    output.clear();
                    flate2::read::DeflateDecoder::new(input)
                        .read_to_end(&mut output)
                        .map_err(|error| RpcError::failure(error.to_string()))?;
                }
            }
            "br" => {
                brotli::Decompressor::new(input, 4096)
                    .read_to_end(&mut output)
                    .map_err(|error| RpcError::failure(error.to_string()))?;
            }
            _ => {
                return Err(RpcError::failure(format!(
                    "unsupported response Content-Encoding '{encoding}'"
                )));
            }
        }
        decoded = Some(output);
    }
    Ok(decoded.unwrap_or_else(|| body.to_vec()))
}

fn decode_response_body(body: &[u8], content_type: Option<&str>) -> Result<String, RpcError> {
    let charset = content_type
        .and_then(|content_type| {
            content_type.split(';').skip(1).find_map(|parameter| {
                let (name, value) = parameter.trim().split_once('=')?;
                name.trim().eq_ignore_ascii_case("charset").then(|| {
                    value
                        .trim()
                        .trim_matches(|character| character == '"' || character == '\'')
                })
            })
        })
        .unwrap_or("utf-8");
    let encoding = Encoding::for_label(charset.as_bytes())
        .ok_or_else(|| RpcError::failure(format!("unknown response charset '{charset}'")))?;
    let (text, had_errors) = encoding.decode_without_bom_handling(body);
    if had_errors {
        return Err(RpcError::failure(format!(
            "response body contains invalid {charset} data"
        )));
    }
    Ok(text.into_owned())
}

fn python_value_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        _ => value.to_string(),
    }
}

fn rpc_error_message(error: &RpcError) -> String {
    match error {
        RpcError::Error { message, .. } => message.clone(),
        RpcError::Stopped => "backend stopped".to_owned(),
    }
}

async fn clear_rdt_document(backend: &Backend, document: &RdtDocument) -> Result<bool, RpcError> {
    let expression = format!(
        "(() => {{\n\
             if (window[{}] !== {}) return false;\n\
             try {{ delete window.deckyHasConnectedRDT; }} catch (_) {{}}\n\
             try {{ delete window.__REACT_DEVTOOLS_TARGET_WINDOW__; }} catch (_) {{}}\n\
             try {{ delete window[{}]; }} catch (_) {{}}\n\
             window.location.reload();\n\
             return true;\n\
         }})()",
        json!(RDT_DOCUMENT_MARKER),
        json!(document.marker),
        json!(RDT_DOCUMENT_MARKER),
    );
    timeout(RDT_CLEANUP_TIMEOUT, async {
        let result = backend
            .cef
            .evaluate_raw(&document.target, &expression, false)
            .await?;
        if let Some(error) = result.get("exceptionDetails") {
            return Err(RpcError::failure(error.to_string()));
        }
        let owned = result
            .get("result")
            .and_then(|result| result.get("value"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if !owned {
            return Ok(false);
        }
        Ok(true)
    })
    .await
    .map_err(|_| RpcError::failure("RDT page cleanup timed out"))?
}

fn service_manager() -> String {
    if let Some(manager) = std::env::var_os("LINUX_SERVICE_MANAGER")
        && let Ok(manager) = manager.into_string()
    {
        return manager;
    }
    std::fs::canonicalize("/sbin/init")
        .ok()
        .and_then(|path| {
            path.file_name()
                .map(|name| name.to_string_lossy().into_owned())
        })
        .map(|name| {
            if name == "openrc-init" {
                "openrc".to_owned()
            } else if name != "init" {
                name
            } else {
                "no_init".to_owned()
            }
        })
        .unwrap_or_else(|| "systemd".to_owned())
}

async fn service_active(unit: &str, manager: &str) -> Result<bool, RpcError> {
    let output = match manager {
        "systemd" => run_command("systemctl", &["is-active", unit]).await?,
        "openrc" => openrc_command(unit, "status").await?,
        _ => return Ok(true),
    };
    Ok(output.status.success())
}

async fn openrc_command(unit: &str, action: &str) -> Result<Output, RpcError> {
    let mut args = Vec::with_capacity(3);
    if unsafe { libc::geteuid() } != 0 {
        args.push("--user".to_owned());
    }
    args.push(unit.to_owned());
    args.push(action.to_owned());
    let args = args.iter().map(String::as_str).collect::<Vec<_>>();
    run_command("rc-service", &args).await
}

async fn service_start(unit: &str) -> Result<bool, RpcError> {
    let manager = service_manager();
    if service_active(unit, &manager).await? {
        return Ok(true);
    }
    let started = match manager.as_str() {
        "systemd" => run_command("systemctl", &["start", unit]).await?,
        "openrc" => openrc_command(unit, "start").await?,
        _ => return Ok(true),
    };
    if !started.status.success() {
        return Err(command_error("service start", unit, &started));
    }
    Ok(true)
}

async fn service_stop(unit: &str) -> Result<bool, RpcError> {
    let manager = service_manager();
    if manager != "systemd" && manager != "openrc" {
        return Ok(true);
    }
    if manager == "systemd" {
        let active = run_command("systemctl", &["is-active", unit]).await?;
        if !active.status.success() {
            if active.status.code() == Some(3)
                && String::from_utf8_lossy(&active.stdout).trim() == "inactive"
            {
                return Ok(true);
            }
            return Err(command_error("systemctl is-active", unit, &active));
        }
    } else if !service_active(unit, &manager).await? {
        return Ok(true);
    }
    let stopped = match manager.as_str() {
        "systemd" => run_command("systemctl", &["stop", unit]).await?,
        "openrc" => openrc_command(unit, "stop").await?,
        _ => unreachable!("unsupported managers returned above"),
    };
    if !stopped.status.success() {
        return Err(command_error("service stop", unit, &stopped));
    }
    Ok(true)
}

async fn restart_webhelper() -> Result<bool, RpcError> {
    let output = run_command("killall", &["-s", "SIGTERM", "steamwebhelper"]).await?;
    if !output.status.success() {
        return Err(command_error(
            "killall -s SIGTERM",
            "steamwebhelper",
            &output,
        ));
    }
    Ok(true)
}

async fn close_cef_socket() -> Result<(), RpcError> {
    if let Some(enabled) = std::env::var_os("USE_CEF_CLOSE_WORKAROUND")
        && enabled.as_os_str() != std::ffi::OsStr::new("1")
    {
        return Ok(());
    }
    // The original Linux workaround requires Decky to run as root to inject
    // close(2) into steamwebhelper with gdb.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("Can't close CEF socket because Decky isn't running as root.");
        return Ok(());
    }
    let _lock = CEF_SOCKET_CLOSE_LOCK.lock().await;
    let output = run_command("lsof", &["-F", "-iTCP:8080", "-sTCP:LISTEN"]).await?;
    if !output.status.success() || output.stdout.is_empty() {
        return Err(command_error("lsof", "TCP port 8080 listener", &output));
    }
    let listing = String::from_utf8_lossy(&output.stdout);
    let (pid, fd) = parse_cef_listener(&listing).ok_or_else(|| {
        RpcError::failure("lsof found no steamwebhelper CEF listener on port 8080")
    })?;
    let eval = format!("call (int)close({fd})");
    let output = run_command(
        "gdb",
        &["--nx", "-p", &pid, "--batch", "--eval-command", &eval],
    )
    .await?;
    if !output.status.success() {
        return Err(command_error(
            "gdb close",
            &format!("PID {pid}, FD {fd}"),
            &output,
        ));
    }
    Ok(())
}

fn parse_cef_listener(listing: &str) -> Option<(String, String)> {
    let mut pid = None;
    let mut command = None;
    let mut fd = None;
    let mut listening = false;
    for field in listing.lines() {
        if let Some(value) = field.strip_prefix('p') {
            let digits = value
                .chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>();
            pid = (!digits.is_empty()).then_some(digits);
            command = None;
            fd = None;
            listening = false;
        } else if let Some(value) = field.strip_prefix('c') {
            command = Some(value);
        } else if let Some(value) = field.strip_prefix('f') {
            let digits = value
                .chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>();
            fd = (!digits.is_empty()).then_some(digits);
        } else if field == "TST=LISTEN" {
            listening = true;
        }
        if command == Some("steamwebhelper")
            && listening
            && let (Some(pid), Some(fd)) = (&pid, &fd)
        {
            return Some((pid.clone(), fd.clone()));
        }
    }
    None
}

async fn run_command(program: &str, args: &[&str]) -> Result<Output, RpcError> {
    Command::new(program)
        .args(args)
        .env("LD_LIBRARY_PATH", "")
        .output()
        .await
        .map_err(|error| RpcError::failure(format!("failed to execute {program}: {error}")))
}

fn command_error(action: &str, target: &str, output: &Output) -> RpcError {
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
    let detail = match (stdout.is_empty(), stderr.is_empty()) {
        (false, false) => format!("{stdout}; {stderr}"),
        (false, true) => stdout,
        (true, false) => stderr,
        (true, true) => "no command output".to_owned(),
    };
    RpcError::failure(format!(
        "{action} failed for {target} (exit {}): {detail}",
        output.status
    ))
}

async fn run_rdt_proxy(
    listener: TcpListener,
    host: String,
    mut stopped: watch::Receiver<bool>,
) -> Result<(), String> {
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            changed = stopped.changed() => {
                if changed.is_err() || *stopped.borrow() {
                    break;
                }
            }
            accepted = listener.accept() => {
                let (stream, _) = accepted.map_err(|error| format!("RDT proxy accept failed: {error}"))?;
                let host = host.clone();
                connections.spawn(async move { forward_rdt_connection(stream, host).await });
            }
            Some(joined) = connections.join_next(), if !connections.is_empty() => {
                if let Err(error) = joined {
                    eprintln!("RDT proxy connection task failed: {error}");
                }
            }
        }
    }
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    Ok(())
}

async fn forward_rdt_connection(mut local: TcpStream, host: String) {
    let mut remote = match TcpStream::connect((host.as_str(), RDT_PORT)).await {
        Ok(remote) => remote,
        Err(error) => {
            eprintln!("Failed to connect RDT proxy client to {host}:{RDT_PORT}: {error}");
            return;
        }
    };
    if let Err(error) = copy_bidirectional(&mut local, &mut remote).await {
        eprintln!("RDT proxy stream forwarding failed: {error}");
    }
}

#[cfg(test)]
mod tests {
    use super::{FilePickerOptions, filepicker_listing};
    use std::{fs, path::PathBuf};
    use uuid::Uuid;

    struct TestDir(PathBuf);

    impl TestDir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("decky-filepicker-{}", Uuid::new_v4()));
            fs::create_dir_all(&path).expect("create temporary file picker directory");
            Self(path)
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn filepicker_filters_extensions_hides_dotfiles_and_keeps_directories_first() {
        let temp = TestDir::new();
        fs::create_dir(temp.0.join("Folder")).unwrap();
        fs::write(temp.0.join("alpha.TXT"), b"a").unwrap();
        fs::write(temp.0.join("Beta.txt"), b"b").unwrap();
        fs::write(temp.0.join(".hidden.txt"), b"hidden").unwrap();
        fs::write(temp.0.join("ignored.md"), b"ignored").unwrap();

        let listing = filepicker_listing(
            "/unused-home",
            FilePickerOptions {
                path: Some(temp.0.to_string_lossy().into_owned()),
                include_files: true,
                include_folders: true,
                include_ext: Some(vec!["txt".to_owned()]),
                include_hidden: false,
                order_by: "name_desc".to_owned(),
                filter_for: None,
                page: 1,
                max: 10,
            },
        )
        .unwrap();
        let files = listing["files"].as_array().unwrap();
        assert_eq!(listing["total"], 3);
        assert_eq!(files[0]["name"], "Folder");
        assert_eq!(files[0]["isdir"], true);
        assert_eq!(files[1]["name"], "alpha.TXT");
        assert_eq!(files[2]["name"], "Beta.txt");
    }

    #[test]
    fn filepicker_invalid_regex_falls_back_to_literal_substring() {
        let temp = TestDir::new();
        fs::write(temp.0.join("[notes].txt"), b"x").unwrap();
        fs::write(temp.0.join("notes.txt"), b"y").unwrap();
        let listing = filepicker_listing(
            "/unused-home",
            FilePickerOptions {
                path: Some(temp.0.to_string_lossy().into_owned()),
                include_files: true,
                include_folders: false,
                include_ext: Some(vec!["txt".to_owned()]),
                include_hidden: false,
                order_by: "name_desc".to_owned(),
                filter_for: Some("[".to_owned()),
                page: 1,
                max: 10,
            },
        )
        .unwrap();
        assert_eq!(listing["total"], 1);
        assert_eq!(listing["files"][0]["name"], "[notes].txt");
    }
    #[test]
    fn filepicker_supports_regex_lookarounds_and_backreferences() {
        let temp = TestDir::new();
        fs::write(temp.0.join("abba.txt"), b"x").unwrap();
        fs::write(temp.0.join("other.txt"), b"y").unwrap();
        let listing = filepicker_listing(
            "/unused-home",
            FilePickerOptions {
                path: Some(temp.0.to_string_lossy().into_owned()),
                include_files: true,
                include_folders: false,
                include_ext: Some(vec!["txt".to_owned()]),
                include_hidden: false,
                order_by: "name_desc".to_owned(),
                filter_for: Some(r"(?<=a)(b)\1".to_owned()),
                page: 1,
                max: 10,
            },
        )
        .unwrap();
        assert_eq!(listing["total"], 1);
        assert_eq!(listing["files"][0]["name"], "abba.txt");
    }
}
