from app import uniq_full


def test_full_command_passthrough(monkeypatch):
    seen = []
    monkeypatch.setenv("VIDEO_UNIQ_BIN", "/custom/video-uniq")
    monkeypatch.setattr(uniq_full.subprocess, "call", lambda argv: seen.append(argv) or 0)
    assert uniq_full.main(["qa", "in.mp4", "out.mp4", "--vs-corpus", "--metric", "sscd"]) == 0
    assert seen == [["/custom/video-uniq", "qa", "in.mp4", "out.mp4", "--vs-corpus", "--metric", "sscd"]]


def test_missing_upstream_binary(monkeypatch):
    def fail(argv):
        raise FileNotFoundError()
    monkeypatch.setattr(uniq_full.subprocess, "call", fail)
    assert uniq_full.main(["version"]) == 127


def test_no_args(capsys):
    assert uniq_full.main([]) == 0
    assert "Usage" in capsys.readouterr().out
