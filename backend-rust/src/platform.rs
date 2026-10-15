use std::{
    io,
    os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd},
    path::Path,
};

use nix::{
    NixPath,
    errno::Errno,
    sys::{
        inotify::{InitFlags, Inotify},
        signal::{self, Signal},
    },
    unistd::{self, Pid},
};

/// Return the effective UID and GID without allocating or changing process state.
pub(crate) fn effective_user_ids() -> (u32, u32) {
    (unistd::geteuid().as_raw(), unistd::getegid().as_raw())
}

/// Convert an externally supplied PID to a strictly positive pid_t.
///
/// This prevents a u32-to-pid_t cast from becoming kill(0) or kill(-1).
fn checked_pid(pid: u32) -> io::Result<Pid> {
    let pid = i32::try_from(pid).map_err(|_| io::Error::from(Errno::EINVAL))?;
    if pid <= 0 {
        return Err(io::Error::from(Errno::EINVAL));
    }
    Ok(Pid::from_raw(pid))
}

/// Keep the group form of kill from turning group ID 1 into kill(-1).
fn checked_group(pid: u32) -> io::Result<Pid> {
    let group = checked_pid(pid)?;
    if group.as_raw() == 1 {
        return Err(io::Error::from(Errno::EINVAL));
    }
    Ok(group)
}

/// Send a signal to one process. Unknown signal numbers are passed through to
/// the kernel so valid real-time signal numbers retain their POSIX behavior.
pub(crate) fn signal_process(pid: u32, signal_number: i32) -> io::Result<()> {
    let pid = checked_pid(pid)?;
    if signal_number == 0 {
        return signal::kill(pid, None).map_err(io::Error::from);
    }
    if let Ok(signal) = Signal::try_from(signal_number) {
        return signal::kill(pid, signal).map_err(io::Error::from);
    }

    // SAFETY: `pid` was checked positive and representable as pid_t, so this
    // cannot invoke kill's process-group/broadcast meanings. `kill` does not
    // dereference pointers; passing the caller's signal number preserves the
    // kernel's normal handling of real-time and invalid signal values.
    let result = unsafe { libc::kill(pid.as_raw(), signal_number) };
    if result == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// Send a signal to a process group ID greater than one. Group ID 1 is rejected
/// because its negative `kill` PID is `-1`, which has system-wide semantics.
pub(crate) fn signal_group(pid: u32, signal_number: i32) -> io::Result<()> {
    let group = checked_group(pid)?;
    if signal_number == 0 {
        return signal::killpg(group, None).map_err(io::Error::from);
    }
    if let Ok(signal) = Signal::try_from(signal_number) {
        return signal::killpg(group, signal).map_err(io::Error::from);
    }

    // SAFETY: `group` was checked greater than one and representable as pid_t;
    // negating it therefore cannot become kill(0) or kill(-1). `kill` does
    // not dereference pointers, and forwarding the number preserves real-time
    // signal behavior not represented by nix::Signal.
    let result = unsafe { libc::kill(-group.as_raw(), signal_number) };
    if result == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// Create a nonblocking, close-on-exec inotify descriptor without duplicating it.
pub(crate) fn inotify_init() -> io::Result<OwnedFd> {
    Inotify::init(InitFlags::IN_CLOEXEC | InitFlags::IN_NONBLOCK)
        .map(Into::into)
        .map_err(io::Error::from)
}

/// Add a watch while borrowing (and never taking ownership of) the inotify FD.
pub(crate) fn inotify_add_watch(fd: BorrowedFd<'_>, path: &Path, mask: u32) -> io::Result<i32> {
    let watch = path
        .as_os_str()
        .with_nix_path(|c_path| {
            // SAFETY: `fd` is borrowed for this call, and `with_nix_path` keeps
            // the NUL-terminated path alive for the synchronous syscall. The
            // syscall only reads that path and the integer mask.
            unsafe { libc::inotify_add_watch(fd.as_raw_fd(), c_path.as_ptr(), mask) }
        })
        .map_err(io::Error::from)?;
    if watch == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(watch)
    }
}

/// Remove a watch from a borrowed inotify FD without duplicating or closing it.
pub(crate) fn inotify_remove_watch(fd: BorrowedFd<'_>, wd: i32) -> io::Result<()> {
    // SAFETY: `fd` is a live borrowed descriptor for this call; the syscall
    // consumes only the integer descriptor and watch ID and retains no pointer.
    let result = unsafe { libc::inotify_rm_watch(fd.as_raw_fd(), wd) };
    if result == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// Read from a borrowed descriptor without taking ownership or copying the FD.
pub(crate) fn read_fd(fd: BorrowedFd<'_>, buffer: &mut [u8]) -> io::Result<usize> {
    unistd::read(fd, buffer).map_err(io::Error::from)
}

/// Open a pidfd for a strictly positive, representable process ID.
pub(crate) fn process_exit_fd(pid: u32) -> io::Result<OwnedFd> {
    let pid = checked_pid(pid)?;
    // No nix 0.31.3 safe wrapper exposes pidfd_open. On success the kernel
    // returns a new descriptor owned by this call; flags=0 requests defaults.
    // SAFETY: `pid` is positive and representable as pid_t. `SYS_pidfd_open`
    // takes only this PID and a flags integer, and no pointer arguments.
    let descriptor = unsafe { libc::syscall(libc::SYS_pidfd_open, pid.as_raw(), 0) };
    if descriptor == -1 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful pidfd_open returns a fresh, uniquely owned file
    // descriptor with a nonnegative int value widened to c_long; it is
    // transferred exactly once into OwnedFd.
    Ok(unsafe { OwnedFd::from_raw_fd(descriptor as i32) })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signals_reject_process_group_and_broadcast_pid_conversions() {
        for signal in [signal_process, signal_group] {
            for pid in [0, u32::MAX] {
                assert_eq!(
                    signal(pid, 0)
                        .expect_err("invalid signal target")
                        .raw_os_error(),
                    Some(libc::EINVAL),
                );
            }
        }
        assert_eq!(
            signal_group(1, 0)
                .expect_err("broadcast group")
                .raw_os_error(),
            Some(libc::EINVAL),
        );
    }
}
