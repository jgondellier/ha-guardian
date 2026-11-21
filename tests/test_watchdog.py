"""Escalade par paliers, maintenance, garde-fous et alertes."""
from app import db, watchdog
from conftest import run


def simulate(fails, host_ok=True, ssh_ok=True, error="Délai dépassé"):
    """Simule `fails` contrôles en échec puis un retour à la normale."""
    async def go():
        for _ in range(fails):
            watchdog.state.update(ha={"ok": False, "error": error},
                                  host={"ok": host_ok}, ssh={"ok": ssh_ok})
            await watchdog._evaluate(db.get_settings())
            watchdog.state["grace_until"] = 0
        watchdog.state.update(ha={"ok": True})
        await watchdog._evaluate(db.get_settings())
    run(go())


def actions(calls):
    return [c[1] for c in calls if c[0] == "action"]


def test_paliers_mini_pc_joignable(env):
    simulate(12)
    assert actions(env) == ["ssh_core_restart", "ssh_host_reboot", "plug_cycle"]
    assert env[-1][1].startswith("✅")


def test_paliers_mini_pc_injoignable(env):
    simulate(12, host_ok=False, ssh_ok=False)
    assert actions(env) == ["wol", "plug_cycle"]


def test_aucune_action_avant_le_premier_palier(env):
    simulate(2)
    assert actions(env) == []


def test_alerte_avec_boutons(env):
    simulate(2)
    alert = next(c for c in env if c[0] == "notify" and "ne répond pas" in c[1])
    assert "ssh_core_restart" in alert[2] and "maintenance_60" in alert[2]


def test_chien_de_garde_desactive(env):
    db.save_settings({"watchdog_enabled": False})
    simulate(12)
    assert actions(env) == []
    assert any("ne répond pas" in c[1] for c in env if c[0] == "notify")


def test_maintenance_bloque_actions_et_alertes(env):
    watchdog.set_maintenance(60)
    simulate(12)
    assert env == []
    watchdog.set_maintenance(0)
    assert watchdog.maintenance_until() == 0


def test_jeton_refuse_ne_declenche_rien(env):
    simulate(12, error="Jeton refusé (401) : vérifier HA_TOKEN")
    assert actions(env) == []


def test_limite_quotidienne_des_coupures(env):
    db.save_settings({"max_power_cycles_per_day": 0})
    simulate(12)
    assert "plug_cycle" not in actions(env)
    assert any("Intervention manuelle" in c[1] for c in env if c[0] == "notify")


def test_delai_entre_deux_actions(env):
    db.save_settings({"cooldown_minutes": 60})
    simulate(12)
    assert actions(env) == ["ssh_core_restart"]


def test_alertes_envoyees_une_seule_fois(env):
    async def go():
        for active in (True, True, True, False, False):
            await watchdog.alert("disk", active, "Disque", "90 %", recovered="ok")
    run(go())
    assert [c[1] for c in env] == ["Disque", "✅ Rétabli"]


def test_liste_des_capteurs_temoins():
    s = dict(db.DEFAULT_SETTINGS, sentinel_entities="sensor.a\n  binary_sensor.b , \nnimportequoi\n")
    assert watchdog._sentinel_list(s) == ["sensor.a", "binary_sensor.b"]
