"""Bounded binary transport shared by gateway and Core intake."""
import asyncio


class BodyRejected(ValueError):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


async def waitlist_body(request, *, maximum=4096, timeout=10):
    """Public email capture is bounded before buffering at either service."""
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise BodyRejected("waitlist_encoding_invalid", 415)
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media not in {"application/json", "application/x-www-form-urlencoded"}:
        raise BodyRejected("waitlist_content_type", 415)
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > maximum):
        raise BodyRejected("waitlist_input_too_large", 413)
    output = bytearray()
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                if len(output) + len(chunk) > maximum:
                    raise BodyRejected("waitlist_input_too_large", 413)
                output.extend(chunk)
    except TimeoutError:
        raise BodyRejected("waitlist_submission_deadline", 408) from None
    return bytes(output)


async def events_body(request, *, maximum=8192, timeout=10):
    """Anonymous product beacon body. Content-Type is ignored; size is capped."""
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise BodyRejected("events_encoding_invalid", 415)
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > maximum):
        raise BodyRejected("events_input_too_large", 413)
    output = bytearray()
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                if len(output) + len(chunk) > maximum:
                    raise BodyRejected("events_input_too_large", 413)
                output.extend(chunk)
    except TimeoutError:
        raise BodyRejected("events_submission_deadline", 408) from None
    return bytes(output)


async def zip_body(request, *, maximum=32 * 1024 * 1024, expected=None, timeout=20):
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise BodyRejected("import_upload_encoding_invalid", 415)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
        raise BodyRejected("import_upload_type_invalid", 415)
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or not 0 < int(length) <= maximum or expected is not None and int(length) != expected):
        raise BodyRejected("import_upload_size_invalid", 413)
    output = bytearray()
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                if len(output) + len(chunk) > maximum or expected is not None and len(output) + len(chunk) > expected:
                    raise BodyRejected("import_upload_size_invalid", 413)
                output.extend(chunk)
    except TimeoutError:
        raise BodyRejected("import_upload_deadline", 408) from None
    if not output or expected is not None and len(output) != expected:
        raise BodyRejected("import_upload_size_invalid", 413)
    return bytes(output)
