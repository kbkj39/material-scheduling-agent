"""Offline transport-contract tests; these do not establish LLM decision quality."""

from __future__ import annotations

import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from material_agent.models import LiveModel, ModelError


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None

    def open(self, request, timeout):
        self.request = request
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.response).encode())


def call(name="read_material", **arguments):
    return {"id": "call_test", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class LiveAdapterContractTests(unittest.TestCase):
    def model(self, response=None, error=None):
        model = LiveModel(api_key="synthetic-test-key", model="synthetic-model", base_url="http://127.0.0.1:8080/v1")
        model.opener = FakeOpener(response, error)
        return model

    def test_real_adapter_requests_function_call_and_parses_one_action(self):
        model = self.model({
            "choices": [{"message": {"tool_calls": [call(asset_id="M01", reason="先读取素材") ]}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        })
        messages = [{"role": "user", "content": "synthetic request"}]
        tools = [{"type": "function", "function": {"name": "read_material"}}]
        decision = model.decide(messages, tools)
        self.assertEqual(decision["action"], "read_material")
        self.assertEqual(decision["arguments"], {"asset_id": "M01"})
        self.assertEqual(decision["reason"], "先读取素材")
        payload = json.loads(model.opener.request.data)
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["tool_choice"], "required")
        self.assertFalse(payload["parallel_tool_calls"])

    def test_live_mode_missing_credentials_does_not_silently_use_mock(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ModelError):
                LiveModel()

    def test_multiple_model_actions_are_rejected(self):
        model = self.model({"choices": [{"message": {"tool_calls": [call(asset_id="M01"), call(asset_id="M02")]}}]})
        with self.assertRaises(ModelError):
            model.decide([], [])

    def test_null_or_non_object_usage_is_normalized_or_returns_model_error(self):
        for usage in (None, [], "invalid", 0):
            with self.subTest(usage=usage):
                model = self.model({
                    "choices": [{"message": {"tool_calls": [call(asset_id="M01")]}}],
                    "usage": usage,
                })
                try:
                    decision = model.decide([], [])
                except ModelError:
                    continue
                self.assertEqual(decision.get("metadata", {}).get("usage", {}), {})

    def test_http_failure_does_not_leak_response_body_or_credentials(self):
        error = HTTPError("http://127.0.0.1/v1/chat/completions", 500, "private synthetic content", {}, io.BytesIO(b"private response"))
        model = self.model(error=error)
        with self.assertRaises(ModelError) as caught:
            model.decide([], [])
        self.assertEqual(str(caught.exception), "model HTTP error 500")


if __name__ == "__main__":
    unittest.main()
