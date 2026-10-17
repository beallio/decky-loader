use std::{
    io,
    process::{Output, Stdio},
};

use tokio::process::Command;

/// Run a systemd operation in this process's manager scope. User services keep
/// the inherited XDG runtime and bus environment; only the bundled library
/// search path is removed so the host's systemctl loads its own libraries.
pub(crate) async fn systemctl_output(args: &[&str]) -> io::Result<Output> {
    let mut command = Command::new("systemctl");
    if crate::platform::effective_user_ids().0 != 0 {
        command.arg("--user");
    }
    command
        .args(args)
        .env("LD_LIBRARY_PATH", "")
        .stdin(Stdio::null())
        .output()
        .await
}
