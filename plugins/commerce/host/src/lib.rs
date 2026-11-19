//! Thin C boundary for a Go host. zplugin owns plugin loading, ABI checking,
//! typed calls and origin-matched plugin allocations. All handle operations must
//! occur on the OS thread that opened the handle.

use extore_plugin_api::{CommerceApi, CommerceProxy, MAX_CATALOG_BYTES, MAX_CERTIFICATE_BYTES};
use std::{
    cell::RefCell,
    panic::{AssertUnwindSafe, catch_unwind},
    path::{Path, PathBuf},
    rc::Rc,
    thread::{self, ThreadId},
};
use zplugin::{Error, Plugin, UnloadPolicy};

const INVALID: i32 = -2;
const PANICKED: i32 = -5;
const WRONG_THREAD: i32 = -1001;

struct LoadedLibrary {
    path: PathBuf,
    plugin: Rc<Plugin>,
}

thread_local! {
    // Loading a pinned image for every request would accumulate loader references.
    // Keep one operator-selected image per host thread and create fresh instances.
    static LIBRARY: RefCell<Option<LoadedLibrary>> = const { RefCell::new(None) };
}

unsafe fn library(path: &Path) -> std::result::Result<Rc<Plugin>, i32> {
    LIBRARY.with(|slot| {
        let mut slot = slot.try_borrow_mut().map_err(|_| -4)?;
        if let Some(loaded) = slot.as_ref() {
            if loaded.path != path {
                return Err(INVALID);
            }
            return Ok(Rc::clone(&loaded.plugin));
        }
        // SAFETY: forwarded operator-trusted native path contract.
        let plugin =
            Rc::new(unsafe { Plugin::load_path(path, UnloadPolicy::Pinned) }.map_err(status)?);
        *slot = Some(LoadedLibrary {
            path: path.to_owned(),
            plugin: Rc::clone(&plugin),
        });
        Ok(plugin)
    })
}

struct Host {
    owner: ThreadId,
    commerce: CommerceProxy<'static>,
}

#[repr(C)]
pub struct ExtoreBytes {
    pub data: *const u8,
    pub len: usize,
    pub owner: usize,
}

fn status(error: Error) -> i32 {
    match error {
        Error::PluginStatus(code) if code != 0 => code,
        Error::InvalidArgument => INVALID,
        Error::BufferTooSmall => -3,
        Error::Busy => -4,
        Error::Panicked => PANICKED,
        Error::Poisoned => -6,
        _ => -1,
    }
}

fn boundary(operation: impl FnOnce() -> std::result::Result<(), i32>) -> i32 {
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(())) => 0,
        Ok(Err(code)) => code,
        Err(_) => PANICKED,
    }
}

unsafe fn bytes<'a>(
    pointer: *const u8,
    length: usize,
    maximum: usize,
) -> std::result::Result<&'a [u8], i32> {
    if length > maximum || (length != 0 && pointer.is_null()) {
        return Err(INVALID);
    }
    if length == 0 {
        return Ok(&[]);
    }
    // SAFETY: the C caller supplies a readable range for this call.
    Ok(unsafe { std::slice::from_raw_parts(pointer, length) })
}

unsafe fn text<'a>(
    pointer: *const u8,
    length: usize,
    maximum: usize,
) -> std::result::Result<&'a str, i32> {
    // SAFETY: forwarded C range contract; the view cannot escape this call.
    std::str::from_utf8(unsafe { bytes(pointer, length, maximum)? }).map_err(|_| INVALID)
}

#[unsafe(no_mangle)]
pub extern "C" fn extore_host_abi_version() -> u32 {
    1
}

/// # Safety
/// Inputs are readable for their supplied lengths; out_handle is writable and
/// aligned. The path selects operator-trusted native code and its dependencies.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn extore_host_open(
    path: *const u8,
    path_len: usize,
    origin: *const u8,
    origin_len: usize,
    certificate: *const u8,
    certificate_len: usize,
    out_handle: *mut usize,
) -> i32 {
    if out_handle.is_null() {
        return INVALID;
    }
    // SAFETY: guaranteed by the caller.
    unsafe {
        out_handle.write(0);
    }
    boundary(|| {
        // SAFETY: ranges are checked and borrowed only for this call.
        let path = unsafe { text(path, path_len, 16 * 1024)? };
        let origin = unsafe { text(origin, origin_len, 2048)? };
        let certificate = unsafe { bytes(certificate, certificate_len, MAX_CERTIFICATE_BYTES)? };
        if !Path::new(path).is_absolute() {
            return Err(INVALID);
        }
        // SAFETY: the operator selected the trusted, packaged Extore library.
        // zplugin checks the frozen ABI and exact application interface.
        let library = unsafe { library(Path::new(path))? };
        let instance = Plugin::create_owned(&library, CommerceApi::SPEC).map_err(status)?;
        let mut commerce = CommerceApi::wrap(instance).map_err(status)?;
        commerce.configure(origin, certificate).map_err(status)?;
        let handle = Box::into_raw(Box::new(Host {
            owner: thread::current().id(),
            commerce,
        }));
        // SAFETY: guaranteed by the caller; ownership transfers to close.
        unsafe {
            out_handle.write(handle as usize);
        }
        Ok(())
    })
}

/// # Safety
/// handle is an open handle from this bridge, on its opening thread. token is
/// readable for token_len bytes; output is writable and aligned.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn extore_host_catalog(
    handle: usize,
    token: *const u8,
    token_len: usize,
    output: *mut ExtoreBytes,
) -> i32 {
    if output.is_null() {
        return INVALID;
    }
    // SAFETY: guaranteed by the caller.
    unsafe {
        output.write(ExtoreBytes {
            data: std::ptr::null(),
            len: 0,
            owner: 0,
        });
    }
    if handle == 0 {
        return INVALID;
    }
    boundary(|| {
        // SAFETY: only open handles returned by this bridge are accepted by contract.
        let host = unsafe { &mut *(handle as *mut Host) };
        if host.owner != thread::current().id() {
            return Err(WRONG_THREAD);
        }
        // SAFETY: token is a caller-owned, call-scoped readable range.
        let token = unsafe { text(token, token_len, 512)? };
        let response = host.commerce.catalog(token).map_err(status)?;
        if response.len() > MAX_CATALOG_BYTES {
            return Err(INVALID);
        }
        // Release the zplugin Buffer on the opening thread after one business
        // invocation. The caller owns only this separate host allocation.
        let owned = Box::new(response.to_vec());
        let result = ExtoreBytes {
            data: owned.as_ptr(),
            len: owned.len(),
            owner: Box::into_raw(owned) as usize,
        };
        // SAFETY: output is writable by contract.
        unsafe {
            output.write(result);
        }
        Ok(())
    })
}

/// # Safety
/// owner is an unreleased output owner from extore_host_catalog, or zero.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn extore_host_release(owner: usize) {
    if owner != 0 {
        // SAFETY: this is exactly the Box<Vec<u8>> allocated in catalog.
        drop(unsafe { Box::from_raw(owner as *mut Vec<u8>) });
    }
}

/// # Safety
/// handle is an open handle from this bridge, on its opening thread, and cannot
/// be used again after a successful close.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn extore_host_close(handle: usize) -> i32 {
    if handle == 0 {
        return INVALID;
    }
    boundary(|| {
        // SAFETY: open handle contract; the thread is checked before mutation.
        let host = unsafe { &*(handle as *const Host) };
        if host.owner != thread::current().id() {
            return Err(WRONG_THREAD);
        }
        // SAFETY: ownership transfers back exactly once.
        drop(unsafe { Box::from_raw(handle as *mut Host) });
        Ok(())
    })
}
