import json

import httpx
import pytest

from crawler import providers
from crawler.transport import ExtractionError


class Profiles:
    def __init__(self, **overrides):
        self.profile = {
            "id": "fixture",
            "base_url": "https://provider.example/v1",
            "model": "fixture-model",
            "kind": "openai_compatible",
            "credentials": {"api_key": "fixture-secret"},
            **overrides,
        }

    def profiles(self, kind, secrets=False):
        assert kind == "provider" and secrets
        return [self.profile]


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient

    async def public_url(url):
        return None

    monkeypatch.setattr(providers, "validate_url", public_url)
    monkeypatch.setattr(
        providers.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.asyncio
async def test_openai_mapping_and_secret_exist_only_in_request(monkeypatch):
    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-secret"
        assert json.loads(request.content) == {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0,
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "connected"}}]}
        )

    mock_http(monkeypatch, respond)
    assert await providers.complete(Profiles(), "fixture", "hello") == "connected"


@pytest.mark.asyncio
async def test_custom_body_preserves_nested_null_false_array_and_prompt_content(
    monkeypatch,
):
    prompt = 'literal "quotes" and \\slashes\n{{model}}'
    profile = Profiles(
        kind="custom",
        path="/generate",
        body_template={
            "inputs": [{"prompt": None}],
            "settings": {"temperature": None, "stream": False, "stop": []},
            "model": "{{model}}",
            "api_key": "{{api_key}}",
        },
        prompt_field="inputs.0.prompt",
        model_field=None,
        auth_header=None,
        response_path="result",
    )

    def respond(request):
        data = json.loads(request.content)
        assert data == {
            "inputs": [{"prompt": prompt}],
            "settings": {"temperature": None, "stream": False, "stop": []},
            "model": "fixture-model",
            "api_key": "fixture-secret",
        }
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "result": {
                    "filters": {"size": ""},
                    "needs_clarification": False,
                    "explanation": "",
                }
            },
        )

    mock_http(monkeypatch, respond)
    result = json.loads(await providers.complete(profile, "fixture", prompt))
    assert result["filters"] == {"size": ""}


@pytest.mark.asyncio
async def test_custom_null_root_is_json_null_not_absent_body(monkeypatch):
    profile = Profiles(
        kind="custom",
        body_template=None,
        prompt_field=None,
        model_field=None,
        response_path=None,
        credentials={},
    )

    def respond(request):
        assert request.content == b"null"
        assert "authorization" not in request.headers
        return httpx.Response(200, json="connected")

    mock_http(monkeypatch, respond)
    assert await providers.complete(profile, "fixture", "hello") == "connected"


@pytest.mark.asyncio
async def test_anthropic_free_endpoint_has_no_empty_auth_header(monkeypatch):
    def respond(request):
        assert "x-api-key" not in request.headers
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json={"content": [{"text": "connected"}]})

    mock_http(monkeypatch, respond)
    assert (
        await providers.complete(
            Profiles(kind="anthropic", credentials={}), "fixture", "hello"
        )
        == "connected"
    )


@pytest.mark.parametrize("status", [302, 401, 429, 500])
@pytest.mark.asyncio
async def test_http_errors_do_not_include_secret_response_or_url(monkeypatch, status):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            status,
            headers={"location": "https://other.example"},
            text="error: fixture-secret",
        ),
    )
    with pytest.raises(ExtractionError) as caught:
        await providers.complete(Profiles(), "fixture", "hello")
    assert str(caught.value) == f"Provider returned HTTP {status}"
    assert "fixture-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_transport_errors_are_sanitized(monkeypatch):
    def failed(request):
        raise httpx.ConnectError(
            "fixture-secret https://provider.example?key=fixture-secret",
            request=request,
        )

    mock_http(monkeypatch, failed)
    with pytest.raises(ExtractionError, match="Provider request failed") as caught:
        await providers.complete(Profiles(), "fixture", "hello")
    assert "fixture-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "response",
    [None, 3, True, {"choices": []}, {"choices": [{"message": {"content": None}}]}],
)
@pytest.mark.asyncio
async def test_missing_or_nontext_provider_result_is_visible(monkeypatch, response):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=response))
    with pytest.raises(ExtractionError):
        await providers.complete(Profiles(), "fixture", "hello")


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "unexpected"},
        {"credentials": {"api_key": "secret"}},
        {"unrecognized": True},
        {"base_url": "https://provider.example?api_key=secret"},
        {"kind": "custom", "auth_header": "Host"},
        {"kind": "custom", "auth_prefix": "Bearer \n"},
        {"kind": "custom", "path": "//evil.example"},
        {"kind": "custom", "path": "/generate?api_key=secret"},
        {"kind": "custom", "body_template": {"temperature": float("nan")}},
        {"kind": "custom", "body_template": {"token": "plaintext-secret"}},
    ],
)
def test_profile_rejects_unknown_protocol_and_plaintext_credentials(overrides):
    with pytest.raises(ValueError):
        providers.validate_provider_profile(
            {"base_url": "https://provider.example", "model": "m", **overrides}
        )


@pytest.mark.asyncio
async def test_invalid_request_mapping_is_rejected_before_http(monkeypatch):
    def unexpected(request):
        pytest.fail("Invalid mapping must not send a paid request")

    mock_http(monkeypatch, unexpected)
    with pytest.raises(ValueError, match="mapping"):
        await providers.complete(
            Profiles(kind="custom", body_template=None), "fixture", "hello"
        )


@pytest.mark.parametrize(
    "data",
    [
        {"filters": {}, "needs_clarification": "false", "explanation": ""},
        {"filters": {}, "needs_clarification": False},
        {
            "filters": {},
            "needs_clarification": False,
            "explanation": "",
            "prices": [10],
        },
        {"filters": {"size": None}, "needs_clarification": False, "explanation": ""},
        {
            "filters": {"size": {"other": "value"}},
            "needs_clarification": False,
            "explanation": "",
        },
        {"filters": {"size": []}, "needs_clarification": False, "explanation": ""},
        {
            "filters": {"quantity": float("inf")},
            "needs_clarification": False,
            "explanation": "",
        },
        {"filters": {}, "needs_clarification": False, "explanation": "x" * 2001},
    ],
)
@pytest.mark.asyncio
async def test_interpret_rejects_unsupported_or_coerced_instruction_fields(
    monkeypatch, data
):
    async def reply(*args):
        return json.dumps(data)

    monkeypatch.setattr(providers, "complete", reply)
    with pytest.raises(ValueError):
        await providers.interpret(None, "fixture", "use requested stock")


@pytest.mark.asyncio
async def test_instruction_length_is_not_silently_truncated(monkeypatch):
    async def unexpected(*args):
        pytest.fail("Reject input before a paid request")

    monkeypatch.setattr(providers, "complete", unexpected)
    with pytest.raises(ValueError, match="12000"):
        await providers.interpret(None, "fixture", "x" * 12001)


@pytest.mark.asyncio
async def test_interpret_preserves_empty_option_and_finite_typed_values(monkeypatch):
    expected = {
        "filters": {"coating": "", "quantity": [500, 1000], "rush": False},
        "needs_clarification": False,
        "explanation": "Exact requested options",
    }

    async def reply(*args):
        return "```json\n" + json.dumps(expected) + "\n```"

    monkeypatch.setattr(providers, "complete", reply)
    assert (
        await providers.interpret(
            None, "fixture", "matte coating, 500 or 1000, regular"
        )
        == expected
    )


@pytest.mark.asyncio
async def test_duplicate_instruction_fields_cannot_silently_replace_filters(
    monkeypatch,
):
    async def reply(*args):
        return '{"filters":{"size":"A","size":"B"},"needs_clarification":false,"explanation":""}'

    monkeypatch.setattr(providers, "complete", reply)
    with pytest.raises(ValueError, match="valid extraction instructions"):
        await providers.interpret(None, "fixture", "size A")


@pytest.mark.asyncio
async def test_nonstring_or_multiline_secret_is_rejected_without_leaking_it(
    monkeypatch,
):
    async def unexpected(*args):
        pytest.fail("Invalid key must be rejected before URL/network operations")

    monkeypatch.setattr(providers, "validate_url", unexpected)
    for key in ({"secret": "value"}, "synthetic\nsecret", "synthetic☃secret"):
        with pytest.raises(ValueError, match="single-line ASCII") as caught:
            await providers.complete(
                Profiles(credentials={"api_key": key}), "fixture", "hello"
            )
        assert "synthetic" not in str(caught.value)
