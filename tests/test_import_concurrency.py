"""Bound concurrent secret uploads while preserving grant revocation and retries."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from custom_components.homepod_sensor_link import ui
from test_onboarding_v2 import Request, export_fixture


class ImportConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.imports = ui.Imports()
        self.view = ui.ImportView(SimpleNamespace(), self.imports)
        self.nonce = self.imports.grant("flow", "owner")
        self.source, _, export = export_fixture()
        self.body = json.dumps(export).encode()
        cloud = patch.object(ui, "is_cloud_connection", return_value=False)
        cloud.start()
        self.addCleanup(cloud.stop)

    def request(self, body=None, *, nonce=None):
        return Request(self.body if body is None else body, peer="127.0.0.1", nonce=nonce or self.nonce)

    def blocked_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        request = self.request()

        async def chunks(_size):
            request.read = True
            entered.set()
            await release.wait()
            yield self.body

        request.content.iter_chunked = chunks
        return request, entered, release

    async def test_same_grant_rejected_before_body_and_independent_grant_proceeds(self):
        request, entered, release = self.blocked_request()
        task = asyncio.create_task(self.view.post(request, "flow"))
        try:
            await entered.wait()
            duplicate = self.request()
            response = await self.view.post(duplicate, "flow")
            self.assertEqual(response.status, 409)
            self.assertEqual(json.loads(response.text), {"error": "import_in_progress"})
            self.assertFalse(duplicate.read)
            other_nonce = self.imports.grant("other", "owner")
            self.assertEqual((await self.view.post(self.request(nonce=other_nonce), "other")).status, 200)
            self.assertFalse(task.done())
        finally:
            release.set()
            response = await task
        self.assertEqual(response.status, 200)
        self.assertNotIn("uploading", self.imports.pending["flow"])
        duplicate = self.request()
        response = await self.view.post(duplicate, "flow")
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.text), {"error": "already_imported"})
        self.assertFalse(duplicate.read)

    async def test_invalid_input_releases_grant_for_retry(self):
        response = await self.view.post(self.request(b"invalid synthetic export"), "flow")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.text), {"error": "invalid_or_expired_export"})
        self.assertNotIn("uploading", self.imports.pending["flow"])
        self.assertEqual((await self.view.post(self.request(), "flow")).status, 200)

    async def test_body_failure_never_echoes_exception_and_allows_retry(self):
        request = self.request()

        async def broken(_size):
            raise ValueError(self.source["iOSDeviceLTSK"])
            yield b""  # Keep the request-body iterator asynchronous.

        request.content.iter_chunked = broken
        response = await self.view.post(request, "flow")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.text), {"error": "invalid_or_expired_export"})
        self.assertNotIn(self.source["iOSDeviceLTSK"], response.text)
        self.assertNotIn("uploading", self.imports.pending["flow"])
        self.assertEqual((await self.view.post(self.request(), "flow")).status, 200)

    async def test_cancelled_upload_releases_grant_for_retry(self):
        request, entered, _ = self.blocked_request()
        task = asyncio.create_task(self.view.post(request, "flow"))
        try:
            await entered.wait()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertNotIn("uploading", self.imports.pending["flow"])
        self.assertEqual((await self.view.post(self.request(), "flow")).status, 200)

    async def test_timed_out_upload_releases_grant_for_retry(self):
        request, _, _ = self.blocked_request()
        timeout = asyncio.timeout
        with patch.object(ui.asyncio, "timeout", side_effect=lambda _seconds: timeout(0)):
            response = await self.view.post(request, "flow")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.text), {"error": "invalid_or_expired_export"})
        self.assertNotIn("uploading", self.imports.pending["flow"])
        self.assertEqual((await self.view.post(self.request(), "flow")).status, 200)

    async def test_expired_revoked_and_replaced_grants_cannot_be_resurrected(self):
        for action in ("expire", "revoke", "replace"):
            with self.subTest(action=action):
                self.imports.pending.clear()
                self.nonce = self.imports.grant("flow", "owner")
                original = self.imports.pending["flow"]
                request, entered, release = self.blocked_request()
                task = asyncio.create_task(self.view.post(request, "flow"))
                try:
                    await entered.wait()
                    if action == "expire":
                        original["expires"] = 0
                    else:
                        self.imports.pending.pop("flow")
                        if action == "replace":
                            self.nonce = self.imports.grant("flow", "owner")
                finally:
                    release.set()
                    response = await task
                self.assertEqual(response.status, 400)
                self.assertEqual(json.loads(response.text), {"error": "invalid_or_expired_export"})
                self.assertNotIn("data", original)
                self.assertNotIn("uploading", original)
                if action == "replace":
                    self.assertNotIn("data", self.imports.pending["flow"])
                    self.assertNotIn("uploading", self.imports.pending["flow"])
                    self.assertEqual((await self.view.post(self.request(), "flow")).status, 200)
                else:
                    self.assertNotIn("flow", self.imports.pending)
