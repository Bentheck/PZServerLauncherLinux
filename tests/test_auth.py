from __future__ import annotations

from tests.conftest import extract_csrf


def test_owner_bootstrap_then_login(client) -> None:
    setup_page = client.get("/setup")
    assert setup_page.status_code == 200
    csrf = extract_csrf(setup_page.text)

    response = client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "username": "owner",
            "display_name": "Owner",
            "password": "super-secure-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Project Zomboid VPS Dashboard" in dashboard.text


def test_owner_bootstrap_short_password_shows_form_error(client) -> None:
    setup_page = client.get("/setup")
    csrf = extract_csrf(setup_page.text)

    response = client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "username": "owner",
            "display_name": "Owner",
            "password": "short",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Bootstrap the Owner Account" in response.text
    assert "Passwords must be at least 12 characters long." in response.text


def test_login_shell_hides_support_link(client) -> None:
    response = client.get("/setup")

    assert response.status_code == 200
    assert "https://buymeacoffee.com/bentheck" not in response.text
    assert "Buy me a coffee" not in response.text


def test_authenticated_shell_exposes_support_link_and_catchphrase(client) -> None:
    setup_page = client.get("/setup")
    csrf = extract_csrf(setup_page.text)
    client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "username": "owner",
            "display_name": "Owner",
            "password": "super-secure-password",
        },
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Like what I" in response.text
    assert "doing?" in response.text
    assert "https://buymeacoffee.com/bentheck" in response.text
    assert "Buy me a coffee" in response.text


def test_login_flow_after_bootstrap(client) -> None:
    setup_page = client.get("/setup")
    csrf = extract_csrf(setup_page.text)
    client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "username": "owner",
            "display_name": "Owner",
            "password": "super-secure-password",
        },
    )
    client.post("/logout", data={"csrf_token": client.get("/dashboard").text.split('name="csrf_token" value="')[1].split('"')[0]})

    login_page = client.get("/login")
    login_csrf = extract_csrf(login_page.text)
    login_response = client.post(
        "/login",
        data={
            "csrf_token": login_csrf,
            "username": "owner",
            "password": "super-secure-password",
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/dashboard"
