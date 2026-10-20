"""tools/make_wan_config.py 시험 — 만든 파일이 WanConfig 로 읽히는지, 잘못된 입력을 막는지"""

import importlib.util
import json
import os

import pytest

from wan.config import DEFAULTS, WanConfig

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "tools", "make_wan_config.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("make_wan_config", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = _load_tool()


def test_output_loads_with_wanconfig(tmp_path, capsys):
    out = tmp_path / "wan_config.json"
    rc = tool.main(["--relay", "ws://relay.test:9100", "--key", "KEY1", "--name", "CCW 1", "--out", str(out)])
    assert rc == 0

    cfg = WanConfig(directory=str(tmp_path))
    assert cfg.get("enabled") is True
    assert cfg.get("relay_url") == "ws://relay.test:9100"
    assert cfg.get("relay_key") == "KEY1"
    assert cfg.get("name") == "CCW 1"
    assert cfg.get("agent_id") == ""
    assert cfg.get("access_code") == ""
    assert cfg.get("max_quality") == DEFAULTS["max_quality"]
    assert cfg.get("allow_files") is DEFAULTS["allow_files"]

    # 파일에는 WanConfig 의 항목만 있어야 하고, 에이전트 ID 는 처음 실행할 때 만들어진다
    with open(out, encoding="utf-8") as f:
        saved = json.load(f)
    assert set(saved) == set(DEFAULTS)
    assert len(cfg.ensure_id()) == 12


def test_korean_text_is_written_as_is(tmp_path):
    out = tmp_path / "wan_config.json"
    assert tool.main(["--relay", "ws://a:9100", "--name", "현장 1", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "현장 1" in text
    assert "\\u" not in text  # ensure_ascii=False


def test_refuses_overwrite_without_force(tmp_path, capsys):
    out = tmp_path / "wan_config.json"
    out.write_text('{"keep": true}', encoding="utf-8")

    rc = tool.main(["--relay", "ws://a:9100", "--out", str(out)])

    assert rc != 0
    assert out.read_text(encoding="utf-8") == '{"keep": true}'
    assert "--force" in capsys.readouterr().err


def test_force_overwrites(tmp_path):
    out = tmp_path / "wan_config.json"
    out.write_text('{"keep": true}', encoding="utf-8")

    rc = tool.main(["--relay", "ws://new:9100", "--force", "--out", str(out)])

    assert rc == 0
    assert WanConfig(directory=str(tmp_path)).get("relay_url") == "ws://new:9100"


@pytest.mark.parametrize("bad", ["http://a:9100", "https://a:9100", "a:9100", "tcp://a:9100", "ws://", ""])
def test_rejects_non_websocket_url(tmp_path, bad, capsys):
    out = tmp_path / "wan_config.json"

    rc = tool.main(["--relay", bad, "--out", str(out)])
    err = capsys.readouterr().err

    assert rc != 0
    assert not out.exists()
    assert err.startswith("오류:")


def test_wss_is_accepted(tmp_path):
    out = tmp_path / "wan_config.json"
    assert tool.main(["--relay", "wss://relay.example.com", "--out", str(out)]) == 0
    assert WanConfig(directory=str(tmp_path)).get("relay_url") == "wss://relay.example.com"


def test_no_enable_writes_disabled_config(tmp_path):
    out = tmp_path / "wan_config.json"

    rc = tool.main(["--relay", "ws://a:9100", "--no-enable", "--out", str(out)])

    assert rc == 0
    with open(out, encoding="utf-8") as f:
        assert json.load(f)["enabled"] is False
    assert WanConfig(directory=str(tmp_path)).get("enabled") is False


def test_key_defaults_to_empty(tmp_path):
    out = tmp_path / "wan_config.json"
    assert tool.main(["--relay", "ws://a:9100", "--out", str(out)]) == 0
    assert WanConfig(directory=str(tmp_path)).get("relay_key") == ""
