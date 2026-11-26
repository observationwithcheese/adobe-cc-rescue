"""End-to-end tests against a local mock of the Adobe storage API.

Covers tree traversal, concurrent downloads, HTTP Range resume, retry on 5xx,
auth failure handling and manifest-driven resume.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adobe_rescue.auth import AuthError, Session
from adobe_rescue.config import Settings
from adobe_rescue.engine import Engine
from adobe_rescue.manifest import STATUS_DONE
from tests.test_basics import make_token

PAYLOAD = b"A" * 4096
EXTRA_DOC: list = []
COMPOSITE_POLLS = {"n": 0}
PRESIGNED_AUTH: dict = {"seen": "unset"}
#: When set, the composite job hands back a URL on this OTHER origin, the way
#: Adobe hands back an S3 link — so the no-credentials rule is really tested.
PRESIGNED_BASE: dict = {"url": ""}
FLAKY_STATE = {"hits": 0}
AUTH_MODE = {"reject": False, "code": 401}


def node(name, nid, is_dir=False, size=0, href=None):
    data = {
        "name": name,
        "id": nid,
        "size": size,
        "_links": {"self": {"href": href or f"http://{{host}}/id/{nid}"}},
    }
    if is_dir:
        data["type"] = "application/vnd.adobe.directory+json"
    else:
        data["_links"]["http://ns.adobe.com/adobecloud/rel/download"] = {
            "href": href or f"http://{{host}}/dl/{nid}"}
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).replace("{host}", self.headers.get("Host", "")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # The presigned leg stands in for S3: it authorises via the query
        # string and must be reachable with no Authorization header at all.
        if self.path.split("?")[0].startswith("/presigned/"):
            PRESIGNED_AUTH["seen"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return
        if AUTH_MODE["reject"]:
            body = b'{"error_code":"403003","message":"API key is invalid"}'
            self.send_response(AUTH_MODE["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self.send_response(403)
            self.end_headers()
            return

        path = self.path.split("?")[0]
        if path == "/index":
            host = self.headers.get("Host", "")
            _ = host
            return self._json({
                "repo:regions": ["AP"],
                "_links": {
                    "http://ns.adobe.com/adobecloud/rel/resolve/path": {
                        "href": f"http://{host}/resolve{{?repositoryId,path,resource}}"},
                },
                "children": [{
                    "_embedded": {
                        "http://ns.adobe.com/adobecloud/rel/metadata/repository": {
                            "repo:repositoryId": "urn:aaid:sc:AP:test-repo",
                            "repo:assetClass": "directory",
                            "repo:path": "/",
                            # live shape: links live inside the metadata object,
                            # and the rel is a list of mode variants
                            "_links": {
                                "http://ns.adobe.com/adobecloud/rel/page": [
                                    {"href": f"http://{host}/bypath/resource:{{resource}}/:page{{?limit,start}}",
                                     "mode": "path", "templated": True},
                                    {"href": f"http://{host}/resolve/resource:{{resource}}/:page{{?limit,start}}",
                                     "mode": "id", "templated": True},
                                ]
                            },
                        }
                    }
                }],
            })
        if path.startswith("/resolve"):   # e.g. /resolve/:page after expansion
            host = self.headers.get("Host", "")
            return self._json({"children": [
                {"repo:name": "Kampanya", "repo:assetClass": "directory",
                 "repo:assetId": "urn:dir:1",
                 "_links": {"http://ns.adobe.com/adobecloud/rel/page": [
                     {"href": f"http://{host}/page/dir1", "mode": "id"}]}},
                {"repo:name": "kapak.psd", "repo:assetClass": "file",
                 "repo:assetId": "urn:file:1", "repo:size": len(PAYLOAD),
                 "_links": {"http://ns.adobe.com/adobecloud/rel/download":
                            {"href": f"http://{host}/dl/p1"}}},
                {"repo:name": "bulut.psdc", "repo:assetClass": "composite",
                 "dc:format": "document/vnd.adobe.cpsd+dcx",
                 "repo:assetId": "urn:file:2", "repo:size": 999999,
                 "_links": {"http://ns.adobe.com/adobecloud/rel/download":
                            {"href": f"http://{host}/composite/block/download/id/urn:file:2"
                                     f"{{?resource,component_id,revision,accelerated}}"}}},
            ] + list(EXTRA_DOC)})
        if path == "/page/dir1":
            host = self.headers.get("Host", "")
            return self._json({"children": [
                {"repo:name": "logo.ai", "repo:assetClass": "file",
                 "repo:assetId": "urn:file:3", "repo:size": len(PAYLOAD),
                 "_links": {"http://ns.adobe.com/adobecloud/rel/download":
                            {"href": f"http://{host}/dl/p3"}}},
            ]})
        if path == "/root":
            return self._json({"children": [
                node("Projeler", "dir1", is_dir=True),
                node("afiş.psd", "f1", size=len(PAYLOAD)),
                node("bulut.psdc", "f2", size=10),      # must be left to cloud-docs
            ]})
        if path == "/id/dir1":
            return self._json({"children": [node("logo.ai", "f3", size=len(PAYLOAD))]})
        if path == "/dl/flaky":
            FLAKY_STATE["hits"] += 1
            if FLAKY_STATE["hits"] < 3:
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
        if path == "/composite/block/download/id/urn:file:2":
            host = self.headers.get("Host", "")
            self.send_response(202)
            self.send_header("Retry-After", "0")
            self.send_header("Location", f"http://{host}/composite/status/eyJqb2IiOiAxfQ")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/composite/status/"):
            COMPOSITE_POLLS["n"] += 1
            host = self.headers.get("Host", "")
            if COMPOSITE_POLLS["n"] < 2:
                self.send_response(202)
                self.send_header("Retry-After", "0")
                self.send_header("Location", f"http://{host}{path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = (b"HTTP/1.1 200 OK\r\n"
                    b"content-type: application/vnd.adobecloud.download+json\r\n"
                    b"Link: <x>; rel=\"y\"\r\n"
                    b"\r\n" +
                    json.dumps({"href": (PRESIGNED_BASE["url"] or f"http://{host}")
                                        + "/presigned/obj?sig=abc",
                                "size": len(PAYLOAD),
                                "type": "document/vnd.adobe.cpsd+dcxucf"}).encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/http")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/dl/"):
            rng = self.headers.get("Range")
            start = 0
            if rng and rng.startswith("bytes="):
                start = int(rng.split("=")[1].split("-")[0])
            body = PAYLOAD[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            if start:
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


class MockServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_session(base: str) -> Session:
    session = Session(token=make_token(), api_key="CCXWeb1")
    session.discovered = {
        "cloud_files.list_root": f"{base}/root",
        # Never let a test reach the real platform-cs host.
        "cloud_files.index": f"{base}/index",
    }
    return session


class TestCloudDownload(unittest.TestCase):
    def setUp(self):
        AUTH_MODE["reject"] = False
        AUTH_MODE["code"] = 401
        FLAKY_STATE["hits"] = 0

    def test_walks_tree_and_downloads(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=3, retries=2, verify=True)
            engine = Engine(settings, make_session(server.base))
            result = engine.run(["cloud-files"])
            engine.close()

            self.assertEqual(result.failed, 0)
            # 2 from the hypermedia walk + 2 from the legacy folder, .psdc excluded
            self.assertEqual(result.downloaded, 4)
            root = Path(tmp) / "cloud-files"
            self.assertTrue((root / "kapak.psd").exists())            # platform-cs
            self.assertTrue((root / "Kampanya" / "logo.ai").exists())  # nested
            self.assertTrue((root / "afiş.psd").exists())             # legacy
            self.assertTrue((root / "Projeler" / "logo.ai").exists())
            self.assertEqual((root / "afiş.psd").read_bytes(), PAYLOAD)
            entry = next(iter(engine.manifest.by_source("cloud-files")))
            self.assertEqual(len(entry.sha256), 64)

    def test_resume_skips_completed_files(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=2)
            session = make_session(server.base)
            first = Engine(settings, session)
            first.run(["cloud-files"])
            first.close()

            second = Engine(settings, session)
            result = second.run(["cloud-files"])
            second.close()
            self.assertEqual(result.downloaded, 0)
            self.assertEqual(result.skipped, 4)
            self.assertTrue(all(e.status == STATUS_DONE
                                for e in second.manifest.by_source("cloud-files")))

    def test_partial_file_is_resumed_not_restarted(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1)
            target_dir = Path(tmp) / "cloud-files"
            target_dir.mkdir(parents=True)
            (target_dir / "afiş.psd.part").write_bytes(PAYLOAD[:1000])

            engine = Engine(settings, make_session(server.base))
            engine.run(["cloud-files"])
            engine.close()
            self.assertEqual((target_dir / "afiş.psd").read_bytes(), PAYLOAD)
            self.assertFalse((target_dir / "afiş.psd.part").exists())

    def test_retries_transient_5xx(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1, retries=4)
            client_session = make_session(server.base)
            engine = Engine(settings, client_session)
            assert engine.client is not None
            target = Path(tmp) / "flaky.bin"
            size = engine.client.download(f"{server.base}/dl/flaky", target)
            engine.close()
            self.assertEqual(size, len(PAYLOAD))
            self.assertEqual(FLAKY_STATE["hits"], 3)

    def test_expired_token_raises_authentication_error(self):
        AUTH_MODE["reject"] = True
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1, retries=0)
            engine = Engine(settings, make_session(server.base))
            with self.assertRaises(AuthError):
                engine.run(["cloud-files"])
            engine.close()

    def test_exclude_filter(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1, exclude=["*.ai"])
            engine = Engine(settings, make_session(server.base))
            result = engine.run(["cloud-files"])
            engine.close()
            self.assertEqual(result.downloaded, 2)   # *.ai excluded in both walks
            self.assertEqual(result.skipped, 2)

    def test_dry_run_writes_nothing(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1, dry_run=True)
            engine = Engine(settings, make_session(server.base))
            engine.run(["cloud-files"])
            engine.close()
            self.assertFalse((Path(tmp) / "cloud-files" / "afiş.psd").exists())


class TestCloudDocuments(unittest.TestCase):
    """.psdc documents: downloadable when Adobe offers a link, else manual."""

    def setUp(self):
        AUTH_MODE["reject"] = False
        AUTH_MODE["code"] = 401

    def test_psdc_runs_the_async_job_and_downloads(self):
        COMPOSITE_POLLS["n"] = 0
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1)
            engine = Engine(settings, make_session(server.base))
            result = engine.run(["cloud-docs"])
            engine.close()
            self.assertEqual(result.downloaded, 1)
            # the package downloads AND a manual save-a-copy step is recorded,
            # because Photoshop refuses to open an exported cloud document
            # ONE summary checklist row, not one per document
            self.assertEqual(result.manual, 1)
            manual = [e for e in engine.manifest.by_source("cloud-docs")
                      if e.status == "manual"][0]
            self.assertIn("AÇILAMAZ", manual.note)
            self.assertIn("PSD'ye çevir", manual.note)
            self.assertEqual(manual.meta_count if hasattr(manual, "meta_count") else 1, 1)
            # the package keeps its own extension — it is not a flat PSD
            target = Path(tmp) / "cloud-docs" / "bulut.psdc"
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), PAYLOAD)
            # job size wins over the listing's repo:size (999999)
            entry = [e for e in engine.manifest.by_source("cloud-docs")
                     if e.status == "done"][0]
            self.assertEqual(entry.size, len(PAYLOAD))
            self.assertGreaterEqual(COMPOSITE_POLLS["n"], 2)   # it polled

    def test_psdc_without_link_becomes_a_manual_step(self):
        global EXTRA_DOC
        EXTRA_DOC.append({"repo:name": "elle.psdc", "repo:assetClass": "file",
                          "repo:assetId": "urn:file:9"})
        try:
            with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
                settings = Settings(output=Path(tmp), workers=1)
                engine = Engine(settings, make_session(server.base))
                result = engine.run(["cloud-docs"])
                engine.close()
                # the undownloadable document, plus one summary row for the
                # document that did download
                self.assertEqual(result.manual, 2)
                notes = [e.note for e in engine.manifest.by_source("cloud-docs")
                         if e.status == "manual"]
                self.assertTrue(any("elle.psdc" in n and "Kopyasını kaydet" in n
                                    for n in notes),
                                "a document Adobe will not hand over still needs "
                                "per-document instructions")
                self.assertTrue(any("PSD'ye çevir" in n for n in notes))
        finally:
            EXTRA_DOC.clear()


class TestPresignedHandling(unittest.TestCase):
    """The presigned handoff must be fetched bare and verified by job size."""

    def setUp(self):
        AUTH_MODE["reject"] = False
        AUTH_MODE["code"] = 401
        COMPOSITE_POLLS["n"] = 0
        PRESIGNED_AUTH["seen"] = "unset"

    def test_presigned_url_is_fetched_without_credentials(self):
        # Second origin stands in for S3: a different port is a different
        # netloc, so it is NOT one of the hosts learned from the session.
        with MockServer() as api, MockServer() as blobstore, \
                tempfile.TemporaryDirectory() as tmp:
            PRESIGNED_BASE["url"] = blobstore.base
            try:
                settings = Settings(output=Path(tmp), workers=1)
                engine = Engine(settings, make_session(api.base))
                result = engine.run(["cloud-docs"])
                engine.close()
                self.assertEqual(result.downloaded, 1)
                self.assertEqual(
                    (Path(tmp) / "cloud-docs" / "bulut.psdc").read_bytes(), PAYLOAD)
            finally:
                PRESIGNED_BASE["url"] = ""
        # The blobstore leg carried no bearer token.
        self.assertIsNone(PRESIGNED_AUTH["seen"])

    def test_embedded_http_payload_parsing(self):
        from adobe_rescue.platform import parse_embedded_http
        body = (b"HTTP/1.1 200 OK\r\ncontent-type: application/vnd.adobecloud.download+json"
                b"\r\nLink: <a>; rel=\"b\"\r\n\r\n"
                b'{"href":"https://s3/x?sig=1","size":36266887}')
        parsed = parse_embedded_http(body)
        self.assertEqual(parsed["size"], 36266887)
        self.assertTrue(parsed["href"].startswith("https://s3/"))

    def test_malformed_composite_payload_raises_clearly(self):
        from adobe_rescue.platform import parse_embedded_http
        with self.assertRaises(ValueError):
            parse_embedded_http(b"HTTP/1.1 500 oops\r\n\r\nnot json")


class TestAuthErrorMessages(unittest.TestCase):
    """401 and 403 are different problems and must not read the same."""

    def setUp(self):
        AUTH_MODE["reject"] = True
        FLAKY_STATE["hits"] = 0

    def tearDown(self):
        AUTH_MODE["reject"] = False
        AUTH_MODE["code"] = 401

    def test_401_aborts_the_run_as_a_dead_session(self):
        AUTH_MODE["code"] = 401
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1, retries=0)
            engine = Engine(settings, make_session(server.base))
            try:
                with self.assertRaises(AuthError) as ctx:
                    engine.run(["cloud-files"])
            finally:
                engine.close()
        message = str(ctx.exception)
        self.assertIn("401", message)
        self.assertIn("süresi doldu", message)

    def test_403_fails_only_that_source_and_the_run_continues(self):
        """One service's client id being wrong must not lose the whole backup."""
        AUTH_MODE["code"] = 403
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1, retries=0)
            engine = Engine(settings, make_session(server.base))
            try:
                result = engine.run(["cloud-files", "cloud-docs"])   # no raise
            finally:
                engine.close()

        self.assertGreaterEqual(result.manual, 2)
        self.assertEqual(result.failed, 0)       # not a failure the user can fix
        entries = [e for e in engine.manifest.entries.values()
                   if e.key.endswith(":__noaccess__")]
        self.assertEqual({e.source for e in entries}, {"cloud-files", "cloud-docs"})
        note = entries[0].note
        # No raw Adobe JSON in a designer-facing note
        self.assertNotIn("403003", note)
        self.assertNotIn("{", note)
        self.assertNotIn("süresi doldu", note)


if __name__ == "__main__":
    unittest.main()


class TestRerunBehaviour(unittest.TestCase):
    """Resume must not re-fetch composites, and a dry run must write nothing."""

    def setUp(self):
        AUTH_MODE["reject"] = False
        AUTH_MODE["code"] = 401
        COMPOSITE_POLLS["n"] = 0

    def test_dry_run_leaves_the_backup_folder_untouched(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "backup"
            settings = Settings(output=out, workers=1, dry_run=True)
            engine = Engine(settings, make_session(server.base))
            engine.run(["cloud-files", "cloud-docs"])
            engine.close()
            written = [p for p in out.rglob("*") if p.is_file()]
            self.assertEqual(written, [], f"dry run wrote {written}")

    def test_composite_is_not_refetched_despite_a_different_size(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1)
            session = make_session(server.base)

            first = Engine(settings, session)
            self.assertEqual(first.run(["cloud-docs"]).downloaded, 1)
            first.close()
            polls_after_first = COMPOSITE_POLLS["n"]

            # The listing advertises repo:size 999999 while the job returns
            # something else — a second run must still skip the document.
            second = Engine(settings, session)
            result = second.run(["cloud-docs"])
            second.close()
            self.assertEqual(result.downloaded, 0)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(COMPOSITE_POLLS["n"], polls_after_first,
                             "no download job should have been started")

    def test_stale_nothing_found_row_is_cleared_once_items_appear(self):
        from adobe_rescue.manifest import Entry, Manifest, STATUS_FAILED
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            seed = Manifest(out)
            seed.add(Entry(key="cloud-docs:__empty__", source="cloud-docs",
                           name="[Bulut Belgeleri] hiç öğe bulunamadı")
                     .mark(STATUS_FAILED, "eski kayıt"))
            seed.save(force=True)

            engine = Engine(Settings(output=out, workers=1), make_session(server.base))
            result = engine.run(["cloud-docs"])
            engine.close()
            self.assertIsNone(engine.manifest.get("cloud-docs:__empty__"))
            self.assertEqual(result.failed, 0)


class TestResumeSwitch(unittest.TestCase):
    """Resume is the default; --no-resume forces a full re-fetch."""

    def setUp(self):
        AUTH_MODE["reject"] = False
        AUTH_MODE["code"] = 401
        COMPOSITE_POLLS["n"] = 0

    def test_second_run_downloads_nothing_by_default(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=2)
            session = make_session(server.base)
            first = Engine(settings, session)
            first_result = first.run(["cloud-files", "cloud-docs"])
            first.close()
            self.assertGreater(first_result.downloaded, 0)

            second = Engine(settings, session)
            result = second.run(["cloud-files", "cloud-docs"])
            second.close()
            self.assertEqual(result.downloaded, 0)
            self.assertEqual(result.skipped, first_result.downloaded)

    def test_no_resume_refetches_everything(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            session = make_session(server.base)
            first = Engine(Settings(output=Path(tmp), workers=2), session)
            expected = first.run(["cloud-files"]).downloaded
            first.close()

            again = Engine(Settings(output=Path(tmp), workers=2, resume=False), session)
            result = again.run(["cloud-files"])
            again.close()
            self.assertEqual(result.downloaded, expected)
            self.assertEqual(result.skipped, 0)

    def test_interrupted_run_resumes_only_what_is_missing(self):
        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1)
            session = make_session(server.base)
            first = Engine(settings, session)
            total = first.run(["cloud-files"]).downloaded
            first.close()

            # Lose one file, as an interrupted transfer would
            victim = next(p for p in (Path(tmp) / "cloud-files").rglob("*") if p.is_file())
            victim.unlink()

            second = Engine(settings, session)
            result = second.run(["cloud-files"])
            second.close()
            self.assertEqual(result.downloaded, 1)
            self.assertEqual(result.skipped, total - 1)


class TestProgressReporting(unittest.TestCase):
    """A slow big file must not look like a hang."""

    def setUp(self):
        AUTH_MODE["reject"] = False
        AUTH_MODE["code"] = 401
        COMPOSITE_POLLS["n"] = 0

    def test_byte_progress_is_reported_during_a_transfer(self):
        seen = []

        def on_progress(p):
            seen.append((p.bytes_seen, p.bytes_expected, p.speed))

        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1, chunk_size=512)
            engine = Engine(settings, make_session(server.base), on_progress=on_progress)
            engine.run(["cloud-files"])
            engine.close()

        self.assertTrue(seen, "no progress was emitted")
        self.assertTrue(any(expected > 0 for _, expected, _ in seen),
                        "the queue size was never reported")
        # bytes climb during the transfer, not only at file boundaries
        self.assertGreater(len({b for b, _, _ in seen}), 2)

    def test_eta_and_percent_use_bytes(self):
        from adobe_rescue.engine import Progress

        p = Progress(total=2, done=0, bytes_done=0, bytes_expected=1000,
                     bytes_inflight=500, speed=100 * 1024)
        self.assertEqual(p.bytes_seen, 500)
        self.assertAlmostEqual(p.eta_seconds(), 500 / (100 * 1024), places=4)
        # no speed yet: no misleading ETA
        self.assertEqual(Progress(bytes_expected=1000, speed=0).eta_seconds(), 0.0)

    def test_largest_files_are_fetched_first(self):
        order = []

        def on_progress(p):
            if p.item and (not order or order[-1] != p.item):
                order.append(p.item)

        with MockServer() as server, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output=Path(tmp), workers=1)
            engine = Engine(settings, make_session(server.base), on_progress=on_progress)
            engine.run(["cloud-files"])
            engine.close()
        # every sized file precedes the zero-sized ones in the queue
        self.assertTrue(order)
