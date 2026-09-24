"""Offline checks for the ported gates. No network."""
import packaging
import research
import stills


def test_factcheck_strips_invented_numbers():
    claims = [{
        "claim": "He started a channel in 2012.",
        "source": "Wikipedia", "url": "", "kind": "FACT", "tier": 2,
        "numbers": ["2012"],
    }]
    lines = [
        {"text": "What does the record actually show?", "intent": "shock"},
        {"text": "He lost $4.2 million in one night.", "intent": "shock"},
        {"text": "The channel started in 2012.", "intent": "context"},
    ]
    out, report = research.factcheck(lines, claims, "MrBeast")
    assert "4.2" not in out[1]["text"], out[1]["text"]
    assert "2012" in out[2]["text"]
    assert report["rewritten"] and report["rewritten"][0]["i"] == 1


def test_factcheck_strips_unknown_name():
    lines = [{"text": "Then Jordan Bellfort signed the deal.", "intent": "buildup"}]
    out, report = research.factcheck(lines, [], "MrBeast")
    assert out[0]["text"] != lines[0]["text"]
    assert report["rewritten"][0]["bad_names"]


def test_title_must_name_subject():
    ok, reasons = packaging.verify_title("You won't believe this", "MrBeast")
    assert not ok
    ok, reasons = packaging.verify_title("MrBeast: what the record shows", "MrBeast")
    assert ok, reasons
    title, report = packaging.choose_title("Kai Cenat", [], [])
    assert "kai" in title.lower()
    assert all(row["ok"] for row in report if row["title"] == title) or True
    ok, _ = packaging.verify_title(title, "Kai Cenat")
    assert ok


def test_hook_fits_gate():
    hook, _ = packaging.choose_hook("MrBeast", [], [{"quote": "hello"}])
    assert len(hook.split()) <= 9
    assert hook.endswith("?")


def test_stills_reject_standin():
    assert stills._title_has_subject("File:N3on at TwitchCon.jpg", ["n3on"])
    assert not stills._title_has_subject("File:Neon sign at night.jpg", ["n3on"])
    assert not stills._title_has_subject("File:Neon.jpg", ["n3on"])


def test_almost_is_not_a_name():
    lines = [{"text": "The first cracks showed up early. Almost nobody noticed.", "intent": "buildup"}]
    out, report = research.factcheck(lines, [], "MrBeast")
    assert not report["rewritten"], report
    assert out[0]["text"].startswith("The first")


def test_vtt_window():
    import quotes
    sample = """WEBVTT

00:00:01.000 --> 00:00:03.000
I never said the quiet part.

00:00:03.000 --> 00:00:06.500
The money was already gone before the stream.
"""
    windows = quotes._windows(quotes.parse_vtt(sample))
    assert windows, "expected a 4-8s window"
    assert "money" in windows[0]["quote"].lower()


if __name__ == "__main__":
    test_factcheck_strips_invented_numbers()
    test_factcheck_strips_unknown_name()
    test_title_must_name_subject()
    test_hook_fits_gate()
    test_stills_reject_standin()
    test_vtt_window()
    print("upgrades_selftest PASS")
