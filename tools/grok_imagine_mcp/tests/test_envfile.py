from grok_imagine_mcp.server import load_env_file, merged_env


def test_parses_keys_quotes_comments_and_blank_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# ключ xAI\n\nXAI_API_KEY="abc-123"\nGROK_OUTPUT_DIR = \'D:/video\'\nBROKEN LINE\nEMPTY=\n',
                 encoding="utf-8")
    assert load_env_file(f) == {"XAI_API_KEY": "abc-123", "GROK_OUTPUT_DIR": "D:/video", "EMPTY": ""}


def test_missing_file_is_empty(tmp_path):
    assert load_env_file(tmp_path / "нет.env") == {}


def test_real_environment_wins_over_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("XAI_API_KEY=from-file\nGROK_DRIVER=dry-run\n", encoding="utf-8")
    env = merged_env(tmp_path, {"XAI_API_KEY": "from-env"})
    assert env["XAI_API_KEY"] == "from-env" and env["GROK_DRIVER"] == "dry-run"


def test_file_key_is_used_when_env_has_none(tmp_path):
    (tmp_path / ".env").write_text("XAI_API_KEY=from-file\n", encoding="utf-8")
    assert merged_env(tmp_path, {})["XAI_API_KEY"] == "from-file"
