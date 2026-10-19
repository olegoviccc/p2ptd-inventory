import pytest

from tests.fake_inventree import FakeInvenTree, seed


@pytest.fixture
def catalog():
    s = FakeInvenTree()
    seed(s)  # Pioneer DJ XDJ-1000MK2 (pk 1): мануал лежит в папке olegovic
    s.db["company"] += [{"pk": 1, "name": "Pioneer DJ"}, {"pk": 2, "name": "Yamaha"}]
    s.db["part"] += [{"pk": 2, "name": "Yamaha MG12XU", "category": 2, "image": None, "active": True},
                     {"pk": 3, "name": "LED Par", "category": 2, "image": None, "active": True}]  # без модели
    s.db["mpn"].append({"pk": 2, "part": 2, "manufacturer": 2, "MPN": "MG12XU"})
    yield s
    s.close()
