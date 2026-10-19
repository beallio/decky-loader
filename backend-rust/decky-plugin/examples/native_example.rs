//! A small native backend: a starting point for plugin authors and the plugin used
//! by the runtime smoke check. Its `plugin.json` needs `"backend": "bin/backend"`
//! with this program copied to `bin/backend`.

use decky_plugin::{
    Plugin,
    serde_json::{Value, json},
};

#[tokio::main]
async fn main() -> std::io::Result<()> {
    Plugin::new()
        .method(
            "echo",
            |_context, args| async move { Ok(Value::Array(args)) },
        )
        .method("environment", |context, _args| async move {
            let env = context.env();
            Ok(json!({
                "name": env.plugin_name,
                "version": env.plugin_version,
                "user": env.user,
                "settings": env.plugin_settings_dir,
                "runtime": env.plugin_runtime_dir,
                "logs": env.plugin_log_dir,
                "plugin": env.plugin_dir,
            }))
        })
        .method("notify", |context, args| async move {
            context
                .emit("example_event", args)
                .await
                .map_err(|error| error.to_string())?;
            Ok(Value::Null)
        })
        .on_main(|context| async move {
            log::info!("{} started", context.env().plugin_name);
            Ok(())
        })
        .on_unload(|_context| async {
            log::info!("unloading");
            Ok(())
        })
        .on_uninstall(|context| async move {
            log::info!("uninstalling");
            // Remove what the plugin stored; Decky keeps these folders otherwise.
            let marker = context.env().plugin_settings_dir.join("uninstalled");
            std::fs::write(marker, b"").map_err(|error| error.to_string())
        })
        .run()
        .await
}
