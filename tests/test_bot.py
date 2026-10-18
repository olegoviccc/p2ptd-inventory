import asyncio
import io
import json
from types import SimpleNamespace

import pytest
import qrcode

from bot import images, service
from bot.handlers import card_kb, card_text, parse_edit
from bot.inventree import InvenTree
from bot.matching import best_match
from bot.numbering import Numbering
from bot.recognize import Recognition, Recognizer
from tests.fake_inventree import TOKEN, FakeInvenTree

N = Numbering("P2PTD-", 5)


def run(coro):
    return asyncio.run(coro)


# --- номера


def test_numbering():
    assert N.format(42) == "P2PTD-00042"
    assert N.parse("P2PTD-00042") == 42
    assert N.parse("P2P-00042") is None
    assert N.next_after([None, "SN123", "P2PTD-00007", "P2PTD-00012"]) == "P2PTD-00013"
    assert N.next_after([]) == "P2PTD-00001"
    assert N.is_internal(" p2ptd-00001 ")


# --- сопоставление


PARTS = [{"pk": 1, "name": "Pioneer DJ XDJ-1000MK2"}, {"pk": 2, "name": "Shure SM58S"}]
MPNS = [{"part": 1, "MPN": "XDJ-1000MK2"}]


def test_match_by_mpn_ignores_punctuation():
    assert best_match("Pioneer", "XDJ1000 MK2", PARTS, MPNS).part["pk"] == 1


def test_match_by_name_and_fuzzy():
    assert best_match("Shure", "SM58S", PARTS, MPNS).score == 100
    assert best_match("Shure", "SM58-S", PARTS, MPNS).part["pk"] == 2
    assert best_match("Yamaha", "MG12XU", PARTS, MPNS) is None
    assert best_match("Yamaha", "", PARTS, MPNS) is None


# --- изображения


def png(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def test_qr_roundtrip_and_label():
    raw = png(qrcode.make(json.dumps({"stockitem": 17})).convert("RGB"))
    found = images.read_barcodes(raw)
    assert [images.stockitem_from_qr(t) for t in found] == [17]
    assert images.stockitem_from_qr("SN-123") is None

    label = images.label_png(17, "P2PTD-00042", "Pioneer DJ XDJ-1000MK2", "Пешка / Диджейка")
    assert [images.stockitem_from_qr(t) for t in images.read_barcodes(label)] == [17]
    assert len(images.prepare(raw)) > 0


# --- распознавание (Claude подменён)


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.kwargs = payload, stop_reason, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def fake_claude(payload, stop_reason="end_turn"):
    messages = FakeMessages(payload, stop_reason)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


REC = {"manufacturer": "Pioneer DJ", "model": "XDJ-1000MK2", "serial": "VJXK012345",
       "category": "DJ-оборудование / Проигрыватели", "overview_photo": 1,
       "confidence": "high", "notes": ""}


def test_recognizer_request_and_parse():
    client, messages = fake_claude(REC)
    rec = run(Recognizer("claude-opus-5-5", client).recognize(
        [b"a", b"b"], ["DJ-оборудование / Проигрыватели"], ["Pioneer DJ XDJ-1000MK2"], ["VJXK012345"]))
    assert rec.name == "Pioneer DJ XDJ-1000MK2" and rec.overview_photo == 1
    schema = messages.kwargs["output_config"]["format"]["schema"]
    assert "DJ-оборудование / Проигрыватели" in schema["properties"]["category"]["enum"]
    assert messages.kwargs["fallbacks"] == "default"
    assert [b["type"] for b in messages.kwargs["messages"][0]["content"]] == ["image", "image", "text"]


def test_recognizer_refusal_and_bad_index():
    from bot.recognize import RecognitionError
    client, _ = fake_claude(REC, stop_reason="refusal")
    with pytest.raises(RecognitionError):
        run(Recognizer("m", client).recognize([b"a"], ["x"]))
    client, _ = fake_claude({**REC, "overview_photo": 5})
    assert run(Recognizer("m", client).recognize([b"a"], ["DJ-оборудование / Проигрыватели"])).overview_photo == -1


# --- сценарий целиком против поддельного InvenTree


@pytest.fixture
def server():
    s = FakeInvenTree()
    for pk, name, parent in [(1, "DJ-оборудование", None), (2, "Проигрыватели", 1), (3, "Микшеры", 1)]:
        s.db["category"].append({"pk": pk, "name": name, "parent": parent})
    s.db["location"] += [{"pk": 10, "name": "Пешка", "parent": None},
                         {"pk": 11, "name": "Диджейка", "parent": 10}]
    s.db["part"].append({"pk": 1, "name": "Pioneer DJ XDJ-1000MK2", "category": 2,
                         "trackable": False, "purchaseable": True, "image": None})
    s.db["mpn"].append({"pk": 1, "part": 1, "manufacturer": 1, "MPN": "XDJ-1000MK2"})
    # позиция из райдера: 2 шт. без серийника
    s.db["stock"].append({"pk": 1, "part": 1, "location": 11, "quantity": 2, "serial": None, "batch": None})
    # уже выданный номер у другого прибора
    s.db["stock"].append({"pk": 2, "part": 1, "location": 10, "quantity": 1, "serial": "P2PTD-00007", "batch": "X1"})
    yield s
    s.close()


def rec(**kw):
    return Recognition(**{**REC, **kw})


def test_login_token(server):
    assert run(InvenTree(server.url, basic=("anna", "secret")).obtain_token()) == TOKEN


def test_existing_model_from_rider(server):
    it = InvenTree(server.url, token=TOKEN)
    draft = run(service.build_draft(it, rec(), 11, "Пешка / Диджейка", ["VJXK012345"]))
    assert draft.part_pk == 1 and draft.serial_confirmed and draft.rider_stock_pk == 1 and draft.rider_qty == 2
    assert "✓ совпал со штрихкодом" in card_text(draft)

    photos = [("p1.jpg", b"jpeg1"), ("p2.jpg", b"jpeg2")]
    result = run(service.save(it, N, draft, photos, "anna"))

    assert result.number == "P2PTD-00008" and not result.warnings
    item = server.get("stock", result.stock_pk)
    assert item["serial"] == "P2PTD-00008" and item["batch"] == "VJXK012345" and item["status"] == 10
    assert server.get("stock", 1)["quantity"] == 1  # из райдера списана 1 шт.
    assert server.get("part", 1)["trackable"] is True
    assert len(server.db["attachment"]) == 2 and server.part_images[1] == b"jpeg2"

    # тот же прибор повторно: дубликат по заводскому S/N
    again = run(service.build_draft(it, rec(), 11, "Пешка / Диджейка", []))
    assert again.duplicate == "P2PTD-00008"
    assert "Сохранить" not in str(card_kb(again))
    with pytest.raises(ValueError):
        run(service.save(it, N, again, photos, "anna"))


def test_new_model_needs_category_and_is_created(server):
    it = InvenTree(server.url, token=TOKEN)
    draft = run(service.build_draft(
        it, rec(model="DJM-750MK2", serial="", category="Не определено", overview_photo=-1),
        11, "Пешка / Диджейка", []))
    assert draft.part_pk is None and draft.category_pk is None
    with pytest.raises(ValueError):
        run(service.save(it, N, draft, [], "anna"))

    draft.category_pk = 3
    result = run(service.save(it, N, draft, [], "anna"))
    part = server.get("part", server.get("stock", result.stock_pk)["part"])
    assert part["name"] == "Pioneer DJ DJM-750MK2" and part["category"] == 3 and part["trackable"]
    assert any(m["MPN"] == "DJM-750MK2" and m["part"] == part["pk"] for m in server.db["mpn"])
    assert server.get("stock", result.stock_pk)["batch"] is None


def test_serial_collision_retries(server, monkeypatch):
    it = InvenTree(server.url, token=TOKEN)
    calls = []

    async def stale_next(it_, numbering):  # первый раз отдаёт занятый номер
        calls.append(1)
        return "P2PTD-00007" if len(calls) == 1 else "P2PTD-00009"

    monkeypatch.setattr(service, "next_number", stale_next)
    draft = run(service.build_draft(it, rec(serial="NEW1"), 11, "Пешка / Диджейка", []))
    assert run(service.save(it, N, draft, [], "anna")).number == "P2PTD-00009"


def test_lookup(server):
    it = InvenTree(server.url, token=TOKEN)
    assert run(service.lookup(it, N, "p2ptd-00007"))[0]["pk"] == 2
    assert run(service.lookup(it, N, "X1"))[0]["pk"] == 2
    assert run(service.lookup(it, N, "nothing")) == []


def test_parse_edit():
    assert parse_edit("Производитель: Yamaha\nМодель: MG12XU\nS/N: ABC 1\nмусор") == {
        "manufacturer": "Yamaha", "model": "MG12XU", "serial": "ABC 1"}
