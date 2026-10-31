"""Explicit account choice, share incarnations and stale basket contexts. All accounts are synthetic."""

import json

import pytest
from conftest import FakeSite, product
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient
from test_accounts import accounts as accounts  # noqa: F401
from test_accounts import choose, connect, control_of
from test_accounts import keyed as keyed  # noqa: F401

from hktvmall_mcp import connections_state, server
from hktvmall_mcp.accounts import BEARER_CLIENT, DEFAULT
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.fsio import atomic_write_json
from hktvmall_mcp.server import HktvService


def test_own_connection_does_not_prevent_using_a_share_even_after_disconnect(accounts):
    connect(accounts, "alex")
    connect(accounts, "sam", "sam@example.com", "sam-session")
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    assert accounts.account_for("sam") == "sam"  # the offer has no routing side effect
    choose(accounts, "sam", "alex")
    assert accounts.account_for("sam") == "alex"
    epoch = accounts.control("sam").suspend()
    accounts.disconnect_owned("sam", epoch)
    assert accounts.account_for("sam") == "alex"
    assert accounts.control("sam").read()["provider_email"] == "sam@example.com"


def test_a_revoked_and_reissued_grant_never_reactivates_a_selection(accounts):
    connect(accounts, "alex")
    accounts.person("sam")
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    choose(accounts, "sam", "alex")
    grant = accounts.control("alex").read()["shares"]["sam"]["revision"]
    accounts.unshare("alex", "sam", *control_of(accounts, "alex"), grant=grant)
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    with pytest.raises(HktvError, match="shared_access_removed"):
        accounts.account_for("sam")
    with pytest.raises(HktvError, match="shared_access_removed"):
        accounts.unshare("alex", "sam", *control_of(accounts, "alex"), grant=grant, leaving=True)
    choose(accounts, "sam", "alex")
    assert accounts.account_for("sam") == "alex"


@pytest.fixture
def shopping(accounts):
    sites = {}
    for person, quantity in (("alex", 1), ("sam", 2)):
        connect(accounts, person, person + "@example.com", person + "-session")
        real = accounts.get(person)
        sites[person] = FakeSite(lines={"EGG": quantity}, products=[product("EGG")])
        accounts._services[person] = HktvService(
            sites[person], real.state, real.session, accounts.settings, account_id=person
        )
    auth = StaticTokenVerifier(tokens={"backend": {"client_id": BEARER_CLIENT, "scopes": []}})
    app = server.create_server(accounts, auth=auth).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:

        def call(person, name, **arguments):
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers={
                    "Authorization": "Bearer backend",
                    "X-Fleet-Person": person,
                    "Accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-03-26",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            return result.get("structuredContent") or json.loads(result["content"][0]["text"])

        yield sites, call


def test_a_stale_basket_context_cannot_write_to_a_different_account_or_after_switching_back(accounts, shopping):
    sites, call = shopping
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    original = call("sam", "hktv_get_cart")
    assert original["connection"] == {"owner": "sam", "shared": False}
    context = original["account_context"]
    assert len(context) == 64 and "sam" not in context
    choose(accounts, "sam", "alex")
    for token in (None, context, "0" * 64, "é" * 64):
        before = {p: list(site.calls) for p, site in sites.items()}
        result = call("sam", "hktv_update_cart", changes=[{"id": "EGG", "quantity": 3}], account_context=token)
        assert result["error"]["code"] == "account_changed"
        assert {p: site.calls for p, site in sites.items()} == before
    shared = call("sam", "hktv_get_cart")
    assert shared["connection"] == {"owner": "alex", "shared": True}
    updated = call(
        "sam", "hktv_update_cart", changes=[{"id": "EGG", "quantity": 3}], account_context=shared["account_context"]
    )
    assert updated["status"] == "ok" and sites["alex"].lines == {"EGG": 3} and sites["sam"].lines == {"EGG": 2}
    choose(accounts, "sam", "sam")
    rejected = call("sam", "hktv_update_cart", changes=[{"id": "EGG", "quantity": 4}], account_context=context)
    assert rejected["error"]["code"] == "account_changed" and sites["sam"].lines == {"EGG": 2}


def test_contexts_are_bound_to_the_caller_and_reconnect_epoch(accounts, shopping):
    sites, call = shopping
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    choose(accounts, "sam", "alex")
    token = call("alex", "hktv_get_cart")["account_context"]
    result = call("sam", "hktv_update_cart", changes=[{"id": "EGG", "quantity": 3}], account_context=token)
    assert result["error"]["code"] == "account_changed"
    accounts.control("alex").resume()
    result = call("alex", "hktv_update_cart", changes=[{"id": "EGG", "quantity": 3}], account_context=token)
    assert result["error"]["code"] == "account_changed" and sites["alex"].lines == {"EGG": 1}


def test_history_permission_is_checked_before_live_or_cached_order_data(accounts, shopping, monkeypatch):
    sites, call = shopping
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    choose(accounts, "sam", "alex")
    service = accounts.get("alex")
    cached = []
    monkeypatch.setattr(service, "_cached_items", lambda summary: cached.append(summary) or [])
    sites["alex"].orders["completed"] = [{"code": "SYNTHETIC", "statusDisplay": "completed"}]
    for items in (True, False):
        result = call("sam", "hktv_recent_orders", items=items)
        assert result["error"]["code"] == "history_not_shared"
    assert call("sam", "hktv_status", check=True)["error"]["code"] == "history_not_shared"
    assert cached == [] and not sites["alex"].calls
    grant = accounts.control("alex").read()["shares"]["sam"]["revision"]
    accounts.share("alex", "sam", *control_of(accounts, "alex"), history=True, grant=grant)
    assert call("sam", "hktv_recent_orders")["error"]["code"] == "shared_access_removed"
    choose(accounts, "sam", "alex")
    sites["alex"].orders["completed"] = []
    assert call("sam", "hktv_recent_orders")["orders"] == []
    assert ("order_list", 0, 0) in sites["alex"].calls


def test_leaving_does_not_select_the_owned_account_or_change_other_members(accounts):
    connect(accounts, "alex")
    connect(accounts, "sam", "sam@example.com", "sam-session")
    accounts.person("kim")
    for member in ("sam", "kim"):
        accounts.share("alex", member, *control_of(accounts, "alex"))
        choose(accounts, member, "alex")
    before = accounts.control("sam").read()
    grant = accounts.control("alex").read()["shares"]["sam"]["revision"]
    accounts.unshare("alex", "sam", *control_of(accounts, "alex"), grant=grant, leaving=True)
    with pytest.raises(HktvError, match="account_selection_needed"):
        accounts.account_for("sam")
    after = accounts.control("sam").read()
    assert after["provider_email"] == before["provider_email"]
    assert after["epoch"] == before["epoch"] and after["generation"] == before["generation"]
    assert accounts.owns("sam") and accounts.account_for("kim") == "alex"


def legacy(accounts, person):
    data = accounts.control(person).read()
    for field in connections_state.PEOPLE_FIELDS - {"shared_with"}:
        data.pop(field)
    data["version"] = 1
    atomic_write_json(accounts.control(person).path, data)
    return data


def test_legacy_routing_and_full_history_grants_migrate_without_changing_credentials_or_barriers(accounts):
    connect(accounts, "sam", "sam@example.com", "sam-session")
    accounts.person("alex")
    accounts.share("sam", "alex", *control_of(accounts, "sam"))
    before = {p: legacy(accounts, p) for p in ("alex", "sam")}
    blob = (accounts.directory / "sam" / "session.bin").read_bytes()
    assert accounts.account_for("alex") == "sam"
    assert accounts.account_for("sam") == "sam"
    for person in before:
        after = accounts.control(person).read()
        assert after["version"] == 2
        assert all(after[k] == v for k, v in before[person].items() if k != "version")
    assert accounts.control("sam").read()["shares"]["alex"]["order_history"] is True
    assert (accounts.directory / "sam" / "session.bin").read_bytes() == blob
    assert accounts.control(DEFAULT).read()["version"] == 1


def test_partial_migration_resumes_with_the_same_grant_reference(accounts, monkeypatch):
    connect(accounts, "sam", "sam@example.com", "sam-session")
    accounts.person("alex")
    accounts.share("sam", "alex", *control_of(accounts, "sam"))
    for person in ("alex", "sam"):
        legacy(accounts, person)
    real_write = connections_state.atomic_write_json

    def interrupted(path, data):
        if path.parent.name == "sam" and data["version"] == 2:
            raise OSError("synthetic crash")
        real_write(path, data)

    with monkeypatch.context() as patched:
        patched.setattr(connections_state, "atomic_write_json", interrupted)
        with pytest.raises(OSError, match="synthetic crash"):
            accounts.person("alex")
    selected = accounts.control("alex").read()["selection"]
    assert selected["account"] == "sam" and accounts.control("sam").read()["version"] == 1
    assert accounts.account_for("alex") == "sam"
    assert accounts.control("alex").read()["selection"] == selected


def test_a_reader_rebuilds_a_cached_service_after_another_writer_reconnects(accounts, keyed, service, clock):
    from hktvmall_mcp.accounts import Accounts

    connect(accounts, "alex")
    reader = Accounts(keyed, accounts.transport, service, clock=clock)
    old = reader.get("alex")
    epoch = accounts.control("alex").resume()
    accounts.adopt_owned(
        "alex",
        epoch,
        "alex@example.com",
        [
            {
                "name": "JSESSIONID",
                "value": "renewed",
                "domain": "www.hktvmall.com",
                "path": "/",
                "secure": True,
                "expires": None,
            },
        ],
        "Synthetic Browser",
    )
    with pytest.raises(HktvError, match="connection_disconnected"):
        old.session.checkout_client()
    current = reader.get("alex")
    assert current is not old and current.session.checkout_client()[0] == 2
