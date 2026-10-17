from app.config import Settings


def test_dotenv_loads_documented_local_values_without_overriding_host(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('GEMINI_API_KEY="harmless-test-value"\nCOOKIE_SECURE=false\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    settings=Settings.from_env()
    assert settings.gemini_api_key == "harmless-test-value"
    assert settings.cookie_secure is False
    monkeypatch.setenv("COOKIE_SECURE", "true")
    assert Settings.from_env().cookie_secure is True
