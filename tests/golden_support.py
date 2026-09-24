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

Import it as ``from tests.golden_support import ...``; the bare
``golden_support`` form raises ``ModuleNotFoundError`` under pytest 9's
importlib mode, for the same reason ``tests.conftest`` is imported by its
package path.
"""

from __future__ import annotations

import email.parser
import email.policy
import io
from email.message import EmailMessage
from typing import TYPE_CHECKING

import httpx2
import pikepdf
from PIL import Image, ImageDraw

from saneless.paperless import PaperlessClient
from tests.conftest import StubScannerBackend, scan_batch

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
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
    takes the same keywords and returns a real client over ``recorder``.  One
    attempt per upload: the fallback is reached exactly as at any retry count,
    and no backoff pause is ever reached.

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
        return PaperlessClient(
            url=url,
            token=token,
            consume_dir=consume_dir,
            max_retries=1,
            transport=httpx2.MockTransport(recorder),
        )

    return build_client


def cli_client_builder(recorder: RecordingPaperless) -> Callable[..., PaperlessClient]:
    """
    Build the stand-in for ``saneless.cli.PaperlessClient``.

    The ``scan`` command passes its arguments positionally, so this stand-in
    takes them positionally; otherwise it is ``web_client_builder``'s client.

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
        return PaperlessClient(
            url,
            token,
            consume_dir,
            max_retries=1,
            transport=httpx2.MockTransport(recorder),
        )

    return build_client
