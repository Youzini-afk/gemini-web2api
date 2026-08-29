import base64
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from gemini_web2api import admin_api
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG, SETTINGS_FIELDS
from gemini_web2api.gemini import generate, generate_stream
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api.tools import messages_to_prompt


def _stream_line(text):
    inner = [None] * 20
    inner[4] = [[None, [text]]]
    payload = [["wrb.fr", None, json.dumps(inner)]]
    # The parser intentionally ignores implausibly short frames.
    return json.dumps(payload) + (" " * 220) + "\n"


class MergeConfigTests(unittest.TestCase):
    def test_fork_and_upstream_defaults_are_both_present(self):
        self.assertIn("admin_password", DEFAULT_CONFIG)
        self.assertIs(DEFAULT_CONFIG["temporary_chats"], False)
        self.assertEqual(DEFAULT_CONFIG["default_model"], "gemini-3.6-flash")
        self.assertIn("temporary_chats", SETTINGS_FIELDS)

    def test_compose_persists_the_directory_used_by_the_container_config(self):
        root = Path(__file__).resolve().parents[1]
        compose = (root / "docker-compose.local.yml").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("- ./data:/app/config", compose)
        self.assertIn("GEMINI_WEB2API_CONFIG=/app/config/config.json", dockerfile)


class ToolCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def test_anthropic_base64_image_shape_is_preserved(self):
        image_data = base64.b64encode(b"anthropic image").decode()
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/webp",
                        "data": image_data,
                    },
                },
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [(b"anthropic image", "image/webp")])

    def test_oversized_tool_parameters_are_slimmed_without_losing_tool(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "Still callable",
                "parameters": {
                    "type": "object",
                    "properties": {"huge_marker": {"description": "x" * 40000}},
                },
            },
        }]

        prompt, _ = messages_to_prompt(
            [{"role": "user", "content": "use it"}],
            tools,
            "auto",
        )

        self.assertIn("large_tool", prompt)
        self.assertIn("Still callable", prompt)
        self.assertNotIn("huge_marker", prompt)


class TransportMergeTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG.update({
            "gemini_bl": "boq_assistant-bard-web-server_old.01_p0",
            "retry_attempts": 1,
            "retry_delay_sec": 0,
            "request_timeout_sec": 5,
            "log_requests": False,
        })

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    @mock.patch("gemini_web2api.gemini.extract_response_text", return_value="ok")
    @mock.patch("gemini_web2api.gemini._record_node_health")
    @mock.patch("gemini_web2api.gemini._select_mihomo_proxy", return_value=("node-a", "http://proxy-a", "ok"))
    @mock.patch("gemini_web2api.gemini._enabled_clash_candidates_exist", return_value=True)
    @mock.patch("gemini_web2api.browser_transport.ensure_available")
    @mock.patch("gemini_web2api.browser_transport.allow_python_fallback", return_value=False)
    @mock.patch("gemini_web2api.browser_transport.available", return_value=True)
    def test_405_refresh_retries_same_request_without_dropping_node_route(
        self,
        _available,
        _fallback,
        _ensure,
        _has_nodes,
        select_proxy,
        record_health,
        _extract,
    ):
        def update_bl(proxy):
            self.assertEqual(proxy, "http://proxy-a")
            CONFIG["gemini_bl"] = "boq_assistant-bard-web-server_new.02_p0"
            return True

        with mock.patch(
            "gemini_web2api.gemini._post_upstream",
            side_effect=[RuntimeError("Gemini upstream returned HTTP 405"), "raw"],
        ) as post, mock.patch(
            "gemini_web2api.gemini.update_bl_if_needed",
            side_effect=update_bl,
        ):
            result = generate("hello", 1, 4, preferred_raw_uri="node-a")

        self.assertEqual(result, "ok")
        self.assertEqual(post.call_count, 2)
        self.assertIn("boq_assistant-bard-web-server_old.01_p0", post.call_args_list[0].args[1])
        self.assertIn("boq_assistant-bard-web-server_new.02_p0", post.call_args_list[1].args[1])
        select_proxy.assert_called_once_with(set(), "node-a")
        record_health.assert_called_once_with("node-a", True, latency_ms=mock.ANY)

    @mock.patch("gemini_web2api.gemini._record_node_health")
    @mock.patch("gemini_web2api.gemini._enabled_clash_candidates_exist", return_value=True)
    @mock.patch("gemini_web2api.browser_transport.ensure_available")
    @mock.patch("gemini_web2api.browser_transport.allow_python_fallback", return_value=False)
    @mock.patch("gemini_web2api.browser_transport.available", return_value=True)
    def test_stream_retry_deduplicates_partial_text_across_nodes(
        self,
        _available,
        _fallback,
        _ensure,
        _has_nodes,
        record_health,
    ):
        routes = [
            ("node-a", "http://proxy-a", "ok"),
            ("node-b", "http://proxy-b", "ok"),
        ]

        def first_stream(*_args, **_kwargs):
            yield _stream_line("hel")
            raise RuntimeError("network unreachable")

        def second_stream(*_args, **_kwargs):
            yield _stream_line("hel")
            yield _stream_line("hello")

        with mock.patch(
            "gemini_web2api.gemini._select_mihomo_proxy",
            side_effect=routes,
        ), mock.patch(
            "gemini_web2api.browser_transport.stream",
            side_effect=[first_stream(), second_stream()],
        ):
            chunks = list(generate_stream("hello", 1, 4, preferred_raw_uri="node-a"))

        self.assertEqual(chunks, ["hel", "lo"])
        self.assertEqual(record_health.call_args_list[0].args[:2], ("node-a", False))
        self.assertEqual(record_health.call_args_list[-1].args[:2], ("node-b", True))

    @mock.patch("gemini_web2api.gemini._record_node_health")
    @mock.patch("gemini_web2api.gemini._enabled_clash_candidates_exist", return_value=True)
    @mock.patch("gemini_web2api.browser_transport.ensure_available")
    @mock.patch("gemini_web2api.browser_transport.allow_python_fallback", return_value=False)
    @mock.patch("gemini_web2api.browser_transport.available", return_value=True)
    def test_stream_405_refresh_still_retries_when_retry_attempts_is_one(
        self,
        _available,
        _fallback,
        _ensure,
        _has_nodes,
        record_health,
    ):
        def stale_stream(*_args, **_kwargs):
            raise RuntimeError("Gemini TLS helper stream failed (405)")
            yield  # pragma: no cover - keeps this function a generator

        def fresh_stream(*_args, **_kwargs):
            yield _stream_line("ok")

        with mock.patch(
            "gemini_web2api.gemini._select_mihomo_proxy",
            side_effect=[
                ("node-a", "http://proxy-a", "ok"),
                ("node-a", "http://proxy-a", "ok"),
            ],
        ), mock.patch(
            "gemini_web2api.browser_transport.stream",
            side_effect=[stale_stream(), fresh_stream()],
        ), mock.patch(
            "gemini_web2api.gemini.update_bl_if_needed",
            return_value=True,
        ) as update_bl:
            chunks = list(generate_stream("hello", 1, 4, preferred_raw_uri="node-a"))

        self.assertEqual(chunks, ["ok"])
        update_bl.assert_called_once_with("http://proxy-a")
        record_health.assert_called_once_with("node-a", True, latency_ms=mock.ANY)


class EndpointMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read().decode()
        connection.close()
        return response.status, payload

    @mock.patch("gemini_web2api.server.admin_api.get_all_key_values", return_value=["secret-key"])
    def test_v1beta_auth_accepts_google_header_and_query_key(self, _keys):
        status, _ = self.request("GET", "/v1beta/models")
        self.assertEqual(status, 401)

        status, _ = self.request(
            "GET",
            "/v1beta/models",
            headers={"x-goog-api-key": "secret-key"},
        )
        self.assertEqual(status, 200)

        status, _ = self.request("GET", "/v1beta/models?key=secret-key")
        self.assertEqual(status, 200)

    @mock.patch("gemini_web2api.server.admin_api.get_all_key_values", return_value=[])
    def test_api_request_updates_fork_admin_stats(self, _keys):
        admin_api.reset_stats()
        status, _ = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        with admin_api._stats_lock:
            self.assertEqual(admin_api._stats["total_requests"], 1)
            self.assertEqual(admin_api._stats["success_count"], 1)

    @mock.patch("gemini_web2api.server.select_request_proxy", return_value=("node-a", "http://proxy-a"))
    @mock.patch("gemini_web2api.server.fetch_image_bytes", return_value=b"\xff\xd8\xffjpeg")
    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/ref")
    @mock.patch("gemini_web2api.server.generate", return_value="ok")
    @mock.patch("gemini_web2api.server.admin_api.get_all_key_values", return_value=[])
    def test_multimodal_upload_and_inference_prefer_same_node(
        self,
        _keys,
        generate_mock,
        upload_image,
        fetch_image,
        _select,
    ):
        body = json.dumps({
            "model": "gemini-3.6-flash",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
                ],
            }],
        })
        status, _ = self.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 200)
        fetch_image.assert_called_once_with("https://example.com/a.jpg", proxy="http://proxy-a")
        upload_image.assert_called_once_with(
            b"\xff\xd8\xffjpeg",
            "image.png",
            "image/jpeg",
            proxy="http://proxy-a",
        )
        self.assertEqual(generate_mock.call_args.kwargs["preferred_raw_uri"], "node-a")


if __name__ == "__main__":
    unittest.main()
