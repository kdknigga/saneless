"""
Shared end-to-end test infrastructure: what went in, and what came out.

An end-to-end test that only checks a job's final state proves the pipeline
*finished*, not that it delivered the right thing.  The pieces here let a test
prove the rest -- the right pages, in the right order, carrying the operator's
metadata, reaching paperless-ngx exactly once:

- ``distinct_page`` draws pages that differ in their pixels, so each one can be
  told apart without trusting the code under test.
- ``DistinctPageScanner`` spools those pages through the pipeline's own sink
  and keeps a copy of every spooled PNG, because the job workspace is deleted
  when ``run_pipeline`` returns.
- ``RecordingPaperless`` answers the paperless-ngx API from memory and keeps
  every request it was sent; ``multipart_fields`` reads an upload back field by
  field with a real MIME parser, never with substring checks.
- ``png_idat`` and ``embedded_streams`` compare a spooled page with a PDF page
  byte for byte: img2pdf copies a PNG's compressed pixel data into the PDF
  untouched, so equal bytes mean the PDF page *is* that spooled file.
- ``web_client_builder`` and ``cli_client_builder`` stand in for the two
  places the application builds its ``PaperlessClient``.
- ``loopback_paperless`` answers the same API over a real socket on
  127.0.0.1, for the checks an in-memory transport cannot make.

Import it as ``from tests.golden_support import ...``; the bare
``golden_support`` form raises ``ModuleNotFoundError`` under pytest 9's
importlib mode, for the same reason ``tests.conftest`` is imported by its
package path.
"""

from __future__ import annotations

import contextlib
import email.parser
import email.policy
import io
import json
import logging
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, override
from urllib.parse import parse_qs, urlsplit

import httpx2
import pikepdf
from PIL import Image, ImageDraw

from saneless.logging_config import configure_logging
from saneless.paperless import PaperlessClient
from tests.conftest import StubScannerBackend, scan_batch

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from pathlib import Path

    from saneless.scanner.base import PageRecord, PageSink, ScanBatch, ScanSettings

DOCUMENTS_PATH = "/api/documents/post_document/"
TASKS_PATH = "/api/tasks/"
TAGS_PATH = "/api/tags/"
CORRESPONDENTS_PATH = "/api/correspondents/"

# An empty page in the paginated shape paperless-ngx lists collections in.
_EMPTY_COLLECTION: dict[str, object] = {
    "count": 0,
    "next": None,
    "previous": None,
    "results": [],
}

# A PNG file opens with an 8-byte signature; chunks follow it, each one a
# 4-byte length, a 4-byte type, that many payload bytes and a 4-byte CRC.
_PNG_SIGNATURE_LENGTH = 8
_PNG_CHUNK_OVERHEAD = 12


def distinct_page(index: int) -> Image.Image:
    """
    Draw a page no other page of the same job can be mistaken for.

    The index is drawn as a run of marks along the top edge, so two pages
    differ in their **pixels** -- and therefore in their spooled PNG bytes and
    in the stream the PDF embeds -- rather than only in a file name.  A test
    that reads a PDF back to prove page order needs exactly that: content it
    can tell apart without trusting the thing under test.

    The body rectangle keeps every page far from the blank-page thresholds, so
    empty-page detection never removes one by accident.

    Args:
        index: The 0-based page number.  Up to 28 pages fit across the top
            edge, which is more than any test here scans.

    Returns:
        A 120x160 RGB page carrying that index.

    """
    page = Image.new("RGB", (120, 160), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((10, 30, 110, 150), fill="black")
    for mark in range(index + 1):
        left = 2 + mark * 4
        draw.rectangle((left, 2, left + 2, 8), fill="black")
    return page


def png_idat(png: bytes) -> bytes:
    """
    Concatenate a PNG's IDAT payloads: its compressed pixel data itself.

    This is what img2pdf embeds when it passes a suitable PNG through -- the
    zlib stream is copied into a ``/FlateDecode`` image object untouched -- so
    it is directly comparable with what pikepdf reads back out of the PDF.
    Comparing these bytes is a much stronger claim than comparing decoded
    pixels: it says the PDF's page *is* that spooled file, not merely a page
    that looks like it.

    Args:
        png: A whole PNG file, as the spool wrote it.

    Returns:
        Every IDAT chunk's payload, concatenated in file order.

    """
    payload = bytearray()
    position = _PNG_SIGNATURE_LENGTH
    while position < len(png):
        length = int.from_bytes(png[position : position + 4], "big")
        chunk_type = png[position + 4 : position + 8]
        if chunk_type == b"IDAT":
            payload += png[position + 8 : position + 8 + length]
        position += length + _PNG_CHUNK_OVERHEAD
    return bytes(payload)


def embedded_streams(pdf: bytes | Path) -> list[bytes]:
    """
    Read each PDF page's single embedded image stream, in page order.

    Raw, not decoded, so the result can be compared with ``png_idat``.

    Args:
        pdf: The PDF itself, or the path of an assembled PDF on disk.

    Returns:
        One raw stream per page, in the order the pages appear in the PDF.

    """
    opened = (
        pikepdf.open(io.BytesIO(pdf)) if isinstance(pdf, bytes) else pikepdf.open(pdf)
    )
    streams: list[bytes] = []
    with opened as document:
        for page in document.pages:
            (image,) = pikepdf.Page(page).get_images().values()
            streams.append(image.read_raw_bytes())
    return streams


class DistinctPageScanner(StubScannerBackend):
    """
    A scanner that feeds pages it can later identify, one pass per call.

    Each ``scan_pages`` call takes the next entry of ``passes`` -- the page
    indices that pass feeds, in feed order -- and spools ``distinct_page`` for
    each through the pipeline's own sink.  A manual-duplex run is two passes:
    the fronts, then the backs in the order the flipped stack feeds them.

    The spooled PNG of every page is copied into ``spooled`` the moment the
    sink has written it, because the files live in the job workspace and that
    is deleted when ``run_pipeline`` returns.

    The ``host`` parameter makes the class usable wherever the CLI builds its
    ``SaneBackend(host=...)``; a test hands the CLI a factory that returns one
    prepared instance.
    """

    def __init__(
        self,
        host: str = "",
        *,
        passes: Sequence[Sequence[int]] = ((0,),),
        rejected: Sequence[int] = (),
    ) -> None:
        """
        Prepare the passes this scanner will feed.

        Args:
            host: Accepted for the CLI's ``SaneBackend(host=...)`` call shape;
                unused.
            passes: The page indices each successive call feeds, in order.
            rejected: How many sheets each pass reports having skipped; a pass
                without an entry skipped none.

        """
        self.host = host
        self.passes = tuple(tuple(indices) for indices in passes)
        self.rejected = tuple(rejected)
        self.spooled: dict[int, bytes] = {}
        self.calls = 0

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Feed the next pass through the caller's sink.

        Args:
            device_id: Ignored; this scanner scans nothing real.
            settings: Only ``resolution`` is used, and only to report it back.
            sink: The pipeline's sink, which receives every page of the pass.

        Returns:
            A batch of this pass's records and its skipped-sheet count.

        Raises:
            AssertionError: If called more times than there are passes, so a
                rescan fails loudly instead of feeding the same pages again.

        """
        index = self.calls
        self.calls += 1
        if index >= len(self.passes):
            msg = (
                f"scan_pages was called {self.calls} time(s), but "
                f"DistinctPageScanner was given only {len(self.passes)} pass(es)"
            )
            raise AssertionError(msg)
        records: list[PageRecord] = []
        for page_index in self.passes[index]:
            image = distinct_page(page_index)
            try:
                record = sink.add(image)
            finally:
                image.close()
            self.spooled[page_index] = record.path.read_bytes()
            records.append(record)
        skipped = self.rejected[index] if index < len(self.rejected) else 0
        return scan_batch(records, resolution=settings.resolution, rejected=skipped)


def unexpected_request(request: httpx2.Request) -> httpx2.Response:
    """
    Answer a request no case expected, in a way that names itself.

    A silently-tolerated stray request is how an end-to-end test stops being
    one.  Answering non-200 turns a mis-dispatch into a PaperlessError quoting
    the method and path, instead of letting the run drift into a timeout whose
    message says nothing about the real mistake.

    Args:
        request: The request that reached a handler which was not expecting it.

    Returns:
        A 418 whose body names the offending method and path.

    """
    return httpx2.Response(
        418,
        text=(
            f"the end-to-end transport was not expecting "
            f"{request.method} {request.url.path}"
        ),
    )


def multipart_fields(request: httpx2.Request) -> list[tuple[str, str | bytes]]:
    """
    Read every form part of an upload, in wire order.

    A real MIME parser rather than substring checks on the body: a multipart
    boundary is random and often contains digits, so ``"12" in body`` can pass
    for a request that never sent the value.

    Args:
        request: A multipart request whose body has been read, as
            ``httpx2.MockTransport`` does before calling its handler.

    Returns:
        One ``(name, value)`` pair per part.  A text field's value is a
        ``str``; a file's is its raw ``bytes``.

    Raises:
        AssertionError: If the body is not a multipart message, or a part has
            no name or no payload.

    """
    head = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
    parser = email.parser.BytesParser(policy=email.policy.default)
    message = parser.parsebytes(head + request.content)
    if not isinstance(message, EmailMessage) or not message.is_multipart():
        msg = f"{request.method} {request.url.path} did not carry a multipart body"
        raise AssertionError(msg)
    fields: list[tuple[str, str | bytes]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if not isinstance(name, str) or not isinstance(payload, bytes):
            msg = f"a form part of {request.url.path} has no name or no payload"
            raise AssertionError(msg)
        if part.get_filename() is None:
            fields.append((name, payload.decode()))
        else:
            fields.append((name, payload))
    return fields


class RecordingPaperless:
    """
    Answer the paperless-ngx API from memory and remember every request.

    A callable ``httpx2.MockTransport`` handler.  Every upload is issued its
    own task id, and a poll is answered only for an id that was issued, so a
    run cannot pass by polling a task that was never created.  Tag and
    correspondent lists are answered empty, because the index page and the
    status refresher ask for them on their own; a test therefore filters the
    recorded requests by method and path and never counts them all.

    Anything else is answered by ``unexpected_request``.
    """

    def __init__(self, *, refuse_uploads: bool = False) -> None:
        """
        Start with no requests recorded and no tasks issued.

        Args:
            refuse_uploads: Answer every upload with a 500, as a restarting
                paperless-ngx would, so a client with a consume directory
                falls back to it.

        """
        self.requests: list[httpx2.Request] = []
        self.issued: list[str] = []
        self._refuse_uploads = refuse_uploads

    @property
    def refuses_uploads(self) -> bool:
        """
        Say whether every upload is answered with a 500.

        Returns:
            The ``refuse_uploads`` this recorder was built with.

        """
        return self._refuse_uploads

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """
        Record a request and answer it.

        Args:
            request: The request the client sent.

        Returns:
            The answer paperless-ngx would give, or a 418 naming the request.

        """
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == DOCUMENTS_PATH:
            if self._refuse_uploads:
                return httpx2.Response(500, text="paperless-ngx is restarting")
            task_id = f"golden-task-{len(self.issued) + 1}"
            self.issued.append(task_id)
            return httpx2.Response(200, json=task_id)
        if request.method == "GET" and path == TASKS_PATH:
            polled = str(request.url.params.get("task_id", ""))
            if polled not in self.issued:
                return unexpected_request(request)
            return httpx2.Response(200, json=[{"task_id": polled, "status": "SUCCESS"}])
        if request.method == "GET" and path in {TAGS_PATH, CORRESPONDENTS_PATH}:
            return httpx2.Response(200, json=_EMPTY_COLLECTION)
        return unexpected_request(request)

    def _matching(self, method: str, path: str) -> list[httpx2.Request]:
        """
        Select the recorded requests with one method and path.

        Args:
            method: The HTTP method to keep.
            path: The URL path to keep.

        Returns:
            The matching requests, in the order they were sent.

        """
        return [
            request
            for request in self.requests
            if request.method == method and request.url.path == path
        ]

    def uploads(self) -> list[httpx2.Request]:
        """
        Return every upload attempt, in the order it was sent.

        Returns:
            The ``POST`` requests to the document upload endpoint.

        """
        return self._matching("POST", DOCUMENTS_PATH)

    def polls(self) -> list[httpx2.Request]:
        """
        Return every task poll, in the order it was sent.

        Returns:
            The ``GET`` requests to the task endpoint.

        """
        return self._matching("GET", TASKS_PATH)

    def upload_fields(self, index: int) -> list[tuple[str, str | bytes]]:
        """
        Read one upload's form fields, in wire order.

        Args:
            index: Which upload, counting from 0.

        Returns:
            The upload's ``(name, value)`` pairs, as ``multipart_fields``
            returns them.

        """
        return multipart_fields(self.uploads()[index])

    def document(self, index: int) -> bytes:
        """
        Return the PDF one upload carried.

        Args:
            index: Which upload, counting from 0.

        Returns:
            The bytes of the upload's ``document`` part.

        Raises:
            AssertionError: If the upload carried no ``document`` file part.

        """
        for name, value in self.upload_fields(index):
            if name == "document" and isinstance(value, bytes):
                return value
        msg = f"upload {index} carried no document file part"
        raise AssertionError(msg)


def web_client_builder(recorder: RecordingPaperless) -> Callable[..., PaperlessClient]:
    """
    Build the stand-in for ``saneless.web.app.PaperlessClient``.

    ``create_app`` builds its client with keyword arguments, so the stand-in
    takes the same keywords and returns a real client over ``recorder``.  The
    client keeps the production retry count, so "uploaded exactly once" is
    proved at the count a real appliance runs with.  Only when ``recorder``
    refuses every upload does it get one attempt: the fallback is reached
    exactly as at any retry count, and no backoff pause is ever reached.

    Args:
        recorder: The in-memory paperless-ngx every request goes to.

    Returns:
        A callable with the keyword-only ``url, token, consume_dir`` shape.

    """

    def build_client(
        *, url: str, token: str, consume_dir: Path | None = None
    ) -> PaperlessClient:
        """
        Build a real client whose transport is ``recorder``.

        Returns:
            A client that never opens a socket.

        """
        if recorder.refuses_uploads:
            return PaperlessClient(
                url=url,
                token=token,
                consume_dir=consume_dir,
                max_retries=1,
                transport=httpx2.MockTransport(recorder),
            )
        return PaperlessClient(
            url=url,
            token=token,
            consume_dir=consume_dir,
            transport=httpx2.MockTransport(recorder),
        )

    return build_client


def cli_client_builder(recorder: RecordingPaperless) -> Callable[..., PaperlessClient]:
    """
    Build the stand-in for ``saneless.cli.PaperlessClient``.

    The ``scan`` command passes its arguments positionally, so this stand-in
    takes them positionally; otherwise it is ``web_client_builder``'s client,
    with the production retry count unless ``recorder`` refuses every upload.

    Args:
        recorder: The in-memory paperless-ngx every request goes to.

    Returns:
        A callable with the positional ``url, token, consume_dir`` shape.

    """

    def build_client(
        url: str, token: str, consume_dir: Path | None = None
    ) -> PaperlessClient:
        """
        Build a real client whose transport is ``recorder``.

        Returns:
            A client that never opens a socket.

        """
        if recorder.refuses_uploads:
            return PaperlessClient(
                url,
                token,
                consume_dir,
                max_retries=1,
                transport=httpx2.MockTransport(recorder),
            )
        return PaperlessClient(
            url, token, consume_dir, transport=httpx2.MockTransport(recorder)
        )

    return build_client


# The session cookie the loopback server sets on every upload answer.  Nothing
# saneless itself logs carries response headers, so this value can reach a log
# only through httpcore2's DEBUG trace, which prints them: a test that finds it
# has found that trace switched on.
LOOPBACK_SESSION_COOKIE = "sessionid=lb-5f0c8e2d91"

LOOPBACK_TASK_ID = "loopback-task-1"


@dataclass(frozen=True)
class LoopbackHit:
    """
    One request the loopback server received.

    Attributes:
        method: The HTTP method.
        path: The URL path, without the query string.
        authorization: The ``Authorization`` header as received, or None.

    """

    method: str
    path: str
    authorization: str | None


@dataclass(frozen=True)
class LoopbackPaperless:
    """
    A running loopback paperless-ngx: where it listens and what it was sent.

    Attributes:
        url: The base URL to hand to ``PaperlessClient``.
        hits: Every request received, in arrival order.  Empty means nothing
            reached the socket at all.

    """

    url: str
    hits: list[LoopbackHit] = field(default_factory=list)


def _loopback_handler(hits: list[LoopbackHit]) -> type[BaseHTTPRequestHandler]:
    """
    Build a request handler class that records into ``hits``.

    Args:
        hits: The list every received request is appended to.

    Returns:
        A handler class for ``ThreadingHTTPServer``.

    """

    class _Handler(BaseHTTPRequestHandler):
        """Answer the paperless-ngx endpoints a client uses, and record each call."""

        def _record(self) -> str:
            """
            Record this request and return its path.

            Returns:
                The URL path, without the query string.

            """
            path = urlsplit(self.path).path
            hits.append(
                LoopbackHit(self.command, path, self.headers.get("Authorization"))
            )
            return path

        def _answer(
            self, status: int, payload: object, headers: dict[str, str] | None = None
        ) -> None:
            """
            Send one JSON response and end it.

            Args:
                status: The HTTP status code.
                payload: The value to encode as the JSON body.
                headers: Extra response headers.

            """
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            """Accept an upload and issue a task id; refuse any other POST."""
            path = self._record()
            length = self.headers.get("Content-Length")
            if length is None:
                # The body has to be drained before answering, and a chunked
                # one is more than this server needs to read: a client that
                # stops sending a length fails here, loudly.
                self._answer(411, {"detail": "Length Required"})
                return
            self.rfile.read(int(length))
            if path != DOCUMENTS_PATH:
                self._answer(404, {"detail": "Not found."})
                return
            self._answer(200, LOOPBACK_TASK_ID, {"Set-Cookie": LOOPBACK_SESSION_COOKIE})

        def do_GET(self) -> None:
            """Answer a task poll and the tag and correspondent lists."""
            path = self._record()
            if path == TASKS_PATH:
                query = parse_qs(urlsplit(self.path).query)
                task_id = query.get("task_id", [""])[0]
                self._answer(200, [{"task_id": task_id, "status": "SUCCESS"}])
            elif path in {TAGS_PATH, CORRESPONDENTS_PATH}:
                self._answer(200, _EMPTY_COLLECTION)
            else:
                self._answer(404, {"detail": "Not found."})

        @override
        def log_message(self, format: str, *args: object) -> None:
            """Stay silent: the default writes every request to stderr."""

    return _Handler


@contextlib.contextmanager
def production_debug_logging() -> Generator[None]:
    """
    Configure logging as ``output.log_level = "DEBUG"`` does, then undo it.

    A test that captures logs at DEBUG with only ``caplog.set_level`` checks a
    setup nobody runs: nothing caps the HTTP libraries there, so httpcore2's
    trace -- which quotes request headers, the token among them -- is captured
    too.  This runs the real ``configure_logging`` instead, the worst case a
    deployment can choose.  On exit the handlers it added are closed and
    removed and the root and ``saneless`` levels are put back; the HTTP library
    loggers are reset by the suite-wide fixture.

    Yields:
        Nothing; the restore runs on exit.

    """
    root = logging.getLogger()
    before = root.handlers[:]
    level = root.level
    configure_logging(None, "DEBUG", max_bytes=1024, backup_count=1)
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(level)
        logging.getLogger("saneless").setLevel(logging.NOTSET)


@contextlib.contextmanager
def loopback_paperless() -> Generator[LoopbackPaperless]:
    """
    Serve a minimal paperless-ngx on a real socket on 127.0.0.1.

    ``httpx2.MockTransport`` replaces the network transport, h11 included, so
    a request h11 would refuse to put on the wire -- a header value with
    surrounding whitespace or a control character in it -- sails through a
    mock and is answered.  Only a real socket shows what the production
    transport does with it, and whether anything reached the server at all.

    The server answers an upload with a task id and a ``Set-Cookie`` header
    (``LOOPBACK_SESSION_COOKIE``), a task poll with SUCCESS, and the tag and
    correspondent lists with an empty page.  Anything else is a 404.  It
    listens on an ephemeral port and is shut down and closed on exit, so no
    socket outlives the test.

    Yields:
        Where the server listens, and every request it received.

    """
    hits: list[LoopbackHit] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _loopback_handler(hits))
    thread = threading.Thread(
        target=server.serve_forever, name="loopback-paperless", daemon=True
    )
    thread.start()
    try:
        yield LoopbackPaperless(url=f"http://127.0.0.1:{server.server_port}", hits=hits)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
