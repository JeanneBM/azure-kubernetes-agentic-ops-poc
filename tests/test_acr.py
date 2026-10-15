import httpx
import pytest

from agentic_ops.acr import AcrRegistry, RegistryError
from agentic_ops.safety import parse_image


class Cred:
    def get_token(self, scope):
        assert scope == "https://management.azure.com/.default"
        return type("T", (), {"token": "aad"})()


def registry(manifest_status, *, exchange_status=200):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/oauth2/exchange":
            assert b"grant_type=access_token" in request.content
            return httpx.Response(exchange_status, json={"refresh_token": "rt"})
        if request.url.path == "/oauth2/token":
            assert b"scope=repository%3Atest-app%3Apull" in request.content
            return httpx.Response(200, json={"access_token": "at"})
        assert request.headers["Authorization"] == "Bearer at"
        return httpx.Response(manifest_status)

    return AcrRegistry(credential=Cred(), http_client=httpx.Client(transport=httpx.MockTransport(handler))), calls


IMAGE = parse_image("acrprod.azurecr.io/test-app:1.4.2")


def test_existing_manifest():
    reg, calls = registry(200)
    assert reg.exists(IMAGE) is True
    assert calls[-1] == ("HEAD", "/v2/test-app/manifests/1.4.2")


def test_missing_manifest():
    reg, _ = registry(404)
    assert reg.exists(IMAGE) is False


@pytest.mark.parametrize("status", [401, 403, 500])
def test_other_statuses_raise(status):
    reg, _ = registry(status)
    with pytest.raises(RegistryError):
        reg.exists(IMAGE)


def test_token_failure_raises():
    reg, _ = registry(200, exchange_status=401)
    with pytest.raises(RegistryError):
        reg.exists(IMAGE)
