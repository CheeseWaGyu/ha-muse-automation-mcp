import pytest

pytestmark = pytest.mark.anyio


async def test_only_allowlisted_and_existing(svc):
    r = await svc.catalog.get_catalog()
    ids = {e["entity_id"] for e in r["entities"]}
    assert ids == {"light.living_ceiling", "sensor.living_temp", "binary_sensor.living_presence"}
    assert "light.secret_bedroom" not in ids   # HA にあるが許可リスト外
    assert "light.ghost" not in ids            # 許可リストにあるが HA に無い
    assert "automation.my_legacy" not in ids


async def test_fields_and_area(svc):
    r = await svc.catalog.get_catalog()
    by = {e["entity_id"]: e for e in r["entities"]}
    assert by["light.living_ceiling"]["area"] == "リビング"       # YAML 指定
    assert by["sensor.living_temp"]["area"] == "リビング"          # HA（template）由来
    assert by["sensor.living_temp"]["device_class"] == "temperature"
    assert by["sensor.living_temp"]["unit_of_measurement"] == "°C"
    assert by["sensor.living_temp"]["state"] == "21.5"
    assert by["binary_sensor.living_presence"]["area"] is None


async def test_filters(svc):
    r = await svc.catalog.get_catalog(domains=["light"])
    assert [e["entity_id"] for e in r["entities"]] == ["light.living_ceiling"]
    r = await svc.catalog.get_catalog(areas=["リビング"])
    assert {e["entity_id"] for e in r["entities"]} == {"light.living_ceiling", "sensor.living_temp"}
    r = await svc.catalog.get_catalog(tags=["presence"])
    assert [e["entity_id"] for e in r["entities"]] == ["binary_sensor.living_presence"]
    r = await svc.catalog.get_catalog(domains=["sensor"], tags=["lighting"])  # AND
    assert r["entities"] == []


async def test_include_attributes_false(svc):
    r = await svc.catalog.get_catalog(include_attributes=False)
    assert all("device_class" not in e and "unit_of_measurement" not in e for e in r["entities"])


async def test_missing_or_broken_file_is_empty(svc, settings):
    settings.catalog_path.write_text("{{{ not yaml", encoding="utf-8")
    assert (await svc.catalog.get_catalog())["entities"] == []
    settings.catalog_path.unlink()
    assert (await svc.catalog.get_catalog())["entities"] == []
