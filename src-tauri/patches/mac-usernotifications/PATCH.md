# Patched mac-usernotifications 0.3.1

Source: the `mac-usernotifications` 0.3.1 crate by Hendrik Sollich, whose
manifest identifies `https://github.com/hoodie/mac-usernotifications`.
This distribution uses the Apache-2.0 option from `MIT OR Apache-2.0`.
See `LICENSE-APACHE` for its full text. The local crate metadata identifies
the published version but does not record the upstream Git revision.

Supymem modified these files:

1. `src/delegate.rs`: Add `List` to `Banner | Sound`; on macOS 14+, otherwise
   foreground notifications are accepted then discarded.
2. `src/notification.rs`: Use `block_on` for `send_blocking` delivery so the
   `CFRunLoop::is_waiting()` check does not reject delivery from the poll thread.
3. `src/lib.rs`: Export `install_presenting_delegate` so the host can install
   the delegate on the main thread before the worker starts.
4. `Cargo.lock`: Update the locked dependency versions.
