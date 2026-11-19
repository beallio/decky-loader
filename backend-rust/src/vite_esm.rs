use std::collections::HashSet;

/// Build the pre-document shim injected into Vite/Rolldown SharedJSContext pages before `library.js`
/// runs. Registers every instrumented ES module's top-level `var`/`function` bindings into
/// `globalThis.__DECKY_ESM__`, exposes a `window.webpackChunksteamui` compatibility view so
/// `@decky/ui` and existing plugins work unmodified, normalizes Oxc's backtick/let-destructuring
/// output in `Function.prototype.toString`, and installs an import map that routes all SteamUI
/// chunk imports through Decky's local `/steamui/` endpoint while pinning `library.js` back to
/// its original `steamloopback.host` URL to prevent duplicate module instances.
pub(crate) fn build_esm_shim(library_src: &str) -> String {
    let rel = library_src
        .strip_prefix("https://steamloopback.host")
        .unwrap_or(library_src);
    let rel = if rel.starts_with('/') {
        rel.to_owned()
    } else {
        format!("/{rel}")
    };
    let import_map = serde_json::json!({
        "imports": {
            "https://steamloopback.host/": "http://localhost:1337/steamui/",
            format!("http://localhost:1337/steamui{rel}"): format!("https://steamloopback.host{rel}"),
        }
    })
    .to_string();
    let import_map_literal = serde_json::to_string(&import_map).unwrap_or_else(|_| "{}".to_owned());
    VITE_ESM_SHIM_TEMPLATE.replace("__IMPORT_MAP_JSON__", &import_map_literal)
}

const VITE_ESM_SHIM_TEMPLATE: &str = r#"(() => {
  if (globalThis.__DECKY_ESM_SHIM_INSTALLED__) return;
  globalThis.__DECKY_ESM_SHIM_INSTALLED__ = true;

  const origToString = Function.prototype.toString;
  const simpleBacktick = /`([^`\\\n\r"]*?)`/g;
  const toStringCache = new WeakMap();
  Function.prototype.toString = function toString() {
    if (typeof this !== "function") return Object.prototype.toString.call(this);
    const raw = origToString.call(this);
    if (raw.indexOf("`") === -1 && raw.indexOf("let{") === -1) return raw;
    let cached = toStringCache.get(this);
    if (cached && cached[0] === raw) return cached[1];
    let norm = raw.replace(simpleBacktick, (m, inner) => (inner.includes("${") ? m : '"' + inner + '"'));
    norm = norm.replace(/\blet\{/g, "const{");
    try { toStringCache.set(this, [raw, norm]); } catch {}
    return norm;
  };

  const esm = (globalThis.__DECKY_ESM__ = new Map());
  const seenObjs = new WeakSet();
  const regEntry = (id, val) => {
    esm.set(id, val);
    try {
      const mods = window.DFL?.modules;
      if (mods && typeof mods.set === "function") mods.set(id, val);
    } catch {}
  };
  globalThis.__DECKY_ESM_REG__ = (chunkId, box) => {
    regEntry("esm:" + chunkId, box);
    for (const k of Object.keys(box)) {
      let v;
      try { v = box[k]; } catch { continue; }
      if (!v || typeof v !== "object" || Array.isArray(v) || v === window || v === document || v.$$typeof) continue;
      if (typeof Node === "function" && v instanceof Node) continue;
      if (typeof EventTarget === "function" && v instanceof EventTarget) continue;
      if (seenObjs.has(v)) continue;
      const proto = Object.getPrototypeOf(v);
      if (proto === Object.prototype || proto === null || v[Symbol.toStringTag] === "Module") {
        seenObjs.add(v);
        regEntry("esm:" + chunkId + ":" + k, v);
      }
    }
  };

  if (!window.webpackChunksteamui) {
    const req = (id) => esm.get(id);
    req.m = new Proxy({}, {
      ownKeys: () => [...esm.keys()],
      getOwnPropertyDescriptor: (_, k) => (esm.has(k) ? { enumerable: true, configurable: true, value: true } : undefined),
      has: (_, k) => esm.has(k),
    });
    Object.defineProperty(window, "webpackChunksteamui", {
      configurable: true,
      get: () => ({ push: ([, , cb]) => { if (typeof cb === "function") cb(req); } }),
    });
  }

  let mapInjected = false;
  const injectImportMap = (head) => {
    if (mapInjected || !head) return;
    mapInjected = true;
    const s = document.createElement("script");
    s.type = "importmap";
    s.textContent = __IMPORT_MAP_JSON__;
    head.insertBefore(s, head.firstChild);
  };
  if (document.head) {
    injectImportMap(document.head);
  } else {
    const mo = new MutationObserver(() => {
      if (document.head) {
        injectImportMap(document.head);
        mo.disconnect();
      }
    });
    mo.observe(document, { childList: true, subtree: true });
  }
})();"#;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TokKind {
    Ident,
    Number,
    String,
    Punct,
    Regex,
}

#[derive(Debug)]
struct Token<'a> {
    kind: TokKind,
    text: &'a str,
    preceded_by_newline: bool,
}

fn is_ident_start(b: u8) -> bool {
    b.is_ascii_alphabetic() || b == b'_' || b == b'$' || b >= 0x80
}

fn is_ident_continue(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_' || b == b'$' || b >= 0x80
}

fn keyword_allows_regex(word: &str) -> bool {
    matches!(
        word,
        "return"
            | "case"
            | "throw"
            | "in"
            | "of"
            | "instanceof"
            | "typeof"
            | "void"
            | "delete"
            | "new"
            | "else"
            | "do"
            | "yield"
            | "await"
            | "export"
            | "default"
    )
}

fn is_excluded_binding(name: &str) -> bool {
    matches!(
        name,
        "__proto__"
            | "toString"
            | "valueOf"
            | "constructor"
            | "hasOwnProperty"
            | "default"
            | "async"
            | "await"
            | "yield"
            | "return"
            | "class"
            | "function"
            | "var"
            | "let"
            | "const"
            | "import"
            | "export"
            | "from"
            | "as"
            | "of"
            | "in"
            | "new"
            | "void"
            | "typeof"
            | "delete"
            | "throw"
            | "if"
            | "else"
            | "for"
            | "while"
            | "do"
            | "switch"
            | "case"
            | "break"
            | "continue"
            | "try"
            | "catch"
            | "finally"
            | "true"
            | "false"
            | "null"
            | "this"
            | "super"
    )
}

fn starts_new_statement(word: &str) -> bool {
    matches!(
        word,
        "var"
            | "let"
            | "const"
            | "function"
            | "class"
            | "import"
            | "export"
            | "if"
            | "for"
            | "while"
            | "do"
            | "switch"
            | "try"
            | "return"
            | "throw"
            | "break"
            | "continue"
            | "async"
    )
}

/// Extract all top-level mutable (`var`, `let`, `function`) binding names from an ES module chunk.
///
/// Tracks strings, comments, template literals (including nested `${...}`), regex literals,
/// and brace/paren/bracket nesting so only true module-top-level declarations are returned.
pub(crate) fn extract_top_level_mutable_names(source: &str) -> Vec<String> {
    let bytes = source.as_bytes();
    let len = bytes.len();
    let mut i = 0usize;
    let mut brace_depth = 0usize;
    let mut paren_depth = 0usize;
    let mut bracket_depth = 0usize;
    let mut template_brace_stack: Vec<usize> = Vec::new();
    let mut can_start_regex = true;
    let mut last_top_punct_or_kw = "";
    let mut prev_was_dot_or_colon = false;

    let mut names = Vec::new();
    let mut seen = HashSet::new();

    // State for top-level `var` / `let` declaration list parsing.
    #[derive(Clone, Copy, PartialEq, Eq)]
    enum VarState {
        None,
        ExpectPattern,
        InInitializer,
    }
    let mut var_state = VarState::None;
    let mut expect_fn_name = false;
    // Track object/array destructuring inside a top-level var/let pattern.
    let mut pattern_stack: Vec<u8> = Vec::new(); // b'{' or b'['
    let mut pattern_obj_expect_key = false;
    let mut pattern_pending_key: Option<&str> = None;
    let mut pattern_in_default_depth: Option<(usize, usize, usize)> = None;

    let mut push_name = |name: &str| {
        if !name.is_empty() && !is_excluded_binding(name) && seen.insert(name.to_owned()) {
            names.push(name.to_owned());
        }
    };

    while i < len {
        // 1. Skip whitespace and comments, tracking whether a newline occurred.
        let mut preceded_by_newline = false;
        loop {
            while i < len && matches!(bytes[i], b' ' | b'\t' | b'\r' | b'\n' | 0x0b | 0x0c) {
                if bytes[i] == b'\n' || bytes[i] == b'\r' {
                    preceded_by_newline = true;
                }
                i += 1;
            }
            if i + 1 < len && bytes[i] == b'/' && bytes[i + 1] == b'/' {
                i += 2;
                while i < len && bytes[i] != b'\n' && bytes[i] != b'\r' {
                    i += 1;
                }
                continue;
            }
            if i + 1 < len && bytes[i] == b'/' && bytes[i + 1] == b'*' {
                i += 2;
                while i + 1 < len && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    if bytes[i] == b'\n' || bytes[i] == b'\r' {
                        preceded_by_newline = true;
                    }
                    i += 1;
                }
                i = (i + 2).min(len);
                continue;
            }
            break;
        }
        if i >= len {
            break;
        }

        // 2. Lex one token.
        let start = i;
        let b = bytes[i];
        let token: Token<'_>;

        if b == b'"' || b == b'\'' {
            let quote = b;
            i += 1;
            while i < len {
                if bytes[i] == b'\\' {
                    i = (i + 2).min(len);
                } else if bytes[i] == quote {
                    i += 1;
                    break;
                } else {
                    i += 1;
                }
            }
            token = Token {
                kind: TokKind::String,
                text: &source[start..i],
                preceded_by_newline,
            };
        } else if b == b'`' {
            i += 1;
            while i < len {
                if bytes[i] == b'\\' {
                    i = (i + 2).min(len);
                } else if bytes[i] == b'`' {
                    i += 1;
                    break;
                } else if bytes[i] == b'$' && i + 1 < len && bytes[i + 1] == b'{' {
                    i += 2;
                    brace_depth += 1;
                    template_brace_stack.push(brace_depth);
                    break;
                } else {
                    i += 1;
                }
            }
            can_start_regex = false;
            token = Token {
                kind: TokKind::String,
                text: "`",
                preceded_by_newline,
            };
        } else if b == b'}' && template_brace_stack.last().copied() == Some(brace_depth) {
            // Resume enclosing template literal!
            template_brace_stack.pop();
            brace_depth = brace_depth.saturating_sub(1);
            i += 1;
            while i < len {
                if bytes[i] == b'\\' {
                    i = (i + 2).min(len);
                } else if bytes[i] == b'`' {
                    i += 1;
                    break;
                } else if bytes[i] == b'$' && i + 1 < len && bytes[i + 1] == b'{' {
                    i += 2;
                    brace_depth += 1;
                    template_brace_stack.push(brace_depth);
                    break;
                } else {
                    i += 1;
                }
            }
            can_start_regex = false;
            prev_was_dot_or_colon = false;
            continue;
        } else if b == b'/' && can_start_regex {
            i += 1;
            let mut in_class = false;
            while i < len {
                if bytes[i] == b'\\' {
                    i = (i + 2).min(len);
                } else if bytes[i] == b'[' {
                    in_class = true;
                    i += 1;
                } else if bytes[i] == b']' && in_class {
                    in_class = false;
                    i += 1;
                } else if bytes[i] == b'/' && !in_class {
                    i += 1;
                    while i < len && bytes[i].is_ascii_alphabetic() {
                        i += 1;
                    }
                    break;
                } else if bytes[i] == b'\n' || bytes[i] == b'\r' {
                    break;
                } else {
                    i += 1;
                }
            }
            token = Token {
                kind: TokKind::Regex,
                text: &source[start..i],
                preceded_by_newline,
            };
        } else if is_ident_start(b) {
            i += 1;
            while i < len && is_ident_continue(bytes[i]) {
                i += 1;
            }
            token = Token {
                kind: TokKind::Ident,
                text: &source[start..i],
                preceded_by_newline,
            };
        } else if b.is_ascii_digit() || (b == b'.' && i + 1 < len && bytes[i + 1].is_ascii_digit())
        {
            i += 1;
            while i < len
                && (bytes[i].is_ascii_alphanumeric()
                    || bytes[i] == b'.'
                    || bytes[i] == b'_'
                    || (matches!(bytes[i], b'+' | b'-') && matches!(bytes[i - 1], b'e' | b'E')))
            {
                i += 1;
            }
            token = Token {
                kind: TokKind::Number,
                text: &source[start..i],
                preceded_by_newline,
            };
        } else {
            // Punctuator (check multi-char ++ / -- / ... / =>)
            if i + 2 < len && &bytes[i..i + 3] == b"..." {
                i += 3;
            } else if i + 1 < len
                && matches!(
                    &bytes[i..i + 2],
                    b"++" | b"--" | b"=>" | b"==" | b"!=" | b"<=" | b">=" | b"&&" | b"||" | b"??"
                )
            {
                i += 2;
            } else {
                i += 1;
            }
            token = Token {
                kind: TokKind::Punct,
                text: &source[start..i],
                preceded_by_newline,
            };
        }

        // 3. Process token before updating nesting depths when at/entering top-level declarations.
        let at_top = brace_depth == 0 && paren_depth == 0 && bracket_depth == 0;

        // Handle top-level destructuring pattern if active.
        if var_state == VarState::ExpectPattern && !pattern_stack.is_empty() {
            if let Some((db, dp, dk)) = pattern_in_default_depth {
                // Inside a default value expression inside a destructuring pattern: wait until `,`, `}`, or `]` at that depth.
                let at_def_level = brace_depth == db && paren_depth == dp && bracket_depth == dk;
                if at_def_level && matches!(token.text, "," | "}" | "]") {
                    pattern_in_default_depth = None;
                } else {
                    update_depths(
                        token.text,
                        &mut brace_depth,
                        &mut paren_depth,
                        &mut bracket_depth,
                    );
                    update_regex_flag(&token, &mut can_start_regex);
                    prev_was_dot_or_colon = matches!(token.text, "." | ":");
                    continue;
                }
            }
            match token.text {
                "{" => {
                    pattern_pending_key = None;
                    pattern_stack.push(b'{');
                    pattern_obj_expect_key = true;
                }
                "[" => {
                    pattern_pending_key = None;
                    pattern_stack.push(b'[');
                    pattern_obj_expect_key = false;
                }
                "}" => {
                    if let Some(k) = pattern_pending_key.take() {
                        push_name(k);
                    }
                    pattern_stack.pop();
                    pattern_obj_expect_key = pattern_stack.last().copied() == Some(b'{');
                }
                "]" => {
                    pattern_stack.pop();
                    pattern_obj_expect_key = pattern_stack.last().copied() == Some(b'{');
                }
                ":" => {
                    pattern_pending_key = None;
                    pattern_obj_expect_key = false;
                }
                "," => {
                    if let Some(k) = pattern_pending_key.take() {
                        push_name(k);
                    }
                    pattern_obj_expect_key = pattern_stack.last().copied() == Some(b'{');
                }
                "=" => {
                    if let Some(k) = pattern_pending_key.take() {
                        push_name(k);
                    }
                    pattern_in_default_depth = Some((brace_depth, paren_depth, bracket_depth));
                }
                "..." => {}
                _ if token.kind == TokKind::Ident => {
                    if pattern_stack.last().copied() == Some(b'{') && pattern_obj_expect_key {
                        pattern_pending_key = Some(token.text);
                    } else {
                        push_name(token.text);
                    }
                }
                _ => {}
            }
            update_depths(
                token.text,
                &mut brace_depth,
                &mut paren_depth,
                &mut bracket_depth,
            );
            if pattern_stack.is_empty() {
                var_state = VarState::InInitializer;
            }
            update_regex_flag(&token, &mut can_start_regex);
            prev_was_dot_or_colon = matches!(token.text, "." | ":");
            continue;
        }

        if at_top {
            if expect_fn_name {
                if token.text == "*" {
                    // generator `function* name()`
                } else if token.kind == TokKind::Ident {
                    push_name(token.text);
                    expect_fn_name = false;
                } else {
                    expect_fn_name = false;
                }
            } else {
                match var_state {
                    VarState::ExpectPattern => match token.text {
                        "{" => {
                            pattern_stack.push(b'{');
                            pattern_obj_expect_key = true;
                            pattern_pending_key = None;
                        }
                        "[" => {
                            pattern_stack.push(b'[');
                            pattern_obj_expect_key = false;
                            pattern_pending_key = None;
                        }
                        ";" => var_state = VarState::None,
                        _ if token.kind == TokKind::Ident => {
                            push_name(token.text);
                            var_state = VarState::InInitializer;
                        }
                        _ => var_state = VarState::None,
                    },
                    VarState::InInitializer => {
                        if token.text == "," {
                            var_state = VarState::ExpectPattern;
                        } else if token.text == ";" {
                            var_state = VarState::None;
                        } else if token.preceded_by_newline
                            && token.kind == TokKind::Ident
                            && starts_new_statement(token.text)
                            && !matches!(
                                last_top_punct_or_kw,
                                "=" | "," | "+" | "-" | "*" | "/" | "?" | ":" | "&&" | "||" | "??"
                            )
                        {
                            var_state = VarState::None;
                            if matches!(token.text, "var" | "let") {
                                var_state = VarState::ExpectPattern;
                            } else if token.text == "function" {
                                expect_fn_name = true;
                            }
                        }
                    }
                    VarState::None => {
                        if token.kind == TokKind::Ident && !prev_was_dot_or_colon {
                            let stmt_start = matches!(
                                last_top_punct_or_kw,
                                "" | ";" | "}" | "export" | "default" | "async"
                            ) || token.preceded_by_newline;
                            if stmt_start && matches!(token.text, "var" | "let") {
                                var_state = VarState::ExpectPattern;
                            } else if stmt_start && token.text == "function" {
                                expect_fn_name = true;
                            }
                        }
                    }
                }
            }
            if !matches!(
                token.kind,
                TokKind::String | TokKind::Number | TokKind::Regex
            ) {
                last_top_punct_or_kw = token.text;
            } else {
                last_top_punct_or_kw = "_lit";
            }
        }

        // 4. Update depths & regex flag.
        update_depths(
            token.text,
            &mut brace_depth,
            &mut paren_depth,
            &mut bracket_depth,
        );
        if brace_depth == 0 && paren_depth == 0 && bracket_depth == 0 && token.text == "}" {
            last_top_punct_or_kw = "}";
        }
        update_regex_flag(&token, &mut can_start_regex);
        prev_was_dot_or_colon = matches!(token.text, "." | ":");
    }

    names
}

fn update_depths(text: &str, brace: &mut usize, paren: &mut usize, bracket: &mut usize) {
    match text {
        "{" => *brace += 1,
        "}" => *brace = brace.saturating_sub(1),
        "(" => *paren += 1,
        ")" => *paren = paren.saturating_sub(1),
        "[" => *bracket += 1,
        "]" => *bracket = bracket.saturating_sub(1),
        _ => {}
    }
}

fn update_regex_flag(token: &Token<'_>, can_start_regex: &mut bool) {
    *can_start_regex = match token.kind {
        TokKind::Ident => keyword_allows_regex(token.text),
        TokKind::Number | TokKind::String | TokKind::Regex => false,
        TokKind::Punct => !matches!(token.text, ")" | "]" | "++" | "--"),
    };
}

/// Build the append-only live getter/setter registration footer for an intercepted ES module chunk.
pub(crate) fn build_registration_footer(chunk_id: &str, names: &[String]) -> String {
    if names.is_empty() {
        return String::new();
    }
    let chunk_json = serde_json::to_string(chunk_id).unwrap_or_else(|_| "\"\"".to_owned());
    let mut out = String::with_capacity(64 + names.len() * 34);
    out.push_str("\n;(globalThis.__DECKY_ESM_REG__?.(");
    out.push_str(&chunk_json);
    out.push_str(",{");
    for (idx, name) in names.iter().enumerate() {
        if idx > 0 {
            out.push(',');
        }
        out.push_str("get ");
        out.push_str(name);
        out.push_str("(){return ");
        out.push_str(name);
        out.push_str("},set ");
        out.push_str(name);
        out.push_str("(v){");
        out.push_str(name);
        out.push_str("=v}");
    }
    out.push_str("}));\n");
    out
}

/// Compute the registration footer for a Vite/Rolldown ES module chunk.
/// Returns an empty string for Webpack chunks or modules with no top-level mutable declarations.
pub(crate) fn compute_registration_footer(chunk_id: &str, source: &str) -> String {
    if source.contains("webpackChunksteamui") {
        return String::new();
    }
    let names = extract_top_level_mutable_names(source);
    build_registration_footer(chunk_id, &names)
}

#[cfg(test)]
pub(crate) fn instrument_esm_chunk(chunk_id: &str, source: &str) -> Option<String> {
    let footer = compute_registration_footer(chunk_id, source);
    if footer.is_empty() {
        return None;
    }
    let mut out = String::with_capacity(source.len() + footer.len());
    out.push_str(source);
    out.push_str(&footer);
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_top_level_vars_functions_and_destructuring() {
        let src = r#"
import { o as imp } from "./a.js";
const __vite__mapDeps = (i) => i;
var a = `hello ${(() => { var nested = 1; return { k: 2 }; })()}`, b = /var fake = 1;/g;
/* function commentedOut() {} */
// var lineCommented = 2;
function topFn(e) { var inside = 3; function innerFn() {} return inside; }
async function asyncTop() {}
function* genTop() {}
var { alpha, beta: gamma = { nested: 1 } } = obj, [first, ...rest] = arr;
let mutableLet = function exprFn() {};
export { a as A, topFn as B };
"#;
        let names = extract_top_level_mutable_names(src);
        assert_eq!(
            names,
            vec![
                "a",
                "b",
                "topFn",
                "asyncTop",
                "genTop",
                "alpha",
                "gamma",
                "first",
                "rest",
                "mutableLet"
            ]
        );
    }

    #[test]
    fn skips_webpack_chunks_and_reserved_names() {
        assert!(
            instrument_esm_chunk("sp.js", "(self.webpackChunksteamui||[]).push([]);").is_none()
        );
        let src = "var toString = 1, validName = 2;";
        assert_eq!(extract_top_level_mutable_names(src), vec!["validName"]);
    }

    #[test]
    fn handles_division_vs_regex_and_minified_rolldown_syntax() {
        let src = r#"var x=10/2/5,y=()=>a/`foo`+1,p4=function(e){return(0,Y.jsx)(c3e,{controller:`notification`,method:uee(e)})};function Q2(e){let{a:t}=e;return t}var _A=class{UpdateRoutingInfo(e,t){this.m_history=e}};export{x as X};"#;
        assert_eq!(
            extract_top_level_mutable_names(src),
            vec!["x", "y", "p4", "Q2", "_A"]
        );
    }

    #[test]
    fn footer_format_is_valid_js() {
        let footer = build_registration_footer("chunk~1.js", &["p4".into(), "yPi".into()]);
        assert_eq!(
            footer,
            "\n;(globalThis.__DECKY_ESM_REG__?.(\"chunk~1.js\",{get p4(){return p4},set p4(v){p4=v},get yPi(){return yPi},set yPi(v){yPi=v}}));\n"
        );
    }

    #[test]
    fn build_esm_shim_pins_library_url_in_importmap() {
        let shim = build_esm_shim("/library.js?contenthash=CVZWwsCP");
        assert!(shim.contains("https://steamloopback.host/"));
        assert!(shim.contains("http://localhost:1337/steamui/"));
        assert!(shim.contains("http://localhost:1337/steamui/library.js?contenthash=CVZWwsCP"));
        assert!(shim.contains("https://steamloopback.host/library.js?contenthash=CVZWwsCP"));
        assert!(shim.contains("window.DFL?.modules"));
    }
}
