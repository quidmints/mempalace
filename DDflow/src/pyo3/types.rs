//! PyO3 type marshalling.
//!
//! Helpers for converting between Python `dict` payloads and Rust byte
//! buffers. The Rust log keeps payloads opaque (bytes) but Python-side
//! callers want to pass dicts; this module bridges.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// Convert a Python dict to JSON bytes suitable for log append.
pub fn pydict_to_json_bytes(py: Python<'_>, dict: &PyDict) -> PyResult<Vec<u8>> {
    let json_module = py.import("json")?;
    let s: String = json_module.call_method1("dumps", (dict,))?.extract()?;
    Ok(s.into_bytes())
}

/// Convert JSON bytes back to a Python dict.
pub fn json_bytes_to_pydict<'py>(py: Python<'py>, bytes: &[u8]) -> PyResult<&'py PyDict> {
    let json_module = py.import("json")?;
    let obj = json_module.call_method1("loads", (std::str::from_utf8(bytes)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("payload not utf8: {e}")))?,))?;
    obj.downcast::<PyDict>()
        .map_err(|_| pyo3::exceptions::PyTypeError::new_err("expected JSON object").into())
}

/// Convert a Vec<(u64, String, Vec<u8>)> log-entry tuple into a Python list of tuples.
pub fn entries_to_pylist<'py>(
    py: Python<'py>,
    entries: Vec<(u64, String, Vec<u8>)>,
) -> PyResult<&'py PyList> {
    let list = PyList::empty(py);
    for (offset, kind, payload) in entries {
        let pdict = json_bytes_to_pydict(py, &payload)?;
        list.append((offset, kind, pdict))?;
    }
    Ok(list)
}
