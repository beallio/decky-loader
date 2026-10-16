use std::collections::HashSet;
use std::io::Write;

use pyo3::exceptions::PyValueError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

struct Recursion;

impl Recursion {
    fn enter(py: Python<'_>) -> PyResult<Self> {
        // SAFETY: PyO3 supplies the current interpreter's attached thread state,
        // the GIL stays held for this call, and the static NUL-terminated reason
        // string is valid for the duration required by CPython. A guard is made
        // only after a successful enter and drops exactly once on every path.
        if unsafe { ffi::Py_EnterRecursiveCall(c" while encoding a JSON object".as_ptr()) } != 0 {
            Err(PyErr::fetch(py))
        } else {
            Ok(Self)
        }
    }
}

impl Drop for Recursion {
    fn drop(&mut self) {
        // SAFETY: Recursion is constructible only after a successful matching
        // Py_EnterRecursiveCall and Rust moves/drops this guard exactly once.
        unsafe { ffi::Py_LeaveRecursiveCall() };
    }
}

struct DictSnapshot<'py>(Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>);

impl Drop for DictSnapshot<'_> {
    fn drop(&mut self) {
        // Match CPython's list-of-tuples snapshot: reverse pair order, then
        // value before key. Finalizers can append to an enclosing live list.
        while let Some((key, value)) = self.0.pop() {
            drop(value);
            drop(key);
        }
    }
}

struct Encoder<'py> {
    py: Python<'py>,
    ascii: bool,
    bytes: Vec<u8>,
    containers: HashSet<usize>,
    has_surrogates: bool,
    int_repr: Option<Bound<'py, PyAny>>,
    float_repr: Option<Bound<'py, PyAny>>,
}

impl<'py> Encoder<'py> {
    fn push_hex(&mut self, value: u16) {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        self.bytes.extend_from_slice(b"\\u");
        for shift in [12, 8, 4, 0] {
            self.bytes.push(HEX[((value >> shift) & 15) as usize]);
        }
    }

    fn string_bytes(&mut self, value: &[u8]) {
        self.bytes.push(b'"');
        let mut offset = 0;
        let mut start = 0;
        while offset < value.len() {
            let byte = value[offset];
            if byte < 0x20 || byte == b'"' || byte == b'\\' || (self.ascii && byte == 0x7f) {
                self.bytes.extend_from_slice(&value[start..offset]);
                match byte {
                    b'"' => self.bytes.extend_from_slice(b"\\\""),
                    b'\\' => self.bytes.extend_from_slice(b"\\\\"),
                    b'\x08' => self.bytes.extend_from_slice(b"\\b"),
                    b'\x0c' => self.bytes.extend_from_slice(b"\\f"),
                    b'\n' => self.bytes.extend_from_slice(b"\\n"),
                    b'\r' => self.bytes.extend_from_slice(b"\\r"),
                    b'\t' => self.bytes.extend_from_slice(b"\\t"),
                    _ => self.push_hex(u16::from(byte)),
                }
                offset += 1;
                start = offset;
            } else if self.ascii && byte >= 0x80 {
                self.bytes.extend_from_slice(&value[start..offset]);
                // Input comes only from valid UTF-8 or surrogatepass/WTF-8.
                let (width, mut codepoint) = if byte < 0xe0 {
                    (2, u32::from(byte & 0x1f))
                } else if byte < 0xf0 {
                    (3, u32::from(byte & 0x0f))
                } else {
                    (4, u32::from(byte & 0x07))
                };
                for continuation in &value[offset + 1..offset + width] {
                    codepoint = (codepoint << 6) | u32::from(continuation & 0x3f);
                }
                if codepoint <= 0xffff {
                    self.push_hex(codepoint as u16);
                } else {
                    let supplementary = codepoint - 0x10000;
                    self.push_hex(0xd800 | (supplementary >> 10) as u16);
                    self.push_hex(0xdc00 | (supplementary & 0x3ff) as u16);
                }
                offset += width;
                start = offset;
            } else {
                offset += 1;
            }
        }
        self.bytes.extend_from_slice(&value[start..]);
        self.bytes.push(b'"');
    }

    fn string(&mut self, value: &Bound<'py, PyString>) -> PyResult<()> {
        // SAFETY: this is a live immutable str under the GIL. GetLength readies
        // legacy strings before KIND is inspected. A one-byte str cannot hold
        // surrogates; wider strings must be checked before requesting UTF-8.
        let length = unsafe { ffi::PyUnicode_GetLength(value.as_ptr()) };
        if length < 0 {
            return Err(PyErr::fetch(self.py));
        }
        if unsafe { ffi::PyUnicode_KIND(value.as_ptr()) } != ffi::PyUnicode_1BYTE_KIND {
            for index in 0..length {
                // SAFETY: the immutable string remains live and each index is
                // bounded by its successfully obtained codepoint length.
                let codepoint = unsafe { ffi::PyUnicode_ReadChar(value.as_ptr(), index) };
                if (0xd800..=0xdfff).contains(&codepoint) {
                    // Never populate the UTF-8 cache for surrogates: CPython's
                    // mutable strict error handler can return invalid UTF-8.
                    return self.string_codepoints(value);
                }
            }
        }
        self.string_bytes(value.to_str()?.as_bytes());
        Ok(())
    }

    fn string_codepoints(&mut self, value: &Bound<'py, PyString>) -> PyResult<()> {
        // SAFETY: value is a live PyString borrowed through Bound for 'py and
        // Python<'py> holds the GIL. Its codepoint length bounds each index.
        let length = unsafe { ffi::PyUnicode_GetLength(value.as_ptr()) };
        self.bytes.push(b'"');
        for index in 0..length {
            // SAFETY: index is in 0..length as returned for this immutable str;
            // the same live PyString and held GIL satisfy the API preconditions.
            let codepoint = unsafe { ffi::PyUnicode_ReadChar(value.as_ptr(), index) };
            match codepoint {
                0x22 => self.bytes.extend_from_slice(b"\\\""),
                0x5c => self.bytes.extend_from_slice(b"\\\\"),
                0x08 => self.bytes.extend_from_slice(b"\\b"),
                0x0c => self.bytes.extend_from_slice(b"\\f"),
                0x0a => self.bytes.extend_from_slice(b"\\n"),
                0x0d => self.bytes.extend_from_slice(b"\\r"),
                0x09 => self.bytes.extend_from_slice(b"\\t"),
                0..=0x1f => self.push_hex(codepoint as u16),
                0x7f..=0xffff if self.ascii => self.push_hex(codepoint as u16),
                0x10000..=0x10ffff if self.ascii => {
                    let supplementary = codepoint - 0x10000;
                    self.push_hex(0xd800 | (supplementary >> 10) as u16);
                    self.push_hex(0xdc00 | (supplementary & 0x3ff) as u16);
                }
                0xd800..=0xdfff => {
                    self.has_surrogates = true;
                    self.bytes.extend_from_slice(&[
                        0xe0 | (codepoint >> 12) as u8,
                        0x80 | ((codepoint >> 6) & 0x3f) as u8,
                        0x80 | (codepoint & 0x3f) as u8,
                    ]);
                }
                value if value <= 0x10ffff => {
                    let mut buffer = [0; 4];
                    let text = char::from_u32(value).unwrap().encode_utf8(&mut buffer);
                    self.bytes.extend_from_slice(text.as_bytes());
                }
                _ => return Err(PyErr::fetch(self.py)),
            }
        }
        self.bytes.push(b'"');
        Ok(())
    }

    fn output(&self) -> PyResult<Bound<'py, PyString>> {
        if !self.has_surrogates {
            // SAFETY: bytes is a live Vec<u8>, whose allocation cannot exceed
            // isize::MAX, and it contains only valid UTF-8. The decoder copies
            // the bytes into a new Python str.
            let pointer = unsafe {
                ffi::PyUnicode_DecodeUTF8(
                    self.bytes.as_ptr().cast(),
                    self.bytes.len() as ffi::Py_ssize_t,
                    std::ptr::null(),
                )
            };
            // SAFETY: a non-null return is a new owned reference per CPython;
            // from_owned_ptr_or_err consumes it and handles NULL/error state.
            return Ok(unsafe { Bound::from_owned_ptr_or_err(self.py, pointer) }?
                .cast_into::<PyString>()?);
        }
        // The buffer is produced only by this encoder. Count its valid UTF-8 or
        // lone-surrogate WTF-8 codepoints, then construct Unicode directly.
        fn next(bytes: &[u8], offset: &mut usize) -> u32 {
            let first = bytes[*offset];
            let (width, mut value) = if first < 0x80 {
                (1, u32::from(first))
            } else if first < 0xe0 {
                (2, u32::from(first & 0x1f))
            } else if first < 0xf0 {
                (3, u32::from(first & 0x0f))
            } else {
                (4, u32::from(first & 0x07))
            };
            for byte in &bytes[*offset + 1..*offset + width] {
                value = (value << 6) | u32::from(byte & 0x3f);
            }
            *offset += width;
            value
        }
        let mut offset = 0;
        let mut count = 0;
        let mut maximum = 0;
        while offset < self.bytes.len() {
            maximum = maximum.max(next(&self.bytes, &mut offset));
            count += 1;
        }
        // SAFETY: count and maximum derive from well-formed codepoints written
        // by this encoder; the GIL is held. The API returns a new reference or
        // NULL with an exception, handled immediately below.
        let pointer = unsafe { ffi::PyUnicode_New(count, maximum) };
        // SAFETY: consume the new reference or propagate CPython's error.
        let object = unsafe { Bound::from_owned_ptr_or_err(self.py, pointer) }?;
        offset = 0;
        for index in 0..count {
            let value = next(&self.bytes, &mut offset);
            // SAFETY: object is the newly created, still-unexposed Unicode string;
            // index is below count, each value is a valid Unicode codepoint, and
            // Python<'py> keeps the GIL. WriteChar reports failure as -1 + error.
            if unsafe { ffi::PyUnicode_WriteChar(object.as_ptr(), index, value) } != 0 {
                return Err(PyErr::fetch(self.py));
            }
        }
        Ok(object.cast_into::<PyString>()?)
    }

    fn integer_bytes(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        let mut overflow = 0;
        // SAFETY: value is a live PyLong (dispatch/key validation guarantees
        // this) and the GIL is held. The overflow pointer is valid and initialized.
        let number = unsafe { ffi::PyLong_AsLongLongAndOverflow(value.as_ptr(), &mut overflow) };
        if overflow == 0 {
            write!(&mut self.bytes, "{number}").expect("writing to Vec cannot fail");
        } else {
            if self.int_repr.is_none() {
                self.int_repr = Some(self.py.get_type::<PyInt>().getattr("__repr__")?);
            }
            let representation = self.int_repr.as_ref().unwrap().call1((value,))?;
            self.bytes
                .extend_from_slice(representation.cast::<PyString>()?.to_str()?.as_bytes());
        }
        Ok(())
    }

    fn float_bytes(&mut self, value: &Bound<'py, PyFloat>) -> PyResult<()> {
        let number = value.value();
        if number.is_nan() {
            self.bytes.extend_from_slice(b"NaN");
        } else if number == f64::INFINITY {
            self.bytes.extend_from_slice(b"Infinity");
        } else if number == f64::NEG_INFINITY {
            self.bytes.extend_from_slice(b"-Infinity");
        } else {
            if self.float_repr.is_none() {
                self.float_repr = Some(self.py.get_type::<PyFloat>().getattr("__repr__")?);
            }
            let representation = self.float_repr.as_ref().unwrap().call1((value,))?;
            self.bytes
                .extend_from_slice(representation.cast::<PyString>()?.to_str()?.as_bytes());
        }
        Ok(())
    }

    fn key(&mut self, key: &Bound<'py, PyAny>) -> PyResult<()> {
        if let Ok(text) = key.cast::<PyString>() {
            return self.string(text);
        }
        self.bytes.push(b'"');
        if key.is_none() {
            self.bytes.extend_from_slice(b"null");
        } else if key.is_instance_of::<PyBool>() {
            self.bytes
                .extend_from_slice(if key.is_truthy()? { b"true" } else { b"false" });
        } else if key.is_instance_of::<PyInt>() {
            self.integer_bytes(key)?;
        } else if let Ok(value) = key.cast::<PyFloat>() {
            self.float_bytes(value)?;
        } else {
            // SAFETY: key and its type remain live under the GIL. tp_name is a
            // NUL-terminated C string; the precision matches CPython's encoder.
            unsafe {
                ffi::PyErr_Format(
                    ffi::PyExc_TypeError,
                    c"keys must be str, int, float, bool or None, not %.100s".as_ptr(),
                    (*ffi::Py_TYPE(key.as_ptr())).tp_name,
                );
            }
            return Err(PyErr::fetch(self.py));
        }
        self.bytes.push(b'"');
        Ok(())
    }

    fn sequence<'a>(
        &mut self,
        items: impl Iterator<Item = Bound<'py, PyAny>> + 'a,
    ) -> PyResult<()> {
        self.bytes.push(b'[');
        for (index, value) in items.enumerate() {
            if index != 0 {
                self.bytes.extend_from_slice(b", ");
            }
            self.value(&value)?;
        }
        self.bytes.push(b']');
        Ok(())
    }

    fn list(&mut self, items: &Bound<'py, PyList>) -> PyResult<()> {
        self.bytes.push(b'[');
        let mut index = 0;
        while index < items.len() {
            if index != 0 {
                self.bytes.extend_from_slice(b", ");
            }
            self.value(&items.get_item(index)?)?;
            index += 1;
        }
        self.bytes.push(b']');
        Ok(())
    }

    fn pair(
        &mut self,
        index: usize,
        key: &Bound<'py, PyAny>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        if index != 0 {
            self.bytes.extend_from_slice(b", ");
        }
        self.key(key)?;
        self.bytes.extend_from_slice(b": ");
        self.value(value)
    }

    fn value(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        if value.is_none() {
            self.bytes.extend_from_slice(b"null");
        } else if value.is_instance_of::<PyBool>() {
            self.bytes.extend_from_slice(if value.is_truthy()? {
                b"true"
            } else {
                b"false"
            });
        } else if let Ok(text) = value.cast::<PyString>() {
            self.string(text)?;
        } else if value.is_instance_of::<PyInt>() {
            self.integer_bytes(value)?;
        } else if let Ok(number) = value.cast::<PyFloat>() {
            self.float_bytes(number)?;
        } else if value.is_instance_of::<PyList>()
            || value.is_instance_of::<PyTuple>()
            || value.is_instance_of::<PyDict>()
        {
            let guard = Recursion::enter(self.py)?;
            if let Ok(items) = value.cast::<PyDict>()
                && items.is_empty()
            {
                self.bytes.extend_from_slice(b"{}");
                return Ok(());
            }
            let sequence = if value.is_instance_of::<PyDict>() {
                None
            } else {
                // SAFETY: value is a live list/tuple (or subclass) and the GIL
                // is held. The static C string is NUL terminated; the returned
                // new reference is immediately transferred into Bound below.
                let sequence = unsafe {
                    Bound::from_owned_ptr_or_err(
                        self.py,
                        ffi::PySequence_Fast(
                            value.as_ptr(),
                            c"_iterencode_list needs a sequence".as_ptr(),
                        ),
                    )
                }?;
                if sequence.len()? == 0 {
                    self.bytes.extend_from_slice(b"[]");
                    return Ok(());
                }
                Some(sequence)
            };
            let identifier = value.as_ptr() as usize;
            if !self.containers.insert(identifier) {
                return Err(PyValueError::new_err("Circular reference detected"));
            }
            let encoded = if let Ok(items) = value.cast::<PyDict>() {
                self.bytes.push(b'{');
                let result = (|| {
                    if value.is_exact_instance_of::<PyDict>() {
                        let pairs = DictSnapshot(items.iter().collect());
                        for (index, (key, item)) in pairs.0.iter().enumerate() {
                            self.pair(index, key, item)?;
                        }
                    } else {
                        // SAFETY: value is a live dict/subclass and the GIL is
                        // held. PyMapping_Items returns a new reference (owned
                        // by Bound) or NULL with an exception; no borrowed data
                        // escapes this expression.
                        let mapping = unsafe {
                            Bound::from_owned_ptr_or_err(
                                self.py,
                                ffi::PyMapping_Items(value.as_ptr()),
                            )
                        }?;
                        let mapping = mapping.cast::<PyList>()?;
                        let mut index = 0;
                        while index < mapping.len() {
                            let pair = mapping.get_item(index)?;
                            let tuple = pair
                                .cast::<PyTuple>()
                                .map_err(|_| PyValueError::new_err("items must return 2-tuples"))?;
                            if tuple.len() != 2 {
                                return Err(PyValueError::new_err("items must return 2-tuples"));
                            }
                            self.pair(index, &tuple.get_item(0)?, &tuple.get_item(1)?)?;
                            index += 1;
                        }
                    }
                    Ok(())
                })();
                self.bytes.push(b'}');
                result
            } else {
                let sequence = sequence.as_ref().unwrap();
                if let Ok(items) = sequence.cast::<PyList>() {
                    self.list(items)
                } else {
                    self.sequence(sequence.cast::<PyTuple>()?.iter())
                }
            };
            drop(guard);
            self.containers.remove(&identifier);
            encoded?;
        } else {
            let name = value.getattr("__class__")?.getattr("__name__")?;
            let empty = PyString::new(self.py, "");
            // SAFETY: both arguments are live Python objects under the GIL.
            // PyObject_Format performs Python's special-method lookup, returns
            // a new str reference, or NULL with the original callback exception.
            let formatted = unsafe {
                Bound::from_owned_ptr_or_err(
                    self.py,
                    ffi::PyObject_Format(name.as_ptr(), empty.as_ptr()),
                )
            }?;
            // SAFETY: formatted owns the str returned by PyObject_Format. %U
            // reads its Unicode codepoints directly, including lone surrogates.
            unsafe {
                ffi::PyErr_Format(
                    ffi::PyExc_TypeError,
                    c"Object of type %U is not JSON serializable".as_ptr(),
                    formatted.as_ptr(),
                );
            }
            return Err(PyErr::fetch(self.py));
        }
        Ok(())
    }
}

/// Encode the loader's IPC JSON forms without altering Python's json module.
#[pyfunction(signature = (value, *, ensure_ascii=true))]
fn dumps<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    ensure_ascii: bool,
) -> PyResult<Bound<'py, PyString>> {
    let mut encoder = Encoder {
        py,
        ascii: ensure_ascii,
        bytes: Vec::new(),
        containers: HashSet::new(),
        has_surrogates: false,
        int_repr: None,
        float_repr: None,
    };
    encoder.value(value)?;
    encoder.output()
}

#[pymodule]
fn _decky_native_json(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(dumps, module)?)?;
    module.add("__version__", "0.1.0")?;
    Ok(())
}
