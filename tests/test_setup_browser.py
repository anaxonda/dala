from types import SimpleNamespace

from dala import setup_browser


def test_setup_browser_requires_playwright_even_with_existing_browser(monkeypatch, capsys):
    monkeypatch.setattr(setup_browser, "resolve_browser_executable", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(setup_browser, "is_playwright_available", lambda: False)

    result = setup_browser.main([])

    assert result == 2
    output = capsys.readouterr().out
    assert "Found Chromium-compatible browser" in output
    assert "Playwright is not installed" in output


def test_setup_browser_existing_browser_verifies(monkeypatch, capsys):
    monkeypatch.setattr(setup_browser, "resolve_browser_executable", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(setup_browser, "is_playwright_available", lambda: True)
    monkeypatch.setattr(setup_browser, "verify_browser_launch", lambda executable: 0)

    result = setup_browser.main([])

    assert result == 0
    output = capsys.readouterr().out
    assert "Headless browser support verified" in output


def test_setup_browser_check_only_does_not_install(monkeypatch, capsys):
    monkeypatch.setattr(setup_browser, "resolve_browser_executable", lambda: None)
    monkeypatch.setattr(setup_browser, "is_playwright_available", lambda: True)
    monkeypatch.setattr(setup_browser, "install_playwright_chromium", lambda: 99)

    result = setup_browser.main(["--check-only"])

    assert result == 1
    output = capsys.readouterr().out
    assert "needs Playwright-managed Chromium" in output


def test_setup_browser_installs_and_verifies_managed_chromium(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup_browser, "resolve_browser_executable", lambda: None)
    monkeypatch.setattr(setup_browser, "is_playwright_available", lambda: True)
    monkeypatch.setattr(setup_browser, "install_playwright_chromium", lambda: calls.append("install") or 0)
    monkeypatch.setattr(setup_browser, "verify_browser_launch", lambda executable: calls.append(executable) or 0)

    result = setup_browser.main([])

    assert result == 0
    assert calls == ["install", None]
    output = capsys.readouterr().out
    assert "Installing Playwright-managed Chromium" in output
    assert "Headless browser support verified" in output


def test_install_playwright_chromium_uses_current_python(monkeypatch):
    captured = {}

    def fake_run(cmd, check=False):
        captured["cmd"] = cmd
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup_browser.subprocess, "run", fake_run)

    assert setup_browser.install_playwright_chromium() == 0
    assert captured["cmd"][-3:] == ["playwright", "install", "chromium"]
    assert captured["check"] is False
