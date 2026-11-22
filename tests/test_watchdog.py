"""Escalade par paliers, maintenance, garde-fous et alertes."""
import time

from app import db, network, watchdog
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


def fail_once(minutes_ago_alert=None, minutes_since_incident=None):
    """Un contrôle en échec, en vieillissant au préalable l'incident et la dernière alerte."""
    async def go():
        if minutes_since_incident is not None:
            watchdog.state["incident_since"] = time.time() - minutes_since_incident * 60
        if minutes_ago_alert is not None:
            watchdog.state["last_alert_ts"] = time.time() - minutes_ago_alert * 60
        watchdog.state.update(ha={"ok": False, "error": "Délai dépassé"},
                              host={"ok": True}, ssh={"ok": True})
        await watchdog._evaluate(db.get_settings())
    run(go())


def reminders(calls):
    return [c for c in calls if c[0] == "notify" and "toujours injoignable" in c[1]]


def test_rappel_pendant_une_panne_longue(env):
    db.save_settings({"watchdog_enabled": False, "remind_minutes": 30})
    for _ in range(5):
        fail_once()
    assert len(reminders(env)) == 0          # alerte initiale seulement
    fail_once(minutes_ago_alert=31)
    assert len(reminders(env)) == 1
    assert "maintenance_60" in reminders(env)[0][2]
    fail_once()                               # délai pas encore écoulé
    assert len(reminders(env)) == 1
    fail_once(minutes_ago_alert=31)
    assert len(reminders(env)) == 2


def test_rappel_desactive(env):
    db.save_settings({"watchdog_enabled": False, "remind_minutes": 0})
    for _ in range(3):
        fail_once()
    fail_once(minutes_ago_alert=600)
    assert reminders(env) == []


def test_rappel_apres_maintenance(env):
    """Alerte initiale retenue pendant la maintenance : un rappel part ensuite."""
    db.save_settings({"watchdog_enabled": False, "remind_minutes": 30})
    watchdog.set_maintenance(60)
    for _ in range(4):
        fail_once()
    assert env == []
    watchdog.set_maintenance(0)
    fail_once(minutes_since_incident=40)
    assert len(reminders(env)) == 1


def test_rappel_reinitialise_apres_retablissement(env):
    db.save_settings({"watchdog_enabled": False, "remind_minutes": 30})
    simulate(3)
    assert watchdog.state["last_alert_ts"] is None
    assert reminders(env) == []


def test_texte_du_rappel(env):
    watchdog.state.update(incident_since=time.time() - 95 * 60,
                          incident_steps_done=["ssh_core_restart", "limit"],
                          ha={"ok": False, "error": "Délai dépassé"})
    text = watchdog._reminder_text(80, time.time(), host_ok=True)
    assert "1 h 35" in text and "Redémarrer HA Core (SSH)" in text and "limit" not in text


def test_alertes_envoyees_une_seule_fois(env):
    async def go():
        for active in (True, True, True, False, False):
            await watchdog.alert("disk", active, "Disque", "90 %", recovered="ok")
    run(go())
    assert [c[1] for c in env] == ["Disque", "✅ Rétabli"]


def test_liste_des_capteurs_temoins():
    s = dict(db.DEFAULT_SETTINGS, sentinel_entities="sensor.a\n  binary_sensor.b , \nnimportequoi\n")
    assert watchdog._sentinel_list(s) == ["sensor.a", "binary_sensor.b"]


def test_panne_du_routeur_principal_aucune_intervention(env):
    network.state["devices"] = [{"name": "Routeur", "critical": True, "ok": False}]
    simulate(12)
    assert actions(env) == []
    assert not any("ne répond pas" in c[1] for c in env if c[0] == "notify")


def test_equipement_non_critique_ne_bloque_pas(env):
    network.state["devices"] = [{"name": "Imprimante", "critical": False, "ok": False}]
    simulate(12)
    assert actions(env) == ["ssh_core_restart", "ssh_host_reboot", "plug_cycle"]
