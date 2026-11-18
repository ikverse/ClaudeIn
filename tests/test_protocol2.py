"""Protocol 2, phase 8a: pictures in the question, answers in a set shape, the feature list (plan 4.11).

The first tests lock in what today's apps (Signal Lab phones, Village) rely on, so the additions can't break them.
"""

from __future__ import annotations

import base64
import logging

import claudein_client as cc
from claudein_client.testing import FakeClaudeIn

from claudein.config import HELLO_APP
from claudein.runs import MAX_IMAGE_BYTES, BadRequest, RunSpec

from .conftest import m, result, spec, stream_text, update_fake_claude

#: done's fields today, plus "seq" (phase 8b numbers every event; the phones and Village ignore fields they don't know)
TODAY_DONE_KEYS = {"type", "ending", "answer", "usage", "turns", "api_price_usd", "limits", "queued_ms", "seq"}
FEATURES = ["images", "output_schema", "detach", "priority"]


# --- what today's apps rely on ------------------------------------------------------------------------------------------

async def test_today_hello_fields_unchanged(svc, phone):
    status, body = await phone.get("/hello", auth=None)
    assert status == 200
    assert body["app"] == HELLO_APP == "Signal Lab analyst host"
    assert body["service"] == "ClaudeIn"
    assert isinstance(body["version"], str)
    assert body["claude"] == "2.1.325"


async def test_today_check_fields_unchanged(svc, phone):
    status, body = await phone.get("/check")
    assert status == 200
    assert body["ok"] is True and body["busy"] is False and body["queue"] == 0


async def test_today_plain_question_reaches_claude_as_text(svc, phone, fake):
    events = await phone.ask(spec(history=[{"role": "user", "text": "Hi"}, {"role": "assistant", "text": "Hello"}]))
    assert events[-1]["ending"] == "done"
    prompt, opts = fake.calls[-1]
    assert isinstance(prompt, str)
    assert prompt.startswith("Earlier in this conversation:") and prompt.endswith("Say ready")
    assert opts.output_format is None


async def test_today_done_gets_no_new_fields(svc, phone, program):
    events = await phone.ask(spec())
    assert [e["type"] for e in events] == ["run", "text", "done"]
    assert set(events[-1]) == TODAY_DONE_KEYS
    events = await program.ask(spec(app="village"))
    assert set(events[-1]) == TODAY_DONE_KEYS


# --- pictures in the question -------------------------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 20
GIF = b"GIF89a" + b"\0" * 20
WEBP = b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 20


def pic(raw: bytes, mime: str) -> dict:
    return {"data": base64.b64encode(raw).decode(), "mime_type": mime}


def bad(**over) -> str:
    try:
        RunSpec.parse(spec(**over))
    except BadRequest as e:
        return str(e)
    raise AssertionError("not refused")


def test_picture_checks():
    assert "list" in bad(images="x")
    assert "Too many" in bad(images=[pic(PNG, "image/png")] * 21)
    assert "PNG, JPEG, WebP or GIF" in bad(images=[pic(PNG, "image/bmp")])
    assert "PNG, JPEG, WebP or GIF" in bad(images=[{"data": "AAAA"}])
    assert "needs" in bad(images=[{"mime_type": "image/png"}])
    assert "base64" in bad(images=[{"data": "not base64!!", "mime_type": "image/png"}])
    assert "empty" in bad(images=[{"data": "", "mime_type": "image/png"}])
    assert "not a PNG" in bad(images=[pic(JPEG, "image/png")])
    big = b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_IMAGE_BYTES
    assert "5 MB" in bad(images=[pic(big, "image/png")])
    ok = RunSpec.parse(spec(images=[pic(PNG, "image/png"), pic(JPEG, "image/jpg"), pic(GIF, "image/gif"),
                                    pic(WEBP, "IMAGE/WEBP")]))
    assert [p.mime_type for p in ok.images] == ["image/png", "image/jpeg", "image/gif", "image/webp"]
    lined = base64.encodebytes(PNG * 10).decode()  # base64 broken into lines, as some phones send it
    assert "\n" in lined and RunSpec.parse(spec(images=[{"data": lined, "mime_type": "image/png"}])).images


async def test_bad_picture_is_a_400(svc, phone):
    status, body = await phone.ask(spec(images=[pic(JPEG, "image/png")]))
    assert status == 400 and "not a PNG" in body["error"]


async def read_message(prompt) -> list[dict]:
    """The one user message Claude Code was given (with pictures)."""
    assert not isinstance(prompt, str)
    msgs = [x async for x in prompt]
    assert len(msgs) == 1 and msgs[0]["type"] == "user" and msgs[0]["message"]["role"] == "user"
    return msgs[0]["message"]["content"]


async def test_pictures_then_text_in_one_message(svc, phone, fake):
    seen = {}

    async def script(prompt, options, tools):
        seen["content"] = await read_message(prompt)
        for x in stream_text("Two charts."):
            yield x
        yield result("Two charts.")

    fake.script = script
    events = await phone.ask(spec(images=[pic(PNG, "image/png"), pic(JPEG, "image/jpeg")],
                                  history=[{"role": "user", "text": "Hi"}]))
    assert events[-1]["ending"] == "done" and events[-1]["answer"] == "Two charts."
    c = seen["content"]
    assert [b["type"] for b in c] == ["image", "image", "text"]
    assert c[0]["source"] == {"type": "base64", "media_type": "image/png", "data": pic(PNG, "x")["data"]}
    assert c[1]["source"]["media_type"] == "image/jpeg"
    assert c[2]["text"].startswith("Earlier in this conversation:") and c[2]["text"].endswith("Say ready")


async def test_retry_sends_the_same_pictures(svc, phone, fake):
    seen = []

    async def script(prompt, options, tools):
        seen.append(await read_message(prompt))
        if len(seen) == 1:
            return  # Claude Code stopped without an answer: ClaudeIn asks once more
        yield result("ok")

    fake.script = script
    events = await phone.ask(spec(images=[pic(PNG, "image/png")]))
    assert events[-1]["ending"] == "done"
    assert len(seen) == 2 and seen[0] == seen[1]


async def test_pictures_never_in_logs_or_usage(svc, phone, fake, home, caplog):
    caplog.set_level(logging.DEBUG)
    data = pic(PNG + b"SECRETPIXELS", "image/png")["data"]
    await phone.ask(spec(images=[{"data": data, "mime_type": "image/png"}]))
    stored = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in home.rglob("*") if p.is_file())
    assert data not in stored and data not in caplog.text


# --- answers in a set shape ---------------------------------------------------------------------------------------------

SHAPE = {"type": "object", "properties": {"up": {"type": "boolean"}}, "required": ["up"]}


def test_schema_checks():
    assert '"type" is "object"' in bad(output_schema=[1])
    assert '"type" is "object"' in bad(output_schema={"type": "array"})
    assert "16 KB" in bad(output_schema={"type": "object", "description": "x" * 17000})
    assert RunSpec.parse(spec(output_schema=SHAPE)).output_schema == SHAPE


async def test_shaped_answer(svc, phone, fake):
    async def script(prompt, options, tools):
        assert isinstance(prompt, str)
        yield result('{"up": true}', structured_output={"up": True})

    fake.script = script
    done = (await phone.ask(spec(output_schema=SHAPE)))[-1]
    assert done["ending"] == "done" and done["structured"] == {"up": True} and done["answer"] == '{"up": true}'
    _, opts = fake.calls[-1]
    assert opts.output_format == {"type": "json_schema", "schema": SHAPE}


async def test_no_shape_is_a_format_failure(svc, phone, fake):
    async def gave_up(prompt, options, tools):
        yield result("", subtype="error_max_structured_output_retries", is_error=True)

    async def no_structured(prompt, options, tools):
        yield result("Here you go")

    for script in (gave_up, no_structured):
        fake.script = script
        done = (await phone.ask(spec(output_schema=SHAPE)))[-1]
        assert done["ending"] == "failed: Claude didn't answer in the requested shape."
        assert done["error_kind"] == "format" and "structured" not in done
    assert len(fake.calls) == 2  # a shape failure is an answer: no second try


def tool_use(name: str, tool_input: dict):
    return m({"type": "assistant", "message": {"model": "x", "stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": tool_input}]}})


async def test_structured_output_step_hidden_only_in_shaped_runs(svc, phone, fake):
    async def script(prompt, options, tools):
        yield tool_use("WebSearch", {"query": "EGX"})
        yield tool_use("StructuredOutput", {"up": True})
        yield result("", structured_output={"up": True})

    fake.script = script
    events = await phone.ask(spec(output_schema=SHAPE))
    assert [e["name"] for e in events if e["type"] == "server_tool"] == ["WebSearch"]
    assert events[-1]["structured"] == {"up": True}
    events = await phone.ask(spec())  # without a schema, every tool step is passed on as before
    assert [e["name"] for e in events if e["type"] == "server_tool"] == ["WebSearch", "StructuredOutput"]


async def test_hidden_step_does_not_block_the_retry(svc, phone, fake):
    tries = []

    async def script(prompt, options, tools):
        tries.append(1)
        if len(tries) == 1:
            yield tool_use("StructuredOutput", {"up": True})
            return  # stopped without an answer, and nothing reached the phone: try once more
        yield result("", structured_output={"up": False})

    fake.script = script
    done = (await phone.ask(spec(output_schema=SHAPE)))[-1]
    assert len(tries) == 2 and done["structured"] == {"up": False}


async def test_structured_ignored_without_schema(svc, phone, fake):
    async def script(prompt, options, tools):
        yield result("ok", structured_output={"up": True})

    fake.script = script
    done = (await phone.ask(spec()))[-1]
    assert done["ending"] == "done" and "structured" not in done


async def test_claude_code_without_json_schema(svc, phone, fake, cli):
    update_fake_claude(svc.claude, cli, "2.1.326", [f for f in cli.flags if f != "--json-schema"])
    await svc.claude.refresh()
    _, body = await phone.get("/hello", auth=None)
    assert body["features"] == ["images", "detach", "priority"]
    done =(await phone.ask(spec(output_schema=SHAPE)))[-1]
    assert done["error_kind"] == "unavailable" and "set shape" in done["ending"]
    assert fake.calls == []  # refused before Claude Code was asked
    assert (await phone.ask(spec()))[-1]["ending"] == "done"  # plain questions still work


# --- the feature list ---------------------------------------------------------------------------------------------------

async def test_feature_list(svc, phone, program):
    _, hello = await phone.get("/hello", auth=None)
    assert hello["protocol"] == 2 and hello["features"] == FEATURES
    _, check = await phone.get("/check")
    assert check["features"] == FEATURES
    _, check = await program.get("/check")
    assert check["features"] == FEATURES


async def test_feature_list_empty_when_claude_code_unusable(svc, phone, cli):
    update_fake_claude(svc.claude, cli, "2.1.100")  # too old
    await svc.claude.refresh()
    _, hello = await phone.get("/hello", auth=None)
    assert hello["features"] == []


# --- the client and its fake --------------------------------------------------------------------------------------------

ASK = dict(app="village", model="claude-opus-5-5", system="Be brief.", prompt="Say ready")


async def test_client_pictures_and_shape(svc, fake):
    seen = {}

    async def script(prompt, options, tools):
        seen["content"] = await read_message(prompt)
        seen["format"] = options.output_format
        yield result('{"up": false}', structured_output={"up": False})

    fake.script = script
    r = await cc.ask(**ASK, images=[cc.Image(PNG), cc.Image(base64.b64encode(JPEG).decode(), "image/jpeg")],
                     output_schema=SHAPE)
    assert r.ok and r.structured == {"up": False}
    assert [b.get("source", {}).get("media_type") for b in seen["content"]] == ["image/png", "image/jpeg", None]
    assert seen["format"]["schema"] == SHAPE


async def test_client_plain_result_has_no_structured(svc):
    r = await cc.ask(**ASK)
    assert r.ok and r.structured is None


async def test_fake_shape_and_format_failure():
    fake = FakeClaudeIn().reply('{"up": true}', structured={"up": True}).fail(
        "format", "Claude didn't answer in the requested shape.")
    r = await fake.ask(**ASK, images=[cc.Image(PNG)], output_schema=SHAPE)
    assert r.ok and r.structured == {"up": True}
    assert fake.calls[0]["output_schema"] == SHAPE and len(fake.calls[0]["images"]) == 1
    r = await fake.ask(**ASK)
    assert r.error_kind == "format" and r.structured is None and not r.ok
