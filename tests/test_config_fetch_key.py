"""The config fetch must carry the shared key and say when it is refused.

The cloud started checking the key on 11-09-2026 and this one call was still
sending none, so the campus PC silently fell back to its cached config.json:
recorder and camera changes stopped reaching it while everything else looked
healthy.
"""

import unittest
from unittest.mock import patch

import main


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.headers_sent = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.headers_sent = headers
        return self._response


class ConfigFetchKeyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._config_refused = False

    async def _fetch(self, response):
        client = FakeClient(response)
        with (
            patch.object(main.httpx, "AsyncClient", lambda *a, **k: client),
            patch.object(
                main.agent_auth, "secret_headers", lambda: {"X-Agent-Secret": "k"}
            ),
        ):
            result = await main.fetch_config_from_cloud()
        return client, result

    async def test_the_key_is_sent_with_the_config_request(self):
        client, result = await self._fetch(Response(200, {"dvrs": [{}]}))
        self.assertEqual(client.headers_sent, {"X-Agent-Secret": "k"})
        self.assertIsNotNone(result)
        self.assertFalse(main._config_refused)

    async def test_a_refused_key_is_remembered_for_health(self):
        _, result = await self._fetch(Response(401))
        self.assertIsNone(result)
        self.assertTrue(main._config_refused)

    async def test_an_accepted_key_clears_an_earlier_refusal(self):
        await self._fetch(Response(401))
        await self._fetch(Response(200, {"dvrs": [{}]}))
        self.assertFalse(main._config_refused)


if __name__ == "__main__":
    unittest.main()
