import copy

import pytest

from tests.conftest import good_automation

pytestmark = pytest.mark.anyio


def codes(res):
    return {e["code"] for e in res.errors}


async def test_good_automation_ok(svc):
    r = await svc.validator.validate(good_automation())
    assert r.valid, r.errors
    assert r.automation_id.startswith("muse_presence_notify_")
    assert r.referenced_entities == ["binary_sensor.living_presence"]
    assert "id" not in r.normalized


async def test_nonexistent_entity(svc):
    a = good_automation(triggers=[{"trigger": "state", "entity_id": "light.ghost"}])
    assert "entity_not_found" in codes(await svc.validator.validate(a))


async def test_entity_not_in_allowlist(svc):
    a = good_automation(actions=[{"action": "light.turn_on", "target": {"entity_id": "light.secret_bedroom"}}])
    r = await svc.validator.validate(a)
    assert not r.valid and "entity_not_allowed" in codes(r)


async def test_empty_allowlist_fails_closed(svc, settings):
    settings.catalog_path.unlink()
    r = await svc.validator.validate(good_automation())
    assert not r.valid and {"catalog_empty", "entity_not_allowed"} <= codes(r)


@pytest.mark.parametrize("service", [
    "homeassistant.restart", "homeassistant.stop", "shell_command.rm_all",
    "script.turn_on", "hassio.host_reboot", "python_script.x",
])
async def test_dangerous_services(svc, service):
    a = good_automation(actions=[{"action": service, "target": {"entity_id": "light.living_ceiling"}}])
    r = await svc.validator.validate(a)
    assert "service_dangerous" in codes(r)


async def test_service_domain_must_match_targets(svc):
    ok = good_automation(actions=[{"action": "light.turn_on", "target": {"entity_id": "light.living_ceiling"}}])
    assert (await svc.validator.validate(ok)).valid
    bad = good_automation(actions=[{"action": "switch.turn_on", "target": {"entity_id": "light.living_ceiling"}}])
    assert "service_not_allowed" in codes(await svc.validator.validate(bad))
    no_target = good_automation(actions=[{"action": "light.turn_on"}])
    assert "service_not_allowed" in codes(await svc.validator.validate(no_target))


async def test_policy_can_explicitly_allow(svc, settings):
    settings.policy_path.write_text("extra_allowed_services: ['notify.*']\n", encoding="utf-8")
    a = good_automation(actions=[{"action": "notify.mobile_app_x", "data": {"message": "hi"}}])
    assert (await svc.validator.validate(a)).valid


async def test_nested_actions_are_checked(svc):
    a = good_automation(actions=[{"choose": [{"conditions": [], "sequence": [
        {"action": "homeassistant.restart"}]}]}])
    assert "service_dangerous" in codes(await svc.validator.validate(a))


@pytest.mark.parametrize("bad_id", ["automation_x", "muse_", "muse_UP", "muse_a-b", "../muse_x", "muse_x y", "123"])
async def test_id_format_violation(svc, bad_id):
    r = await svc.validator.validate(good_automation(id=bad_id))
    assert "invalid_id" in codes(r)


async def test_supplied_valid_id_accepted(svc):
    r = await svc.validator.validate(good_automation(id="muse_custom_1"))
    assert r.valid and r.automation_id == "muse_custom_1"


async def test_existing_id_duplicate(svc, fake):
    fake.automations["muse_dup"] = {"id": "muse_dup", "alias": "x"}
    r = await svc.validator.validate(good_automation(id="muse_dup"))
    assert "id_exists" in codes(r)


async def test_indirect_and_dynamic_targets_rejected(svc):
    for target in ({"area_id": "living"}, {"device_id": "abc"}, {"entity_id": "all"},
                   {"entity_id": "{{ states('sensor.living_temp') }}"}):
        a = good_automation(actions=[{"action": "light.turn_on", "target": target}])
        assert not (await svc.validator.validate(a)).valid, target


async def test_template_entity_refs_checked(svc):
    a = good_automation(actions=[{"action": "persistent_notification.create",
                                  "data": {"message": "{{ states('light.secret_bedroom') }}"}}])
    r = await svc.validator.validate(a)
    assert "entity_not_allowed" in codes(r)
    ok = good_automation(actions=[{"action": "persistent_notification.create",
                                   "data": {"message": "{{ states('sensor.living_temp') }}"}}])
    r = await svc.validator.validate(ok)
    assert r.valid and "sensor.living_temp" in r.referenced_entities
    dyn = good_automation(actions=[{"action": "persistent_notification.create",
                                    "data": {"message": "{{ states('sensor.' ~ x) }}"}}])
    assert "dynamic_entity" in codes(await svc.validator.validate(dyn))


async def test_unsupported_action_and_trigger_types(svc):
    a = good_automation(actions=[{"event": "my_event"}])
    assert "action_not_allowed" in codes(await svc.validator.validate(a))
    a = good_automation(triggers=[{"trigger": "webhook", "webhook_id": "x"}])
    assert "trigger_not_allowed" in codes(await svc.validator.validate(a))


async def test_unknown_top_level_key_rejected(svc):
    r = await svc.validator.validate(good_automation(use_blueprint={"path": "x"}))
    assert "schema" in codes(r)


async def test_legacy_syntax_normalized_with_warning(svc):
    legacy = {"alias": "Legacy", "trigger": [{"platform": "state", "entity_id": "binary_sensor.living_presence"}],
              "action": [{"service": "persistent_notification.create", "data": {"message": "m"}}]}
    r = await svc.validator.validate(legacy)
    assert r.valid, r.errors
    assert r.normalized["triggers"][0]["trigger"] == "state"
    assert r.normalized["actions"][0]["action"] == "persistent_notification.create"
    assert any(w["code"] == "legacy_normalized" for w in r.warnings)


async def test_validate_does_not_write(svc, fake):
    await svc.validator.validate(good_automation())
    assert fake.writes() == []


async def test_input_not_mutated(svc):
    a = good_automation()
    before = copy.deepcopy(a)
    await svc.validator.validate(a)
    assert a == before


async def test_empty_states_fail_closed(svc, fake):
    """HA が空の状態一覧を返しても、許可リスト・実在チェックは省略されない（監査指摘 2026-11-19）。"""
    fake.states.clear()
    r = await svc.validator.validate(good_automation())
    assert not r.valid and "entity_not_found" in codes(r)
    a = good_automation(actions=[{"action": "light.turn_on", "target": {"entity_id": "light.secret_bedroom"}}])
    r2 = await svc.validator.validate(a)
    assert not r2.valid and "entity_not_allowed" in codes(r2)


async def test_event_trigger_denied_by_default(svc, settings):
    """event トリガーは既定で拒否し、policy.yaml で明示した場合のみ許可（DESIGN.md と一致）。"""
    a = good_automation(triggers=[{"trigger": "event", "event_type": "my_event"}])
    r = await svc.validator.validate(a)
    assert not r.valid and "trigger_not_allowed" in codes(r)
    settings.policy_path.write_text("extra_allowed_trigger_types: ['event']\n", encoding="utf-8")
    r2 = await svc.validator.validate(a)
    assert r2.valid, r2.errors


def test_sensitive_domains_denied_by_default():
    """lock / alarm_control_panel は同ドメインでも既定拒否（運用指摘 2026-11-19）。"""
    from app.policy import Policy
    p = Policy()
    for svc_name, dom in [("lock.unlock", {"lock"}), ("lock.lock", {"lock"}),
                          ("alarm_control_panel.alarm_disarm", {"alarm_control_panel"}),
                          ("alarm_control_panel.alarm_arm_home", {"alarm_control_panel"})]:
        ok, code, reason = p.check_service(svc_name, dom)
        assert not ok and code == "service_sensitive", (svc_name, code, reason)


def test_sensitive_allowed_only_when_explicit_per_service():
    from app.policy import Policy
    p = Policy(extra_allowed_services=("lock.unlock",))
    assert p.check_service("lock.unlock", {"lock"})[0]
    assert not p.check_service("lock.lock", {"lock"})[0]  # サービス単位の明示が必要


def test_cover_remains_same_domain_allowed():
    from app.policy import Policy
    assert Policy().check_service("cover.open_cover", {"cover"})[0]


def test_sensitive_wildcard_patterns_do_not_allow():
    """高影響ドメインでは extra_allowed_services のワイルドカードは効かない（再精査 2026-11-19）。"""
    from app.policy import Policy
    for pat, svc_name, dom in [("lock.*", "lock.unlock", {"lock"}),
                               ("lock.u*", "lock.unlock", {"lock"}),
                               ("alarm_control_panel.*", "alarm_control_panel.alarm_disarm",
                                {"alarm_control_panel"})]:
        ok, code, _ = Policy(extra_allowed_services=(pat,)).check_service(svc_name, dom)
        assert not ok and code == "service_sensitive", (pat, svc_name, code)
    # 非高影響ドメインのワイルドカードは従来どおり有効
    assert Policy(extra_allowed_services=("notify.*",)).check_service(
        "notify.mobile_app_x", set())[0]
