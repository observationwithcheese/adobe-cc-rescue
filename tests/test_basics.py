"""Unit tests for the pieces that have no network dependency."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from base64 import urlsafe_b64encode
from pathlib import Path

from adobe_rescue import auth, config
from adobe_rescue.auth import Session
from adobe_rescue.manifest import Entry, Manifest, STATUS_DONE
from adobe_rescue.util import safe_name, safe_path, sha256_file


def make_token(exp_offset: int = 3600, email: str = "ozan@example.com") -> str:
    def seg(obj) -> str:
        raw = json.dumps(obj).encode()
        return urlsafe_b64encode(raw).decode().rstrip("=")
    header = seg({"alg": "RS256"})
    payload = seg({"exp": int(time.time()) + exp_offset, "user_id": "U123", "email": email})
    return f"{header}.{payload}.signature"


class TestSafePaths(unittest.TestCase):
    def test_illegal_characters_replaced(self):
        self.assertEqual(safe_name('a<b>c:"d|e?f*g'), "a_b_c__d_e_f_g")

    def test_turkish_preserved(self):
        self.assertEqual(safe_name("Şubat Kampanyası.psd"), "Şubat Kampanyası.psd")

    def test_windows_reserved(self):
        self.assertEqual(safe_name("CON"), "CON_")
        self.assertTrue(safe_name("con.txt").startswith("con_"))

    def test_long_name_keeps_extension(self):
        name = safe_name("x" * 300 + ".psd")
        self.assertTrue(name.endswith(".psd"))
        self.assertLessEqual(len(name), 120)

    def test_traversal_is_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = safe_path(root, ["..", "..", "etc", "passwd"])
            self.assertTrue(str(target.resolve()).startswith(str(root.resolve())))


class TestAuth(unittest.TestCase):
    def test_rejects_garbage(self):
        with self.assertRaises(auth.AuthError):
            auth.from_token("not-a-token")

    def test_accepts_bearer_prefix(self):
        session = auth.from_token("Bearer " + make_token())
        self.assertFalse(session.is_expired())
        self.assertEqual(session.email, "ozan@example.com")

    def test_expiry_detected(self):
        session = auth.from_token(make_token(exp_offset=-10))
        self.assertTrue(session.is_expired())

    def test_headers_contain_token(self):
        session = auth.from_token(make_token())
        headers = session.headers()
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertIn("x-api-key", headers)

    def test_har_extraction(self):
        token = make_token()
        har = {"log": {"entries": [
            {"request": {"url": "https://example.org/unrelated",
                         "headers": [{"name": "authorization", "value": "Bearer nope"}]}},
            {"request": {"url": "https://cc-api-storage.adobe.io/content/storage/id/root",
                         "headers": [
                             {"name": "authorization", "value": f"Bearer {token}"},
                             {"name": "x-api-key", "value": "CreativeCloudWeb"}]}},
        ]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps(har), encoding="utf-8")
            session = auth.from_har(path)
        self.assertEqual(session.token, token)
        self.assertEqual(session.api_key, "CreativeCloudWeb")
        self.assertIn("cloud_files.list_root", session.discovered)

    def test_har_without_token_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.har"
            path.write_text(json.dumps({"log": {"entries": [
                {"request": {"url": "https://adobe.com/x", "headers": []}}]}}), encoding="utf-8")
            with self.assertRaises(auth.AuthError):
                auth.from_har(path)

    def test_endpoint_override_wins(self):
        session = auth.from_token(make_token())
        self.assertEqual(session.endpoint("cloud_files", "list_root"),
                         config.ENDPOINTS["cloud_files"]["list_root"])
        session.discovered["cloud_files.list_root"] = "https://example.test/root"
        self.assertEqual(session.endpoint("cloud_files", "list_root"), "https://example.test/root")


class TestManifest(unittest.TestCase):
    def test_roundtrip_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = Manifest(root)
            entry = Entry(key="cloud-files:1", source="cloud-files", name="a.psd", size=10)
            manifest.add(entry)
            entry.mark(STATUS_DONE)
            manifest.update(entry)
            manifest.save(force=True)

            reloaded = Manifest(root)
            self.assertTrue(reloaded.is_done("cloud-files:1"))
            # re-discovery must not downgrade a completed entry
            reloaded.add(Entry(key="cloud-files:1", source="cloud-files", name="a.psd", size=10))
            self.assertTrue(reloaded.is_done("cloud-files:1"))

    def test_csv_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Manifest(Path(tmp))
            manifest.add(Entry(key="k", source="s", name="ş.psd"))
            path = manifest.export_csv()
            self.assertIn("ş.psd", path.read_text(encoding="utf-8-sig"))


class TestChecksums(unittest.TestCase):
    def test_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.bin"
            path.write_bytes(b"adobe")
            self.assertEqual(len(sha256_file(path)), 64)


class TestDeadline(unittest.TestCase):
    def test_env_override(self):
        os.environ["ADOBE_RESCUE_DEADLINE"] = "2026-11-28T23:00"
        try:
            due = config.deadline()
            self.assertIsNotNone(due)
            self.assertEqual(due.hour, 23)
        finally:
            del os.environ["ADOBE_RESCUE_DEADLINE"]


if __name__ == "__main__":
    unittest.main()


class TestBrowserLogin(unittest.TestCase):
    """Token extraction from Adobe IMS localStorage (no browser needed)."""

    def setUp(self):
        from adobe_rescue import browser_login
        self.bl = browser_login
        self.token = make_token()

    def _storage(self, **overrides):
        items = {
            "adobeid_ims_access_token/CreativeCloudWeb/false/AdobeID,openid":
                json.dumps({"tokenValue": self.token, "expire": 1900000000000}),
            "adobeid_ims_profile/CreativeCloudWeb/false/AdobeID,openid":
                json.dumps({"email": "kullanici@sirket.com", "name": "Ozan"}),
        }
        items.update(overrides)
        return items

    def test_extracts_token_client_and_email(self):
        parsed = self.bl.parse_storage(self._storage())
        self.assertEqual(parsed["token"], self.token)
        self.assertEqual(parsed["api_key"], "CreativeCloudWeb")
        self.assertEqual(parsed["email"], "kullanici@sirket.com")
        self.assertGreater(parsed["expires"], time.time())

    def test_builds_usable_session(self):
        session = self.bl.session_from_storage(self._storage())
        self.assertIsNotNone(session)
        self.assertFalse(session.is_expired())
        self.assertTrue(session.headers()["Authorization"].startswith("Bearer "))

    def test_milliseconds_expiry_normalised(self):
        parsed = self.bl.parse_storage(self._storage())
        self.assertLess(parsed["expires"], 1e11)

    def test_bare_token_value_accepted(self):
        items = {"adobeid_ims_access_token/ExpressWeb/false/x": self.token}
        parsed = self.bl.parse_storage(items)
        self.assertEqual(parsed["token"], self.token)
        self.assertEqual(parsed["api_key"], "ExpressWeb")
        # falls back to the JWT's own exp
        self.assertGreater(parsed["expires"], time.time())

    def test_longest_lived_token_wins(self):
        short = make_token(exp_offset=60)
        items = {
            "adobeid_ims_access_token/A/false/x":
                json.dumps({"tokenValue": short, "expire": 1700000000000}),
            "adobeid_ims_access_token/B/false/x":
                json.dumps({"tokenValue": self.token, "expire": 1900000000000}),
        }
        self.assertEqual(self.bl.parse_storage(items)["api_key"], "B")

    def test_ignores_non_token_entries(self):
        self.assertIsNone(self.bl.parse_storage({"unrelated": "x"}))
        self.assertIsNone(self.bl.parse_storage({}))
        self.assertIsNone(self.bl.parse_storage({"__error__": "SecurityError"}))

    def test_rejects_malformed_values(self):
        items = {"adobeid_ims_access_token/A/false/x": "not-json-not-jwt"}
        self.assertIsNone(self.bl.parse_storage(items))
        items = {"adobeid_ims_access_token/A/false/x": json.dumps({"tokenValue": "abc"})}
        self.assertIsNone(self.bl.parse_storage(items))

    def test_worker_command_targets_self(self):
        cmd = self.bl._worker_command(Path("/tmp/out.json"))
        self.assertIn("--browser-worker", cmd)
        self.assertIn("/tmp/out.json", cmd)


class TestEntryPointRouting(unittest.TestCase):
    """The packaged binary is both the window and the CLI it re-invokes."""

    def test_arguments_route_to_cli(self):
        import types
        from unittest import mock
        from adobe_rescue import app

        with mock.patch("adobe_rescue.cli.main", return_value=7) as cli_main:
            self.assertEqual(app.main(["login", "--browser-worker", "--out", "x"]), 7)
        cli_main.assert_called_once_with(["login", "--browser-worker", "--out", "x"])

    def test_no_arguments_route_to_gui(self):
        import sys as _sys
        import types
        stub = types.ModuleType("adobe_rescue.gui")
        calls = []
        stub.main = lambda: (calls.append(True), 0)[1]   # type: ignore[attr-defined]
        original = _sys.modules.get("adobe_rescue.gui")
        _sys.modules["adobe_rescue.gui"] = stub
        try:
            from adobe_rescue import app
            self.assertEqual(app.main([]), 0)
        finally:
            if original is None:
                _sys.modules.pop("adobe_rescue.gui", None)
            else:
                _sys.modules["adobe_rescue.gui"] = original
        self.assertEqual(len(calls), 1)

    def test_worker_command_is_routable(self):
        """The command browser_login builds must be one app.main() routes to CLI."""
        from unittest import mock
        from adobe_rescue import app, browser_login

        cmd = browser_login._worker_command(Path("/tmp/x.json"))
        # strip the interpreter/executable prefix the OS consumes
        args = cmd[1:]
        if args and args[0] == "-m":
            args = args[2:]
        with mock.patch("adobe_rescue.cli.main", return_value=0) as cli_main:
            app.main(args)
        cli_main.assert_called_once()
        self.assertIn("--browser-worker", cli_main.call_args[0][0])


class TestLoginProbe(unittest.TestCase):
    """PROBE_JS result parsing — the adobeIMS path and the storage fallback."""

    def setUp(self):
        from adobe_rescue import browser_login
        self.bl = browser_login
        self.token = make_token()

    def test_adobe_ims_path_preferred(self):
        probe = json.dumps({
            "via": "adobeIMS",
            "token": self.token,
            "expire": 1900000000000,
            "clientId": "CreativeCloudWeb",
            "email": "kullanici@sirket.com",
            "items": {},
            "keys": ["adobeid_ims_access_token/CreativeCloudWeb/false/x"],
        })
        parsed = self.bl.parse_probe(probe)
        self.assertEqual(parsed["token"], self.token)
        self.assertEqual(parsed["via"], "adobeIMS")
        self.assertEqual(parsed["email"], "kullanici@sirket.com")
        self.assertLess(parsed["expires"], 1e11)

    def test_falls_back_to_storage_when_ims_absent(self):
        probe = json.dumps({
            "via": "", "token": "", "expire": 0, "clientId": "", "email": "",
            "items": {"adobeid_ims_access_token/CreativeCloudWeb/false/x":
                      json.dumps({"tokenValue": self.token, "expire": 1900000000000})},
            "keys": ["adobeid_ims_access_token/CreativeCloudWeb/false/x"],
        })
        parsed = self.bl.parse_probe(probe)
        self.assertEqual(parsed["token"], self.token)
        self.assertEqual(parsed["via"], "storage")

    def test_not_signed_in_yet(self):
        probe = json.dumps({"via": "", "token": "", "items": {}, "keys": []})
        self.assertIsNone(self.bl.parse_probe(probe))
        self.assertIsNone(self.bl.parse_probe(""))
        self.assertIsNone(self.bl.parse_probe(None))

    def test_alternative_key_shapes_match(self):
        for key in ("adobeid_ims_access_token/X/false/y",
                    "ADOBEID_IMS_ACCESS_TOKEN/X/false/y",
                    "ims_access_token_v2",
                    "adobe_ims/token/CreativeCloudWeb"):
            with self.subTest(key=key):
                self.assertTrue(self.bl._looks_like_token_key(key))

    def test_refresh_and_device_tokens_ignored(self):
        self.assertFalse(self.bl._looks_like_token_key("adobeid_ims_refresh_token/X/y"))
        self.assertFalse(self.bl._looks_like_token_key("adobeid_ims_device_token/X/y"))
        items = {"adobeid_ims_refresh_token/X/y": json.dumps({"tokenValue": self.token})}
        self.assertIsNone(self.bl.parse_storage(items))

    def test_probe_keys_returns_names_not_values(self):
        probe = json.dumps({"keys": ["adobeid_ims_access_token/A/b"],
                            "items": {"adobeid_ims_access_token/A/b": self.token}})
        keys = self.bl.probe_keys(probe)
        self.assertEqual(keys, ["adobeid_ims_access_token/A/b"])
        self.assertNotIn(self.token, "".join(keys))

    def test_expired_probe_token_is_not_accepted_as_session(self):
        probe = json.dumps({"token": make_token(exp_offset=-60), "expire": 0,
                            "items": {}, "keys": []})
        parsed = self.bl.parse_probe(probe)
        session = Session(token=parsed["token"], expires=parsed["expires"])
        self.assertTrue(session.is_expired())


class TestCaptureDigest(unittest.TestCase):
    """Learning api keys and endpoints from the web app's own requests."""

    def setUp(self):
        from adobe_rescue import browser_login
        self.bl = browser_login

    def test_learns_api_key_and_endpoint(self):
        entries = [
            {"url": "https://cc-api-storage.adobe.io/content/storage/id/root?limit=50",
             "headers": {"x-api-key": "cc-web-storage", "Authorization": "Bearer x"}},
            {"url": "https://analytics.example.com/t", "headers": {"x-api-key": "nope"}},
        ]
        digest = self.bl.digest_capture(json.dumps(entries))
        self.assertEqual(digest["api_key"], "cc-web-storage")
        self.assertEqual(digest["discovered"]["cloud_files.api_key"], "cc-web-storage")
        self.assertEqual(digest["discovered"]["cloud_docs.api_key"], "cc-web-storage")
        self.assertIn("cloud_files.list_root", digest["discovered"])
        self.assertEqual(digest["requests"], 1)      # non-Adobe host ignored

    def test_per_service_keys_do_not_leak_across_services(self):
        entries = [
            {"url": "https://cc-api-storage.adobe.io/content/storage/id/root",
             "headers": {"x-api-key": "cc-web-storage"}},
            {"url": "https://lr.adobe.io/v2/catalog",
             "headers": {"x-api-key": "LightroomWeb2"}},
        ]
        d = self.bl.digest_capture(entries)["discovered"]
        self.assertEqual(d["cloud_files.api_key"], "cc-web-storage")
        self.assertEqual(d["lightroom.api_key"], "LightroomWeb2")

    def test_learned_key_overrides_builtin_default(self):
        from adobe_rescue.auth import Session
        session = Session(token=make_token())
        self.assertEqual(session.endpoint("cloud_files", "api_key"), "CCXWeb1")
        session.discovered["cloud_files.api_key"] = "cc-web-storage"
        self.assertEqual(session.endpoint("cloud_files", "api_key"), "cc-web-storage")
        # an untouched service keeps its own default
        self.assertEqual(session.endpoint("lightroom", "api_key"), "LightroomWeb")

    def test_replay_headers_collected(self):
        entries = [{"url": "https://cc-api-storage.adobe.io/x",
                    "headers": {"x-api-key": "k", "x-product": "CCWeb/1.0",
                                "cookie": "secret", "authorization": "Bearer t"}}]
        extra = self.bl.digest_capture(entries)["extra_headers"]
        self.assertEqual(extra, {"x-product": "CCWeb/1.0"})
        self.assertNotIn("cookie", extra)
        self.assertNotIn("authorization", extra)

    def test_empty_and_malformed_input(self):
        self.assertEqual(self.bl.digest_capture("[]"), {})
        self.assertEqual(self.bl.digest_capture("not json"), {})
        self.assertEqual(self.bl.digest_capture(None), {})
        self.assertEqual(self.bl.digest_capture([{"url": "https://example.com"}]), {})

    def test_groups_for_url(self):
        self.assertEqual(self.bl.groups_for_url("https://lr.adobe.io/v2/catalog"), ("lightroom",))
        self.assertEqual(self.bl.groups_for_url("https://example.com"), ())


class TestDiscoverCommand(unittest.TestCase):
    """`discover` must print endpoints and api keys but never credentials."""

    def _har(self, tmp: Path) -> Path:
        har = {"log": {"entries": [
            {"request": {"method": "GET",
                         "url": "https://cc-api-storage.adobe.io/content/storage/id/root?limit=50",
                         "headers": [
                             {"name": "authorization", "value": "Bearer SUPERSECRETTOKEN"},
                             {"name": "cookie", "value": "sess=SECRETCOOKIE"},
                             {"name": "x-api-key", "value": "cc-web-storage"}]},
             "response": {"status": 200}},
            {"request": {"method": "GET", "url": "https://cc-api-storage.adobe.io/app.js",
                         "headers": []}, "response": {"status": 200}},
            {"request": {"method": "GET", "url": "https://analytics.example.com/t",
                         "headers": []}, "response": {"status": 200}},
        ]}}
        path = tmp / "c.har"
        path.write_text(json.dumps(har), encoding="utf-8")
        return path

    def test_lists_api_calls_without_secrets(self):
        import io
        import contextlib
        from adobe_rescue.cli import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            har = self._har(Path(tmp))
            stream = io.StringIO()
            # cli.main() rebuilds logging onto sys.stdout, so redirect first.
            with contextlib.redirect_stdout(stream):
                code = cli_main(["discover", "--har", str(har)])
        output = stream.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("cc-api-storage.adobe.io", output)
        self.assertIn("cc-web-storage", output)          # api key: public client id
        self.assertNotIn("SUPERSECRETTOKEN", output)     # never
        self.assertNotIn("SECRETCOOKIE", output)         # never
        self.assertNotIn("app.js", output)               # static assets filtered
        self.assertNotIn("analytics.example.com", output)


class TestTruncatedToken(unittest.TestCase):
    """A token copied from Chrome's Headers *display* is cut off with '…'."""

    def test_ellipsis_token_rejected_with_a_useful_message(self):
        truncated = make_token()[:512] + "…"
        with self.assertRaises(auth.AuthError) as ctx:
            auth.from_token(truncated)
        message = str(ctx.exception)
        self.assertIn("…", message)
        self.assertIn("Copy value", message)

    def test_other_non_ascii_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.check_token_text(make_token() + "ş")

    def test_clean_token_passes(self):
        self.assertIsNone(auth.check_token_text(make_token()))

    def test_bad_header_never_reaches_the_socket(self):
        import http.client, io
        session = auth.from_token(make_token())
        session.extra_headers = {"x-product": "Creative Cloud…Web"}
        headers = session.headers()
        self.assertNotIn("x-product", headers)       # dropped, not crashed
        conn = http.client.HTTPConnection("example.com")
        conn.sock = io.BytesIO()
        conn.putrequest("GET", "/")
        for name, value in headers.items():          # must all be sendable
            conn.putheader(name, value)

    def test_corrupt_authorization_raises_rather_than_being_dropped(self):
        session = auth.Session(token="eyJ.a." + "x" * 10 + "…")
        with self.assertRaises(auth.AuthError) as ctx:
            session.headers()
        self.assertIn("bozuk", str(ctx.exception))

    def test_api_key_defaults_to_the_tokens_own_client(self):
        import json as _json
        from base64 import urlsafe_b64encode

        def seg(obj):
            return urlsafe_b64encode(_json.dumps(obj).encode()).decode().rstrip("=")
        token = f"{seg({'alg':'RS256'})}.{seg({'client_id':'cc-web-storage','exp':9999999999})}.sig"
        session = auth.from_token(token)
        self.assertEqual(session.api_key, "cc-web-storage")
        self.assertEqual(session.headers()["x-api-key"], "cc-web-storage")


class TestFontNames(unittest.TestCase):
    """Font family names are read from the font binary, not the filename."""

    def _font(self, family: str) -> bytes:
        import struct
        encoded = family.encode("utf-16-be")
        records = struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(encoded), 0)
        name_table = struct.pack(">HHH", 0, 1, 6 + 12) + records + encoded
        header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
        entry = b"name" + struct.pack(">III", 0, 12 + 16, len(name_table))
        return header + entry + name_table

    def test_reads_family_name(self):
        from adobe_rescue.fontnames import font_name
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "5088.otf"       # Adobe's opaque file name
            path.write_bytes(self._font("Proxima Nova"))
            self.assertEqual(font_name(path), "Proxima Nova")

    def test_turkish_family_name_survives(self):
        from adobe_rescue.fontnames import font_name, names_in_tree
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1.otf"
            path.write_bytes(self._font("Gülhan Şerif"))
            self.assertEqual(font_name(path), "Gülhan Şerif")
            self.assertEqual(names_in_tree(Path(tmp)), {"Gülhan Şerif"})

    def test_garbage_files_are_ignored(self):
        from adobe_rescue.fontnames import font_name, names_in_tree
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.otf").write_bytes(b"not a font at all")
            (Path(tmp) / "y.txt").write_text("hello", encoding="utf-8")
            self.assertIsNone(font_name(Path(tmp) / "x.otf"))
            self.assertEqual(names_in_tree(Path(tmp)), set())


class TestCredentialScope(unittest.TestCase):
    """The account token must never reach a non-Adobe host."""

    def setUp(self):
        self.session = auth.from_token(make_token())

    def test_adobe_hosts_get_credentials(self):
        for url in ("https://platform-cs.adobe.io/index",
                    "https://platform-cs-jpn3.adobe.io/content/storage/id/x/:page",
                    "https://cc-api-storage.adobe.io/files"):
            with self.subTest(url=url):
                headers = self.session.headers(url=url)
                self.assertIn("Authorization", headers)
                self.assertIn("x-api-key", headers)

    def test_presigned_cdn_links_get_none(self):
        for url in ("https://s3.amazonaws.com/bucket/obj?X-Amz-Signature=abc",
                    "https://cdn.cloudfront.net/x",
                    "https://evil.example.com/steal"):
            with self.subTest(url=url):
                headers = self.session.headers(url=url)
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("x-api-key", headers)
                self.assertNotIn(self.session.token, repr(headers))

    def test_lookalike_domains_are_not_adobe(self):
        for url in ("https://adobe.io.evil.com/x", "https://notadobe.com/x"):
            with self.subTest(url=url):
                self.assertNotIn("Authorization", self.session.headers(url=url))

    def test_hosts_learned_from_the_session_are_trusted(self):
        self.assertFalse(self.session.trusted("http://127.0.0.1:8080/x"))
        self.session.discovered["cloud_files.index"] = "http://127.0.0.1:8080/index"
        self.assertTrue(self.session.trusted("http://127.0.0.1:8080/x"))

    def test_no_url_given_still_authenticates(self):
        self.assertIn("Authorization", self.session.headers())


class TestLinkTemplates(unittest.TestCase):
    """Adobe's link templates, in the shapes the live API returns."""

    def setUp(self):
        from adobe_rescue import platform
        self.p = platform

    def test_drops_unsupplied_path_segment(self):
        # Guessing resource= returns 4001 "Resource=... is not supported";
        # omitting the segment returns 200.
        template = ("https://platform-cs-jpn3.adobe.io/content/storage/id/R"
                    "/resource:{resource}/:page{?orderBy,start,limit,type}")
        self.assertEqual(self.p.expand(template, limit=100),
                         "https://platform-cs-jpn3.adobe.io/content/storage/id/R"
                         "/:page?limit=100")

    def test_fills_path_segment_when_given(self):
        out = self.p.expand("https://x/id/R/resource:{resource}/:page", resource="page")
        self.assertEqual(out, "https://x/id/R/resource:page/:page")

    def test_prefers_id_mode_link(self):
        links = {"http://ns.adobe.com/adobecloud/rel/page": [
            {"href": "https://x/by-path", "mode": "path"},
            {"href": "https://x/by-id", "mode": "id"}]}
        self.assertEqual(self.p.pick_link(links, ("rel/page",)), "https://x/by-id")

    def test_links_inside_embedded_metadata_are_found(self):
        node = {"_embedded": {"http://ns.adobe.com/adobecloud/rel/metadata/repository": {
            "repo:repositoryId": "urn:aaid:sc:AP:r",
            "_links": {"http://ns.adobe.com/adobecloud/rel/page": [
                {"href": "https://x/page", "mode": "id"}]}}}}
        self.assertEqual(self.p.pick_link(self.p.links_of(node), ("rel/page",)),
                         "https://x/page")

    def test_composite_is_an_asset_not_a_directory(self):
        doc = {"repo:assetClass": "composite", "repo:name": "Balcony_Bg.psdc",
               "dc:format": "document/vnd.adobe.cpsd+dcx"}
        self.assertFalse(self.p.is_directory(doc))
        self.assertEqual(self.p.node_name(doc), "Balcony_Bg.psdc")
        self.assertTrue(self.p.is_directory({"repo:assetClass": "directory"}))


class TestDcxSalvage(unittest.TestCase):
    """What can be recovered from a .psdc without Adobe."""

    def _package(self, tmp: Path, cols: int, renditions: dict) -> Path:
        import zipfile
        pkg = tmp / "Belge.psdc"
        with zipfile.ZipFile(pkg, "w") as z:
            z.writestr("mimetype", "application/vnd.adobe.cpsd+dcx")
            z.writestr("manifest", json.dumps({"cpsd#cols": cols, "cpsd#rows": cols,
                                               "cpsd#sheet-count": 46}))
            z.writestr("layer-data", b"\x00" * 128)          # not a PSD stream
            for name, size in renditions.items():
                z.writestr(name, b"\x89PNG" + b"\x00" * size)
        return pkg

    def test_identifies_a_full_resolution_rendition(self):
        from adobe_rescue import dcx
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._package(Path(tmp), 1024, {
                "renditions/image-1024-1024.png": 5000,
                "renditions/image-512-512.png": 900})
            facts = dcx.describe(pkg)
            self.assertTrue(facts["full_resolution"])
            self.assertEqual(facts["entry"], "renditions/image-1024-1024.png")

    def test_flags_a_rendition_smaller_than_the_document(self):
        from adobe_rescue import dcx
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._package(Path(tmp), 6000, {"renditions/image-1920-1920.png": 5000})
            facts = dcx.describe(pkg)
            self.assertFalse(facts["full_resolution"])
            self.assertEqual(facts["width"], 1920)
            self.assertEqual(facts["cols"], 6000)

    def test_extracts_the_largest_one(self):
        from adobe_rescue import dcx
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = self._package(root, 1024, {
                "renditions/image-1024-1024.png": 5000,
                "renditions/thumbnail-512-512.png": 900})
            out = dcx.extract_rendition(pkg, root / "out")
            self.assertIsNotNone(out)
            self.assertIn("tam çözünürlük", out.name)
            self.assertEqual(len(out.read_bytes()), 5004)

    def test_package_without_renditions_is_handled(self):
        from adobe_rescue import dcx
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._package(Path(tmp), 1024, {})
            self.assertFalse(dcx.describe(pkg)["ok"])
            self.assertIsNone(dcx.extract_rendition(pkg, Path(tmp) / "out"))

    def test_corrupt_package_does_not_raise(self):
        from adobe_rescue import dcx
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.psdc"
            bad.write_bytes(b"not a zip")
            self.assertFalse(dcx.describe(bad).get("ok"))
            self.assertIsNone(dcx.extract_rendition(bad, Path(tmp) / "out"))


class TestPhotoshopBatchSafety(unittest.TestCase):
    """This tool closes documents, so eligibility is a safety boundary."""

    def setUp(self):
        from adobe_rescue import psd_convert
        self.m = psd_convert

    def _doc(self, **kw):
        base = {"name": "Doc.psdc", "saved": True, "cloud": True, "hasPath": False}
        base.update(kw)
        return base

    def test_unsaved_document_is_never_touched(self):
        for doc in (self._doc(saved=False),
                    self._doc(saved=False, cloud=False, hasPath=True, name="work.psd")):
            with self.subTest(doc=doc["name"]):
                ok, why = self.m.eligibility(doc, None, False)
                self.assertFalse(ok)
                self.assertIn("kaydedilmemiş", why)

    def test_local_file_is_left_alone_by_default(self):
        ok, _ = self.m.eligibility(
            self._doc(name="rapor.psd", cloud=False, hasPath=True), None, False)
        self.assertFalse(ok)

    def test_new_unsaved_document_is_never_converted(self):
        """A brand-new doc reports saved=true and has no path: the decoy case.

        Without requiring cloudDocument it would be saved as a blank canvas
        under a real document's name, closing the user's new work and
        blocking the genuine document as a resume-skip.
        """
        decoy = self._doc(name="SomeTVStuff", cloud=False, hasPath=False)
        for allowed in (None, {"sometvstuff"}):
            with self.subTest(allowed=allowed):
                ok, why = self.m.eligibility(decoy, allowed, False)
                self.assertFalse(ok)
                self.assertIn("bulut belgesi değil", why)

    def test_cloud_document_with_a_synthetic_path_is_still_eligible(self):
        """Cloud docs DO return a fullName, so a path must not disqualify."""
        doc = self._doc(name="SomeTVStuff.psdc", cloud=True, hasPath=True)
        self.assertTrue(self.m.eligibility(doc, None, False)[0])

    def test_local_file_only_with_explicit_opt_in(self):
        ok, _ = self.m.eligibility(
            self._doc(name="rapor.psd", cloud=False, hasPath=True), None, True)
        self.assertTrue(ok)

    def test_new_untitled_document_is_left_alone(self):
        for name in ("Untitled-1", "Adsız-2", "untitled"):
            with self.subTest(name=name):
                ok, _ = self.m.eligibility(
                    self._doc(name=name, cloud=False, hasPath=False), None, False)
                self.assertFalse(ok)
                # even with --include-local, a never-saved doc is off limits
                self.assertFalse(self.m.eligibility(
                    self._doc(name=name, cloud=False, hasPath=False), None, True)[0])

    def test_name_list_is_an_exact_allowlist(self):
        allowed = {"balcony_bg"}
        self.assertTrue(self.m.eligibility(
            self._doc(name="Balcony_Bg.psdc"), allowed, False)[0])
        # even a genuine cloud document is skipped when not on the list
        self.assertFalse(self.m.eligibility(
            self._doc(name="Something Else.psdc"), allowed, False)[0])

    def test_collision_gets_a_suffix_rather_than_silent_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "Doc.psd").write_bytes(b"x")
            # not converted by this run: a different document with the same name
            self.assertEqual(self.m.unique_target(out, "Doc", set()).name, "Doc (2).psd")
            # converted by this run: a genuine resume skip
            self.assertIsNone(self.m.unique_target(out, "Doc", {"doc"}))

    def test_manifest_names_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"entries": [
                {"name": "BubbleSpawner.psdc", "source": "cloud-docs"},
                {"name": "Balcony_Bg.psdc", "source": "cloud-docs"},
                {"name": "aktif-fontlar.txt", "source": "fonts"}]}), encoding="utf-8")
            names = self.m.load_names(path)
            self.assertEqual(names, {"bubblespawner", "balcony_bg"})

    def test_manifest_allowlist_covers_only_cloud_documents(self):
        """A manifest lists presets and fonts too; the allowlist must not."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"entries": [
                {"name": "SomeTVStuff.psdc", "source": "cloud-docs"},
                {"name": "aktif-fontlar.txt", "source": "fonts"},
                {"name": "brush.abr", "source": "presets"},
                {"name": "bookmarks.json", "source": "cloud-files"}]}), encoding="utf-8")
            names = self.m.load_names(path)
            self.assertEqual(names, {"sometvstuff"})

    def test_manifest_csv_is_filtered_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.csv"
            path.write_text("kaynak;ad;yol\n"
                            "cloud-docs;Balcony_Bg.psdc;x\n"
                            "presets;brush.abr;y\n", encoding="utf-8-sig")
            self.assertEqual(self.m.load_names(path), {"balcony_bg"})

    def test_local_file_sharing_a_cloud_documents_name_is_not_touched(self):
        """A local Balloon.psd must not be closed because Balloon.psdc is listed."""
        allowed = {"balloon"}
        local = self._doc(name="Balloon.psd", cloud=False, hasPath=True)
        ok, why = self.m.eligibility(local, allowed, False)
        self.assertFalse(ok)
        self.assertIn("bulut belgesi değil", why)
        # the real cloud document is still eligible
        self.assertTrue(self.m.eligibility(
            self._doc(name="Balloon.psdc"), allowed, False)[0])

    def test_include_local_still_respects_the_name_list(self):
        allowed = {"balloon"}
        self.assertTrue(self.m.eligibility(
            self._doc(name="Balloon.psd", cloud=False, hasPath=True), allowed, True)[0])
        self.assertFalse(self.m.eligibility(
            self._doc(name="Baska.psd", cloud=False, hasPath=True), allowed, True)[0])

    def test_real_document_names_from_the_account(self):
        """Exact names seen in Ozan's Open Cloud Document dialog."""
        cases = {
            "Balcony_Bg (1).psdc": "balcony_bg",
            "Balcony_Bg.psdc": "balcony_bg",
            "Firefly detaylı oda, fazla eşya.psdc": "firefly detaylı oda, fazla eşya",
            "SDFGenerator Piece-Recovered.psdc": "sdfgenerator piece-recovered",
            "Balloon (1)": "balloon",
            "LightBulb-256.psdc": "lightbulb-256",
        }
        for name, key in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.m.match_key(name), key)

    def test_copy_suffix_does_not_defeat_the_allowlist(self):
        """The dialog shows "Balcony_Bg (1)"; the manifest has the original."""
        allowed = {"balcony_bg"}
        doc = self._doc(name="Balcony_Bg (1).psdc", cloud=True, hasPath=True)
        self.assertTrue(self.m.eligibility(doc, allowed, False)[0])

    def test_turkish_and_punctuation_survive_in_the_filename(self):
        stem = self.m.safe_stem("Firefly detaylı oda, fazla eşya.psdc")
        self.assertEqual(stem, "Firefly detaylı oda, fazla eşya")

    def test_illegal_characters_are_replaced_not_dropped(self):
        self.assertEqual(self.m.safe_stem('a<b>c:"d|e?f*g.psdc'), "a_b_c__d_e_f_g")
        self.assertEqual(self.m.safe_stem("   "), "belge")
        self.assertTrue(len(self.m.safe_stem("x" * 400)) <= 120)

    def test_two_copies_write_to_separate_files(self):
        """Balcony_Bg and Balcony_Bg (1) are different documents."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            first = self.m.unique_target(out, self.m.safe_stem("Balcony_Bg.psdc"), set())
            first.write_bytes(b"x")
            second = self.m.unique_target(out, self.m.safe_stem("Balcony_Bg (1).psdc"), set())
            self.assertEqual(second.name, "Balcony_Bg (1).psd")
            self.assertNotEqual(first, second)

    def test_document_count_distinguishes_rows_from_unique_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"entries": [
                {"name": "Balcony_Bg.psdc", "source": "cloud-docs"},
                {"name": "Balcony_Bg (1).psdc", "source": "cloud-docs"},
                {"name": "SomeTVStuff.psdc", "source": "cloud-docs"},
                {"name": "brush.abr", "source": "presets"}]}), encoding="utf-8")
            self.assertEqual(self.m.count_documents(path), 3)
            self.assertEqual(len(self.m.load_names(path)), 2)   # the copies share a key

    def test_cloud_paths_rebuilt_from_the_manifest(self):
        """Repository paths come from the backup layout, no session needed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"entries": [
                {"name": "SomeTVStuff.psdc", "source": "cloud-docs",
                 "path": "cloud-docs/cloud-content/SomeTVStuff.psdc"},
                {"name": "Deep.psdc", "source": "cloud-docs",
                 "path": "cloud-docs/cloud-content/alt/klasor/Deep.psdc"},
                {"name": "SomeTVStuff.png", "source": "cloud-docs",
                 "path": "cloud-docs/_duz-gorseller/SomeTVStuff.png"},
                {"name": "brush.abr", "source": "presets", "path": "presets/brush.abr"},
            ]}, ensure_ascii=False), encoding="utf-8")

            docs = self.m.cloud_documents(path)
            self.assertEqual([d["name"] for d in docs], ["SomeTVStuff.psdc", "Deep.psdc"])
            self.assertEqual(docs[1]["repo_path"], "/cloud-content/alt/klasor/Deep.psdc")

    def test_cloud_path_uses_forward_slashes_only(self):
        """A backslash before 'repo' becomes \\r inside the JS string."""
        full = self.m.cloud_path_for("/cloud-content/Doc.psdc")
        self.assertEqual(full, "C:/cloud-content/repo:~/cloud-content/Doc.psdc")
        self.assertNotIn("\\\\", full)

    def test_turkish_document_path_survives(self):
        full = self.m.cloud_path_for("/cloud-content/Firefly detaylı oda, fazla eşya.psdc")
        self.assertTrue(full.endswith("Firefly detaylı oda, fazla eşya.psdc"))
        # and it survives the trip through a JS string literal
        self.assertIn("detaylı", json.loads(self.m.js_string(full)))

    def test_custom_cloud_root_is_honoured(self):
        full = self.m.cloud_path_for("/x/Doc.psdc", root="D:/other/repo:~")
        self.assertEqual(full, "D:/other/repo:~/x/Doc.psdc")

    def test_manifest_found_in_a_differently_dated_folder(self):
        """The default folder is date-stamped; a run after midnight must still find it."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Desktop"
            old = desktop / "Adobe-Backup-20261126"
            old.mkdir(parents=True)
            (old / "manifest.json").write_text(json.dumps({"entries": [
                {"name": "Doc.psdc", "source": "cloud-docs",
                 "path": "cloud-docs/cloud-content/Doc.psdc"}]}), encoding="utf-8")

            real_home = Path.home
            Path.home = staticmethod(lambda: home)      # type: ignore[assignment]
            try:
                found = self.m.find_manifest(desktop / "PSD")
            finally:
                Path.home = real_home                   # type: ignore[assignment]
            self.assertEqual(found, old / "manifest.json")

    def test_manifest_without_cloud_documents_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / "manifest.json").write_text(json.dumps({"entries": [
                {"name": "brush.abr", "source": "presets", "path": "presets/brush.abr"}]}),
                encoding="utf-8")
            real_home = Path.home
            Path.home = staticmethod(lambda: Path(tmp))  # type: ignore[assignment]
            try:
                self.assertIsNone(self.m.find_manifest(out))
            finally:
                Path.home = real_home                    # type: ignore[assignment]

    def test_elevation_failure_explains_itself(self):
        errors = [("Photoshop.Application", self.m.SERVER_EXEC_FAILURE, "Server execution failed")]
        admin = self.m.connect_error(errors, True)
        self.assertIn("YÖNETİCİ", admin)
        self.assertIn("normal bir komut penceresinden", admin)
        plain = self.m.connect_error(errors, False)
        self.assertNotIn("YÖNETİCİ", plain)
        # and the eleven "Invalid class string" lines stay out of it
        noisy = [("Photoshop.Application", -2147221005, "Invalid class string")] * 11
        self.assertEqual(self.m.connect_error(noisy, False).count("Invalid class string"), 1)


class TestReportAfterConversion(unittest.TestCase):
    """A finished conversion must not still read as 61 jobs to do."""

    def _manifest(self, root: Path, converted: bool):
        from adobe_rescue.manifest import Entry, Manifest, STATUS_DONE, STATUS_MANUAL

        manifest = Manifest(root)
        for i in range(3):
            manifest.add(Entry(key=f"cloud-docs:{i}", source="cloud-docs",
                               name=f"Doc{i}.psdc", size=10).mark(STATUS_DONE))
        row = Entry(key="cloud-docs:__to-psd__", source="cloud-docs",
                    name="3 bulut belgesini katmanlı PSD'ye çevir")
        row.mark(STATUS_DONE, "3/3 bulut belgesi kaydedildi: C:/psd") if converted \
            else row.mark(STATUS_MANUAL, "çevirmeniz gerekiyor")
        manifest.add(row)
        return manifest

    def test_warning_banner_before_conversion(self):
        from adobe_rescue.engine import Result
        from adobe_rescue.report import write_html

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = write_html(self._manifest(root, False), Result(),
                              root / "r.html").read_text(encoding="utf-8")
            self.assertIn("AÇILAMAZ", html)
            self.assertIn("_duz-gorseller", html)      # the real folder name
            self.assertNotIn("_onizlemeler", html)

    def test_banner_turns_green_once_converted(self):
        from adobe_rescue.engine import Result
        from adobe_rescue.report import write_checklist, write_html

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root, True)
            html = write_html(manifest, Result(), root / "r.html").read_text(encoding="utf-8")
            self.assertIn("Düzenlenebilir PSD: 3/3", html)
            self.assertNotIn("AÇILAMAZ", html)
            checklist = write_checklist(manifest, root / "T.md").read_text(encoding="utf-8")
            self.assertIn("PSD'ye çevrildi", checklist)
            self.assertNotIn("açılamaz", checklist.lower())

    def test_conversion_marks_the_row_and_rewrites_the_report(self):
        from adobe_rescue import psd_convert
        from adobe_rescue.manifest import Manifest, STATUS_DONE

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._manifest(root, False).save(force=True)
            psd_convert.update_backup_report(root / "manifest.json", root / "psd", 3, 3)

            reloaded = Manifest(root)
            row = reloaded.get("cloud-docs:__to-psd__")
            self.assertEqual(row.status, STATUS_DONE)
            self.assertIn("3/3", row.note)
            self.assertIn("Düzenlenebilir PSD: 3/3",
                          (root / "rapor.html").read_text(encoding="utf-8"))
